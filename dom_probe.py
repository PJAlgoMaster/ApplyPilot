"""Read-only LinkedIn DOM probe. Answers "why did the locator miss?" with ground truth, not guesses.

    python dom_probe.py https://www.linkedin.com/in/someone
    python dom_probe.py --company "Acme"        # resolve a recruiter off the people search first
    python dom_probe.py --company "Acme" --search-only
    python dom_probe.py --surface "https://www.linkedin.com/messaging/compose/?profileUrn=..."
    python dom_probe.py --open-more https://www.linkedin.com/in/someone   # the ONE safe click

It navigates, waits with the PRODUCTION _hydrated_main, screenshots, and dumps what is actually in the
DOM: every visible button inside <main>, whether it sits in the <h1>'s section, whether a recommendation
rail is inside main too, and what each production locator resolves to (0 = the turn-11 miss, >1 = the
turn-10 strict-mode violation). Then it says which door the flow WOULD have taken.

It never types and never sends. The only click it can make is `--open-more` on a profile's overflow
button, which opens a dropdown and nothing else. That is the whole point: the selectors get fixed
against a real page without a single message reaching anybody.

ponytail: one file, reuses automations' context/locators/waits so a finding here is a finding there.
Delete it once the selectors are stable, or keep it as the thing to run when LinkedIn reskins again.
"""
import asyncio
import sys

if sys.platform == "win32":
    with __import__("warnings").catch_warnings():
        __import__("warnings").simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    # A recruiter's own posts carry emoji, and the console codepage is cp1252: without this the whole probe
    # dies on UnicodeEncodeError mid-dump, losing the diagnosis to a character in somebody's status update.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import automations as A
import config

_MORE_RE = __import__("re").compile(r"^\s*More\s*$", __import__("re").I)

# The only clicks this file may make. Every one of them opens a surface and delivers nothing: a dropdown,
# the invite page (Connect is a plain navigation to /preload/custom-invite/, which then needs its own
# Send), or the note-entry state of that invite modal (which also still needs its own Send). Anything that
# transmits ("Send", "Send without a note", "Submit") is absent on purpose and the whitelist is checked
# before the click, not after. "connect" runs the PRODUCTION _open_connect, so what is verified is the
# shipped door rather than a probe-only reimplementation of it.
_SAFE_CLICKS = ("More", "Add a note", "connect", "connect+note", "message", "message+draft")

