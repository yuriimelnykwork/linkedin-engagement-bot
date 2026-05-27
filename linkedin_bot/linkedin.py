"""Playwright-based LinkedIn client.

Encapsulates: login (with 2FA / challenge pause), feed scraping, liking,
and author-profile context gathering.

LinkedIn's DOM is obfuscated and changes often, so the locators here are
intentionally defensive: each piece of data is fetched via the first
selector that matches from a small list of fallbacks, and missing data is
recorded as a scrape_note rather than raising. This trades a bit of code
volume for resilience — the alternative is a brittle one-shot path that
breaks the first time LinkedIn re-renames a CSS class.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    BrowserContext,
    ElementHandle,
    Page,
    Playwright,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)

from .config import Settings
from .models import AuthorContext, Post

log = logging.getLogger(__name__)


LOGIN_URL = "https://www.linkedin.com/login"
FEED_URL = "https://www.linkedin.com/feed/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


def _first_text(handle: ElementHandle, selectors: list[str]) -> Optional[str]:
    """Return inner_text() of the first selector that resolves, or None."""
    for sel in selectors:
        try:
            el = handle.query_selector(sel)
            if el:
                text = (el.inner_text() or "").strip()
                if text:
                    return text
        except Exception:  # noqa: BLE001 — defensive scraping
            continue
    return None


def _first_attr(handle: ElementHandle, selectors: list[str], attr: str) -> Optional[str]:
    for sel in selectors:
        try:
            el = handle.query_selector(sel)
            if el:
                val = el.get_attribute(attr)
                if val:
                    return val.strip()
        except Exception:  # noqa: BLE001
            continue
    return None


_AUTHOR_TRAILING_NOISE = re.compile(
    r"\s*(•|·|-)\s*(\d+(st|nd|rd|th)?[-\s]?(degree|connection|зв'язок|зв’язок)?[-+]?|"
    r"\d+-(й|га|го|та|та ін)\+?|Premium|Following|Followed|Слідкую|Слідкуєте).*$",
    re.IGNORECASE,
)


def _clean_author_name(raw: str) -> str:
    """Strip LinkedIn's connection-degree / Premium / Following suffixes.

    LinkedIn appends a bullet separator and a connection-degree marker
    after the author's name. The separator character varies across locales
    (LinkedIn uses several Unicode bullet variants), and we also see
    invisible joiners and zero-width characters. Two-step cleanup:

      1. Normalise weird whitespace to plain spaces, then split on the first
         bullet-like character.
      2. As a final safety net, strip anything from the first standalone
         digit onward — author names never contain digits in normal use, so
         a trailing "3-й+" or "2nd" reliably indicates degree chrome.
    """
    cleaned = re.sub(r"[   ​‌‍  ﻿]", " ", raw).strip()
    # Bullet-like glyphs across locales.
    cleaned = re.split(r"\s*[•·●◦∙‧⁃․|]\s*", cleaned, maxsplit=1)[0].strip()
    cleaned = _AUTHOR_TRAILING_NOISE.sub("", cleaned).strip()
    # Final safety net: cut at the first standalone digit.
    m = re.search(r"\s\d", cleaned)
    if m:
        cleaned = cleaned[: m.start()].strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" •·-,;—–")
    return cleaned[:80]


def _parse_count(raw: Optional[str]) -> int:
    """LinkedIn renders counts as '1,234', '1.2K', '3M', etc."""
    if not raw:
        return 0
    raw = raw.replace(",", "").strip()
    m = re.search(r"([\d.]+)\s*([KMB]?)", raw, re.IGNORECASE)
    if not m:
        return 0
    n = float(m.group(1))
    suffix = m.group(2).upper()
    if suffix == "K":
        n *= 1_000
    elif suffix == "M":
        n *= 1_000_000
    elif suffix == "B":
        n *= 1_000_000_000
    return int(n)


class LinkedInClient:
    def __init__(self, settings: Settings, playwright: Playwright) -> None:
        self.settings = settings
        self.pw = playwright
        self.browser = playwright.chromium.launch(
            headless=settings.headless,
            slow_mo=120 if not settings.headless else 0,
            args=["--disable-blink-features=AutomationControlled"],
        )
        storage = (
            str(settings.auth_state_path)
            if settings.auth_state_path.exists()
            else None
        )
        # Deliberately do NOT pin locale. Forcing locale="en-US" makes
        # LinkedIn serve a React-driven login form with React-Aria inputs
        # that Playwright can't focus or fill cleanly. Leaving locale alone
        # lets LinkedIn serve its legacy server-rendered form, whose inputs
        # carry plain `id="username"` / `id="password"` and accept `.fill()`.
        self.context: BrowserContext = self.browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            storage_state=storage,
        )
        self.context.set_default_timeout(20_000)
        self.page: Page = self.context.new_page()

    # ---------------------------------------------------------------- lifecycle

    def close(self) -> None:
        try:
            self.context.storage_state(path=str(self.settings.auth_state_path))
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not persist auth state: %s", exc)
        self.context.close()
        self.browser.close()

    # ------------------------------------------------------------------- login

    def login(self) -> None:
        """Sign in if a session is not already active.

        If LinkedIn shows a captcha / 2FA challenge we pause and let the
        human in front of the headed browser complete it; once the page
        lands on /feed we resume.
        """
        self.page.goto(FEED_URL, wait_until="domcontentloaded")
        # Give the SPA a moment to render any auth-only nav before deciding.
        if self._wait_for_logged_in_marker(timeout=10_000):
            log.info("Already logged in via persisted auth_state.json.")
            return

        log.info("Logging in fresh.")
        self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        # /login redirects to /feed when there's a valid session — handle that.
        if "/feed" in self.page.url and self._wait_for_logged_in_marker(timeout=5_000):
            log.info("LinkedIn redirected to /feed — already authenticated.")
            return
        self._fill_login_form()

        deadline = time.time() + 300  # 5 minutes — gives the human time to clear 2FA / captcha
        last_url: str | None = None
        challenge_warned = False
        while time.time() < deadline:
            self.page.wait_for_timeout(1500)
            url = self.page.url
            if url != last_url:
                log.info("Login wait — URL=%s", url)
                last_url = url
            if "/feed" in url and self._wait_for_logged_in_marker(timeout=4_000):
                log.info("Login completed.")
                return
            if any(token in url for token in ("/checkpoint", "/uas/", "challenge", "verify")):
                if not challenge_warned:
                    log.warning(
                        "LinkedIn challenge detected — please complete it "
                        "(2FA / captcha / email PIN) in the open browser window. "
                        "Waiting up to 5 minutes total."
                    )
                    challenge_warned = True
        # Timeout — dump a screenshot so the user can see where we got stuck.
        try:
            self.page.screenshot(
                path=str(self.settings.output_dir / "screenshots" / "login_timeout.png"),
                full_page=True,
            )
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(
            "Login did not complete within 5 minutes. The current URL is "
            f"{self.page.url!r}. If LinkedIn showed a verification step, "
            "complete it manually next time — the script will resume once "
            "the URL contains '/feed'. Screenshot saved to "
            "output/screenshots/login_timeout.png."
        )

    def _focus_then_type(self, *, selectors: list[str], value: str, label: str) -> None:
        """Focus an input via any of the candidate selectors, then type.

        Why type instead of fill: the modern LinkedIn login uses React Aria
        wrappers around real `<input>` elements that are visually clipped.
        Playwright's `.fill()` writes to the DOM value but doesn't fire the
        synthetic `change` event React listens for, so the form state stays
        empty. Real keyboard input via `page.keyboard.type` triggers the
        full event chain (keydown → keypress → input → change) and React
        commits the value.
        """
        last_error: Exception | None = None
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                if loc.count() == 0:
                    continue
                # `focus()` works on visually clipped inputs, unlike `.click()`.
                loc.focus(timeout=4_000)
                # Use a tiny per-key delay so React batches each event.
                self.page.keyboard.type(value, delay=25)
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
        raise RuntimeError(
            f"Could not focus the {label} field via any of {selectors}. "
            f"Last error: {last_error}"
        )

    def _fill_login_form(self) -> None:
        """Fill the LinkedIn login form regardless of which layout is shown.

        LinkedIn has at least two live variants: the legacy one with
        `#username` / `#password`, and a newer one whose inputs are
        identified only by their visible label. We try a sequence of
        strategies and the first that lands typed characters wins. If
        all strategies fail, we save a screenshot to make the failure
        easy to diagnose without rerunning.
        """
        email = self.settings.linkedin_email
        password = self.settings.linkedin_password

        # The modern LinkedIn form is React-driven: `.fill()` (even with
        # force=True) sets the input's `.value` but does NOT trigger the
        # synthetic React onChange handler that mirrors the value into the
        # form state — so submit fails silently with empty fields. The robust
        # path is to focus the input and type via the keyboard, which fires
        # real `keydown`/`input` events that React picks up.
        try:
            self._focus_then_type(
                selectors=[
                    "input[autocomplete='username']",
                    "input[type='email']",
                    "input#username",
                    "input[name='session_key']",
                ],
                value=email,
                label="email",
            )
            self._focus_then_type(
                selectors=[
                    "input[autocomplete='current-password']",
                    "input[type='password']",
                    "input#password",
                    "input[name='session_password']",
                ],
                value=password,
                label="password",
            )
            log.info("Login form filled via keyboard typing.")
        except Exception as exc:  # noqa: BLE001
            self.page.screenshot(
                path=str(self.settings.output_dir / "screenshots" / "login_form_missing.png"),
                full_page=True,
            )
            raise RuntimeError(
                f"Could not fill the login form on {self.page.url}. "
                f"Last error: {exc}. See output/screenshots/login_form_missing.png."
            ) from exc

        # Submit — also try a few options.
        for sel in (
            "button[data-litms-control-urn='login-submit']",
            "button[type='submit'][aria-label='Sign in']",
            "button[type='submit']",
        ):
            btn = self.page.locator(sel).first
            if btn.count() > 0:
                btn.click()
                return
        # Final fallback: press Enter in the password field.
        self.page.keyboard.press("Enter")

    def _first_locator(self, selectors: list[str], timeout: int = 5_000):
        """Return the first selector locator that becomes visible, or None.

        Used during login because LinkedIn keeps renaming the form fields —
        we want to try several variants without giving up if the first misses.
        """
        deadline = time.time() + (timeout / 1000.0)
        while time.time() < deadline:
            for sel in selectors:
                try:
                    loc = self.page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        return loc
                except Exception:  # noqa: BLE001
                    continue
            self.page.wait_for_timeout(250)
        return None

    def _wait_for_logged_in_marker(self, timeout: int = 8_000) -> bool:
        """Poll briefly for any 'authenticated session' marker on the page."""
        deadline = time.time() + (timeout / 1000.0)
        while time.time() < deadline:
            if self._looks_logged_in():
                return True
            self.page.wait_for_timeout(400)
        return False

    def _looks_logged_in(self) -> bool:
        """Return True if the page state matches an authenticated session.

        We use two signals jointly:
          (a) URL contains /feed or /in/ (authenticated routes)
          (b) The login form is NOT present.
        This is more durable than any positive nav selector because
        LinkedIn renames nav classes more often than it changes the URL
        or removes the login form on an authed page.
        """
        url = self.page.url
        if "/login" in url or "/checkpoint" in url:
            return False
        on_auth_route = ("/feed" in url) or ("/in/" in url) or ("/mynetwork" in url)
        if not on_auth_route:
            return False
        try:
            # Either the form input is gone, or we see a positive nav signal.
            no_login_form = self.page.locator("input#username, input[name='session_key']").count() == 0
            positive_signals = [
                "#global-nav",
                "nav.global-nav",
                "header.global-nav",
                "div.feed-shared-update-v2",
                "img.global-nav__me-photo",
                "input[placeholder*='earch']",   # the global search box is auth-only
            ]
            has_positive = any(
                self.page.locator(sel).count() > 0 for sel in positive_signals
            )
            return no_login_form and (has_positive or "/feed" in url)
        except Exception:  # noqa: BLE001
            return False

    # ----------------------------------------------------------- feed warmup

    def warmup_follow(self, target: int = 10) -> int:
        """Click 'Follow' on up to N people / pages so the home feed populates.

        Brand-new LinkedIn accounts land on /feed/ with only recommendations
        and ads — there are no actual posts to like or comment on. To make
        the rest of the pipeline meaningful, we proactively follow a small
        number of recommended sources. We never click 'Connect' (that's a
        pending request that the other party must accept) — only 'Follow',
        which takes effect immediately and starts populating the feed.

        Returns the number of follow clicks that succeeded.
        """
        log.info("Warmup: trying to follow up to %d recommendations.", target)
        # Try a few sources known to expose follow buttons.
        sources = [
            "https://www.linkedin.com/feed/",
            "https://www.linkedin.com/mynetwork/",
            "https://www.linkedin.com/mynetwork/discover/",
        ]
        # The labels LinkedIn uses for the follow control across UI locales.
        follow_label_re = (
            r"^(Follow|Слідкувати|Слідкуй|Підписатись|Подписаться|Подписаться "
            r"на|Subscribe|Дотримуйтесь)"
        )
        clicks = 0
        for src in sources:
            if clicks >= target:
                break
            try:
                self.page.goto(src, wait_until="domcontentloaded")
                self.page.wait_for_timeout(2500)
            except Exception as exc:  # noqa: BLE001
                log.debug("Warmup: could not open %s — %s", src, exc)
                continue

            # Scroll a bit so more recommendations are mounted.
            for _ in range(3):
                self.page.mouse.wheel(0, 1600)
                self.page.wait_for_timeout(600)

            try:
                # `has-text` matches partial text, case-insensitive. Cover the
                # English and Ukrainian UIs since LinkedIn picks the locale
                # from system / cookies.
                follow_buttons = self.page.locator(
                    "button:has-text('Слідкувати'), button:has-text('Follow'), "
                    "button:has-text('Підписатись'), button:has-text('Subscribe')"
                )
                n_btn = follow_buttons.count()
            except Exception:  # noqa: BLE001
                n_btn = 0
            log.info("Warmup: %d follow-like buttons visible on %s", n_btn, src)

            for i in range(min(n_btn, target - clicks)):
                btn = follow_buttons.nth(i)
                try:
                    btn.scroll_into_view_if_needed(timeout=2000)
                    label_before = (btn.inner_text() or "").strip()
                    btn.click(timeout=3000)
                    self.page.wait_for_timeout(800)
                    clicks += 1
                    log.info("  followed (%d/%d) — was %r", clicks, target, label_before)
                except Exception as exc:  # noqa: BLE001
                    log.debug("  click skipped: %s", exc)
                    continue
                if clicks >= target:
                    break

        log.info("Warmup done — %d successful follow clicks.", clicks)
        return clicks

    # --------------------------------------------------------------- feed scrape

    # As of late 2025 LinkedIn's main app ships fully obfuscated CSS module
    # classes (random hex tokens) and no `data-id` / `data-urn` on post
    # containers. The selectors below intentionally avoid class names: each
    # post is reliably wrapped in a `div[role="listitem"]` that lives inside
    # a `[data-testid="lazy-column"]`. We anchor the scraper there.
    _FEED_CARD_SELECTORS = [
        # New layout — search/hashtag pages, modern feed.
        "[data-testid='lazy-column'] div[role='listitem']",
        "div[role='list'] > div[role='listitem']",
        # Legacy layout — kept as fallback for older endpoints/locales.
        "div.feed-shared-update-v2",
        "[data-id^='urn:li:activity']",
        "[data-urn^='urn:li:activity']",
        "[data-id^='urn:li:share']",
    ]

    def _feed_card_query(self) -> str:
        return ", ".join(self._FEED_CARD_SELECTORS)

    def collect_feed_posts(
        self,
        n: int,
        source_url: str = FEED_URL,
        like_each: bool = False,
    ) -> list[Post]:
        """Collect up to N posts, optionally clicking Like on each as we go.

        We like during collection (not in a separate pass) because the new
        LinkedIn DOM doesn't give us a stable selector to re-find a card
        later — synthetic `post_id` values can't be queried back from the
        page. Liking in-place avoids that problem.
        """
        self.page.goto(source_url, wait_until="domcontentloaded")
        try:
            self.page.wait_for_selector(self._feed_card_query(), timeout=20_000)
        except PWTimeoutError:
            log.warning(
                "No feed cards appeared at %s within 20s — feed may be empty.",
                source_url,
            )
            return []

        seen: dict[str, Post] = {}
        max_scrolls = 30
        for scroll_index in range(max_scrolls):
            cards = self.page.query_selector_all(self._feed_card_query())
            for card in cards:
                try:
                    post = self._extract_post(card)
                except Exception as exc:  # noqa: BLE001
                    log.debug("Skipping a card due to extraction error: %s", exc)
                    continue
                if not post or post.post_id in seen:
                    continue
                if like_each:
                    self._like_in_card(card, post)
                seen[post.post_id] = post
                if len(seen) >= n:
                    break
            if len(seen) >= n:
                break
            self.page.mouse.wheel(0, 2200)
            self.page.wait_for_timeout(900)
            log.debug("Scroll %d — collected %d/%d posts", scroll_index + 1, len(seen), n)

        return list(seen.values())[:n]

    # Visible button-text labels for the Like action across locales.
    _LIKE_BUTTON_TEXTS = (
        "Like", "Подобається", "Сподобалося", "Сподобатися",
        "Понравилось", "Нравится", "Лайк",
    )
    # aria-label text fragments that identify the same button on locales
    # where the visible text is icon-only.
    _LIKE_ARIA_FRAGMENTS = (
        "react like", "кнопк реакц", "реакція", "реакции", "вподобан",
    )

    def _like_in_card(self, card: ElementHandle, post: Post) -> None:
        """Click the Like button inside the given card element.

        LinkedIn renders the Like action as `<button>...Подобається</button>`
        with an aria-label that describes the reaction state, not the verb.
        We match by visible inner text first (most reliable across locales)
        and fall back to aria-label fragments.
        """
        try:
            card.scroll_into_view_if_needed(timeout=2000)
            self.page.wait_for_timeout(250)
            target: Optional[ElementHandle] = None
            for btn in card.query_selector_all("button"):
                visible = (btn.inner_text() or "").strip()
                aria = (btn.get_attribute("aria-label") or "").lower()
                if any(v == visible for v in self._LIKE_BUTTON_TEXTS):
                    target = btn
                    break
                if any(frag in aria for frag in self._LIKE_ARIA_FRAGMENTS):
                    target = btn
                    break
            if target is None:
                post.like_outcome = "failed: like button not found"
                return
            if (target.get_attribute("aria-pressed") or "").lower() == "true":
                post.liked = True
                post.like_outcome = "already_liked"
                return
            # `force=True` makes Playwright skip the actionability check.
            # Useful here because LinkedIn occasionally overlays the action
            # bar with a translation tooltip / hovered reaction picker.
            try:
                target.click(timeout=2500)
            except Exception:  # noqa: BLE001
                target.click(timeout=2500, force=True)
            self.page.wait_for_timeout(500)
            post.liked = True
            post.like_outcome = "liked"
        except Exception as exc:  # noqa: BLE001
            post.like_outcome = f"failed: {exc!s}"

    def _extract_post(self, card: ElementHandle) -> Optional[Post]:
        """Extract a Post from a single feed-card element.

        We rely on three structurally stable handles inside any post:
          1. The first author link — `a[href*='/in/']` or `a[href*='/company/']`.
             Its href gives us the profile URL and (via slug) a stable post id
             when LinkedIn doesn't expose one.
          2. The Like button — `button[aria-label]` whose label starts with
             a recognised "Like" verb (cross-locale).
          3. The longest text block — the actual post body. We pick the
             longest visible span / div that isn't the author link's text.

        Class-name based selectors are NOT used; LinkedIn ships obfuscated
        random class hashes that change without warning.
        """
        # Try legacy data-id/urn first; fall back to author-href hash.
        post_id = (
            card.get_attribute("data-id")
            or card.get_attribute("data-urn")
            or card.get_attribute("data-entity-urn")
            or ""
        )

        # Among multiple /in/ or /company/ links inside one card, the wrapper
        # link covering the whole post has empty innerText (it's an aria-hidden
        # bounding link). The real author link has the visible name as text.
        author_link = None
        author_url: Optional[str] = None
        author_name = ""
        for cand in card.query_selector_all("a[href*='/in/'], a[href*='/company/']"):
            href = cand.get_attribute("href") or ""
            if not href:
                continue
            text = (cand.inner_text() or "").strip()
            if not author_link:
                author_link = cand  # remember the first link as URL source
                norm = href if href.startswith("http") else f"https://www.linkedin.com{href}"
                author_url = norm.split("?")[0]
            if text and not author_name:
                author_name = text.split("\n")[0][:80]
                if author_url is None:
                    norm = href if href.startswith("http") else f"https://www.linkedin.com{href}"
                    author_url = norm.split("?")[0]
                break

        # Fallback for author_name: skip the screen-reader h2 ("Feed post") and
        # take the next short line in the card.
        if not author_name:
            lines = [s.strip() for s in (card.inner_text() or "").splitlines() if s.strip()]
            sr_labels = {"Допис у стрічці", "Feed post", "Допис", "Post"}
            for line in lines:
                if line in sr_labels:
                    continue
                if 2 <= len(line) <= 80:
                    author_name = line
                    break
        if not author_name:
            author_name = "Unknown"

        # Clean trailing chrome that LinkedIn appends to author names
        # ("• 3-й+", "• 2-й", "Premium", "Following", etc.). Runs *after*
        # the fallback so both paths get the same hygiene.
        author_name = _clean_author_name(author_name)

        if not post_id:
            if author_url:
                slug = author_url.rstrip("/").split("/")[-1]
                seed = (card.inner_text() or "")[:80]
                post_id = f"post::{slug}::{abs(hash(seed)) & 0xFFFFFF:x}"
            else:
                seed = (card.inner_text() or "")[:120]
                if not seed:
                    return None
                post_id = f"post::anon::{abs(hash(seed)) & 0xFFFFFF:x}"

        # Post body: longest text node in the card that is NOT the author link.
        body, author_headline = self._extract_text_and_headline(card, author_link)

        # Media type: presence of <video> / <img> elements.
        media_type = "text"
        if card.query_selector("video, [data-test-id='video-player']"):
            media_type = "video"
        elif card.query_selector("img[src*='media.licdn.com']"):
            media_type = "image"

        reactions = self._extract_count_for(card, [
            "reaction", "реакц", "лайк", "like", "вподобан",
        ])
        comments = self._extract_count_for(card, ["comment", "коментар", "комментар"])
        reposts = self._extract_count_for(card, ["repost", "share", "поділ", "репост"])

        return Post(
            post_id=post_id,
            author_name=author_name,
            author_profile_url=author_url,
            author_headline=author_headline,
            text=body,
            media_type=media_type,
            reactions=reactions,
            comments=comments,
            reposts=reposts,
            permalink=None,
        )

    def _extract_text_and_headline(
        self,
        card: ElementHandle,
        author_link: Optional[ElementHandle],
    ) -> tuple[str, Optional[str]]:
        """Return (body, headline). Body is the longest text block; headline
        is whatever short text sits near the author link (job title etc)."""
        author_text = (author_link.inner_text() if author_link else "") or ""
        author_text = author_text.strip()

        # Screen-reader-only h2 labels that we never want to surface as
        # headline text.
        SR_LABELS = {
            "Допис у стрічці", "Feed post", "Допис", "Post",
            "Reposted by", "Поширено", "Спонсоровано",
        }
        candidates = card.query_selector_all("span, p")
        bodies: list[str] = []
        headlines: list[str] = []
        for el in candidates:
            try:
                t = (el.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if not t or t == author_text or t in SR_LABELS:
                continue
            if len(t) > 80:
                bodies.append(t)
            elif 8 <= len(t) <= 80:
                headlines.append(t)

        body = max(bodies, key=len) if bodies else ""
        # Headline: shortest meaningful candidate that follows the author link.
        headline = next((h for h in headlines if h != author_text), None)
        return body, headline

    def _extract_count_for(self, card: ElementHandle, keywords: list[str]) -> int:
        """Find a button whose aria-label mentions any of the keywords and
        parse its numeric prefix. LinkedIn surfaces reaction / comment /
        repost counts in aria-labels even when the visible text is icon-only.
        """
        try:
            buttons = card.query_selector_all("button[aria-label]")
        except Exception:  # noqa: BLE001
            return 0
        for btn in buttons:
            label = (btn.get_attribute("aria-label") or "").lower()
            if any(k in label for k in keywords):
                count = _parse_count(label)
                if count:
                    return count
        return 0

    # ----------------------------------------------------------------- liking

    def like_post(self, post: Post) -> None:
        """Find the post card by post_id and click its Like button.

        Mutates `post.liked` / `post.like_outcome` so the caller can report.
        """
        try:
            card = self.page.query_selector(f"div[data-id='{post.post_id}']") \
                or self.page.query_selector(f"div[data-urn='{post.post_id}']")
            if not card:
                # Card may have unmounted while scrolling — find it by author + first line.
                post.like_outcome = "failed: card not in DOM"
                return

            card.scroll_into_view_if_needed()
            self.page.wait_for_timeout(400)

            btn = card.query_selector("button.react-button__trigger") \
                or card.query_selector("button[aria-label^='React Like']") \
                or card.query_selector("button[aria-label*='Like']")
            if not btn:
                post.like_outcome = "failed: like button not found"
                return

            pressed = (btn.get_attribute("aria-pressed") or "").lower() == "true"
            if pressed:
                post.liked = True
                post.like_outcome = "already_liked"
                return

            btn.click()
            self.page.wait_for_timeout(700)
            post.liked = True
            post.like_outcome = "liked"
        except PWTimeoutError as exc:
            post.like_outcome = f"failed: timeout {exc!s}"
        except Exception as exc:  # noqa: BLE001
            post.like_outcome = f"failed: {exc!s}"

    # ------------------------------------------------------- author profile scrape

    def fetch_author_context(self, post: Post) -> AuthorContext:
        ctx = AuthorContext(profile_url=post.author_profile_url or "")
        if not post.author_profile_url:
            ctx.scrape_notes.append("Author profile URL missing on the feed card.")
            return ctx
        try:
            self.page.goto(post.author_profile_url, wait_until="domcontentloaded")
            self.page.wait_for_timeout(2500)
        except Exception as exc:  # noqa: BLE001
            ctx.scrape_notes.append(f"Could not open profile page: {exc!s}")
            return ctx

        try:
            ctx.full_name = self._inner_text(["h1.text-heading-xlarge", "h1"]) or post.author_name
            ctx.headline = self._inner_text([
                "div.text-body-medium.break-words",
                "div.pv-text-details__left-panel div.text-body-medium",
            ])
            ctx.location = self._inner_text([
                "span.text-body-small.inline.t-black--light.break-words",
                "div.pv-text-details__left-panel span.text-body-small",
            ])
            ctx.about = self._inner_text([
                "section[data-section='summary'] div.inline-show-more-text",
                "section.summary div.inline-show-more-text",
                "div#about ~ div.display-flex span[aria-hidden='true']",
            ])
            ctx.current_role = self._inner_text([
                "section[data-section='experience'] li:first-child .t-bold span[aria-hidden='true']",
                "section.experience-section li:first-child h3",
            ])
            ctx.company = self._inner_text([
                "section[data-section='experience'] li:first-child span.t-14.t-normal span[aria-hidden='true']",
                "section.experience-section li:first-child p.pv-entity__secondary-title",
            ])
            ctx.mutual_connections = self._inner_text([
                "a[href*='mutualConnections'] span",
                "span.t-black--light.t-normal a[href*='mutualConnections']",
            ])
        except Exception as exc:  # noqa: BLE001
            ctx.scrape_notes.append(f"Header scrape error: {exc!s}")

        # Recent activity — try the dedicated activity URL for richer signal.
        try:
            activity_url = post.author_profile_url.rstrip("/") + "/recent-activity/all/"
            self.page.goto(activity_url, wait_until="domcontentloaded")
            self.page.wait_for_timeout(2000)
            snippets: list[str] = []
            for card in self.page.query_selector_all(
                "div.feed-shared-update-v2, [data-id^='urn:li:activity']"
            )[:5]:
                snippet = _first_text(
                    card,
                    [".update-components-text", ".feed-shared-update-v2__description"],
                )
                if snippet:
                    snippets.append(snippet[:280])
            ctx.recent_post_snippets = snippets
        except Exception as exc:  # noqa: BLE001
            ctx.scrape_notes.append(f"Recent activity scrape error: {exc!s}")

        return ctx

    def _inner_text(self, selectors: list[str]) -> Optional[str]:
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0:
                    text = (loc.inner_text(timeout=2000) or "").strip()
                    if text:
                        return text
            except Exception:  # noqa: BLE001
                continue
        return None

    # ------------------------------------------------------------ screenshots

    def screenshot(self, path: Path, label: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=False)
        log.info("Screenshot: %s (%s)", path, label)
        return path


# ----------------------------------------------------------------- context manager

class linkedin_session:  # noqa: N801 — used as a context manager, lowercase reads nicer
    """`with linkedin_session(settings) as client: ...`"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pw: Playwright | None = None
        self.client: LinkedInClient | None = None

    def __enter__(self) -> LinkedInClient:
        self._pw = sync_playwright().start()
        self.client = LinkedInClient(self.settings, self._pw)
        return self.client

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        if self.client:
            self.client.close()
        if self._pw:
            self._pw.stop()
