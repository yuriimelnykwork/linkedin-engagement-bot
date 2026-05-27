"""Diagnose what feed-card selectors actually match on the logged-in /feed."""

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
        browser = pw.chromium.launch(headless=False, slow_mo=80)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            storage_state=str(settings.auth_state_path) if settings.auth_state_path.exists() else None,
        )
        page = ctx.new_page()
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        for _ in range(8):
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(900)
        # Sample selectors and counts.
        results = {}
        candidates = [
            "div.feed-shared-update-v2",
            "[data-id^='urn:li:activity']",
            "[data-urn^='urn:li:activity']",
            "[data-id^='urn:li:share']",
            "div.scaffold-finite-scroll__content > div",
            "main [data-id]",
            "main [data-urn]",
            ".update-components-actor",
            "article",
        ]
        for sel in candidates:
            results[sel] = page.locator(sel).count()
        OUT.mkdir(exist_ok=True)
        (OUT / "feed_selector_counts.json").write_text(json.dumps(results, indent=2))
        print(json.dumps(results, indent=2))

        # Dump first card markup to inspect.
        first_dataid = page.eval_on_selector_all(
            "main [data-id], main [data-urn]",
            "els => els.slice(0, 3).map(e => ({"
            "id: e.getAttribute('data-id') || e.getAttribute('data-urn'),"
            "class: e.className,"
            "tag: e.tagName,"
            "html: e.outerHTML.slice(0, 1500)"
            "}))",
        )
        (OUT / "feed_first_cards.json").write_text(json.dumps(first_dataid, indent=2))

        page.screenshot(path=str(OUT / "screenshots" / "feed_diag.png"), full_page=True)
        browser.close()


if __name__ == "__main__":
    main()
