"""Read-only-ish: open a LinkedIn profile, click Message, print what the messaging DOM actually is.

    python dm_probe.py https://www.linkedin.com/in/<slug>

The ONE mutation is the Message click, which opens a composer and sends nothing. No text is typed, no
send button is touched, no row is written. This exists because the alternative diagnostic is re-running
the pipeline, and that re-mails and re-DMs live recruiters on every pass.
"""
from __future__ import annotations

import asyncio
import sys

import automations

_DUMP = """() => {
    const vis = el => {
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return `${Math.round(r.width)}x${Math.round(r.height)} ${s.visibility} ${s.display}`;
    };
    const line = el => `${el.tagName.toLowerCase()}.${(el.className || '').toString().trim()
        .split(/\\s+/).slice(0, 3).join('.')}` +
        `[${(el.getAttribute('aria-label') || (el.innerText || '').trim().slice(0, 30))}] ${vis(el)}`;
    const q = sel => [...document.querySelectorAll(sel)].slice(0, 12).map(line);
    return {
        overlay: q('[class*="msg-overlay"]'),
        controls: q('button[class*="msg-overlay"]'),
        boxes: q('div.msg-form__contenteditable,[contenteditable="true"],textarea'),
        forms: q('form.msg-form,[class*="msg-form"]'),
        dialogs: q('div[role="dialog"]'),
        minimized: q('[class*="minimized"],[class*="is-minimized"]'),
        premium: [...document.querySelectorAll('div,section,aside')].filter(el =>
            /message anyone|with premium|Premium/i.test(el.innerText || '') &&
            el.getBoundingClientRect().height > 120 && el.getBoundingClientRect().height < 700 &&
            el.querySelectorAll('div,section').length < 40).slice(0, 6).map(el =>
                line(el) + ` role=${el.getAttribute('role')} modal=${el.getAttribute('aria-modal')}` +
                ` pos=${getComputedStyle(el).position} z=${getComputedStyle(el).zIndex}`),
        iframes: [...document.querySelectorAll('iframe')].slice(0, 6)
            .map(f => (f.getAttribute('src') || f.id || f.title || 'anonymous').slice(0, 70)),
    };
}"""


async def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    url = argv[0] if argv else ""
    if not url:
        print(__doc__)
        return 2
    page = await (await automations._context("linkedin")).new_page()
    page.set_default_timeout(8_000)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        main_area = await automations._hydrated_main(page)
        await automations._clear_paywall(page)
        for label, when in (("before", None), ("after Message click", "click")):
            if when:
                clicked = await automations._click_named(main_area, automations._MESSAGE_RE)
                print(f"\nMessage clicked: {clicked}")
                await page.wait_for_timeout(9_000)   # the upsell paints several seconds after the click
            seen = await page.evaluate(_DUMP)
            print(f"\n--- {label} ---")
            for key, rows in seen.items():
                print(f"  {key}:")
                for row in rows or ["    <none>"]:
                    print(f"    {row}")
        print(f"\n_visible(_MSG_BOX): {await automations._visible(page, automations._MSG_BOX, timeout=3000)}")
        # querySelectorAll cannot pierce a shadow root; Playwright locators can. If these disagree, the
        # upsell and the dock are inside web components and every string selector in automations is blind.
        print(f"\nurl: {page.url}")
        print(f"frames: {[f.url[:60] for f in page.frames]}")
        print(f"elements in document: {await page.evaluate('document.getElementsByTagName(\"*\").length')}")
        for label, loc in (("text 'message anyone'", page.get_by_text("message anyone", exact=False)),
                           ("text 'With Premium'", page.get_by_text("With Premium", exact=False)),
                           ("role dialog", page.get_by_role("dialog")),
                           ("Messaging dock text", page.get_by_text("Messaging", exact=True)),
                           ("msg-overlay css", page.locator('[class*="msg-overlay"]'))):
            try:
                print(f"  locator {label}: count={await loc.count()}")
            except Exception as exc:
                print(f"  locator {label}: {type(exc).__name__}: {exc}")
        # Every real selector automations would try, counted against the live surface. A zero here names
        # exactly which key is stale instead of leaving "composer never appeared" to mean anything.
        print("\nselector counts:")
        for name in ("_PAYWALL", "_DISMISS", "_MSG_BOX", "_SEND_MSG", "_DRAWER_RECIPIENT"):
            for sel in getattr(automations, name):
                try:
                    print(f"  {name:<18} {sel[:64]:<66} {await page.locator(sel).count()}")
                except Exception as exc:
                    print(f"  {name:<18} {sel[:64]:<66} {type(exc).__name__}")
        await page.screenshot(path="dm_probe.png", full_page=False)
        print("screenshot: dm_probe.png")
    finally:
        try:
            await page.close()
        except Exception:
            pass
        await automations.shutdown()
    return 0


if __name__ == "__main__":
    factory = asyncio.ProactorEventLoop if sys.platform == "win32" else None
    raise SystemExit(asyncio.run(main(sys.argv[1:]), loop_factory=factory))
