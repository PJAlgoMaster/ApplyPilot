"""Read-only: open the pages that reported "no apply control" and print what they actually contain.

    python ats_probe.py            # the 5 rows the harness last ran
    python ats_probe.py 39 38      # only those job ids

Nothing is clicked except a cookie wall, nothing is uploaded, nothing is submitted, no mail is sent and
no row is touched. This exists because the alternative diagnostic - re-running test_5_jobs.py - re-emails
the same five recruiters every pass, and two rounds have already gone out.
"""
from __future__ import annotations

import asyncio
import sys

import automations
import database

DEFAULT_IDS = (40, 39, 38, 36, 35)

_PROBE = """() => {
    const t = el => (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 45);
    const vis = el => {
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
    };
    const all = [...document.querySelectorAll('a,button,input[type=submit],[role=button],div,span')]
        .filter(el => el.children.length < 3);          // a wrapper div is not a control
    const applyish = all.filter(el => /apply|submit/i.test(t(el)) ||
                                      /apply/i.test(el.className || '') ||
                                      /apply/i.test(el.id || ''));
    return {
        title: document.title.slice(0, 80),
        url: location.href,
        controls: applyish.slice(0, 12).map(el =>
            `${vis(el) ? '' : 'HIDDEN '}${el.tagName.toLowerCase()}` +
            `${el.id ? '#' + el.id : ''}` +
            `${el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.') : ''}` +
            `[${t(el)}]${el.getAttribute('href') ? ' -> ' + el.getAttribute('href').slice(0, 70) : ''}`),
        iframes: [...document.querySelectorAll('iframe')].slice(0, 5)
            .map(f => (f.getAttribute('src') || f.id || 'anonymous').slice(0, 70)),
        overlay: [...document.querySelectorAll('div,section')].filter(el => {
            const s = getComputedStyle(el);
            return (s.position === 'fixed' || s.position === 'sticky') && parseInt(s.zIndex || 0) > 100
                   && el.getBoundingClientRect().height > 200;
        }).slice(0, 4).map(el => `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}` +
                                 `.${(el.className || '').toString().trim().split(/\\s+/)[0]}`),
        closed: /no longer accepting|no longer available|applications closed|expired|position has been filled/i
            .test(document.body.innerText || ''),
        login: /sign in|log in|create account|register/i.test(document.body.innerText || ''),
        words: (document.body.innerText || '').trim().split(/\\s+/).length,
        // Every field a wizard would have to answer. "stalled before submit" only names the symptom; the
        // question that stopped it is always one of these, and its label is what a selector has to match.
        fields: [...document.querySelectorAll('input,select,textarea')]
            .filter(el => el.type !== 'hidden').slice(0, 30).map(el => {
                const id = el.id || el.name || '';
                const lab = (id && document.querySelector(`label[for="${id}"]`)) ||
                            el.closest('label') || el.closest('div,fieldset')?.querySelector('label');
                return `${el.tagName.toLowerCase()}[${el.type || ''}]${id ? '#' + id : ''}` +
                       `${el.required ? ' REQUIRED' : ''} "${((lab && lab.innerText) ||
                         el.getAttribute('aria-label') || el.placeholder || '').trim().slice(0, 45)}"` +
                       `${el.value ? ' =filled' : ''}`;
            }),
    };
}"""


async def look(page, label: str) -> None:
    """Print the page's apply surface. Consent walls are dismissed first - they hide everything."""
    if await automations._dismiss_consent(page):
        await page.wait_for_timeout(800)
    # The same wait the real apply path now takes, so the probe measures what _apply_flow will see and
    # not a shell that paints two seconds later.
    print(f"  hydrated: {await automations._hydrate(page)} words")
    # The real verdict function, not a copy of it: the diagnostic is only worth anything if it reports what
    # _apply_flow will decide. "" means retryable, "closed" unwinnable, "applied" already on file.
    print(f"  verdict: {await automations._apply_report(page) or '<retryable miss>'}")
    try:
        seen = await page.evaluate(_PROBE)
    except Exception as exc:
        print(f"  {label}: the DOM would not answer ({type(exc).__name__}: {exc})")
        return
    print(f"  landed: {seen['url'][:110]}")
    print(f"  title:  {seen['title']}  ({seen['words']} words"
          f"{', CLOSED posting' if seen['closed'] else ''}"
          f"{', login/register text present' if seen['login'] else ''})")
    for line in seen["controls"] or ["<no apply-ish control in the DOM at all>"]:
        print(f"    control: {line}")
    for line in seen.get("fields") or []:
        print(f"    field:   {line}")
    if seen["iframes"]:
        print(f"    iframes: {seen['iframes']}")     # an apply form inside one is invisible to locators
    if seen["overlay"]:
        print(f"    overlays: {seen['overlay']}")    # a tall fixed layer makes every click miss


async def probe_board(job: dict) -> None:
    """The board page itself, in the logged-in persistent profile the pipeline uses."""
    platform = (job.get("platform") or "").lower()
    page = None
    try:
        page = await (await automations._context(platform)).new_page()
        page.set_default_timeout(8_000)
        await page.goto(job["job_url"], wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(2_500)
        await look(page, f"{platform} board")
        external = await automations._external_url(page)
        print(f"  external url: {external or '<none found>'}")
        return external
    except Exception as exc:
        print(f"  {platform} board: {type(exc).__name__}: {exc}")
        return ""
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


async def probe_ats(url: str) -> None:
    """The employer's own page, exactly as _ats_session_apply sees it: guest context, headless."""
    domain = automations._ats_domain(url)
    state = automations._ats_state_path(domain)
    browser = ctx = page = None
    try:
        browser = await (await automations._driver()).chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=str(state) if state.exists() else None,
                                        user_agent=automations._UA,
                                        viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        page.set_default_timeout(8_000)
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(3_000)
        await look(page, f"ats {domain}")
    except Exception as exc:
        print(f"  ats {domain}: {type(exc).__name__}: {exc}")
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        for closeable in (ctx, browser):
            if closeable is not None:
                try:
                    await closeable.close()
                except Exception:
                    pass


async def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    ids = [int(a) for a in argv if a.isdigit()]
    urls = [a for a in argv if a.startswith("http")]
    await database.init_db()
    for url in urls:                    # a raw ATS/form url, so the wizard's own stall page can be read
        print(f"\n{'=' * 78}\nurl · {url[:100]}")
        await probe_ats(url)
    for job_id in ids or ([] if urls else list(DEFAULT_IDS)):
        job = await database.get(job_id)
        if not job:
            print(f"\njob {job_id}: not in the database")
            continue
        print(f"\n{'=' * 78}\njob {job_id} · {job['company']} · {job['platform']} · {job['title'][:50]}")
        external = await probe_board(job)
        if external:
            await probe_ats(external)
    await automations.shutdown()
    return 0


if __name__ == "__main__":
    factory = asyncio.ProactorEventLoop if sys.platform == "win32" else None
    raise SystemExit(asyncio.run(main(sys.argv[1:]), loop_factory=factory))
