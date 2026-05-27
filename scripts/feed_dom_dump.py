"""Walk the main feed column and dump structural fingerprints of each child
so we can figure out which selector to use even if class names have changed."""

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
        page.goto("https://www.linkedin.com/feed/hashtag/?keywords=startups", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        for _ in range(10):
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(800)
        dump = page.evaluate("""() => {
            const main = document.querySelector('main') || document.body;
            // Find any element whose innerText looks like a meaningful post (>100 chars
            // and has at least one #hashtag), and report its tag/class chain.
            const out = [];
            const visit = (el) => {
                if (!el || out.length > 8) return;
                const txt = (el.innerText || '').trim();
                if (txt.length > 100 && /#\\w+/.test(txt)) {
                    out.push({
                        tag: el.tagName,
                        id: el.id,
                        classes: (el.className || '').toString().split(' ').filter(s => s).slice(0, 8),
                        dataAttrs: Object.fromEntries([...el.attributes].filter(a => a.name.startsWith('data-')).map(a => [a.name, a.value])),
                        textHead: txt.slice(0, 200),
                        parentTag: el.parentElement?.tagName,
                        parentClasses: (el.parentElement?.className || '').toString().split(' ').filter(s => s).slice(0, 8),
                    });
                    return; // Stop descending — we want the topmost match.
                }
                for (const c of el.children || []) visit(c);
            };
            visit(main);
            return { url: location.href, found: out };
        }""")
        OUT.mkdir(exist_ok=True)
        (OUT / "feed_dom_dump.json").write_text(json.dumps(dump, indent=2))
        print(json.dumps(dump, indent=2)[:4000])
        page.screenshot(path=str(OUT / "screenshots" / "feed_dom_dump.png"), full_page=True)
        browser.close()


if __name__ == "__main__":
    main()
