"""Dump the internal structure of the FIRST post card on the search feed,
so we can spot the author-name element and the like-button selector."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

from linkedin_bot.config import Settings

OUT = Path(__file__).resolve().parent.parent / "output"


def main() -> None:
    settings = Settings.load()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, slow_mo=60)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            storage_state=str(settings.auth_state_path) if settings.auth_state_path.exists() else None,
        )
        page = ctx.new_page()
        page.goto(
            "https://www.linkedin.com/search/results/content/?keywords=%23startups",
            wait_until="domcontentloaded",
        )
        page.wait_for_timeout(6000)
        for _ in range(2):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(800)

        info = page.evaluate("""() => {
            const card = document.querySelector("[data-testid='lazy-column'] div[role='listitem'], div[role='list'] > div[role='listitem']");
            if (!card) return { error: 'no card' };
            const all_links = [...card.querySelectorAll('a')].map(a => ({
                href: a.getAttribute('href'),
                text: (a.innerText || '').trim().slice(0, 80),
                ariaLabel: a.getAttribute('aria-label'),
            }));
            const all_buttons = [...card.querySelectorAll('button')].map(b => ({
                text: (b.innerText || '').trim().slice(0, 60),
                ariaLabel: b.getAttribute('aria-label'),
                ariaPressed: b.getAttribute('aria-pressed'),
            }));
            const all_strong = [...card.querySelectorAll('strong, span[aria-hidden], h2, h3')].slice(0, 12).map(e => ({
                tag: e.tagName,
                text: (e.innerText || '').trim().slice(0, 80),
            }));
            return { all_links, all_buttons, all_strong, cardOuterHTMLHead: card.outerHTML.slice(0, 600) };
        }""")
        OUT.mkdir(exist_ok=True)
        (OUT / "post_internal.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))
        print(json.dumps(info, indent=2, ensure_ascii=False))
        browser.close()


if __name__ == "__main__":
    main()