# One evaluate, because every extra round-trip is another chance for the SPA to re-render underneath us.
# Returns only primitives: this crosses the CDP boundary.
_DUMP_JS = """() => {
  const mains = [...document.querySelectorAll('main')];
  const main = mains[0];
  const out = {url: location.href, title: document.title, mains: mains.length};
  if (!main) return out;

  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const label = el => {
    if (!el) return '';
    const s = el.closest('section, aside, div[data-view-name]');
    if (!s) return '';
    return (s.getAttribute('aria-label') || s.id || (s.className || '').toString()).slice(0, 50);
  };

  const h1 = main.querySelector('h1');
  const h1Section = h1 ? h1.closest('section') : null;
  out.mainVisible = vis(main);
  out.mainChildren = main.children.length;
  out.h1 = h1 ? (h1.innerText || '').trim().slice(0, 60) : null;
  out.headings = [...main.querySelectorAll('h1,h2,h3,[role=heading]')].filter(vis)
    .slice(0, 4).map(h => h.tagName + ':' + (h.innerText || '').trim().slice(0, 40));
  out.h1SectionClass = h1Section ? (h1Section.className || '').toString().slice(0, 70) : null;
  out.h1SectionButtons = h1Section ? h1Section.querySelectorAll('button').length : 0;

  // Connection degree drives which door exists at all: 1st = Message, 2nd/3rd = Connect (often demoted).
  const deg = main.innerText.match(/\\b(1st|2nd|3rd)\\b/);
  out.degree = deg ? deg[1] : null;

  // Is the "More profiles for you" rail inside main on THIS layout? That is what .first has to outrank.
  const railRe = /More profiles for you|People also viewed|More profiles|Similar profiles|You might like/i;
  const railEls = [...main.querySelectorAll('section, aside, div[data-view-name]')]
    .filter(s => railRe.test((s.innerText || '').slice(0, 120)));
  const inRail = el => railEls.some(r => r.contains(el));

  out.allButtons = main.querySelectorAll('button').length;
  out.buttons = [...main.querySelectorAll('button')].filter(vis).map(b => ({
    text: (b.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 45),
    aria: (b.getAttribute('aria-label') || '').slice(0, 60),
    top: Math.round(b.getBoundingClientRect().top + window.scrollY),
    inH1Section: !!(h1Section && h1Section.contains(b)),
    region: label(b),
  })).slice(0, 12);

  // The real question: what TAG and ROLE do the outreach controls actually have? A <button>-only
  // locator misses an <a> or a div[role=button], and that is invisible in a screenshot.
  const CAND = /^(connect|message|follow|more|invite|pending)\\b/i;
  out.actions = [...main.querySelectorAll('button, a, [role=button], [role=link], [role=menuitem]')]
    .map((el, idx) => ({el, idx}))
    .filter(({el}) => vis(el))
    .map(({el, idx}) => {
      const r = el.getBoundingClientRect();
      return {idx, tag: el.tagName, role: el.getAttribute('role') || '',
              text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 40),
              aria: (el.getAttribute('aria-label') || '').slice(0, 55),
              href: (el.getAttribute('href') || '').slice(0, 130),
              top: Math.round(r.top + window.scrollY), left: Math.round(r.left),
              rail: inRail(el)};
    })
    .filter(a => CAND.test(a.text) || CAND.test(a.aria))
    .slice(0, 30);

  out.rails = railEls.map(s => ({label: label(s.firstElementChild) || (s.className || '').toString().slice(0, 40),
                buttons: [...s.querySelectorAll('button')].filter(vis).length,
                top: Math.round(s.getBoundingClientRect().top + window.scrollY)}))
    .slice(0, 6);
  out.profileAnchorsInMain = main.querySelectorAll('a[href*="/in/"]').length;

  out.dialogs = [...document.querySelectorAll('div[role=dialog]')].filter(vis)
    .map(d => (d.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 90));
  out.drawerOpen = !!document.querySelector('.msg-overlay-conversation-bubble, .msg-form__contenteditable');
  return out;
}"""


async def _count(loc) -> str:
    """How many elements a locator resolves to. 0 is a miss, >1 is what strict mode raises on."""
    try:
        return str(await loc.count())
    except Exception as exc:
        return f"ERR {type(exc).__name__}: {exc}"


async def _named(scope, pattern) -> str:
    """What _click_named would resolve to: link first, then button, same order as production."""
    for role in ("link", "button"):
        n = await _count(scope.get_by_role(role, name=pattern))
        if n != "0":
            return f"{n} ({role})"
    return "0"


