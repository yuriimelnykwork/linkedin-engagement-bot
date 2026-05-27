"""Find stable anchors (ARIA roles, data-* attrs) we can use as selectors."""

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
        # Use the search redirect URL we observed.
        page.goto(
            "https://www.linkedin.com/search/results/content/?keywords=%23startups",
            wait_until="domcontentloaded",
        )
        page.wait_for_timeout(6000)
        for _ in range(4):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(800)

        # 1) Collect all distinct data-* attribute names anywhere in the document.
        attrs = page.evaluate("""() => {
            const counts = {};
            for (const el of document.querySelectorAll('*')) {
                for (const a of el.attributes) {
                    if (a.name.startsWith('data-') || a.name.startsWith('aria-') || a.name === 'role') {
                        counts[a.name] = (counts[a.name] || 0) + 1;
                    }
                }
            }
            return counts;
        }""")

        # 2) Find elements whose innerText contains both "•" (degree separator)
        #    and at least one #hashtag — likely a feed post — and report the
        #    chain of ancestors with their attributes.
        chains = page.evaluate("""() => {
            const out = [];
            const isPostish = (el) => {
                const t = (el.innerText || '').trim();
                return t.length > 150 && /#\\w+/.test(t) && /•/.test(t);
            };
            for (const el of document.querySelectorAll('span, div, article')) {
                if (out.length >= 2) break;
                if (!isPostish(el)) continue;
                // Walk up and find an ancestor with a data-* attribute.
                let cur = el;
                const chain = [];
                while (cur && cur !== document.body) {
                    const a = Object.fromEntries(
                        [...cur.attributes].filter(x => x.name.startsWith('data-') || x.name === 'role' || x.name.startsWith('aria-')).map(x => [x.name, x.value.slice(0, 80)])
                    );
                    chain.push({
                        tag: cur.tagName,
                        attrs: a,
                    });
                    cur = cur.parentElement;
                }
                out.push({ textHead: (el.innerText || '').slice(0, 200), chain: chain.slice(0, 10) });
            }
            return out;
        }""")
        OUT.mkdir(exist_ok=True)
        (OUT / "dom_anchors.json").write_text(json.dumps({"attrs_present": attrs, "post_ancestor_chains": chains}, indent=2))
        print(json.dumps({"attrs_present_top": dict(sorted(attrs.items(), key=lambda kv: -kv[1])[:20]), "first_chain": chains[0] if chains else None}, indent=2))
        page.screenshot(path=str(OUT / "screenshots" / "hashtag_search.png"), full_page=True)
        browser.close()


if __name__ == "__main__":
    main()
