"""One-off diagnostic: opens the LinkedIn login page and prints every
<input> element it finds, along with whether Playwright considers it
visible / editable. Useful when login automation breaks."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

OUT = Path(__file__).resolve().parent.parent / "output"
OUT.mkdir(exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def main() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        inputs = page.eval_on_selector_all(
            "input",
            """els => els.map((e, i) => ({
                index: i,
                tag: e.tagName,
                type: e.type,
                name: e.name,
                id: e.id,
                autocomplete: e.autocomplete,
                ariaLabel: e.getAttribute('aria-label'),
                placeholder: e.placeholder,
                rect: e.getBoundingClientRect().toJSON(),
                style_opacity: getComputedStyle(e).opacity,
                style_visibility: getComputedStyle(e).visibility,
                style_position: getComputedStyle(e).position,
            }))""",
        )
        diag_path = OUT / "login_inputs.json"
        diag_path.write_text(json.dumps(inputs, indent=2))
        print(f"Wrote {diag_path}")
        for inp in inputs:
            print(json.dumps(inp))

        # Also: try React-native-setter input via JS and see if it sticks.
        page.evaluate("""() => {
            const inp = document.querySelector("input[type='email'], input[autocomplete='username']");
            if (!inp) return 'no input';
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(inp, 'TEST_VALUE');
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            inp.dispatchEvent(new Event('change', { bubbles: true }));
            return inp.value;
        }""")
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / "screenshots" / "diag_after_js_inject.png"), full_page=True)

        browser.close()


if __name__ == "__main__":
    main()