async def probe(url: str, company: str = "", search_only: bool = False) -> dict:
    page = None
    try:
        page = await (await A._context("linkedin")).new_page()
        page.set_default_timeout(A._STEP_TIMEOUT)

        if company:
            print(f"\n== people search: {company} recruiter")
            url_found, name = "", ""
            try:
                url_found, name = await A._recruiter_from_search(page, company)
            except Exception as exc:
                print(f"   search RAISED: {type(exc).__name__}: {exc}")
            print(f"   landed on : {page.url}")
            print(f"   resolved  : {url_found or '(nothing)'} · {name or '(no name)'}")
            if search_only or not url_found:
                dump = await page.evaluate(_DUMP_JS)
                await page.screenshot(path=str(A._DEBUG_SHOT), full_page=True)
                print(f"   screenshot: {A._DEBUG_SHOT}")
                _print_dump(dump)
                return {"stage": "search", "recruiter": url_found, "dump": dump}
            url = url_found

        print(f"\n== profile: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=A._NAV_TIMEOUT)
        if any(w in page.url for w in ("/authwall", "/login", "/checkpoint")):
            print(f"   LOGIN WALL: {page.url}\n   re-authenticate browser_sessions/linkedin by hand")
            await page.screenshot(path=str(A._DEBUG_SHOT), full_page=True)
            return {"stage": "authwall", "url": page.url}

        # The production wait, so a hydration failure here is the same failure the pipeline reports.
        hydrated, main_area = True, None
        try:
            main_area = await A._hydrated_main(page)
        except Exception as exc:
            hydrated = False
            print(f"   _hydrated_main RAISED: {type(exc).__name__}: {exc}")

        dump = await page.evaluate(_DUMP_JS)
        await page.screenshot(path=str(A._DEBUG_SHOT), full_page=True)
        print(f"   screenshot: {A._DEBUG_SHOT}")
        _print_dump(dump)

        if not hydrated:
            return {"stage": "hydrate-fail", "dump": dump}

        # What the flow's own locators see. This is the cross-reference the screenshot cannot give.
        slug = m.group(1) if (m := A._LI_PROFILE.match(page.url)) else ""
        counts = {
            "OLD  Connect (main >> button >> ^Connect$)":
                await _count(main_area.locator("button").filter(has_text=A._CONNECT_RE)),
            "OLD  Message (main >> button >> ^Message$)":
                await _count(main_area.locator("button").filter(has_text=A._MESSAGE_RE)),
            "OLD  More    (aria*='More actions', :has-text('More'))":
                await _count(main_area.locator(
                    "button[aria-label*='More actions'], button:has-text('More')")),
            # What the flow uses now. Each of these must be exactly 1 on a live profile.
            "NEW  Connect (main >> vanityName href)":
                await _count(main_area.locator(A._CONNECT_SEL.format(slug=slug)).first) if slug
                else "no slug in url",
            "NEW  Connect (main >> link|button ^Connect$)": await _named(main_area, A._CONNECT_RE),
            "NEW  Message (main >> link|button ^Message$)": await _named(main_area, A._MESSAGE_RE),
            "NEW  More    (main >> link|button ^More$)": await _named(main_area, A._MORE_RE),
            # Read-only. This anchor's aria-label says a click WITHDRAWS the outstanding invite, so the
            # probe counts it and never touches it - and _SAFE_CLICKS has no entry for it either.
            "NEW  Pending (page >> aria*=Pending + own /in/ href)":
                await _count(page.locator(A._PENDING_SEL.format(slug=slug)).first) if slug
                else "no slug in url",
            "NEW  Premium upsell (_PAYWALL, any door)":
                "1" if await A._visible(page, A._PAYWALL) else "0",
        }
        print("\n   production locators")
        for k, v in counts.items():
            head = v.split()[0]
            flag = ("  <- MISS" if head == "0" else "  <- ambiguous, .first decides"
                    if head.isdigit() and int(head) > 1 else "")
            print(f"     {k:48s} {v}{flag}")

        door = ("connect via href" if counts["NEW  Connect (main >> vanityName href)"] == "1" else
                "connect via name" if counts["NEW  Connect (main >> link|button ^Connect$)"] != "0" else
                "PENDING - invite already out, nothing to send"
                if counts["NEW  Pending (page >> aria*=Pending + own /in/ href)"] not in ("0", "no slug in url")
                else "more-actions menu" if counts["NEW  More    (main >> link|button ^More$)"] != "0" else
                "message" if counts["NEW  Message (main >> link|button ^Message$)"] != "0" else
                "NONE - would fail")
        print(f"\n   flow would take: {door}")
        return {"stage": "profile", "door": door, "counts": counts, "dump": dump}
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


# Why the dialog dump below can come back empty while Playwright counts 1: document.querySelectorAll does
# not cross shadow boundaries, and Playwright's CSS engine does. This walk descends every open shadowRoot
# and reports where the dialog actually lives, so the discrepancy is diagnosed rather than guessed at.
# The invite modal lives in a shadow root, so _SURFACE_JS's querySelectorAll cannot see inside it. This
# dumps the same information through Playwright, which pierces open shadow roots - and it reports the
# PRODUCTION selectors' counts alongside the raw controls, so a mismatch between what the modal ships and
# what automations.py looks for shows up as a number rather than as a silent miss at send time.
async def _dump_dialog(page, dialog, label: str) -> None:
    if not await dialog.count():
        print(f"   {label}: absent")
        return
    print(f"   {label} text: {(await dialog.inner_text())[:120].replace(chr(10), ' ')!r}")
    for name, sel in (("_NOTE_SEL", A._NOTE_SEL), ("_NOTE_BOX", A._NOTE_BOX),
                      ("_SEND_INVITE", A._SEND_INVITE), ("_DISMISS", ", ".join(A._DISMISS))):
        print(f"      {name:13s} {await dialog.locator(sel).count()}")
    ctl = dialog.locator("button, a[role=button]")
    for i in range(min(await ctl.count(), 8)):
        b = ctl.nth(i)
        print(f"      control {(await b.inner_text())[:26]!r:<28} "
              f"aria={await b.get_attribute('aria-label')!r:<24} off={not await b.is_enabled()}")
        # The send control on the current composer has neither text nor an aria-label, so the only thing
        # that can name it is its markup. Printed for exactly the unnamed ones, and short.
        if not (await b.inner_text()).strip() and not await b.get_attribute("aria-label"):
            print(f"              unnamed: {(await b.evaluate('e => e.outerHTML'))[:220]!r}")
    fld = dialog.locator("textarea, input, [contenteditable=true]")
    for i in range(min(await fld.count(), 5)):
        f = fld.nth(i)
        print(f"      field   {await f.evaluate('e => e.tagName')} id={await f.get_attribute('id')!r} "
              f"name={await f.get_attribute('name')!r} maxlen={await f.get_attribute('maxlength')!r} "
              f"role={await f.get_attribute('role')!r} aria={(await f.get_attribute('aria-label') or '')[:30]!r}")


_SHADOW_JS = """() => {
  const hits = [], hosts = [];
  const walk = (root, path) => {
    for (const d of root.querySelectorAll('div[role=dialog], div[role=alertdialog]')) hits.push(path);
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) {
      hosts.push(path + '/' + el.tagName.toLowerCase());
      walk(el.shadowRoot, path + '/' + el.tagName.toLowerCase());
    }
  };
  walk(document, '');
  return {dialogsAt: hits, shadowHosts: hosts.slice(0, 12), hostCount: hosts.length,
          iframes: document.querySelectorAll('iframe').length};
}"""

_SURFACE_JS = """() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const cls = el => (el.className || '').toString().slice(0, 55);
  const out = {url: location.href, title: document.title};
  out.dialogs = [...document.querySelectorAll('div[role=dialog], div[role=alertdialog]')]
    .map(d => ({aria: (d.getAttribute('aria-label') || '').slice(0, 50), cls: cls(d), vis: vis(d),
                text: (d.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 160),
                controls: [...d.querySelectorAll('button, a, [role=button]')]
                  .map(b => `${b.tagName}:${(b.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 24)}`
                            + `|${(b.getAttribute('aria-label') || '').slice(0, 30)}`
                            + (b.disabled ? '|OFF' : '')).slice(0, 10),
                fields: [...d.querySelectorAll('textarea, input, [contenteditable=true]')]
                  .map(f => `${f.tagName}#${f.id || ''}[${f.getAttribute('name') || ''}]`
                            + `.${cls(f).split(' ').pop()}`).slice(0, 6)}));
  out.menus = [...document.querySelectorAll('[role=menu], [role=listbox], .artdeco-dropdown__content')]
    .filter(vis).map(m => ({cls: cls(m),
      items: [...m.querySelectorAll('[role=menuitem], li, button, a')].filter(vis)
        .map(i => ({tag: i.tagName, role: i.getAttribute('role') || '',
                    text: (i.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 35),
                    aria: (i.getAttribute('aria-label') || '').slice(0, 45)})).slice(0, 14)}));
  out.textareas = [...document.querySelectorAll('textarea')].filter(vis).map(t => ({
    id: t.id || '', name: t.name || '', ph: (t.placeholder || '').slice(0, 35), cls: cls(t),
    maxlen: t.maxLength, aria: (t.getAttribute('aria-label') || '').slice(0, 40)}));
  out.editables = [...document.querySelectorAll('[contenteditable=true], [role=textbox]')].filter(vis)
    .map(e => ({tag: e.tagName, cls: cls(e), role: e.getAttribute('role') || '',
                aria: (e.getAttribute('aria-label') || '').slice(0, 40),
                ph: (e.getAttribute('data-placeholder') || e.getAttribute('aria-placeholder')
                     || '').slice(0, 35)}));
  out.inputs = [...document.querySelectorAll('input[type=text], input[type=search], input:not([type])')]
    .filter(vis).map(i => ({id: i.id || '', name: i.name || '', ph: (i.placeholder || '').slice(0, 30),
                            cls: cls(i), aria: (i.getAttribute('aria-label') || '').slice(0, 40)}));
  const SEND = /^(send|add a note|submit|post|done|invite|next|continue)\\b|send now|send invitation/i;
  out.sendish = [...document.querySelectorAll('button, a, [role=button]')].filter(vis)
    .map(b => ({tag: b.tagName, role: b.getAttribute('role') || '',
                text: (b.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 30),
                aria: (b.getAttribute('aria-label') || '').slice(0, 45), cls: cls(b),
                off: !!b.disabled || b.getAttribute('aria-disabled') === 'true'}))
    .filter(b => SEND.test(b.text) || SEND.test(b.aria)).slice(0, 20);
  return out;
}"""


async def surface(url: str, open_more: str = "", click: str = "") -> dict:
    """Dump an input surface (invite-with-note page, message composer, overflow menu). Never sends.

    `open_more` is a profile url: click that profile's overflow button, because a dropdown is the one
    surface no href reaches. `click` names a button on the surface page to press first, whitelisted to
    _SAFE_CLICKS so this file can reveal the note textarea without ever pressing a Send.
    """
    if click and click not in _SAFE_CLICKS:
        raise SystemExit(f"refusing to click {click!r}: not in {_SAFE_CLICKS} (it might transmit)")
    page = None
    try:
        page = await (await A._context("linkedin")).new_page()
        page.set_default_timeout(A._STEP_TIMEOUT)
        target = open_more or url
        print(f"\n== surface: {target}")
        await page.goto(target, wait_until="domcontentloaded", timeout=A._NAV_TIMEOUT)
        try:
            await A._hydrated_main(page)
        except Exception as exc:
            print(f"   _hydrated_main RAISED: {type(exc).__name__}: {exc}")
        if any(w in page.url for w in ("/authwall", "/login", "/checkpoint")):
            print(f"   LOGIN WALL: {page.url}")
            return {}
        if open_more:
            main_area = page.locator("main").first
            more = main_area.get_by_role("button", name=_MORE_RE).first
            print(f"   clicking the overflow button (opens a menu; sends nothing)")
            await more.click()
            await asyncio.sleep(2.0)
            # _MENU_SEL is a comma list and Playwright resolves those in DOM ORDER, not selector order, so
            # .first is only the menu if nothing else in the list appears earlier in the document. An
            # embedded video player ships two hidden div[role=dialog] of its own, which is exactly the kind
            # of thing that steals .first - measured here rather than assumed.
            for name, sel in (("_MENU_SEL", A._MENU_SEL), ("div[role=menu]", "div[role=menu]"),
                              ("[role=menu]", "[role=menu]"), ("_DIALOG", A._DIALOG)):
                print(f"      {name:16s} page={await page.locator(sel).count()}")
            menu = page.locator(A._MENU_SEL).first
            print(f"      _MENU_SEL .first is a {await menu.evaluate('e => e.tagName')} "
                  f"role={await menu.get_attribute('role')!r} visible={await menu.is_visible()}")
            print(f"      Connect menuitem under _MENU_SEL .first: "
                  f"{await menu.get_by_role('menuitem', name=A._CONNECT_RE).count()}")
            print(f"      Connect menuitem page-wide            : "
                  f"{await page.get_by_role('menuitem', name=A._CONNECT_RE).count()}")
            # The menu's Connect reads "Connect" but an anchored name match finds 0 of it, so the accessible
            # name is not its text. Whatever names it is what the door has to key on - printed in full,
            # along with the href, because the href is the one thing that already identifies this profile.
            items = page.locator("[role=menu]").first.locator("[role=menuitem]")
            for i in range(await items.count()):
                it = items.nth(i)
                print(f"      menuitem {(await it.inner_text())[:26]!r:<28} "
                      f"aria={await it.get_attribute('aria-label')!r} "
                      f"href={(await it.get_attribute('href') or '')[:60]!r}")
                print(f"               html={(await it.evaluate('e => e.outerHTML'))[:200]!r}")
            slug = A._LI_PROFILE.match(page.url)
            if slug:
                sel = A._CONNECT_SEL.format(slug=slug.group(1))
                print(f"      _CONNECT_SEL page={await page.locator(sel).count()} "
                      f"main={await page.locator('main').first.locator(sel).count()} "
                      f"menu={await page.locator('[role=menu]').first.locator(sel).count()}")
            print(f"      menu inside main: "
                  f"{await page.locator('main').first.locator('[role=menu]').count()}")
        if click:
            print(f"   clicking {click!r} (reveals a surface; sends nothing)")
        if click in ("connect", "connect+note"):
            main_area = page.locator("main").first
            print(f"   _open_connect returned {await A._open_connect(page, main_area)}")
            dialog = page.locator(A._DIALOG).first
            print(f"   invite dialog seen: {await A._seen(dialog, A._DM_TIMEOUT)}")
            print(f"   url now: {page.url}")
            # Playwright, not document.querySelectorAll: the current invite modal renders inside an open
            # shadow root, which Playwright's CSS engine pierces and querySelectorAll cannot. The JS dump
            # further down therefore reports nothing for it - these counts are the real answer, and the
            # shadow walk proves why the two disagree instead of leaving it a mystery.
            print(f"   shadow walk: {await page.evaluate(_SHADOW_JS)}")
            await _dump_dialog(page, dialog, "invite dialog")
            if click == "connect+note":
                # The one extra step, still sending nothing: Add a note only reveals the textarea, and the
                # invite needs its own Send afterwards. Worth probing because _NOTE_BOX is keyed on the OLD
                # ember textarea (#custom-message / name=message) and the overlay is a different render
                # stack - if it does not match there, the note silently degrades to no invite at all.
                print(f"   clicking 'Add a note' (reveals the textarea; sends nothing)")
                print(f"   add-note clicked: {await A._click(dialog.locator(A._NOTE_SEL).first)}")
                await asyncio.sleep(2.0)
                await _dump_dialog(page, dialog, "note-entry state")
                print(f"   _NOTE_BOX page-wide (what _fill_first sees): "
                      f"{await page.locator(A._NOTE_BOX).count()}")
        elif click in ("message", "message+draft"):
            # Opens the composer. Answers the one question the constants comment in automations.py cannot
            # answer by inspection: whether the SPA intercepts the Message anchor into an in-place drawer
            # (so the composer must NOT be scoped to main) or lets it navigate to /messaging/compose/ (a
            # page with a main of its own).
            main_area = page.locator("main").first
            before = page.url
            print(f"   _click_named(Message) returned "
                  f"{await A._click_named(main_area, A._MESSAGE_RE)}")
            await asyncio.sleep(3.0)
            print(f"   url {'CHANGED' if page.url != before else 'unchanged'}: {page.url}")
            print(f"   shadow walk: {await page.evaluate(_SHADOW_JS)}")
            for name, sel in (("_MSG_BOX", ", ".join(A._MSG_BOX)),
                              ("_INMAIL_SUBJECT", ", ".join(A._INMAIL_SUBJECT)),
                              ("_DRAWER_RECIPIENT", ", ".join(A._DRAWER_RECIPIENT)),
                              ("_SEND_MSG", ", ".join(A._SEND_MSG))):
                print(f"      {name:17s} page-wide {await page.locator(sel).count()}")
            await _dump_dialog(page, page.locator(A._DIALOG).first, "dialog after Message")
            for name, sel in (("editables", "[contenteditable=true]"), ("textareas", "textarea"),
                              ("paywall", ", ".join(A._PAYWALL))):
                print(f"      {name:17s} page-wide {await page.locator(sel).count()}")
            if click == "message+draft":
                # The one place this file writes anything. _SEND_MSG resolves to 0 on an empty composer, so
                # whether the send control exists AT ALL on this build can only be answered after the box
                # is filled - which is exactly the order _message_fallback uses. Nothing is sent: no Enter
                # is pressed, no send is clicked, and the draft is discarded again below. A draft is visible
                # only to the account that wrote it.
                print(f"   filling the composer with a placeholder (no Enter, no send, discarded after)")
                print(f"   _fill_first(_MSG_BOX): "
                      f"{await A._fill_first(page, A._MSG_BOX, 'draft', timeout=A._DM_TIMEOUT)}")
                await asyncio.sleep(1.5)
                print(f"      _SEND_MSG after fill  {await page.locator(', '.join(A._SEND_MSG)).count()}")
                # Candidate keys for an icon-only send button, measured rather than guessed. Whichever of
                # these is 1 page-wide AND 1 inside the composer is the one worth shipping.
                for cand in ("button.msg-form__send-btn", "form.msg-form", "form.msg-form button[type=submit]",
                             "button[type=submit]", "div.msg-form button[type=submit]"):
                    print(f"      cand {cand:42s} page={await page.locator(cand).count()} "
                          f"dialog={await page.locator(A._DIALOG).first.locator(cand).count()}")
                await _dump_dialog(page, page.locator(A._DIALOG).first, "composer once filled")
                for lbl in ("Close your draft conversation", "Discard"):
                    if await A._click(page.get_by_role("button", name=lbl).first, 2_000):
                        print(f"   draft discarded via {lbl!r}")
        elif click:
            await page.get_by_role("button", name=click, exact=True).first.click()
        if click:
            await asyncio.sleep(1.5)
        dump = await page.evaluate(_SURFACE_JS)
        await page.screenshot(path=str(A._DEBUG_SHOT), full_page=True)
        print(f"   landed on : {page.url}")
        print(f"   screenshot: {A._DEBUG_SHOT}")
        for d in dump.get("dialogs") or []:
            print(f"   DIALOG vis={d['vis']} aria={d['aria']!r} cls={d['cls']!r}")
            print(f"          text={d['text']!r}")
            print(f"          fields={d['fields']}")
            for c in d["controls"]:
                print(f"          control {c}")
        for m in dump.get("menus") or []:
            print(f"   MENU cls={m['cls']!r}")
            for i in m["items"]:
                print(f"        {i['tag']:<7} role={i['role']:<9} {i['text']:<35.35} {i['aria']}")
        for t in dump.get("textareas") or []:
            print(f"   TEXTAREA id={t['id']!r} name={t['name']!r} maxlen={t['maxlen']} "
                  f"ph={t['ph']!r}\n            cls={t['cls']!r} aria={t['aria']!r}")
        for e in dump.get("editables") or []:
            print(f"   EDITABLE {e['tag']} role={e['role']!r} ph={e['ph']!r}\n            "
                  f"cls={e['cls']!r} aria={e['aria']!r}")
        for i in dump.get("inputs") or []:
            print(f"   INPUT id={i['id']!r} name={i['name']!r} ph={i['ph']!r} cls={i['cls']!r} "
                  f"aria={i['aria']!r}")
        print(f"\n   send-ish controls")
        print(f"     {'tag':<7} {'role':<8} {'off':<5} {'text':<22} {'aria-label':<32} class")
        for b in dump.get("sendish") or []:
            print(f"     {b['tag']:<7} {b['role']:<8} {str(b['off']):<5} {b['text']:<22.22} "
                  f"{b['aria']:<32.32} {b['cls'][:34]}")
        if open_more:
            await page.keyboard.press("Escape")
        return dump
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


def _print_dump(d: dict) -> None:
    print(f"   page      : {d.get('title', '')[:70]}")
    print(f"   <main>     x{d.get('mains')} visible={d.get('mainVisible')} "
          f"children={d.get('mainChildren')}")
    if not d.get("mains"):
        print("   NO <main> AT ALL - not a profile page, or the shell never rendered")
        return
    print(f"   h1        : {d.get('h1')!r}  degree={d.get('degree')}")
    print(f"   headings  : {d.get('headings')}")
    print(f"   h1 section: {d.get('h1SectionClass')!r} holds {d.get('h1SectionButtons')} buttons")
    print(f"   /in/ links inside main: {d.get('profileAnchorsInMain')}  "
          f"(>1 means a profile rail lives in main)")
    for r in d.get("rails") or []:
        print(f"   RAIL in main: {r['label']!r} top={r['top']} buttons={r['buttons']}")
    if d.get("dialogs"):
        for t in d["dialogs"]:
            print(f"   DIALOG open: {t!r}")
    if d.get("drawerOpen"):
        print("   messaging drawer already open")

    print(f"\n   outreach candidates inside <main> (button|a|role=button, name ~ connect/message/more)")
    print(f"     {'dom#':>5} {'tag':<7} {'role':<8} {'top':>5} {'x':>5} rail  "
          f"{'text':<14} {'aria-label':<32} href")
    for a in d.get("actions") or []:
        print(f"     {a['idx']:>5} {a['tag']:<7} {a['role']:<8} {a['top']:>5} {a['left']:>5} "
              f"{'YES ' if a['rail'] else '  . '} {a['text']:<14.14} {a['aria']:<32.32} {a['href']}")

    print(f"\n   first visible <button>s inside <main> ({d.get('allButtons')} total)")
    for b in d.get("buttons") or []:
        print(f"     top={b['top']:>5} h1sec={str(b['inH1Section']):5} {b['text']:<24.24} "
              f"{b['aria']:<34.34} {b['region'][:24]}")


async def main() -> None:
    args = sys.argv[1:]
    company, search_only, url, mode, more, click = "", False, "", "probe", "", ""
    while args:
        a = args.pop(0)
        if a == "--company":
            company = args.pop(0) if args else ""
        elif a == "--search-only":
            search_only = True
        elif a == "--surface":
            mode = "surface"
        elif a == "--click":
            mode, click = "surface", args.pop(0) if args else ""
        elif a == "--open-more":
            mode, more = "surface", args.pop(0) if args else ""
        else:
            url = a
    if not url and not company and not more:
        print(__doc__)
        return
    print(f"headless={config.HEADLESS}  session={config.BROWSER_SESSIONS / 'linkedin'}")
    try:
        if mode == "surface":
            await surface(url, more, click)
        else:
            await probe(url, company, search_only)
    finally:
        await A.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
