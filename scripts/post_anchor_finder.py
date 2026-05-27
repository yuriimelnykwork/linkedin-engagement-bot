"""Find a stable selector for a single post.

Strategy: locate the *smallest* DOM element that contains a known post-text
fragment, then walk up to find the topmost ancestor whose innerText still
matches that post AND whose innerText length is < 1.6× the post's own length
(so we stop before the ancestor swallows the next post or the chrome).
That ancestor is the post card. Then we inspect its tag, role,
data-testid attributes — anything we can build a durable selector from.
"""

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
        for _ in range(6):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(900)

        info = page.evaluate("""() => {
            const main = document.querySelector('main') || document.body;
            // Author links to a person's profile are stable: /in/<slug>.
            const links = main.querySelectorAll('a[href*="/in/"]');
            if (!links.length) return { error: 'no /in/ links found' };

            // Pick the first link whose enclosing area is large enough to be a post card.
            const summarize = (el) => ({
                tag: el.tagName,
                classes: (el.className || '').toString().split(' ').filter(Boolean).slice(0, 4),
                role: el.getAttribute('role'),
                dataTestid: el.getAttribute('data-testid'),
                dataView: el.getAttribute('data-view-name'),
                dataChameleon: el.getAttribute('data-chameleon-result-urn'),
                dataId: el.getAttribute('data-id'),
                dataUrn: el.getAttribute('data-urn'),
                dataEntity: el.getAttribute('data-entity-urn'),
                textLen: (el.innerText || '').length,
                rectH: el.getBoundingClientRect().height|0,
            });

            const out = [];
            for (let i = 0; i < Math.min(links.length, 5); i++) {
                const link = links[i];
                const ancestors = [];
                let cur = link;
                while (cur && cur !== main && ancestors.length < 18) {
                    ancestors.push(summarize(cur));
                    cur = cur.parentElement;
                }
                out.push({
                    href: link.getAttribute('href'),
                    linkText: (link.innerText || '').slice(0, 60),
                    ancestors,
                });
            }

            // Also count data-chameleon-result-urn occurrences (LinkedIn search uses this).
            const chameleonCount = document.querySelectorAll('[data-chameleon-result-urn]').length;
            const entityCount = document.querySelectorAll('[data-entity-urn]').length;
            const viewCount = document.querySelectorAll('[data-view-name]').length;
            return { chameleonCount, entityCount, viewCount, links: out };
        }""")

        OUT.mkdir(exist_ok=True)
        (OUT / "post_anchor.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))
        print(json.dumps(info, indent=2, ensure_ascii=False)[:4000])
        browser.close()


if __name__ == "__main__":
    main()
