"""Playwright multi-platform apply, Hunter.io HR discovery, stdlib SMTP outreach, jobspy ingestion.

Browser contexts are persistent (`browser_sessions/<platform>`) and assume the user is already
logged in on each platform — there is no registration or login flow here by design.
"""
from __future__ import annotations

import asyncio
import email
import html
import imaplib
import json
import random
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid, parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

import ai_engine
import config
import database
import parsing

DM_LIMIT = 300
NOTE_LIMIT = 195           # LinkedIn's free invite note caps at 200 chars; 195 leaves room for a strip
_STEP_TIMEOUT = 6_000      # per-selector wait: site DOM drifts, fail fast and report instead of hanging
_NAV_TIMEOUT = 45_000
_DM_TIMEOUT = 12_000       # LinkedIn's profile top-card and composer both lazy-render; 6s under-waits
_ACT_TIMEOUT = 4_000       # per outreach control: cascade to the next door fast, never hang the pipeline
_HYDRATE_TIMEOUT = 15_000  # <main> is server-rendered but empty; the profile hydrates into it after
_IDLE_TIMEOUT = 5_000      # best-effort only: LinkedIn polls, so networkidle usually never fires


class RunAborted(BaseException):
    """The operator walked away from a manual apply. Not an error - an instruction to write nothing.

    Raised where a human was asked to press ENTER and did not, and it has to be a BaseException: the
    whole app is written to turn any `except Exception` into a verdict, and every one of those handlers
    - the per-job guard in pipeline._submit_events, the interceptor's own catch-all, the worker in
    main.py - would otherwise record 'failed' or carry on to the outreach mail for a job whose
    application never happened. The mail is the harm: it tells a recruiter an application is on its way
    when the form is still sitting open on screen. Nothing has left the machine, so nothing gets
    written, the row stays in 'notapplied' with both facts still NULL, and the next run picks it up
    exactly where this one stopped.
    """

# LinkedIn outreach surfaces. Every control that starts an outreach action is looked up inside the
# primary content area <main> and nowhere else.
#
# Two failure modes, opposite directions. Querying `page` globally hit Playwright's strict mode, which
# raises the moment a locator resolves to more than one element - a profile page carries the same button
# labels in the top card, the "More profiles for you" rail, and the messaging overlay drawer. Narrowing
# to the single `section.artdeco-card` that owns the <h1> then over-corrected past brittle into
# impossible: the rewritten profile has no <h1> inside <main> at all (the name renders as an <h2>) and no
# artdeco-card either, so that locator could never match and every page reported `profile top card never
# rendered`. <main> is the widest scope that still excludes the messaging drawer (a sibling of main).
#
# Scope alone is not enough, though, and pretending otherwise is what kept this broken: the rail IS
# inside main on the current build. What actually separates the primary action bar from the rail is the
# accessible name (see _CONNECT_RE below), with .first and DOM order as the tiebreak, not the whole net.
_MAIN = "main"
# Anchored and case-insensitive, and matched against the ACCESSIBLE NAME rather than the text. The
# "More profiles for you" rail lives inside <main> on the current build (240 /in/ anchors in there), and
# its own controls carry aria-labels - "Message Sakshi Vyas", "Invite Shivam Pathak to connect" - which
# override their text and so exclude them from an anchored name match. A has_text filter would hit all
# eight of them; the accessible name is what tells the primary action bar apart from the rail.
_CONNECT_RE = re.compile(r"^\s*Connect\s*$", re.I)
_MESSAGE_RE = re.compile(r"^\s*Message\s*$", re.I)
_MORE_RE = re.compile(r"^\s*More\s*$", re.I)
# The one key that names THIS profile and no other. Connect is an <a href> whose aria-label is
# "Invite <Name> to connect", so a ^Connect$ name match finds nothing at all and the rail's anchors have
# the identical shape - only the vanityName in the href distinguishes them. Formatted with the slug out
# of the profile url the flow is already standing on.
_CONNECT_SEL = 'a[href*="custom-invite"][href*="vanityName={slug}"]'
# An invite already outstanding: the action bar swaps Connect for a Pending anchor and Connect is
# legitimately gone, so _open_connect misses for a reason that is not a DOM drift. Measured live on a
# recruiter the pipeline had invited minutes earlier from an earlier job at the same company - the bar read
# Message / Pending / More and the main-scoped vanityName href counted 0.
#
# NEVER clicked. Its aria-label is "Pending, click to withdraw invitation", so pressing it retracts the
# invite that already went out. Only ever read, through _seen/_visible.
#
# Slug-keyed for the same reason door 1 is: the recommendation rail lives INSIDE <main>, so it would carry
# other people's pending invites too, and only this profile's own slug tells them apart. The href here is
# the profile's own url (measured: https://www.linkedin.com/in/<slug>/), not a custom-invite link.
_PENDING_SEL = "a[aria-label*='Pending'][href*='/in/{slug}']"
# On some profiles LinkedIn demotes Connect into the overflow menu and promotes Follow - measured on two
# live 3rd-degree profiles that shipped the two different layouts, so both are current, not old-vs-new.
#
# The :visible pseudo-class on every alternative is the load-bearing part, and it is what a comma list on
# this page needs: Playwright resolves a comma list in DOM ORDER, not selector order, so ANY earlier match
# steals .first - and _click/_seen then wait for state="visible" on that steal and time out, reporting the
# real control as absent. Measured, on a profile whose Featured section embeds a video: div[role='dialog']
# in this list resolved .first to a hidden `vjs-modal-dialog vjs-hidden`, and div.artdeco-dropdown__content
# resolved it to a hidden leftover div - two different decoys on the same page, both invisible, both fatal.
# :visible ends the whole class of bug rather than the two instances of it.
#
# The item inside is NOT matched by name. Its text reads "Connect" and it carries no aria-label, yet
# get_by_role("menuitem", name=/^Connect$/) resolves to 0 page-wide - the label lives in nested children
# under obfuscated hashed classes and the computed accessible name is not the text. The href is what
# identifies it, and anchoring still matters for a different reason: the menu's first entry is
# "Send profile in a message", so a loose match here would share this profile with a third party.
_MENU_SEL = "[role='menu']:visible, div.artdeco-dropdown__content:visible"
# Same DOM-order hazard, same fix, and this one is the more dangerous of the two: the invite modal is found
# through this selector, and the embedded video player ships TWO hidden role=dialog nodes that sit earlier
# in the document, so without :visible the flow reported "no invite dialog opened" while the modal was
# open and on screen.
_DIALOG = "div[role='dialog']:visible"
_NOTE_SEL = "button[aria-label*='Add a note'], button:has-text('Add a note')"
_NOTE_BOX = "textarea#custom-message, textarea[name='message']"
_SEND_INVITE = ("button[aria-label*='Send invitation'], button[aria-label*='Send now'], "
                "button:has-text('Send')")
# Dialog-scoped first: the bare aria-label matched FIVE buttons page-wide on a live profile (the top-card
# rail and the ad slots each carry one), and _click_first takes .first of each candidate in turn, so the
# unscoped key was closing something other than the modal. The bare forms stay last for an upsell that
# renders without role=dialog.
_DISMISS = (f"{_DIALOG} button[aria-label*='Dismiss']", f"{_DIALOG} button[aria-label*='Close']",
            f"{_DIALOG} button[aria-label*='Cancel']", "button[aria-label*='Dismiss']",
            "button[aria-label*='Cancel']", "button:has-text('Cancel')")
# Free accounts get an InMail upsell instead of a composer on out-of-network profiles. It is recognised
# and closed, never filled: a paywalled modal that is typed into and submitted delivers nothing at all.
#
# 'Premium' alone, not 'Try Premium': the same upsell ships at least three different headlines and the two
# specific ones here matched none of the copy actually served. Measured live, sitting OVER a recruiter's
# profile - "Job Search Smarter with Premium" with a "Reactivate Premium: 50% Off" button - while the
# message channel reported the unactionable "message composer never appeared", because the composer was
# behind it. One broad key covers every wording; every alternative stays dialog-scoped so the word
# appearing somewhere in the page chrome cannot trip it.
_PAYWALL = (f"{_DIALOG}:has-text('Premium')", f"{_DIALOG}:has-text('Upgrade')",
            f"{_DIALOG}:has-text('0 InMail')", "a:has-text('Try Premium')")
# One capture per failed attempt, written by the public entry points so a thrown timeout leaves a
# diagnostic too, not only a clean miss. Absolute, so it lands beside the code whatever the cwd is.
_DEBUG_SHOT = config.BASE_DIR / "linkedin_dm_debug.png"

# Composer surfaces, deliberately NOT scoped to main. Both triggers are <a href> anchors whose href is only
# the no-JS fallback - /preload/custom-invite/?vanityName=... and /messaging/compose/?profileUrn=... - and
# the SPA intercepts the click, so what actually opens is an IN-PLACE overlay with the url unchanged. Both
# overlays render inside an open shadow root, outside <main>: Playwright's CSS engine pierces shadow roots,
# document.querySelectorAll does not, which is why a JS DOM dump of these surfaces comes back empty while
# these selectors resolve fine. Scoping the composer to main would break the flow; scoping the trigger is
# what keeps the rail out of reach.

# Premium/InMail modals carry a mandatory subject; free accounts never render one, so it is probed with a
# short timeout and skipped, never waited on for the full _DM_TIMEOUT. The composer itself is a
# contenteditable div on some builds and a real textarea on others - fill() on the wrong node silently
# no-ops and the send click then posts an empty message, so both shapes are listed.
_INMAIL_SUBJECT = ("input.msg-form__subject", "input[name='subject']")
_MSG_BOX = ("div.msg-form__contenteditable[contenteditable='true']",
            "div.msg-form__contenteditable",
            "textarea.msg-form__message-text",
            "div[role='textbox'][contenteditable='true']",
            "textarea[name='message']")
# Tell-tale of the global drawer: an empty recipient typeahead. If that is on screen the composer is
# addressed to nobody, so nothing may be typed into it and the attempt is reported as a miss.
_DRAWER_RECIPIENT = ("div.msg-connections-typeahead__search-field",
                     "input[placeholder*='Type a name']", "input[aria-label*='Type a name']")
# Every one of the old three candidates resolved to 0 on the current composer and the message channel
# reported "send button missing" with the text already typed. Two things changed at once: the class lost
# four letters (msg-form__send-button -> msg-form__send-btn) and the button became an icon-only circle -
# `<button class="msg-form__send-btn ..." type="submit"><svg role="none" aria-hidden="true">` - so it has no
# text and no aria-label, and every :has-text('Send') and get_by_role name match misses it by construction.
# The class is the exact key here; the form-scoped submit is the one that survives the next rename. Bare
# button[type='submit'] is deliberately NOT in this list: it resolves to 3 on a profile with the composer
# open, and _click_first takes .first of each candidate in turn, so it would press the wrong control.
_SEND_MSG = ("button.msg-form__send-btn", "form.msg-form button[type='submit']",
             "button.msg-form__send-button", "button[type='submit']:has-text('Send')",
             "button:has-text('Send')")

# Candidate lists per step — first visible match wins, so a renamed button on one site degrades
# to a reported miss instead of a crash.
SELECTORS: dict[str, dict[str, list[str]]] = {
    "linkedin": {
        "apply": ["button.jobs-apply-button", "button[aria-label*='Easy Apply']", "button:has-text('Easy Apply')"],
        "upload": ["input[type=file][name*='resume']", "input[type=file][accept*='pdf']", "input[type=file]"],
        "advance": ["button[aria-label='Continue to next step']", "button[aria-label='Review your application']",
                    "button:has-text('Next')", "button:has-text('Review')"],
        "submit": ["button[aria-label='Submit application']", "button:has-text('Submit application')"],
    },
    "indeed": {
        "apply": ["#indeedApplyButton", "button:has-text('Apply now')", "button:has-text('Easily apply')"],
        "upload": ["input[type=file][accept*='pdf']", "input[type=file]"],
        "advance": ["button:has-text('Continue')", "button:has-text('Next')", "button:has-text('Review')"],
        "submit": ["button:has-text('Submit application')", "button:has-text('Submit')"],
    },
    "naukri": {
        "apply": ["#apply-button", "button:has-text('Apply')", "button:has-text('I am interested')"],
        "upload": ["input[type=file][accept*='pdf']", "input[type=file]"],
        "advance": ["button:has-text('Save and Continue')", "button:has-text('Continue')",
                    "button:has-text('Next')"],
        "submit": ["button:has-text('Submit')", "button:has-text('Apply')"],
    },
    "internshala": {
        "apply": ["#apply-button-container button", "button:has-text('Apply now')", "a:has-text('Apply now')"],
        "upload": ["input[type=file][accept*='pdf']", "input[type=file]"],
        "advance": ["button:has-text('Next')", "button:has-text('Continue')"],
        "submit": ["#submit", "button:has-text('Submit application')", "button:has-text('Submit')"],
    },
}

# External-ATS surfaces (Workday, Greenhouse, Lever, SuccessFactors, Taleo). Only driven headlessly on
# a domain we already hold a saved session for - anything unknown goes to the human interceptor, since
# a blind wizard walk on a stranger's ATS submits half-filled forms under the user's real name.
_ATS_SELECTORS: dict[str, list[str]] = {
    "apply": ["a[data-automation-id='adventureButton']", "button[data-automation-id='applyButton']",
              "a#apply_button", "button:has-text('Apply Now')", "a:has-text('Apply Now')",
              "button:has-text('Submit application')", "button:has-text('Apply')", "a:has-text('Apply')"],
    "upload": ["input[type=file][accept*='pdf']", "input[type=file][name*='resume']", "input[type=file]"],
    "advance": ["button[data-automation-id='bottom-navigation-next-button']",
                "button:has-text('Save and Continue')", "button:has-text('Continue')",
                "button:has-text('Next')", "button:has-text('Review')"],
    "submit": ["button[data-automation-id='bottom-navigation-next-button']:has-text('Submit')",
               "button:has-text('Submit application')", "button:has-text('Submit')"],
}
# Where the off-site URL hides when a board has no Easy Apply. Anchors are read (no click needed);
# boards that use a <button> get clicked and the popup's URL is taken instead. :has-text() is a
# case-insensitive substring match, so one spelling per phrase covers every board's capitalisation.
_EXTERNAL_APPLY = ("a.jobs-apply-button[href]", "a[data-tracking-control-name*='apply'][href]",
                   "a[href*='/safety/go']", "a:has-text('Apply on company site')",
                   "a:has-text('Apply on company website')",
                   "a:has-text('Apply on employer site')", "a:has-text('Apply externally')",
                   "a:has-text('Apply on the company')", "button:has-text('Apply on company site')",
                   "button:has-text('Apply on company website')", "button.jobs-apply-button")

_PW: Any = None
_CONTEXTS: dict[str, Any] = {}
# One wording for the one failure that is not the page's fault, so it is greppable in the notes column
# and so `_context`'s recovery - not a selector change - is the obvious next step for whoever reads it.
_DEAD_BROWSER = ("the browser stopped answering mid-apply (playwright driver connection lost) - "
                 "not a page problem, safe to retry")
# Matched only inside an except-block around a browser call, where any mention of the driver or a closed
# target is the transport rather than the page. Covers the three spellings Playwright uses for it.
_DRIVER_LOST = re.compile(r"connection closed|driver|browser has been closed|"
                          r"target (page|closed)|page has been closed", re.I)
# Only one visible interceptor may own the terminal at a time: two jobs prompting on one stdin means
# whichever thread wins the read consumes the other's ENTER and both sessions get saved half-open.
_INTERCEPT_LOCK = asyncio.Lock()


async def _human(lo: float = 2.5, hi: float = 5.0) -> None:
    """Randomised pacing — bot-speed clicking is what gets accounts flagged."""
    await asyncio.sleep(random.uniform(lo, hi))


async def _driver():
    """The one Playwright driver for the process, started on first use, stopped by shutdown()."""
    global _PW
    if _PW is None:
        from playwright.async_api import async_playwright

        _PW = await async_playwright().start()
    return _PW


async def _alive(ctx) -> bool:
    """Does the driver behind this cached context still answer?

    It has to round-trip, and that is not a stylistic preference. Measured against a persistent context
    after the Node driver was stopped underneath it:
        ctx.pages                  -> 1        (a real-looking page count, answered from client memory)
        ctx.browser.is_connected() -> True     (Playwright's own liveness answer, cached the same way)
        ctx.cookies()              -> TargetClosedError
    so both of the obvious checks report a dead browser as healthy, and a probe built on either is dead
    code. cookies() is the cheapest call that has to cross the wire; the crossing is the whole probe.
    """
    try:
        await ctx.cookies()
        return True
    except Exception:
        return False


async def _restart_driver(reason: str) -> None:
    """Throw the driver away and start a fresh one, so the rest of the batch is not lost with it.

    A dead Playwright driver never comes back: `Connection closed while reading from the driver` is a
    broken transport, not a slow page. Four rows of one live batch died on it - three reported it
    verbatim, and the fourth ("wizard stalled before submit") is the same crash seen from inside
    _apply_flow, where the browser died mid-apply and the selector walk took the blame. Without this,
    every job after the crash fails identically and the report points at the wrong thing.
    """
    global _PW
    pw, _PW = _PW, None
    _CONTEXTS.clear()
    if pw is not None:
        try:
            await pw.stop()
        except Exception:
            pass
    print(f"  browser driver: restarted ({reason})")


async def _launch_profile(platform: str, headless: bool | None = None):
    session_dir = config.BROWSER_SESSIONS / platform
    session_dir.mkdir(parents=True, exist_ok=True)
    ctx = await (await _driver()).chromium.launch_persistent_context(
        str(session_dir),
        headless=config.HEADLESS if headless is None else headless,
        viewport={"width": 1440, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    ctx.on("close", lambda _ctx, p=platform: _CONTEXTS.pop(p, None))
    return ctx


async def _context(platform: str, headless: bool | None = None):
    """One persistent context per profile, reused for the process lifetime.

    Contexts are cached on purpose: relaunching per call would throw away the logged-in profile lock
    and cost seconds per job. Pages are what get created per call, and every caller closes them in a
    finally. The one leak that cache can cause is a *dead* context being handed out forever after a
    crash or a manual browser close, so the entry is probed before it is reused and Playwright's own
    close event evicts it too.

    The probe is here rather than at each call site because this is the choke point every platform goes
    through - one recovery covers linkedin, naukri and the ATS path alike. The close event alone was not
    enough: it fires when a context is closed cleanly, and it never fires when the transport underneath
    it dies, which is exactly the case that took down four rows.
    """
    if platform in _CONTEXTS and not await _alive(_CONTEXTS[platform]):
        _CONTEXTS.pop(platform, None)
        await _restart_driver(f"the {platform} profile stopped answering")
    if platform not in _CONTEXTS:
        try:
            ctx = await _launch_profile(platform, headless)
        except Exception as exc:
            # A launch that fails is the other half of the same crash: the cached driver was already
            # dead when this call started, so the retry has to be behind a fresh one.
            await _restart_driver(f"{type(exc).__name__} launching {platform}")
            ctx = await _launch_profile(platform, headless)
        _CONTEXTS[platform] = ctx
    return _CONTEXTS[platform]


async def shutdown() -> None:
    """Close every context and stop the driver. Safe to call twice; a stuck close never blocks exit."""
    for ctx in list(_CONTEXTS.values()):
        await _close_quietly(ctx)
    _CONTEXTS.clear()
    global _PW
    if _PW is not None:
        try:
            await asyncio.wait_for(_PW.stop(), 15.0)
        except Exception:
            pass
        finally:
            _PW = None      # cleared even if stop() throws, or the next boot reuses a dead driver


async def _click_first(page, selectors: list[str], timeout: int = _STEP_TIMEOUT) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click()
            return True
        except Exception:
            continue
    return False


# The outreach flow holds locator OBJECTS rather than selector strings, because a control scoped inside
# the top card cannot be expressed as one CSS string (filter(has=...) has no CSS equivalent). These two
# are _click_first / _visible for a single already-built locator.
async def _click(loc, timeout: int = _ACT_TIMEOUT) -> bool:
    try:
        await loc.wait_for(state="visible", timeout=timeout)
        await loc.click()
        return True
    except Exception:
        return False


async def _seen(loc, timeout: int = 1_500) -> bool:
    """True if the locator is on screen. Probe only - nothing is clicked or typed."""
    try:
        await loc.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


async def _upload(page, selectors: list[str], pdf_path) -> bool:
    for sel in selectors:
        try:
            await page.locator(sel).first.set_input_files(str(pdf_path), timeout=_STEP_TIMEOUT)
            return True
        except Exception:
            continue
    return False


async def _fill_identity(page) -> int:
    """Type name/email/phone into EMPTY identity inputs. Returns how many were filled.

    A guest ATS form (Greenhouse, Lever, a Workday quick-apply) refuses submit with its required fields
    blank, so uploading a resume and clicking Submit lands nowhere - which is what "wizard stalled" was.
    Only empty inputs are touched: on LinkedIn and Naukri these fields arrive prefilled from the profile,
    and overwriting a board's own value with an env var is how a typo reaches a real recruiter.

    ponytail: covers the four fields every form asks for. Anything bespoke (notice period, work
    authorisation) still needs a human, which is what the interceptor is for.
    """
    fields = (
        (("input[name*='first' i][type=text]", "input[id*='first' i]",
          "input[autocomplete='given-name']"), ai_engine.CANDIDATE.split()[0]),
        (("input[name*='last' i][type=text]", "input[id*='last' i]",
          "input[autocomplete='family-name']"), ai_engine.CANDIDATE.split()[-1]),
        (("input[name*='full' i][type=text]", "input[autocomplete='name']",
          "input[id*='fullname' i]"), ai_engine.CANDIDATE),
        (("input[type=email]", "input[name*='email' i]", "input[id*='email' i]"),
         ai_engine.CONTACT.get("email", "")),
        (("input[type=tel]", "input[name*='phone' i]", "input[id*='phone' i]"),
         ai_engine.CONTACT.get("phone", "")),
    )
    filled = 0
    for selectors, value in fields:
        if not value:
            continue
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=1_200)
                if (await loc.input_value() or "").strip():
                    break                      # the board already knows this - do not overwrite it
                await loc.fill(value)
                filled += 1
                break
            except Exception:
                continue
    return filled


# A consent acknowledgement, as opposed to a factual question that happens to be a required checkbox.
_CONSENT_RE = re.compile(r"confirm|agree|consent|privacy|terms|acknowledge|read and underst|i have read",
                         re.I)


async def _fill_required(page) -> tuple[int, list[str]]:
    """Answer the required selects and consent checkboxes that block submit.

    Returns (how many were set, the labels of the questions deliberately left alone). Those labels ARE the
    stall reason: "extra questions need a human" names a symptom, and one attended pass needs to know which
    question to answer.

    This is the other half of "wizard stalled before submit". Measured on NTT's Phenom guest form: the PDF
    was attached and the name and email fields were in, and submit was still refused by an empty required
    `select#deviceType` and an unticked consent checkbox - neither of which any upload or text fill answers.
    The same two controls gate Greenhouse, Lever and Workday guest forms.

    Deliberately narrow, because every value written here is read by a recruiter as the candidate's own:

    * a required <select> gets its first real option ONLY when the options are not a yes/no pair. Every
      factual eligibility question - sponsorship, notice period, relocation, disability - is a yes/no, and
      guessing one misrepresents the user to an employer. A non-binary list is form plumbing ("Device
      Type", "How did you hear about us?") where any value is true enough to submit.
    * a required checkbox is ticked ONLY when its label reads as a consent or privacy acknowledgement. A
      required box can equally say "I require visa sponsorship", and ticking that is a lie on the form.

    Whatever is skipped leaves the wizard stalled, which is exactly the interceptor's cue: a human then
    answers it once in a visible browser and the saved session carries the answer forward.
    """
    try:
        todo = await page.evaluate("""() => {
            const placeholder = t => !t || /^(select|choose|please|--|\\s*$)/i.test(t);
            const label = el => {
                const l = (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) ||
                          el.closest('label') ||
                          (el.closest('div,fieldset') || document).querySelector('label');
                return ((l && l.innerText) || el.getAttribute('aria-label') || '').trim();
            };
            const out = [];
            const all = [...document.querySelectorAll('select,input[type=checkbox]')];
            all.forEach((el, nth) => {
                const r = el.getBoundingClientRect();
                if (!el.required || (r.width === 0 && r.height === 0)) return;
                if (el.tagName === 'SELECT') {
                    if ((el.value || '').trim()) return;              // the form already answered it
                    const opts = [...el.options].filter(o => !placeholder((o.value || '').trim()));
                    const texts = [...el.options].map(o => (o.text || '').trim().toLowerCase())
                        .filter(t => !placeholder(t));
                    if (!opts.length) return;
                    out.push({kind: 'select', nth, value: opts[0].value.trim(), label: label(el),
                              yesno: texts.length > 0 && texts.length <= 2 &&
                                     texts.every(t => /^(yes|no)$/.test(t))});
                } else if (!el.checked) {
                    out.push({kind: 'check', nth, label: label(el)});
                }
            });
            return out;
        }""")
    except Exception:
        return 0, []                 # best-effort: a stalled wizard is still reported, just not helped
    controls = page.locator("select,input[type=checkbox]")     # same selector, so nth is the same element
    filled, blocked = 0, []
    for item in todo:
        text = (item.get("label") or "").replace("\n", " ").strip()[:60]
        if item["kind"] == "select" and item.get("yesno"):
            print(f"  wizard: '{text}' left unanswered - a yes/no question is the user's to answer")
            blocked.append(text or "an unlabelled required dropdown")
            continue
        if item["kind"] == "check" and not _CONSENT_RE.search(text):
            print(f"  wizard: '{text}' left unticked - not a consent box, so ticking it answers for the user")
            blocked.append(text or "an unlabelled required checkbox")
            continue
        try:
            loc = controls.nth(item["nth"])
            if item["kind"] == "select":
                await loc.select_option(item["value"], timeout=2_500)
            else:
                await loc.check(timeout=2_500)
            filled += 1
        except Exception:
            continue
    return filled, blocked


# Every element type that can be an apply control. Anchors and buttons are the usual case; Naukri paints
# both its Apply button and its post-application `Applied` state as plain divs, so a list narrower than
# this makes _hydrate stop waiting before that state exists and _apply_report then never sees it.
_CONTROLS = 'a,button,input[type=submit],[role=button],div[class*="apply" i],span[class*="apply" i]'


async def _hydrate(page, timeout: int = 12_000) -> int:
    """Wait until an apply control has actually rendered. Returns the word count it settled on.

    Measured live on three employer career sites: tatacommunications answered "Career Portal", 1 word;
    NTT's Phenom page answered 188 words with its Apply anchor still unrendered, while Blue Yonder's
    identical build carried the anchor at 894. So the useful signal is the control, not the size - a body
    text check passes on a half-painted page and every selector then misses a button that arrives a
    second later. A page that never grows one (a canvas app, an expired posting) just costs the wait.
    """
    for _ in range(2):
        try:
            await page.wait_for_function("""() => {
                const w = (document.body?.innerText || '').trim().split(/\\s+/).length;
                if (w <= 40) return false;
                return [...document.querySelectorAll('""" + _CONTROLS + """')].some(el =>
                    /apply/i.test((el.innerText || el.value || '') + ' ' + (el.className || '') +
                                  ' ' + (el.id || '')));
            }""", timeout=timeout)
            break
        except Exception:
            # A Phenom/Aurelia careers site gates its hydration XHR on consent: measured 282 words on NTT's
            # page with the OneTrust wall still up and 1213 the moment it was accepted. The arrival-time
            # dismiss loses that race because the wall paints after the page does, so the wait pays for a
            # second pass - but only when a wall was actually there to click, never on a thin or dead page.
            if not await _dismiss_consent(page):
                break                # a genuinely thin page (a canvas app, a 404) is the caller's to report
    try:
        return len((await page.evaluate("document.body.innerText") or "").split())
    except Exception:
        return 0


async def _canvas_app(page) -> bool:
    """True when the page paints itself into a canvas instead of a DOM.

    jobs.tatacommunications.com is a Flutter web build: it ships flutter_bootstrap.js and renders every
    control inside <flt-glass-pane>, so document.querySelectorAll finds no button, no text and no apply
    link no matter how long the wait is. Worth naming, because "no apply control" invites a selector hunt
    that cannot ever succeed here - a human at a real browser is the only route.
    """
    try:
        return bool(await page.evaluate(
            "() => !!document.querySelector('flt-glass-pane, flutter-view, flt-scene-host, "
            "script[src*=\"flutter_bootstrap\"]')"))
    except Exception:
        return False


async def _submitted(page) -> bool:
    """True when the page says the application landed.

    A careers site puts unrelated `Submit` buttons on the job page (job alerts, site search), and
    _apply_flow tries submit before advance, so without this a job-alert subscription would be recorded as
    an application - the one failure mode worse than not applying, because the row is never retried.
    """
    try:
        return bool(await page.evaluate(
            """() => /application (was |has been |is )?(sent|submitted|received|complete)"""
            """|thank you for applying|thanks for applying|successfully applied|applied successfully"""
            """|we (have )?received your application|your application (was|has been) sent/i"""
            """.test(document.body.innerText || '')"""))
    except Exception:
        return False


async def _dismiss_consent(page) -> bool:
    """Click away a cookie/consent wall. Returns True if one was there.

    A Phenom or Avature careers site opens with a OneTrust overlay that covers the whole viewport, so
    Playwright's actionability check on the Apply button never passes and the miss reads identically to
    "this posting has no apply control". Accepting is the same click a human makes to see the page.
    """
    for sel in ("#onetrust-accept-btn-handler", "#truste-consent-button",
                "button:has-text('Accept All Cookies')", "button:has-text('Accept all')",
                "button:has-text('Accept Cookies')", "button[aria-label*='Accept all' i]"):
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=1_200)
            await loc.click()
            print(f"  ats: consent wall dismissed ({sel})")
            await _human(0.4, 0.9)
            return True
        except Exception:
            continue
    return False


async def _scopes(page) -> list:
    """The page, then every child frame - because a Playwright locator does not cross an iframe boundary.

    Greenhouse, SmartApply and Lever embeds render the ENTIRE wizard inside an iframe (`#grnhse_iframe`,
    `gh_jid` URLs), so every submit/advance selector resolves to nothing on the main frame and the walk
    reports "no submit or advance control" with the form sitting right there. Measured on job 21 (Samsara):
    a visible Apply Now was clicked, the page landed on samsara.com/...?gh_jid=8138802, and the run stalled.
    Main frame first, so a site that needs no frame pays nothing.
    """
    try:
        return [page, *(f for f in page.frames if f is not page.main_frame)]
    except Exception:
        return [page]                # a stub, or a page that closed mid-walk


async def _wizard_walk(scope, page, sel: dict[str, list[str]], pdf_path) -> tuple[bool | None, str]:
    """Attach the PDF and walk ONE surface's wizard. (True, msg) confirmed · (False, msg) a Submit was
    pressed and never confirmed · (None, reason) this surface has no wizard, so try the next frame."""
    uploaded = await _upload(scope, sel["upload"], pdf_path)
    identity = await _fill_identity(scope)
    # The other half of a stall: a required dropdown or consent box the upload cannot answer. Runs on every
    # step, because a Phenom wizard reveals its consent checkbox only on the page that submits.
    answered, blocked = await _fill_required(scope)
    identity += answered
    clicked = False
    for _ in range(6):
        if await _click_first(scope, sel["submit"], timeout=2_500):
            clicked = True
            await _human(1.5, 3.0)
            # Either surface may carry the confirmation: an embedded form prints it inside the iframe,
            # while an employer's own form redirects the whole tab to a thank-you page.
            if await _submitted(scope) or (scope is not page and await _submitted(page)):
                done = "submitted" if uploaded else "submitted (no upload field; profile resume used)"
                return True, f"{done}, {identity} identity field(s) typed" if identity else done
            continue                # that Submit was not the application's - keep walking the wizard
        if not await _click_first(scope, sel["advance"], timeout=2_500):
            break
        await _human(1.5, 3.0)
        identity += await _fill_identity(scope)    # a multi-page wizard asks again on the next step
        answered, more = await _fill_required(scope)
        identity += answered
        blocked += [q for q in more if q not in blocked]     # the same step re-probed must not double-list
    if clicked:
        # Deliberately a failure, not a pass: an employer careers page carries Submit buttons for job
        # alerts and site search, and recording one of those as an application retires the row forever
        # without anyone having applied. A false negative costs one retry; this costs the job.
        return False, "clicked submit but the page never confirmed an application (wrong Submit or a stall)"
    if blocked:
        # The questions by name, not "extra questions need a human": these are answered ONCE in a visible
        # browser and the saved session carries them forward, so the operator needs to know which ones.
        return None, "wizard stalled before submit - unanswered: " + "; ".join(blocked[:4])
    return None, "wizard stalled before submit (no submit or advance control the selectors can see)"


async def _open_apply(page, sel: dict[str, list[str]]) -> bool:
    """Click whatever opens the application on this page, over every table that could own the control.

    A board link that redirects to the employer's own careers host changes which selector table applies:
    in.indeed.com/rc/clk landed on careers.hcltech.com, where the control is the anchor
    a.unify-apply-now[Apply now] - and indeed's apply list has no anchor variant at all, so the run
    reported "no apply control" against a page holding two of them. The ATS table does carry anchors, so it
    is tried as a tail on every platform before anything is called a miss. Consent first: a wall makes
    every control unclickable rather than absent, and dismissing OneTrust is exactly what revealed
    HCLTech's anchor to the probe.
    """
    if await _click_first(page, sel["apply"]):
        return True
    if await _dismiss_consent(page) and await _click_first(page, sel["apply"], timeout=2_500):
        return True
    return await _click_first(page, [s for s in _ATS_SELECTORS["apply"] if s not in sel["apply"]],
                              timeout=2_500)


async def _apply_flow(page, sel: dict[str, list[str]], pdf_path) -> tuple[bool, str]:
    """Open the apply widget, attach the tailored PDF, walk the wizard until submit is confirmed.

    An off-site listing is recognised the moment its Apply click has moved us off this host, and the walk
    is abandoned there rather than run against a page that is no longer the board. Two wizard walks were
    being paid for every external job: this one, which could only ever find the employer's ATS wearing the
    board's selectors, then the ATS's own pass - and the human's ENTER prompt sat behind both, minutes
    after the browser had already opened. Where the click landed is therefore checked before the FIRST
    selector is tried, and the caller's `_landed_or_linked` turns that same url into the handoff.
    """
    words = await _hydrate(page)     # every apply miss crosses here, so wait for the render once, here
    home = _ats_domain(page.url)     # the board's own host, before any click can move the tab off it
    if not await _open_apply(page, sel):
        # A thin page is a transient, not a verdict. Measured on both indeed rows of one live batch: the
        # employer redirect answered 37 words with no control at all, while a read-only probe of the same
        # two urls minutes later rendered 518 words with the Apply anchor sitting there. One reload costs
        # seconds; calling that a miss costs the application. Only below the floor, so a real page that
        # simply moved its button is still reported on the first pass.
        if words < 120:
            try:
                await page.reload(wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
                words = await _hydrate(page)
            except Exception:
                pass                 # a page that will not reload is reported by the miss probe below
        if not await _open_apply(page, sel):
            return await _apply_miss(page, words)
    await _human()
    if handed := _off_host(page, home):
        print(f"  apply: off-site listing - the click handed off to {urlsplit(handed).hostname}, "
              f"skipping the board's wizard walk")
        return False, f"the apply control handed off to {urlsplit(handed).hostname}"
    stall, hard = "", ""
    for scope in await _scopes(page):
        verdict, msg = await _wizard_walk(scope, page, sel, pdf_path)
        if verdict:
            return True, msg
        if verdict is False:
            hard = msg           # a Submit was pressed on this surface; do not go press another one
            break
        if not stall or "unanswered" in msg:
            stall = msg          # the named questions beat "no control found" as a report
    # Every post-click failure re-probes, because the pre-click probe never ran on this path: a click that
    # landed can still leave the board showing `Applied`. Measured on job 24 (Beghou) - the run reported
    # "no submit or advance control" while Naukri rendered div[Applied], so an application already on file
    # was retried forever against a page with no Apply button left to click.
    verdict = await _apply_report(page)
    if verdict == "applied":
        return True, "the board shows this application as already submitted - nothing left to click"
    if verdict == "dead":
        return False, _DEAD_BROWSER
    return False, hard or stall


async def _apply_miss(page, words: int) -> tuple[bool, str]:
    """Name which kind of miss this was. Split out so the post-click stall can reuse the same probe."""
    # Probe here, not in _external_url: this is the one place every miss passes through - a board
    # listing, an employer's own ATS page, an expired session - and the printout says which it was.
    verdict = await _apply_report(page)
    if verdict == "applied":
        # A win, not a miss: the board itself says this application is on file, so there is no control
        # left to click and no retry that can improve on it. Reporting it as a failure is what kept
        # job 25 queued - Naukri rendered `Applied` and every apply selector looked for `Apply`.
        return True, "the board already shows this application as submitted - nothing left to click"
    if verdict == "dead":
        # Checked before the canvas probe: a Flutter portal and a crashed browser both fail to yield a
        # control, and only one of them is worth a retry. The context resets itself, so the retry button
        # is a real fix here rather than a repeat of the same crash.
        return False, _DEAD_BROWSER
    if await _canvas_app(page):
        return False, ("canvas-rendered career portal (Flutter): it has no DOM controls to click at "
                       "all, so this one needs a human at a real browser")
    if verdict == "closed":
        # Terminal, and it has to say so: no selector, saved session or human at the keyboard can
        # apply to a posting the employer took down, so a retry only spends a browser launch.
        return False, "the posting is closed - no application is possible, this job is unwinnable"
    return False, f"no apply control after {words}-word render (external-apply or expired session)"


async def _fill_first(page, selectors: tuple[str, ...], text: str,
                      timeout: int = _DM_TIMEOUT) -> bool:
    """LinkedIn's composer is a contenteditable div, the invite note is a real textarea. Try both.

    `timeout` is per selector. Optional surfaces (the InMail subject, which only premium accounts get)
    pass a short one so a free account does not burn _DM_TIMEOUT per selector proving its absence.
    """
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click()
            try:
                await loc.fill(text)
            except Exception:
                await page.keyboard.type(text, delay=12)   # some builds refuse fill() on the div
            return True
        except Exception:
            continue
    return False


async def _visible(page, selectors: tuple[str, ...], timeout: int = 1_500) -> bool:
    """True if any selector is on screen. Probe only — nothing is clicked or typed."""
    for sel in selectors:
        try:
            await page.locator(sel).first.wait_for(state="visible", timeout=timeout)
            return True
        except Exception:
            continue
    return False


async def _click_named(scope, pattern, timeout: int = _ACT_TIMEOUT) -> bool:
    """Click the control inside `scope` whose ACCESSIBLE NAME matches `pattern`. Link or button.

    Two reasons this is not `scope.locator("button").filter(has_text=...)`, which is what it replaces:

    - Tag. LinkedIn's current profile ships Connect and Message as <a href> anchors, not <button>s, so a
      button-only lookup resolves to 0 and every door in the flow cascades shut. Older builds, and the
      1st-degree layout, still use buttons - hence both roles, link first because that is what the
      current build serves.
    - Name vs text. The rail inside <main> has its own Connect/Message anchors with the same text; their
      aria-labels name the person ("Message Sakshi Vyas") and so fail an anchored name match, where a
      has_text filter matches all of them. `.first` alone was carrying that, on DOM order.
    """
    for role in ("link", "button"):
        if await _click(scope.get_by_role(role, name=pattern).first, timeout):
            return True
    return False


async def _hydrated_main(page):
    """The primary content area, once the SPA has actually rendered into it.

    LinkedIn ships an empty <main> in the HTML document and hydrates the profile in afterwards, so a
    locator resolved on domcontentloaded finds buttons that do not exist yet. This is the one wait that
    is allowed to raise: a main that never becomes visible is a dead url, a throttle or a redirect, and
    the caller's screenshot net wants to see that page rather than get a tidy string back.
    """
    main_area = page.locator(_MAIN).first
    await main_area.wait_for(state="visible", timeout=_HYDRATE_TIMEOUT)
    try:
        # Secondary renders (the action bar is one of them). Best-effort by design: LinkedIn holds open
        # polls and websockets, so networkidle frequently never fires - the timeout IS the normal path.
        await page.wait_for_load_state("networkidle", timeout=_IDLE_TIMEOUT)
    except Exception:
        pass
    return main_area


async def _open_connect(page, main_area) -> bool:
    """Reach the invite surface. Two doors, both keyed by this profile's own vanityName."""
    m = _LI_PROFILE.match(page.url)
    href = _CONNECT_SEL.format(slug=m.group(1)) if m else ""
    # 1. The href key, which is exact: it carries this profile's own vanityName. Scoped to main, but the
    #    slug is what actually protects this: the recommendation rail lives INSIDE main and carries
    #    fourteen other people's custom-invite anchors, and only the slug tells them apart.
    if href and await _click(main_area.locator(href).first):
        return True
    # There is deliberately NO name-based Connect door here. A `Connect` accessible-name match scoped to
    # main resolves to the "People similar to <person>" rail as readily as to the action bar - measured live
    # on an Accenture recruiter who was already Followed, where it opened an invite dialog for an unrelated
    # third party and only failed to send because that rail's dialog has no add-note affordance. An invite
    # is irreversible outreach to someone who never appeared in any job, so this door is keyed by the
    # profile's own vanityName or it does not exist. Door 2 below is the same href under the overflow menu.
    # 2. Demoted into the overflow menu - the OTHER layout LinkedIn currently serves, where Follow is
    #    promoted onto the action bar and Connect moves in here (measured live: the bar reads
    #    Message / Follow / More and main-scoped Connect is legitimately 0). Two measured reasons this
    #    door is href-keyed under the menu instead of a main-scoped name match:
    #      - the menu is PORTALED OUT of <main>: `main >> [role=menu]` counts 0 while the page counts 1,
    #        so main_area can never reach the item no matter how it is matched;
    #      - the item's accessible name is not its text. It reads "Connect" and carries no aria-label,
    #        yet an anchored menuitem name match resolves to 0 page-wide, because the label sits in
    #        nested children under obfuscated hashed classes.
    #    The same vanityName href that keys door 1 IS on that menu item, so this stays exact rather than
    #    positional: it can only ever match this profile's own invite link.
    if not await _click_named(main_area, _MORE_RE):
        return False
    await _human(0.6, 1.4)
    menu = page.locator(_MENU_SEL).first
    if href and await _click(menu.locator(href).first):
        return True
    # Legacy: the older menu did name its item. Anchored on purpose - the menu's first entry is
    # "Send profile in a message", and a loose match there would share this profile with a third party
    # instead of inviting them.
    return await _click(menu.get_by_role("menuitem", name=_CONNECT_RE).first)


async def _connect_with_note(page, main_area, note: str) -> tuple[str, str]:
    """Free channel, tried first: Connect, then "Add a note". Returns (state, message).

    state is "note" when a personalised invite went out, "invite" when the invite went out without a
    note (LinkedIn shows a direct-send dialog on some profiles), "pending" when an invite from an earlier
    run is still outstanding so there is nothing left to send, and "" when nothing was sent.

    Preferred over InMail on purpose: a note on a connection request is free and reaches 2nd/3rd-degree
    recruiters, where Message would hit the Premium paywall.
    """
    if not await _open_connect(page, main_area):
        # Door 3 opens the overflow menu and, when Connect is not in there, leaves it open ON TOP of the
        # action bar - which would swallow the Message click that comes next. Escape costs nothing on the
        # layouts where no menu ever opened.
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        # Connect being absent is not always a DOM problem: an invite already sent from an earlier job at
        # the same company replaces it with a Pending anchor. Checked only HERE, on the miss path, so the
        # happy path pays nothing for it - and read, never clicked, because clicking withdraws that invite.
        m = _LI_PROFILE.match(page.url)
        if m and await _visible(page, (_PENDING_SEL.format(slug=m.group(1)),), timeout=_ACT_TIMEOUT):
            return "pending", "invite already pending from an earlier run - nothing more to send"
        return "", "no connect control in the main profile area"
    await _human(1.0, 2.0)
    # Connect renders the invite modal IN PLACE, with the url unchanged: the SPA intercepts the anchor and
    # the /preload/custom-invite/ href is only the no-JS fallback (useful as an identity key, never as a
    # destination). The modal also lands inside an open shadow root, which is why document.querySelectorAll
    # finds nothing there while Playwright - which pierces open shadow roots - counts it fine. Still the
    # nav-sized wait rather than the click-sized one: measured, the modal needs seconds, and _ACT_TIMEOUT
    # here was reporting "no invite dialog opened" while it was still rendering.
    dialog = page.locator(_DIALOG).first
    if not await _seen(dialog, _DM_TIMEOUT):
        return "", "connect clicked but no invite dialog opened"
    if not await _click(dialog.locator(_NOTE_SEL).first):
        # Direct-send dialog: the invite can still go, just unpersonalised. Reported as such, and the
        # message channel is still tried afterwards, because an invite is not a delivered message.
        if await _click(dialog.locator(_SEND_INVITE).first):
            return "invite", "connected without a note (no add-note affordance)"
        await _click_first(page, _DISMISS, timeout=1_500)
        return "", "invite dialog had neither an add-note button nor a send button"
    note_text = note[:NOTE_LIMIT].strip()
    # _fill_first's own default (_DM_TIMEOUT), not the click-sized _ACT_TIMEOUT it used to be passed: the
    # textarea lands in the SAME slow shadow-root overlay whose dialog wait was already widened for exactly
    # this reason, and 4s there was reporting "the note textarea never appeared" while it was rendering.
    if not await _fill_first(page, (_NOTE_BOX,), note_text):
        # No textarea after Add a note means the free personalised-invite allowance (3 a month) is spent:
        # LinkedIn swaps the note field for an upsell. The plain invite is still available and still puts
        # this candidate in front of the recruiter, so it is sent rather than thrown away - measured live,
        # this was two of five jobs reaching nobody with a working invite one click away.
        if await _click(dialog.locator(_SEND_INVITE).first):
            return "invite", "connected without a note (personalised-invite allowance spent)"
        await _click_first(page, _DISMISS, timeout=1_500)
        return "", "add-note clicked but the note textarea never appeared"
    await _human(1.0, 2.0)
    if not await _click(dialog.locator(_SEND_INVITE).first):
        return "", "note typed but the send-invitation button is missing (nothing sent)"
    # Dialog detaching is the only proof LinkedIn accepted it; a dialog still on screen means the invite
    # bounced (weekly invite cap, or an account restriction) however clean the clicks looked.
    try:
        await dialog.wait_for(state="detached", timeout=_ACT_TIMEOUT)
    except Exception:
        return "", "invite dialog stayed open after send (weekly invite cap, or a restricted account)"
    return "note", "Connected with personalized note"


async def _clear_paywall(page) -> None:
    """Dismiss a Premium/InMail upsell modal if one is on screen. Cheap enough to call before each channel.

    Escape is the fallback because at least one variant ships no close control the _DISMISS list matches.
    """
    if await _visible(page, _PAYWALL):
        if not await _click_first(page, _DISMISS, timeout=_ACT_TIMEOUT):
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
        await _human(0.5, 1.2)


async def _message_fallback(page, main_area, message: str, role: str, company: str) -> tuple[bool, str]:
    """Second channel: main's Message control, for 1st-degree, premium and open profiles."""
    if not await _click_named(main_area, _MESSAGE_RE):
        return False, "no message button in the main profile area"
    # Also an in-place overlay, not a navigation: the url stays on the profile and the "New message" drawer
    # renders inside the same shadow root the invite modal uses, pre-addressed to that person. It is also
    # SLOW - measured at over three seconds - and that is why the composer is waited for HERE, before the
    # paywall and drawer probes rather than after them. Those probes have short timeouts by design, so on an
    # empty page they all returned "not present" and the guards were reporting on a surface that had not
    # rendered yet: the paywall got misreported as "composer never appeared", and the unaddressed-drawer
    # check - the one that must never let a message be typed to nobody - was answering before the drawer
    # existed. Waiting first makes all three probes answer about the real surface.
    await _human(1.0, 2.0)
    composer = await _visible(page, _MSG_BOX, timeout=_DM_TIMEOUT)
    # Paywall checked once, after the wait, and before the composer-absence report: on a free account the
    # upsell is the REASON the composer is missing, so testing it first turns "composer never appeared" into
    # something actionable - and it still blocks an upsell that renders over an already-open composer.
    #
    # The timeout is the composer's whenever the composer never came, because the upsell paints LATE: probed
    # live, absent at +9s after the Message click and present at +15s. The 1500ms default therefore reported
    # the useless "composer never appeared" on the one account state it exists to explain. When the composer
    # IS open there is nothing to wait for, so that case keeps the short probe and a working send is never
    # stalled behind it.
    if await _visible(page, _PAYWALL, timeout=1_500 if composer else _DM_TIMEOUT):
        await _click_first(page, _DISMISS, timeout=1_500)
        return False, "inmail paywall - this profile is not messageable on a free account"
    if not composer:
        return False, "message composer never appeared"
    if await _visible(page, _DRAWER_RECIPIENT):
        return False, "global messaging drawer opened with no recipient (nothing sent)"
    subject = (f"Regarding open roles at {company}" if company
               else f"Regarding the {role} role" if role else "")
    if subject and await _fill_first(page, _INMAIL_SUBJECT, subject, timeout=1_500):
        await _human(0.5, 1.2)                            # premium InMail: subject is mandatory
    if not await _fill_first(page, _MSG_BOX, message[:DM_LIMIT], timeout=_DM_TIMEOUT):
        return False, "message composer never appeared"
    await _human(1.0, 2.0)
    # Only reachable once the box holds text: the send button is disabled and unnamed until then, so this
    # lookup happens strictly after the fill. Its keys are in _SEND_MSG - class first, form-scoped submit
    # second - because there is no text or aria-label on it to match.
    if not await _click_first(page, _SEND_MSG, timeout=_ACT_TIMEOUT):
        return False, "composer filled but send button missing (nothing sent)"
    return True, "Direct Message sent"


async def _dm_on_page(page, message: str, role: str = "", company: str = "") -> tuple[bool, str]:
    """Outreach on a LinkedIn PROFILE page. Never call this on a search results page.

    Connect + "Add a note" first (free, works on 2nd/3rd degree), then the Message control. Both triggers
    are resolved inside <main> by accessible name, so the recommendation rail that also lives in there
    stays out of reach. Raises if main never hydrates - the caller owns the screenshot for both that and
    a plain miss.
    """
    main_area = await _hydrated_main(page)
    # Cleared BEFORE either channel runs, not inside one of them. A Premium upsell that renders over the
    # profile on arrival breaks both: the composer is behind it, and _DIALOG is :visible-filtered so the
    # invite flow's `page.locator(_DIALOG).first` would resolve to this modal instead of the invite modal
    # and then hunt for "Add a note" inside an upsell. Measured live over a recruiter's profile.
    await _clear_paywall(page)
    state, why = await _connect_with_note(page, main_area, message)
    if state == "note":
        return True, why
    await _human(1.0, 2.0)
    # And again here, because the note path SUMMONS one: with the monthly custom-invite allowance spent,
    # LinkedIn answers "Add a note" with "You've used all your monthly custom invites" over the profile.
    # Measured live on an Accenture recruiter - the Message button was right there on the action bar and
    # this modal was intercepting it, so the job reported "no message button" and reached nobody.
    await _clear_paywall(page)
    ok, msg = await _message_fallback(page, main_area, message, role, company)
    if ok:
        return True, msg
    if state == "invite":
        return True, f"{why}; message fallback also missed: {msg}"
    # An invite already outstanding is a reached recruiter, not a failed one: the connection request from
    # the earlier job is still in their inbox and LinkedIn allows nothing further on a free account. Sending
    # nothing is the correct outcome here, so it is reported as reached rather than dragging the row to
    # failed and burning a screenshot on a page where nothing is broken.
    if state == "pending":
        return True, f"{why} (message channel: {msg})"
    return False, f"neither connect-note nor message succeeded (connect: {why} | message: {msg})"


async def _debug_shot(page) -> str:
    """Full-page capture of whatever is on screen, for a failure that needs eyes on the real DOM.

    Returns the suffix for the failure message, or "" when the screenshot itself failed - a broken
    capture must never mask the reason the outreach missed.

    Security: this is a full-page image of a third party's LinkedIn profile, ie personal data about
    someone who did not consent to being photographed. It stays local (gitignored) and is for debugging
    a selector, nothing else.
    """
    try:
        await page.screenshot(path=str(_DEBUG_SHOT), full_page=True)
    except Exception as exc:
        print(f"  screenshot failed too: {type(exc).__name__}: {exc}")
        return ""
    print(f"  screenshot saved to {_DEBUG_SHOT}")
    return f" [Screenshot saved to {_DEBUG_SHOT.name}]"



_LI_PROFILE = re.compile(r"^https?://(?:[\w-]+\.)?linkedin\.com/in/([^/?#]+)", re.I)
# Out-of-network results render the name as "LinkedIn Member" with a dead href. Messaging one is
# impossible, so they are skipped rather than attempted and reported as a DOM failure.
_HIDDEN_NAME = re.compile(r"^(?:linkedin\s+member|linkedin\s+user|member)$", re.I)
_A11Y_NOISE = re.compile(r"\b(?:view|message|connect|follow)\b.*?\bprofile\b|"
                         r"^\W*(?:status is|current company:|•).*", re.I)


def _profile_url(href: str) -> str:
    """Canonical https://www.linkedin.com/in/<slug> for a search-result href, or "" if it is not one.

    Tracking query strings (?miniProfileUrn=, ?trk=) are dropped: they are per-search and would make
    the same recruiter look like a new person on every run.
    """
    m = _LI_PROFILE.match((href or "").strip())
    return f"https://www.linkedin.com/in/{m.group(1)}" if m else ""


def _clean_name(text: str) -> str:
    """First visible line of a result card's anchor, minus LinkedIn's screen-reader duplicates.

    The anchor's innerText is typically the name followed by a visually-hidden "View X's profile", and
    on some builds the name is repeated verbatim; both would otherwise defeat the hidden-profile check.
    """
    for line in (text or "").splitlines():
        line = _A11Y_NOISE.sub(" ", line).strip(" \t·•|-")
        if not line:
            continue
        half = len(line) // 2                                # "Priya SharmaPriya Sharma"
        if len(line) % 2 == 0 and line[:half] == line[half:]:
            line = line[:half]
        return re.sub(r"\s{2,}", " ", line).strip()
    return ""


def _pick_recruiter(cards: list[dict], skip: "set[str] | frozenset[str]" = frozenset()) -> tuple[str, str]:
    """First messageable recruiter in DOM order. Returns (profile_url, display_name) or ("", "").

    Hidden "LinkedIn Member" rows, non-/in/ hrefs and anyone in `skip` are dropped first. Among what
    survives, a card whose text actually reads like a hiring desk wins over the plain first hit - the
    search string is only a hint, and LinkedIn happily returns the company's developers above its
    recruiters. Falling back to the first valid card keeps the channel alive when no headline says
    "recruiter" at all.
    """
    seen, valid = set(), []
    for card in cards:
        url = _profile_url(card.get("href", ""))
        if not url or url in skip or url in seen:
            continue
        name = _clean_name(card.get("name", ""))
        if not name or _HIDDEN_NAME.match(name):
            continue
        seen.add(url)
        valid.append((url, name, card.get("card", "")))
    for url, name, blurb in valid:
        if _HR_ROLE.search(blurb) or _HR_ROLE.search(name):
            return url, name
    return (valid[0][0], valid[0][1]) if valid else ("", "")


# Read inside `main` only: the global nav's me-menu also holds an /in/ anchor, and picking that would
# make the bot DM itself. Returns one dict per anchor; all filtering happens in _pick_recruiter, which
# is plain Python and therefore testable without a browser.
_HARVEST_JS = """() => {
  const root = document.querySelector('main') || document.body;
  return [...root.querySelectorAll('a[href*="/in/"]')].map(a => {
    const card = a.closest('li') || a.closest('div[data-view-name]') || a.parentElement;
    return {
      href: a.href,
      name: (a.innerText || a.getAttribute('aria-label') || '').trim(),
      card: (card ? card.innerText : '').replace(/\\s+/g, ' ').trim().slice(0, 300),
    };
  });
}"""


async def _recruiter_from_search(page, company: str,
                                 skip: "set[str] | frozenset[str]" = frozenset()) -> tuple[str, str]:
    """Run the people search for `company` recruiters and return (profile_url, name) of the first hit."""
    await page.goto(
        f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(f'{company} recruiter')}",
        wait_until="domcontentloaded", timeout=_NAV_TIMEOUT,
    )
    try:
        await page.wait_for_selector("main a[href*='/in/']", timeout=_DM_TIMEOUT)
    except Exception:
        return "", ""                                   # zero results, or the SRP never hydrated
    await _human(1.0, 2.5)                              # lazy cards land after first paint
    return _pick_recruiter(await page.evaluate(_HARVEST_JS), skip)


# Recruiters this process already reached, so the next job at the same company gets the NEXT name instead
# of the same inbox twice. Measured need: two jobs from one board resolved to the same recruiter minutes
# apart, and the second attempt could only ever land on her now-Pending invite.
_DM_DONE: set[str] = set()
_DM_LOG = config.BROWSER_SESSIONS / "dm_attempted.json"


def _dm_done() -> set[str]:
    """Profiles already attempted - this run and every earlier one."""
    if not _DM_DONE:
        try:
            _DM_DONE.update(json.loads(_DM_LOG.read_text("utf-8")))
        except Exception:
            pass
    return _DM_DONE


def _dm_mark(profile: str) -> None:
    """Record an attempt so no later run can send to that person again."""
    _dm_done().add(profile)
    try:
        _DM_LOG.write_text(json.dumps(sorted(_DM_DONE)), "utf-8")
    except Exception as exc:
        print(f"  linkedin dm: attempt log not written ({exc}) - this profile could be retried tomorrow")


async def send_linkedin_dm(profile_url: str, message: str, role: str = "",
                           company: str = "") -> tuple[bool, str]:
    """Outreach to a recruiter at `profile_url` over the already-authenticated browser_sessions/linkedin.

    No login flow: the persistent context carries the user's cookies. A bounce to /authwall or /login
    means that profile needs re-authenticating by hand, and is reported as such rather than retried.
    `role`/`company` only fill the subject line of a premium InMail modal; free accounts never see one.
    """
    if not profile_url or "linkedin.com/" not in profile_url:
        return False, f"linkedin dm: not a linkedin url: {profile_url!r}"
    if not (message or "").strip():
        return False, "linkedin dm: empty message"
    if profile_url in _dm_done():
        return False, "linkedin dm: this profile was already attempted - not contacting the same person twice"
    if config.DRY_RUN:
        return True, f"linkedin dm: dry-run, would DM {profile_url}"

    page = None
    try:
        page = await (await _context("linkedin")).new_page()
        page.set_default_timeout(_STEP_TIMEOUT)
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
        if "/authwall" in page.url or "/login" in page.url or "/checkpoint" in page.url:
            return False, "linkedin dm: bounced to a login wall - re-authenticate browser_sessions/linkedin"
        await _human()
        # The screenshot net. It catches BOTH shapes of failure - a clean miss (every selector cascaded
        # and nothing was sendable) and a thrown one (main never hydrated, the context died mid-click) -
        # because on a live site the thrown shape is the one that tells you least without an image.
        try:
            ok, msg = await _dm_on_page(page, message, role, company)
        except Exception as exc:
            ok, msg = False, f"{type(exc).__name__}: {exc}"
        if ok:
            return True, f"linkedin dm: {msg}"
        return False, f"linkedin outreach failed: {msg}{await _debug_shot(page)}"
    except Exception as exc:
        return False, f"linkedin dm: {type(exc).__name__}: {exc}"
    finally:
        await _close_quietly(page)


async def send_linkedin_company_dm(company: str, message: str, role: str = "") -> tuple[bool, str]:
    """Last-resort outreach: resolve a named recruiter at `company`, then DM that profile directly.

    Two pages, never one: the search page is only ever read for an href. Messaging from the results
    list is what clicked the messaging overlay's own compose control and opened the recipient-less
    "New message" drawer, so the DM itself always happens on a concrete /in/ profile.

    ponytail: kept because nothing populates recruiter_linkedin_url yet, so without it the DM channel
    would be dead code. Drop it once recruiter URLs are actually scraped.
    """
    if not (company or "").strip() or not (message or "").strip():
        return False, "linkedin dm: no company or empty message"
    if config.DRY_RUN:
        return True, f"linkedin dm: dry-run, would search {company} recruiters"

    page = None
    try:
        page = await (await _context("linkedin")).new_page()
        page.set_default_timeout(_STEP_TIMEOUT)
        # Same net, one page earlier. This function never calls _dm_on_page - send_linkedin_dm does, on
        # its own page - so what is guarded here is recruiter resolution, which is where a search-results
        # layout change would strand the whole channel with nothing to look at.
        try:
            profile, name = await _recruiter_from_search(page, company, _dm_done())
            why = (f"no messageable recruiter found for {company}" if not _DM_DONE else
                   f"no messageable recruiter left for {company} (every one found was already attempted)")
        except Exception as exc:
            profile, name, why = "", "", f"{type(exc).__name__}: {exc}"
        if "/authwall" in page.url or "/login" in page.url or "/checkpoint" in page.url:
            return False, "linkedin dm: bounced to a login wall - re-authenticate browser_sessions/linkedin"
        if not profile:
            return False, f"linkedin outreach failed: {why}{await _debug_shot(page)}"
    except Exception as exc:
        return False, f"linkedin dm: {type(exc).__name__}: {exc}"
    finally:
        await _close_quietly(page)

    try:
        ok, msg = await send_linkedin_dm(profile, message, role, company)
    finally:
        _dm_mark(profile)
    return ok, f"{msg} [{name} · {profile}]"


def _warn(*lines: str) -> None:
    """Print a banner, degrading to ASCII when stdout cannot encode the glyphs.

    The interceptor banner carries a warning sign on purpose - it is the one message a human must not
    scroll past - but a redirected stdout on Windows is cp1252, and an unencodable character there
    raises UnicodeEncodeError in the middle of an application instead of printing.
    """
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    for line in lines:
        try:
            line.encode(enc)
        except (UnicodeEncodeError, LookupError):
            line = "".join("!" if ch == "⚠" else ch for ch in line if ch.isascii() or ch == "⚠")
        print(line, flush=True)


def _ats_domain(url: str) -> str:
    """Filesystem-safe session key for an external ATS url, or "" if it has no host.

    The full host is kept rather than the registrable domain: every Workday tenant
    (acme.myworkdayjobs.com) and every Greenhouse/Lever board carries its own login, so folding them
    onto myworkdayjobs.com would replay one employer's cookies at another and invalidate both.
    """
    host = (urlsplit(url or "").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return re.sub(r"[^a-z0-9.-]", "-", host).strip(".-")


def _ats_state_path(domain: str) -> Path:
    """browser_sessions/ats_<domain>.json — gitignored with the rest of the session dir."""
    return config.BROWSER_SESSIONS / f"ats_{domain}.json"


def _unwrap(url: str) -> str:
    """LinkedIn's interstitial carries the real ATS url in ?url= — return that, else the url itself.

    Measured live: every off-site listing's Apply anchor points at www.linkedin.com/safety/go/?url=<the
    employer's ATS>. Judged by its own host it is "still on LinkedIn", so the off-board test rejected it
    and three of five jobs reported "no apply control" while the ATS link was sitting in the href.
    """
    parts = urlsplit(url or "")
    if "linkedin." in (parts.hostname or "").lower() and "/safety/go" in parts.path:
        inner = dict(parse_qsl(parts.query)).get("url", "")
        if inner.startswith("http"):
            return inner
    return url


def _off_board(url: str, here: str) -> bool:
    """True when `url` leaves the job boards entirely - the only case that is an external ATS.

    "Different host from this page" is not enough: a listing can link to another board, and treating
    indeed.com or a linkedin.com permalink as an employer portal would prompt a human to log into a
    site that already has a persistent profile, then key a session file to an aggregator every
    employer shares.
    """
    host = (urlsplit(url or "").hostname or "").lower()
    return bool(host) and host != here and not any(b.rstrip(".") in host for b in _NOT_THE_COMPANY)


async def _external_url(page) -> str:
    """The off-site application url behind a non-Easy-Apply listing, or "".

    Anchors are read, not clicked, so nothing on the board is navigated away from. LinkedIn uses a
    <button> that opens a new tab instead, so that one is clicked under a popup expectation and the
    popup is closed again the moment its url is known.
    """
    here = urlsplit(page.url).hostname or ""
    for sel in _EXTERNAL_APPLY:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=2_500)
        except Exception:
            continue
        try:
            href = await loc.get_attribute("href") or ""
        except Exception:
            href = ""
        if (href := _unwrap(href)).startswith("http") and _off_board(href, here):
            return href
        try:
            async with page.context.expect_page(timeout=_STEP_TIMEOUT) as popped:
                await loc.click()
            popup = await popped.value
            try:
                await popup.wait_for_load_state("domcontentloaded", timeout=_STEP_TIMEOUT)
                url = popup.url
            finally:
                await _close_quietly(popup)
            if (url := _unwrap(url)).startswith("http") and _off_board(url, here):
                return url
        except Exception:
            continue
    return ""


def _off_host(page, home: str) -> str:
    """The tab the Apply click took us off `home` to, or "" while we are still on that host.

    Sync on purpose, and read off nothing but URLs: this is the check that can run the instant a click
    returns, before any wizard is walked, which is the whole reason it is split out. It must never fall
    back to reading links off the page - a board that is happily walking its own Easy Apply wizard still
    has the employer's "apply on company site" anchor sitting in the DOM, and taking that as a handoff
    would abandon an application that is going through fine.
    """
    for candidate in (page, *getattr(page.context, "pages", ())):
        try:
            url = _unwrap(candidate.url)
        except Exception:
            continue
        if url.startswith("http") and _off_board(url, home):
            return url
    return ""


async def _landed_or_linked(page, board: str) -> str:
    """Where an off-site Apply control actually went - the tab it left, or the tab it opened.

    An off-site listing's Apply control is usually the link itself: the click navigates THIS tab to the
    employer's ATS. By the time the wizard has failed, `page` is the ATS, so _external_url can only
    read the ATS's own page for a link back to itself and returns nothing. Measured live on a LinkedIn
    listing: the click landed on vanderlande.wd3.myworkdayjobs.com, no url was found, and the job was
    recorded as "no apply control" with the external-ATS path never entered - so no session was saved
    for that host and no human was ever prompted. Where the tab went is checked first, against the
    board the job was listed on; reading a link off the page stays the fallback for the links that
    merely sit there without being clicked.
    """
    return _off_host(page, board) or await _external_url(page)


async def _apply_report(page) -> str:
    """Print the apply-ish controls a page really has when no selector matched, and name the miss.

    Returns "closed", "applied", "dead" or "" - the outcomes a miss actually has, because they need
    different answers: a closed posting is terminal, an already-applied one is a success nobody recorded,
    a dead browser is a crash that says nothing about the page, and only the empty case is worth another
    browser launch. Without the printout "no apply control" is indistinguishable between an expired
    posting, a live posting whose button text moved, and a logged-out session, so it says which one it was
    on the first run instead of after a guessing round.

    The applied probe needs both an exact "Applied" label AND an apply-ish class: LinkedIn's job list rail
    marks previously-applied cards with a bare `Applied` span, and matching that would report the job the
    user is looking at as submitted when it never was - the one failure worse than a missed application.
    """
    try:
        seen = await page.evaluate("""() => {
            const t = el => (el.innerText || el.value || '').trim().slice(0, 40);
            const all = [...document.querySelectorAll('""" + _CONTROLS + """')];
            const body = document.body.innerText || '';
            const named = el => `${el.tagName.toLowerCase()}${
                (el.className || '').trim() ? '.' + (el.className || '').trim().split(/\\s+/)[0] : ''}[${
                t(el)}]`;
            return {
                apply: all.filter(el => /apply/i.test(t(el)) || /apply/i.test(el.className || ''))
                    .slice(0, 8).map(el => `${el.tagName.toLowerCase()}[${t(el)}]${
                        el.getAttribute('href') ? ' -> ' + el.getAttribute('href').slice(0, 60) : ''}`),
                closed: /no longer accepting|no longer available|applications closed|expired/i.test(body),
                applied: all.some(el => /^applied$/i.test(t(el)) && /apply/i.test(el.className || ''))
                    || /you have already applied|already applied (to|for) this/i.test(body),
                others: all.filter(el => !/apply/i.test(t(el)) && !/apply/i.test(el.className || '')
                    && el.getClientRects().length && t(el)).slice(0, 8).map(named),
            };
        }""")
    except Exception as exc:
        # Not "" - a browser that will not answer is not a page with no apply control, and collapsing the
        # two is what let a crash be reported as selector drift. The caller is the only place that can tell
        # the operator which happened, so the distinction has to leave here intact. It is narrower than
        # "evaluate raised", though: a page that navigated out from under the probe answers nothing either
        # and is still a plain miss, so only the transport signatures count as dead.
        print(f"  apply probe: the DOM would not answer ({type(exc).__name__})")
        return "dead" if _DRIVER_LOST.search(f"{type(exc).__name__}: {exc}") else ""
    if seen.get("closed"):
        print("  apply probe: the posting says it is closed - no button exists to click")
    if seen.get("applied"):
        print("  apply probe: the board already shows this one as applied")
    print(f"  apply probe: {seen.get('apply') or 'no apply-ish control on the page at all'}")
    # The controls that are NOT apply-ish, because a stall lives among exactly those: Naukri answers a
    # successful Apply click with a chatbot drawer whose advance control is a div with no `apply` in it,
    # so the printout above shows the button that already worked and nothing about what blocked. Named
    # here, the next selector is read off a real page instead of guessed.
    if seen.get("others"):
        print(f"  apply probe: other visible controls: {seen['others']}")
    try:
        print(f"  apply probe: landed {page.url}")
    except Exception:
        pass                         # a closed page still owes the verdict below
    # Applied wins over closed: an employer closes a posting after collecting applications, and the one
    # already on file is the fact worth recording. Closed-and-never-applied is the unwinnable case.
    return "applied" if seen.get("applied") else ("closed" if seen.get("closed") else "")


async def _close_quietly(page=None, ctx=None, browser=None, timeout: float = 10.0) -> None:
    """Release browser handles without letting one of them hold the run open.

    Playwright's close() has no timeout of its own, and the launch flags carry --disable-hang-monitor,
    so a renderer that has stopped answering leaves close() pending for good: the batch stops on that
    job and never says why. Each handle is bounded, and anything that will not close inside the bound
    is abandoned to the driver rather than waited on - a leaked tab beats a run that never advances.
    """
    for handle in (page, ctx, browser):
        if handle is None:
            continue
        try:
            await asyncio.wait_for(handle.close(), timeout)
        except Exception:
            pass


async def _ats_session_apply(url: str, domain: str, pdf_path) -> tuple[bool, str]:
    """Walk the generic ATS wizard headlessly, replaying the saved session for `domain` if one exists.

    A missing session file is not a reason to skip: Greenhouse, Lever and most Workday guest forms take
    a name, an email and a resume from an anonymous visitor, so the headless pass is tried either way and
    the human interceptor is only paid for when the form actually demands an account.

    Storage state is rewritten on success so rotated cookies survive; on failure the file is left
    exactly as it was, because a half-expired session is still better than none for the interceptor.
    """
    state = _ats_state_path(domain)
    browser = ctx = page = None
    try:
        browser = await (await _driver()).chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(storage_state=str(state) if state.exists() else None,
                                        user_agent=_UA,
                                        viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        page.set_default_timeout(_STEP_TIMEOUT)
        await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
        await _human(1.0, 2.0)
        await _dismiss_consent(page)   # an overlay makes every Apply button unclickable, not absent
        ok, msg = await _apply_flow(page, _ATS_SELECTORS, pdf_path)
        if ok:
            await ctx.storage_state(path=str(state))
        return ok, msg
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        await _close_quietly(page, ctx, browser)


# The dashboard's answer to the console's ENTER. "Press ENTER when the form is filled" is the one step a
# browser run cannot do by itself, and under uvicorn there is no console to press it on - the operator
# drives a visible Chromium from a web page, so the release has to live on that page too. main.py points
# ON_HUMAN at a callable that folds the notice into the running task's own line, and the two buttons it
# paints post back into release_human() below.
# ponytail: ONE wait at a time, which is exactly what the browser lock and _INTERCEPT_LOCK already
# guarantee - a second manual apply cannot be open while the first one holds the browser.
ON_HUMAN = None                     # Callable[[dict], None] | None, set by main._run for browser tasks
_HUMAN: dict | None = None          # the wait in flight: {"gate", "ok", "job", "domain"}


def human_waiting() -> dict | None:
    """What the dashboard should be offering a button for right now, or None. Read by /health."""
    return {"job": _HUMAN["job"], "domain": _HUMAN["domain"]} if _HUMAN else None


def release_human(job_id: int, done: bool) -> bool:
    """Answer the wait with done / walked away. False when nobody is waiting on that job.

    The job id is checked rather than ignored: a stale dashboard, a second tab and a wrong row all reach
    this from the open internet of one's own browser, and releasing a wait that belongs to another job
    would drive a form the human is not looking at.
    """
    if not _HUMAN or (_HUMAN["job"] and job_id != _HUMAN["job"]):
        return False
    _HUMAN["ok"] = done
    _HUMAN["gate"].set()
    return True


async def _wait_enter(prompt: str = "\nPress ENTER to continue: ", notice: dict | None = None) -> bool:
    """Wait for ENTER on the console - or on the dashboard - without parking a thread nothing can join.
    False on Ctrl-C / EOF, and on the row's own "didn't send" button.

    `asyncio.to_thread(input, ...)` looks like the obvious read and is a trap on Windows: the thread blocks
    on stdin, it cannot be cancelled, and asyncio.Runner.close() then spends shutdown_default_executor's
    300-second join on it - which is one of the two reasons a shutdown after an interceptor had been used
    never completed and ended in a force-killed PowerShell. Polling the console instead makes the wait an
    ordinary awaitable that a cancel actually stops, so Ctrl-C during a manual application unwinds the
    batch normally. msvcrt is Windows-only, and everywhere else the read stays in a thread because this
    hang is a Windows console one.

    `notice` is the same wait told to the dashboard: main.py folds it into the task's line, so the panel
    names the job and paints the two buttons, and this returns the moment one of them is posted. Both
    sources stay live at once - the terminal keeps working for a run started from a console.
    """
    global _HUMAN
    print(prompt, end="", flush=True)
    gate = asyncio.Event()
    _HUMAN = {"gate": gate, "ok": False, "job": 0, "domain": (notice or {}).get("domain", "")}
    if notice and ON_HUMAN:
        ON_HUMAN(notice)                 # the hook names the job - only the caller of the run knows it
    _HUMAN["job"] = (notice or {}).get("job_id") or 0
    try:
        if sys.platform != "win32":
            ask = asyncio.ensure_future(asyncio.to_thread(input))
            seen = asyncio.ensure_future(gate.wait())
            try:
                done, _ = await asyncio.wait({ask, seen}, return_when=asyncio.FIRST_COMPLETED)
                if ask in done:
                    ask.result()                     # EOFError / KeyboardInterrupt mean "walked away"
                    return True
                return _HUMAN["ok"]
            except (EOFError, KeyboardInterrupt):
                return False
            finally:
                seen.cancel()                        # the parked input thread is a Windows-only hazard
        import msvcrt
        while True:
            # The await IS the fix: it is the point a cancelled task is allowed to land on, and the reason
            # this can be torn down while the human is still filling in a form. It is also where the
            # dashboard's answer is noticed - the console poll is 150ms, so a click lands like a keystroke.
            await asyncio.sleep(0.15)
            if gate.is_set():
                print()
                return _HUMAN["ok"]
            while msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in ("\r", "\n"):
                    print()
                    return True
                if key in ("\x03", "\x1a"):     # Ctrl-C / Ctrl-Z reach us as keystrokes, never as signals
                    print()
                    return False
    finally:
        _HUMAN = None


async def _ats_intercept(url: str, domain: str) -> tuple[bool, str]:
    """Human-in-the-loop fallback: visible browser, manual application, then save the session.

    The wait is unbounded by design - the mandate is that the batch holds until the human is done - and
    it is answerable from two places: the console (ENTER, cancellable, see _wait_enter) and the dashboard
    (the two buttons main.py paints from the notice below, which is the only reason this path exists
    under uvicorn at all). What is still refused is neither of them: no console and no dashboard means
    nobody can answer, and opening a browser nothing will ever drive would stall the request forever.
    """
    state = _ats_state_path(domain)
    if config.DRY_RUN:
        return False, f"dry-run, would open {domain} in a visible browser for manual apply"
    if not (config.HITL and (sys.stdin and sys.stdin.isatty() or ON_HUMAN)):
        return False, (f"external ATS {domain} needs a human at the keyboard - rerun this job from your "
                       f"own console to apply once and save browser_sessions/{state.name}, after which "
                       f"every future job at {domain} submits headlessly")

    async with _INTERCEPT_LOCK:
        browser = ctx = page = None
        try:
            browser = await (await _driver()).chromium.launch(
                headless=False, args=["--disable-blink-features=AutomationControlled"])
            ctx = await browser.new_context(
                storage_state=str(state) if state.exists() else None,
                viewport={"width": 1440, "height": 900},
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
            # Boards hand out tracker links (click.appcast.io, a jobvite redirect). The host that was
            # clicked is not the host the human logs into, and keying the session to the tracker would
            # replay one employer's cookies at every other employer behind the same redirector, which is
            # exactly what _ats_domain keeps the full host to avoid. The landed host owns the login.
            # ponytail: costs one extra prompt per tracker link, since the miss is only visible after
            # navigating. Upgrade path is recording the tracker -> landed pair on the row.
            if (landed := _ats_domain(page.url)) and landed != domain:
                print(f"  ats {domain}: redirected to {landed} - session keyed to the landed host")
                domain, state = landed, _ats_state_path(landed)
            _warn(
                "",
                "=" * 78,
                f"⚠️ EXTERNAL ATS DETECTED ({domain}): Application opened in visible browser.",
                "⚠️ Please log in and complete the application manually.",
                "⚠️ Press ENTER in this terminal when finished to save the session and continue...",
                "=" * 78,
                f"   url:     {url}",
                f"   session: {state}",
            )
            if not await _wait_enter(notice={
                    "phase": "apply", "state": "human", "domain": domain,
                    "msg": f"manual apply · {domain} · fill the form in the open browser, then press ✓"}):
                # Not `return False, "...abandoned"`. A miss would be handed back as an ordinary
                # apply-failure, and apply-failure is a route the pipeline completes by mailing the
                # recruiter - so walking away from the form would send a letter about an application
                # that was never submitted. Only the human knows whether that form got sent, and the
                # answer is "not by us". Refusing to guess is the whole of this raise.
                raise RunAborted(f"{domain}: you left the manual apply unfinished")
            state.parent.mkdir(parents=True, exist_ok=True)
            await ctx.storage_state(path=str(state))
            print(f"  ats session saved: {state}")
            return True, f"external apply completed for {domain}"
        except Exception as exc:
            return False, f"{domain} interceptor: {type(exc).__name__}: {exc}"
        finally:
            await _close_quietly(page, ctx, browser)


async def _external_apply(url: str, pdf_path) -> tuple[bool, str]:
    """Headless wizard first, human interceptor second. The interceptor is what creates the session."""
    domain = _ats_domain(url)
    if not domain:
        return False, f"external listing with an unusable url: {url!r}"
    have = _ats_state_path(domain).exists()
    ok, msg = await _ats_session_apply(url, domain, pdf_path)
    print(f"  ats {domain}: {'saved session' if have else 'guest form'} -> "
          f"{'submitted' if ok else 'no'} · {msg}")
    if ok:
        return True, f"external apply completed for {domain} ({msg})"
    return await _ats_intercept(url, domain)


async def execute_platform_apply(job: dict, pdf_path) -> tuple[bool, str]:
    """Submit the application itself. Outreach is NOT done here — pipeline.run_job routes that.

    Sending the DM inline used to double-fire once the pipeline started routing outreach explicitly.

    A board listing with no Easy Apply widget is not a skip: the off-site url is read off the page and
    handed to the external-ATS path, which replays a saved session for that host or drops a human into
    a visible browser once to create one. The board page is closed before either runs.

    That url is taken from where the click landed before it is read off the page. An off-site listing's
    Apply control IS the link, so the click moves this tab to the employer's ATS and a page-read can
    then only find the ATS linking to itself - which is how an off-site job ended up ground against the
    board's selectors and recorded as "no apply control", with no session saved and nobody prompted.
    """
    platform = (job.get("platform") or "").lower()
    if platform not in SELECTORS:
        return False, f"unsupported platform {platform!r}"
    if not job.get("job_url"):
        return False, "no job_url"
    if config.DRY_RUN:
        return True, f"{platform}: dry-run, no submit"

    page, external, msg = None, "", "apply flow did not run"
    board = urlsplit(job["job_url"]).hostname or ""
    try:
        page = await (await _context(platform)).new_page()
        page.set_default_timeout(_STEP_TIMEOUT)
        await page.goto(job["job_url"], wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
        await _human()
        ok, msg = await _apply_flow(page, SELECTORS[platform], pdf_path)
        if ok:
            return True, f"{platform}: {msg}"
        external = await _landed_or_linked(page, board)
    except Exception as exc:
        # Three of the four rows of one live batch stopped here, with the raw
        # `BrowserType.launch_persistent_context: Connection closed while reading from the driver` in the
        # notes column - a message that reads like a page problem and is not one. _context already retried
        # against a fresh driver by this point, so reaching here means the transport died twice: evict the
        # entry and say what it was, rather than leak a stack string into the operator's queue.
        if not _DRIVER_LOST.search(f"{type(exc).__name__}: {exc}"):
            return False, f"{platform}: {type(exc).__name__}: {exc}"
        _CONTEXTS.pop(platform, None)
        return False, f"{platform}: {_DEAD_BROWSER}"
    finally:
        await _close_quietly(page)

    if not external:
        return False, f"{platform}: {msg}"
    print(f"  external listing: {external}")
    return await _external_apply(external, pdf_path)


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/128.0.0.0 Safari/537.36")
_TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)


def _get(url: str, params: dict | None = None, timeout: float = 12.0) -> str:
    """Blocking GET returning body text, or "" on any failure. Callers treat "" as no data."""
    import httpx

    try:
        r = httpx.get(url, params=params, timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": _UA, "Accept-Language": "en-IN,en;q=0.9"})
        return r.text if r.status_code == 200 else ""
    except Exception:
        return ""


def _text(fragment: str) -> str:
    """HTML fragment to readable text. Good enough to feed a JD to the regexes and the AI engine."""
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ",
                  html.unescape(_TAGS.sub(" ", fragment)).replace("\r", ""))).strip()


_HR_ROLE = re.compile(r"\b(?:hr|human\s+resources?|talent|talent\s+acquisition|recruit\w*|"
                      r"people\s+ops\w*|people\s+team|hiring|staffing|personnel|campus)\b", re.I)
_LEGAL_NOISE = frozenset((
    "pvt", "private", "ltd", "limited", "inc", "incorporated", "llc", "llp", "plc", "gmbh",
    "corp", "corporation", "company", "co", "group", "holdings", "ventures", "labs",
    "technologies", "technology", "tech", "solutions", "services", "systems", "software",
    "consulting", "consultancy", "global", "international", "worldwide", "india", "the", "and", "of",
))
_SUFFIX_LABELS = frozenset((
    "com", "co", "net", "org", "io", "ai", "in", "us", "uk", "eu", "asia", "de", "fr", "jp", "sg",
    "au", "ca", "me", "info", "biz", "xyz", "online", "site", "app", "dev", "cloud", "gov", "edu",
    "ac", "email", "www", "mail", "careers", "jobs", "corp", "people",
))
_FREEMAIL = frozenset(("gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "outlook.com",
                       "hotmail.com", "live.com", "aol.com", "proton.me", "protonmail.com",
                       "icloud.com", "rediffmail.com", "yandex.com", "mail.com", "zoho.com"))


def _is_hr(entry: dict) -> bool:
    """Does this contact actually work in hiring? Blank position counts as no."""
    return bool(_HR_ROLE.search(" ".join(str(entry.get(k) or "")
                                         for k in ("position", "department", "seniority"))))


def _company_tokens(company: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", str(company or "").lower()) if t]


def _corroborated(domain: str, company: str, job_url: str) -> bool:
    """Does this email domain plausibly belong to the company on the job row?

    Hunter resolves a company NAME to a domain on its own side, and that step can be wrong: a
    made-up "AcmeCorp" came back as a live stranger's mailbox. Emailing a resume cannot be undone,
    so an uncorroborated domain is dropped rather than tried.
    """
    domain = domain.strip().lower().lstrip("@")
    if not domain or "." not in domain or domain in _FREEMAIL:
        return False
    host = urlsplit(str(job_url or "")).netloc.lower().removeprefix("www.")
    if host and (host == domain or host.endswith(f".{domain}")):
        return True                                   # posting lives on the company's own site
    tokens = _company_tokens(company)
    if not tokens:
        return False
    labels = [p for p in domain.split(".") if p not in _SUFFIX_LABELS]
    squashed = "".join(labels)
    if not squashed:
        return False
    core = [t for t in tokens if t not in _LEGAL_NOISE] or tokens
    joined = "".join(core)
    initials = "".join(t[0] for t in tokens)          # "Tata Consultancy Services" -> tcs.com
    return (squashed in joined or joined in squashed or squashed == initials
            or any(len(t) >= 4 and t in squashed for t in core))


def _pick_hr(emails: list[dict], company: str = "", job_url: str = "") -> str | None:
    """An HR-titled contact at a domain that corroborates this company, or None.

    Both halves must hold. Hunter's name-to-domain step can land on an unrelated company, and its
    highest-confidence hit is often a generic marketing or sales mailbox — a real observed pair was
    marketing@dtcc.asia for a company whose domain is dtcc.com. Either fault sends a resume to a
    stranger, so a failed check skips the cold email instead of guessing.

    ponytail: name corroboration cannot separate acmecorp.eu from acmecorp.com — it rejects an
    unrelated domain, not a same-name impostor. Add Hunter's paid domain verification if that lands.
    """
    hr = [e for e in emails
          if e.get("value") and _is_hr(e)
          and _corroborated(str(e["value"]).rsplit("@", 1)[-1], company, job_url)]
    if not hr:
        return None
    hr.sort(key=lambda e: -(e.get("confidence") or 0))
    return hr[0]["value"]


def _hunter_lookup(company: str, job_url: str = "") -> str | None:
    """Hunter by company name. No domain guessing — a wrong guess cold-emails a stranger."""
    if not (config.HUNTER_API_KEY and company):
        return None
    from pyhunter import PyHunter

    hunter = PyHunter(config.HUNTER_API_KEY)
    try:
        if (hunter.account_information() or {}).get("calls", {}).get("left") == 0:
            return None
    except Exception:
        return None

    people: list[dict] = []
    for department in ("hr", None):
        try:
            data = hunter.domain_search(company=company, limit=10, department=department) or {}
        except Exception:
            continue
        people += data.get("emails") or []
        if hit := _pick_hr(people, company, job_url):
            return hit
    return None


_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
# Assets, not mailboxes: "logo@2x.png" and "sprite@3x.svg" are shaped exactly like an address.
_NOT_A_TLD = frozenset(("png", "jpg", "jpeg", "gif", "webp", "svg", "css", "js", "json", "html",
                        "htm", "php", "asp", "aspx", "woff", "woff2", "ttf", "ico", "mp4", "pdf"))
# Machine and wrong-department mailboxes. A resume sent here is either bounced or read by sales.
_NOT_A_PERSON = re.compile(
    r"^(?:no[-._]?reply|do[-._]?not[-._]?reply|reply|postmaster|mailer[-._]?daemon|bounce\w*|abuse|"
    r"webmaster|hostmaster|admin|administrator|root|sysadmin|unsubscribe|newsletter|subscribe|"
    r"privacy|legal|dpo|grievance|compliance|security|soc|sales|presales|marketing|media|press|"
    r"partner\w*|billing|invoice\w*|accounts?|finance|payments?|order\w*|test|example|user|email|"
    # Substring branches, not exact: the accommodations desk is printed in the legal boilerplate of
    # almost every US posting, on the company's OWN domain - so _jd_email's "own domain means their
    # recruiter" rule accepts it, and a cold job pitch lands with the people who process disability
    # accommodation requests. Measured: samsara.com published `accessibleinterviewing@`, which matched
    # no exact name here and was mailed. eeo/veteran desks sit in the same paragraph and are the same
    # mistake. A real recruiter localpart never contains these stems.
    r"[a-z0-9._-]*(?:accommodat|accessib|eeo|equal[-._]?opportunit|veteran)[a-z0-9._-]*)$",
    re.I)
# HR-ish localpart. Boundaries are explicit rather than \b because \b sees the "hr" inside "chris"
# and "shreya" — every third Indian recruiter's first name would otherwise read as a role mailbox.
_HR_LOCAL = re.compile(
    r"(?:^|[^a-z])(?:hr|hrd|human[._\-]?resources?|talent\w*|recruit\w*|career\w*|hiring|jobs?|"
    r"apply|application\w*|resume\w*|cv|placement\w*|campus|staffing|people[._\-]?ops?|"
    r"internship\w*|joinus|workwithus)(?:$|[^a-z])", re.I)


def _emails_in_text(text: str) -> list[str]:
    """Every plausible address in a blob, lowercased, order preserved, deduped."""
    out: dict[str, None] = {}
    for raw in _EMAIL.findall(text or ""):
        addr = raw.strip(".-_").lower()
        local, _, domain = addr.rpartition("@")
        if not local or domain.rsplit(".", 1)[-1] in _NOT_A_TLD or _NOT_A_PERSON.match(local):
            continue
        out.setdefault(addr, None)
    return list(out)


def _jd_email(description: str, company: str, job_url: str = "") -> str | None:
    """TIER 1 — an address the posting itself published. Free, and the least guessy source there is.

    Two shapes are accepted. A corroborated company domain is trusted whatever the localpart, because
    a JD that prints an address on its own domain is naming its own recruiter. An uncorroborated
    domain is only accepted when it is freemail AND the localpart is a hiring mailbox: small Indian
    firms genuinely publish `hr.acmelabs@gmail.com`, but a random stranger's gmail in a JD body is
    not a contact. Anything else — an unrelated corporate domain, a vendor, a candidate's own address
    pasted into the text — is dropped rather than mailed.
    """
    found = _emails_in_text(description or "")
    if not found:
        return None
    ranked: list[tuple[int, str]] = []
    for addr in found:
        local, _, domain = addr.rpartition("@")
        hr = bool(_HR_LOCAL.search(local))
        if _corroborated(domain, company, job_url):
            ranked.append((0 if hr else 1, addr))
        elif hr and domain in _FREEMAIL:
            ranked.append((2, addr))
    ranked.sort(key=lambda p: p[0])
    return ranked[0][1] if ranked else None


_SERP_ENGINES = (("google", "https://www.google.com/search?num=30&hl=en&q="),
                 ("duckduckgo", "https://duckduckgo.com/?q="),
                 ("bing", "https://www.bing.com/search?count=30&q="))
_CONSENT = ["#L2AGLb", "button:has-text('Accept all')", "button:has-text('I agree')",
            "#onetrust-accept-btn-handler"]
# Result hosts that can never be the company: search engines themselves, aggregators, socials.
_NOT_THE_COMPANY = ("google.", "bing.", "duckduckgo.", "duck.com", "yahoo.", "microsoft.",
                    "youtube.", "linkedin.", "facebook.", "instagram.", "twitter.", "x.com",
                    "reddit.", "quora.", "wikipedia.", "glassdoor.", "ambitionbox.", "indeed.",
                    "naukri.", "internshala.", "shine.", "monster.", "timesjobs.", "foundit.")


def _guarded_hr(text: str, company: str, job_url: str) -> str | None:
    """First address in a blob that is BOTH on a corroborated domain AND an HR-ish mailbox.

    Same guard every other tier ends at. It is what keeps a scraped page's `business@`, `info@` or
    `whistleblower@` — all of which real careers pages do publish — out of the send path.
    """
    for addr in _emails_in_text(text):
        local, _, domain = addr.rpartition("@")
        if _corroborated(domain, company, job_url) and _HR_LOCAL.search(local):
            return addr
    return None


async def _serp(page, query: str) -> tuple[str, list[str]]:
    """Run one dork through the first engine that actually renders results. Returns (innerText, hrefs).

    Engines are tried in order and Google is first because it is the one the brief names. A bot wall
    is detected rather than parsed: Google redirects to /sorry/index, and a blocked endpoint answers
    with a ~200-character stub where a real SERP is several thousand characters.
    """
    for _engine, base in _SERP_ENGINES:
        try:
            await page.goto(base + quote_plus(query), wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
            await _click_first(page, _CONSENT, timeout=2_000)
            await _human(2.0, 4.0)
            if "/sorry/" in page.url or "captcha" in page.url.lower():
                continue
            text = await page.evaluate("() => document.body.innerText")
            if len(text) < 500:
                continue
            hrefs = await page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href^=\"http\"]')).map(a => a.href)")
            return text, hrefs
        except Exception:
            continue
    return "", []


async def _dork_hr(company: str, job_url: str = "", page_budget: int = 2) -> str | None:
    """TIER 3 — Google dork executed by a headless Playwright browser, not an HTTP client.

    Two dorks are run; result text is regexed first, then the top corroborated result pages are
    opened in the same browser and regexed, because an address almost never appears in a snippet.

    ponytail: measured on real Pune companies (Persistent, Cybage, Zensar) from this machine —
    Google answers /sorry/index "unusual traffic" on every attempt, headed or headless, so the chain
    falls through to DuckDuckGo (which renders ~6k chars and 40+ result links) and then Bing. The
    corroborated careers pages it finds publish info@, business@ and whistleblower@ addresses, none of
    which clear the HR-mailbox guard, so the measured yield of this tier is currently zero HR
    addresses. It is kept because it is cheap and occasionally a small firm does print hr@ on its
    contact page — but Tier 1 and Hunter are what actually carry outreach. A paid SERP API, or Google
    with a residential proxy pool and solved consent, is the upgrade path.
    """
    if not company:
        return None
    page = None
    try:
        page = await (await _context("osint", headless=True)).new_page()
        page.set_default_timeout(_STEP_TIMEOUT)
        candidates: dict[str, None] = {}
        for query in (f'"{company}" ("HR" OR "Recruiter" OR "Talent") "@"',
                      f'"{company}" ("hr@" OR "careers@" OR "talent@")'):
            text, hrefs = await _serp(page, query)
            if hit := _guarded_hr(text, company, job_url):
                return hit
            for href in hrefs:
                host = urlsplit(href).netloc.lower().removeprefix("www.")
                if (host and not any(s in host for s in _NOT_THE_COMPANY)
                        and _corroborated(host, company, job_url)):
                    candidates.setdefault(href, None)
        for href in list(candidates)[:page_budget]:
            try:
                await page.goto(href, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
                await _human(1.5, 3.0)
                body = await page.evaluate("() => document.body.innerText")
                if hit := _guarded_hr(body, company, job_url):
                    return hit
            except Exception:
                continue
        return None
    except Exception:
        return None
    finally:
        await _close_quietly(page)
        # The osint profile holds no login worth keeping, so the whole context is torn down here, not
        # just the page: a resident headless Chromium for the app's lifetime is real memory for a tier
        # that runs at most once per job. The 4 board profiles are deliberately NOT closed this way -
        # they carry the authenticated sessions and close once, at shutdown().
        if (ctx := _CONTEXTS.get("osint")) is not None:
            await _close_quietly(ctx)
            _CONTEXTS.pop("osint", None)   # the close handler usually evicts; a close that hung cannot


# Legal FORM words only — never industry words. "Acme Labs" and "Acme Technologies" are two different
# employers and must never share a vault row, so `labs`/`technologies`/`solutions` stay in the key even
# though _LEGAL_NOISE (used for domain matching, where a wrong answer is merely a miss) strips them.
_LEGAL_FORMS = frozenset(("pvt", "private", "ltd", "limited", "inc", "incorporated", "llc", "llp",
                          "plc", "gmbh", "corp", "corporation", "company", "co", "and", "of", "the"))


def _company_key(company: str) -> str:
    """Stable email_vault key for an employer: the company name minus its legal form, squashed.

    NOT the mail domain, even though the column is called `domain`. The domain is unknown until an
    address is actually found, and a cache that can only be read *after* the expensive lookup saves
    nothing - the key has to be derivable from the job row alone.

    Only legal-form words are stripped, so "Acme Labs Pvt Ltd" and "  ACME  LABS " collapse to
    `acmelabs` while "Acme Technologies" keeps its own row. Erring towards too many keys costs a
    Hunter credit; erring the other way would mail one company's recruiter about another company's
    job. `_resolve_hr` re-checks every cache hit through `_corroborated` as the second net.
    """
    tokens = _company_tokens(company)
    core = [t for t in tokens if t not in _LEGAL_FORMS] or tokens
    return "".join(core)


def _trusted(addr: str, company: str, job_url: str = "") -> bool:
    """Is this address still an acceptable contact for THIS company?

    The two shapes every tier already accepts: an address on a corroborated company domain, or an
    HR-mailbox on freemail (small firms genuinely publish `hr.acmelabs@gmail.com`). Applied to vault
    reads as the second net behind `_company_key` - two employers whose names collapse to the same key
    would otherwise mean one company's recruiter receiving another company's application.
    """
    local, _, domain = str(addr or "").strip().lower().rpartition("@")
    if not (local and domain):
        return False
    # The wrong-desk check belongs HERE, not only in _emails_in_text: that runs on JD prose, so every
    # address arriving from hunter, the dorks, the OSINT indexes or an already-cached vault row skipped
    # it entirely. This is the one gate all six tiers pass before a mail goes out, and an accommodations
    # or legal desk is never the right recipient no matter which tier found it.
    if _NOT_A_PERSON.match(local):
        return False
    return _corroborated(domain, company, job_url) or (
        domain in _FREEMAIL and bool(_HR_LOCAL.search(local)))


# A hostname or bare domain printed in prose. The last label must be a real-looking suffix, or
# "Node.js" and "requirements.txt" read as company domains.
_HOST_IN_TEXT = re.compile(r"\b(?:https?://)?((?:[A-Za-z0-9][A-Za-z0-9\-]*\.)+[A-Za-z]{2,})\b")


def _company_domain(company: str, job_url: str = "", description: str = "") -> str:
    """The company's own mail domain, or "" - derived from the row, never guessed from the name.

    Guessing a domain from a company name is the one thing this file must never do: `AcmeCorp` ->
    `acmecorp.com` lands on a live stranger who then receives a resume. So the only sources accepted
    are hosts the posting itself printed - its own URL, or a URL in the JD body - and each is still
    put through `_corroborated`. Used as the OSINT search selector; a search needs a domain, and this
    is the honest way to have one.
    """
    host = urlsplit(str(job_url or "")).netloc.lower().removeprefix("www.")
    # A board host trivially "corroborates" itself (host == domain), so linkedin.com would come back as
    # Acme's mail domain and get searched. _NOT_THE_COMPANY is the same exclusion the dork tier uses.
    def ok(cand: str) -> bool:
        return bool(cand) and not any(s in cand for s in _NOT_THE_COMPANY) \
            and _corroborated(cand, company, job_url)

    if ok(host):
        return host
    for raw in _HOST_IN_TEXT.findall(description or ""):
        cand = raw.lower().removeprefix("www.").strip(".")
        if cand.rsplit(".", 1)[-1] in _SUFFIX_LABELS and ok(cand):
            return cand
    return ""


# TIER 4 platforms, in the order the brief names them. Each needs its own login, so each gets its own
# storage_state file under browser_sessions/ - the same shape the external-ATS interceptor uses.
_OSINT_SITES = (("intelx", "https://intelx.io/"),
                ("maxintel", "https://maxintel.org/"),
                ("osintframework", "https://osintframework.com/"))
# Per-site DOM contract. Unverified from this machine - intelx.io refuses automated fetches and
# maxintel.org answers 403 - so every field is a LADDER, not one selector, and _osint_report prints what
# the page actually exposes the moment a rung misses. That printout is what turns these into real
# selectors after the first live run; guessing harder from here would not.
#   url      direct search url, tried first: a query in the address bar needs no search box at all
#   search   the site's own input, used only when the direct url did not already render results
#   spinner  waited for DETACH before reading, so the regex never runs on a half-hydrated table
#   filters  optional narrowing tab (IntelX buckets results by type - Emails is one)
#   results  the container the text is read from; document.body is NOT in this list on purpose
_OSINT_DOM = {
    "intelx": {
        "url": "https://intelx.io/?s={q}",
        "search": ("input#search-input", "input[placeholder*='Search' i]", "input[name='s']",
                   "input[type='search']"),
        "spinner": (".spinner", "[class*='loading']", "[aria-busy='true']"),
        "filters": ("[role='tab']:has-text('Email')", "button:has-text('Emails')",
                    "a[href*='email']:has-text('Email')"),
        "results": ("#tableSearchResult", "table.searchResult", "[class*='searchresult']",
                    "[class*='result'] table", "main table", "table"),
    },
    "maxintel": {
        "url": "",
        "search": ("input#search", "input[name='search']", "input[name='q']", "input[name='domain']",
                   "input[placeholder*='domain' i]", "input[placeholder*='search' i]",
                   "input[type='search']"),
        "spinner": (".spinner", ".loader", "[class*='loading']", "[aria-busy='true']"),
        "filters": ("[role='tab']:has-text('Email')", "button:has-text('Emails')"),
        "results": ("#results", "[class*='result']", "table", "main"),
    },
}
# osintframework.com is a d3 node tree of LINKS TO TOOLS - it indexes tooling, never company data - so
# no query against it can return a mailbox and there is nothing for a regex to find. Skipped by name,
# which also means no human is asked to log into it for a lookup that cannot succeed.
# ponytail: option B from the brief. Option A (walk the tree to the Email node) ends at another site's
# front door, so it would buy a second navigation problem and still zero addresses.
_OSINT_SKIP = {"osintframework":
               "OSINTFramework requires manual node navigation - skipping automated regex scan."}
# One manual-login prompt per platform per process. Without this a 30-job batch stops dead at every
# job to ask a human to log into three sites again - including after they declined the first time.
_OSINT_TRIED: set[str] = set()


def _osint_state_path(name: str) -> Path:
    return config.BROWSER_SESSIONS / f"{name}.json"


async def _osint_login(name: str, url: str) -> bool:
    """Human-in-the-loop bootstrap: visible browser, manual login, then save the session as JSON.

    Same contract as the external-ATS interceptor and gated the same way - a console or the dashboard's
    buttons, never neither, with _INTERCEPT_LOCK serialising the two prompts - because an unbounded wait
    that nobody can answer would hang the request forever.

    ponytail: duplicates ~25 lines of _ats_intercept rather than factoring both onto one helper. The
    ATS version is pinned by 80-odd audit gates on its exact strings and return semantics; the shared
    helper is the upgrade path once the two stop drifting.
    """
    state = _osint_state_path(name)
    if config.DRY_RUN:
        print(f"  osint {name}: dry-run, would open a visible browser for manual login")
        return False
    if not (config.HITL and (sys.stdin and sys.stdin.isatty() or ON_HUMAN)):
        print(f"  osint {name}: needs a one-time manual login and no terminal is attached - run one "
              f"job from a console to create browser_sessions/{state.name}")
        return False

    async with _INTERCEPT_LOCK:
        browser = ctx = page = None
        try:
            browser = await (await _driver()).chromium.launch(
                headless=False, args=["--disable-blink-features=AutomationControlled"])
            ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, user_agent=_UA)
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
            _warn(
                "",
                "=" * 78,
                f"⚠️ OSINT PLATFORM LOGIN NEEDED ({name}): opened in a visible browser.",
                "⚠️ Please log in / solve any captcha manually.",
                "⚠️ Press ENTER in this terminal when done to save the session and continue...",
                "=" * 78,
                f"   url:     {url}",
                f"   session: {state}",
            )
            if not await _wait_enter(notice={
                    "phase": "outreach", "state": "human", "domain": name,
                    "msg": f"login needed · {name} · sign in (and clear any captcha) in the open "
                           f"browser, then press ✓"}):
                return False
            state.parent.mkdir(parents=True, exist_ok=True)
            await ctx.storage_state(path=str(state))
            print(f"  osint session saved: {state}")
            return True
        except Exception as exc:
            print(f"  osint {name} login: {type(exc).__name__}: {exc}")
            return False
        finally:
            await _close_quietly(page, ctx, browser)


async def _first_visible(page, selectors: tuple[str, ...], timeout: int = _ACT_TIMEOUT):
    """The first selector in the ladder that is actually on the page, or None. Never raises."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


async def _osint_report(page, name: str, missing: str) -> None:
    """Print what the page really exposes when a selector ladder misses.

    The whole reason the ladders above are guesses is that neither site can be reached from here. This
    turns one live run into the evidence that replaces them: input ids/names/placeholders and the tab
    labels the page carries, so the next patch is copied rather than guessed.
    """
    try:
        seen = await page.evaluate("""() => ({
            inputs: [...document.querySelectorAll('input,textarea')]
                .filter(el => el.type !== 'hidden')
                .slice(0, 12)
                .map(el => `${el.tagName.toLowerCase()}#${el.id}[name=${el.name}][ph=${
                    el.placeholder}]`),
            tabs: [...document.querySelectorAll("[role='tab'],nav a,button")]
                .map(el => (el.innerText || '').trim()).filter(t => t && t.length < 24).slice(0, 12),
        })""")
    except Exception as exc:
        print(f"  osint {name}: no {missing}, and the DOM would not answer ({type(exc).__name__})")
        return
    print(f"  osint {name}: no {missing} on {page.url}")
    print(f"    inputs: {seen.get('inputs') or 'none'}")
    print(f"    tabs:   {seen.get('tabs') or 'none'}")


async def _osint_probe(page, name: str, url: str, query: str,
                       company: str, job_url: str) -> str | None:
    """Search ONE platform with that platform's own DOM and return an HR mailbox, or None.

    Site-specific, in four steps: land on the site's own search url when it has one, use its own input
    only if that did not already render results, narrow to its Emails bucket when it has one, then read
    the text of the RESULTS CONTAINER - never document.body, which on a logged-in page also carries the
    nav, the account email and any sample addresses the marketing copy prints.
    """
    dom = _OSINT_DOM[name]
    direct = (dom["url"] or "").replace("{q}", quote_plus(query))
    await page.goto(direct or url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)

    # A direct search url renders results by itself. Only fall back to typing when it did not, so a
    # working url is never undone by a click into a search box that reloads the page empty.
    if not await _first_visible(page, dom["results"], timeout=_ACT_TIMEOUT):
        box = await _first_visible(page, dom["search"], timeout=_STEP_TIMEOUT)
        if box is None:
            await _osint_report(page, name, "search input")
            return None
        try:
            await box.click()
            await box.fill(query)
        except Exception:
            await page.keyboard.type(query, delay=15)      # some builds refuse fill() on the input
        await page.keyboard.press("Enter")

    # Spinners first: a results container can exist while still empty, and the regex would read nothing.
    for sel in dom["spinner"]:
        try:
            await page.locator(sel).first.wait_for(state="detached", timeout=_IDLE_TIMEOUT)
        except Exception:
            pass                        # absent or never rendered - both fine, this is best-effort
    try:
        await page.wait_for_load_state("networkidle", timeout=_IDLE_TIMEOUT)
    except Exception:
        pass                            # results hydrate over XHR; networkidle may never fire

    if tab := await _first_visible(page, dom["filters"], timeout=_ACT_TIMEOUT):
        try:                            # narrowing is a bonus, never a requirement
            await tab.click()
            await page.wait_for_load_state("networkidle", timeout=_IDLE_TIMEOUT)
        except Exception:
            pass
    await _human(1.5, 3.0)

    results = await _first_visible(page, dom["results"], timeout=_STEP_TIMEOUT)
    if results is None:
        await _osint_report(page, name, "results container")
        return None
    text = await results.inner_text()
    # Same guard as every other tier: corroborated domain AND an HR-ish mailbox. A breach-dump index
    # returns hundreds of unrelated addresses, so this is what keeps a stranger out of the send path.
    return _guarded_hr(text, company, job_url)


async def _osint_email_search(company_domain: str, company: str = "",
                              job_url: str = "") -> str | None:
    """TIER 4 — walk the OSINT platforms in order and return the first HR mailbox any of them yields.

    Each platform is independent: a missing session, a dead search box, a captcha wall or a throw all
    mean "next platform", never an abort. The saved session is replayed headlessly, so the manual
    login is paid once per platform for the life of the session file.

    Navigation is per site (_OSINT_DOM) and the text is read from that site's results container, not
    from document.body. Platforms in _OSINT_SKIP are declined before the login prompt: no human should
    be asked to authenticate somewhere a lookup provably cannot succeed.
    """
    query = (company_domain or "").strip().lower().lstrip("@") or (company or "").strip()
    if not query:
        return None
    for name, url in _OSINT_SITES:
        if reason := _OSINT_SKIP.get(name):
            print(f"  osint {name}: {reason}")
            continue
        state = _osint_state_path(name)
        if not state.exists():
            if name in _OSINT_TRIED:
                continue
            _OSINT_TRIED.add(name)                 # marked before the prompt: declining counts too
            if not await _osint_login(name, url):
                continue
        browser = ctx = page = None
        try:
            browser = await (await _driver()).chromium.launch(
                headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = await browser.new_context(storage_state=str(state), user_agent=_UA,
                                            viewport={"width": 1440, "height": 900})
            page = await ctx.new_page()
            page.set_default_timeout(_STEP_TIMEOUT)
            if hit := await _osint_probe(page, name, url, query, company, job_url):
                print(f"  osint {name}: hit on {query}")
                return hit
            print(f"  osint {name}: no HR mailbox for {query}")
        except Exception as exc:
            print(f"  osint {name}: {type(exc).__name__}: {exc}")
        finally:
            await _close_quietly(page, ctx, browser)
    return None


async def _resolve_hr(job: dict) -> tuple[str | None, str]:
    """HR resolution waterfall. Returns (email, source); (None, 'none') means skip the cold email.

    Ordered by cost and trust, cheapest first:
      db      a mailbox already on the job row
      vault   the permanent email_vault cache - a company resolved once is never paid for twice
      jd      free regex over the posting text
      hunter  a paid Hunter credit, in a worker thread
      dork    a headless SERP pass (kept: free, already built, measured yield ~0)
      osint   intelx / maxintel / osintframework, one manual login per platform per session file

    Every tier ends at the same guard - `_corroborated` plus an HR-ish localpart - so no tier can
    widen who gets mailed, and every hit is written to the vault so the next job at that employer is
    free. A miss is deliberately NOT cached: a company with no mailbox today may publish one next week.

    hunter/dork/osint each announce themselves and report their elapsed time as they run. The operator
    has just pressed ENTER and is watching for the DM to go out; this waterfall is the gap they see, and
    it is slow because it is doing real network work, not because it is stuck.
    """
    company, job_url = job.get("company") or "", job.get("job_url") or ""
    description = job.get("description") or ""
    key = _company_key(company)
    if stored := (job.get("hr_email") or "").strip():
        await database.save_vault_email(key, stored, "db")     # backfill from rows resolved earlier
        return stored, "db"
    if cached := await database.get_vault_email(key):
        # Second net behind the key: two employers whose names collapse to one key must not share a
        # mailbox. A stale or mismatched hit is ignored, and the tiers below run as if uncached.
        if _trusted(cached, company, job_url):
            return cached, "vault"
        print(f"  vault: cached mailbox does not corroborate {company!r} - ignored")

    hit, source = None, "none"
    if found := _jd_email(description, company, job_url):
        hit, source = found, "jd"
    else:
        # The three tiers below are the whole reason a hit job sits silent after the operator presses
        # ENTER: hunter is a paid round trip, dork spins a headless browser, osint can stop dead for a
        # manual login. None of them can be made fast, so each one announces itself and reports how long
        # it took - the run reads as slow work instead of as a hang, which is what it was reported as.
        domain = _company_domain(company, job_url, description)
        started = time.monotonic()
        for label, probe in (("hunter", lambda: asyncio.to_thread(_hunter_lookup, company, job_url)),
                             ("dork", lambda: _dork_hr(company, job_url)),
                             ("osint", lambda: _osint_email_search(domain, company, job_url))):
            print(f"  hr: {label} lookup ...", end="", flush=True)
            found = await probe()
            print(f" {'hit' if found else 'miss'} · {time.monotonic() - started:.1f}s in")
            if found:
                hit, source = found, label
                break
    if hit:
        await database.save_vault_email(key, hit, source)
        return hit, source
    return None, "none"



def _smtp() -> smtplib.SMTP:
    """Gmail over stdlib smtplib: smtp.gmail.com:587, EHLO -> STARTTLS -> EHLO, then AUTH.

    config.SMTP_PASSWORD must be a Google App Password (myaccount.google.com/apppasswords); a raw
    account password is refused by Gmail. Port 465 stays supported for an implicit-TLS relay.
    """
    ctx = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        return smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ctx, timeout=30)
    server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
    server.ehlo()
    server.starttls(context=ctx)
    server.ehlo()
    return server


def _send_email(to_addr: str, subject: str, paragraphs, pdf_path, in_reply_to: str | None = None) -> str:
    """Send one mail; returns the Message-ID stamped on it so a later IMAP poll can thread replies.

    A controlled Message-ID (make_msgid) is the reliable reply anchor: a recruiter's client echoes it
    in In-Reply-To/References, which poll_replies matches exactly. in_reply_to sets those headers on a
    follow-up so the nudge lands in the original thread rather than as a fresh cold mail.
    """
    body = "\n\n".join(paragraphs) if isinstance(paragraphs, (list, tuple)) else str(paragraphs)
    msgid = make_msgid()
    msg = MIMEMultipart()
    msg["From"] = f"Prince Jha <{config.SMTP_EMAIL}>"
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Message-ID"] = msgid
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.attach(MIMEText(f"{body}\n\nRegards,\nPrince Jha\n{config.SMTP_EMAIL}", "plain", "utf-8"))
    if pdf_path:
        pdf = Path(pdf_path)
        part = MIMEApplication(pdf.read_bytes(), _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=pdf.name)
        msg.attach(part)
    with _smtp() as server:
        server.login(config.SMTP_EMAIL, config.SMTP_PASSWORD)
        server.send_message(msg)
    return msgid


async def find_and_email_hr(job: dict, pdf_path, pitch_body, email_subject: str
                            ) -> tuple[bool, str | None, str, str | None]:
    """Returns (sent, hr_email, message, message_id). hr_email comes back even when sending fails, so
    it persists; message_id is the reply anchor, None whenever nothing actually went out."""
    hr_email, source = await _resolve_hr(job)
    if not hr_email:
        return False, None, "no HR email (vault/jd/hunter/dork/osint all missed) - email skipped", None
    if not (config.SMTP_EMAIL and config.SMTP_PASSWORD):
        return False, hr_email, f"found {hr_email} ({source}), SMTP_EMAIL/SMTP_PASSWORD unset - email skipped", None
    if config.DRY_RUN:
        return False, hr_email, f"dry-run: would email {hr_email} ({source})", None
    try:
        msgid = await asyncio.to_thread(_send_email, hr_email, email_subject, pitch_body, pdf_path)
        return True, hr_email, f"emailed {hr_email} ({source})", msgid
    except Exception as exc:
        return False, hr_email, f"email to {hr_email} failed: {type(exc).__name__}: {exc}", None


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_MONTH_RE = "|".join(_MONTHS)
_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
_DMY_DATE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})\b")
_DAY_MON = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_RE})[a-z]*\b", re.I)
_MON_DAY = re.compile(rf"\b({_MONTH_RE})[a-z]*\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.I)
_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b|\b([01]?\d|2[0-3]):([0-5]\d)\b", re.I)


def _extract_interview_dt(body: str) -> datetime | None:
    """Best-effort interview date+time out of a reply body. None when no date is stated.

    A recruiter's "let's meet on 12 Feb at 3pm" becomes a datetime; time defaults to 10:00 when the
    body names a day but no clock. Deliberately conservative: a body with no parseable date returns
    None rather than guessing, so no phantom invite is written."""
    text = str(body or "")
    year = datetime.now().year
    d = None
    if m := _ISO_DATE.search(text):
        y, mo, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        d = _safe_date(y, mo, day)
    elif m := _DMY_DATE.search(text):
        day, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        d = _safe_date(y, mo, day)
    elif m := _DAY_MON.search(text):
        d = _safe_date(year, _MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
    elif m := _MON_DAY.search(text):
        d = _safe_date(year, _MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
    if d is None:
        return None
    hour, minute = 10, 0
    if t := _TIME_RE.search(text):
        if t.group(1):
            hour, minute = int(t.group(1)) % 12, int(t.group(2) or 0)
            if t.group(3).lower() == "p":
                hour += 12
        else:
            hour, minute = int(t.group(4)), int(t.group(5))
    return datetime(d.year, d.month, d.day, hour, minute)


def _safe_date(y: int, mo: int, day: int):
    try:
        return datetime(y, mo, day)
    except ValueError:
        return None


def _write_ics(job: dict, when: datetime) -> Path:
    """Write a local .ics invite for a detected interview. No calendar API, no auth - a file the user
    double-clicks. Returns the path. One file per job id, overwritten if the date is re-detected."""
    config.INTERVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    end = when + timedelta(minutes=30)
    company = (job.get("company") or "the company").strip()
    title = (job.get("title") or "the role").strip()
    fmt = "%Y%m%dT%H%M%S"
    uid = f"job{job.get('id', 0)}-{when.strftime(fmt)}@job-apply"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Job-Apply//interview//EN", "BEGIN:VEVENT",
        f"UID:{uid}", f"DTSTAMP:{datetime.now().strftime(fmt)}", f"DTSTART:{when.strftime(fmt)}",
        f"DTEND:{end.strftime(fmt)}", f"SUMMARY:Interview - {company} ({title})",
        f"DESCRIPTION:Reply detected from {job.get('hr_email') or 'recruiter'}. Verify time with them.",
        "END:VEVENT", "END:VCALENDAR"]
    path = config.INTERVIEWS_DIR / f"interview_{job.get('id', 0)}.ics"
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return path


def _msg_body(msg) -> str:
    """Plain-text body of an email.message.Message, first text/plain part or the whole payload."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8",
                                                                 "replace")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        return str(msg.get_payload())


def _imap_scan(by_msgid: dict, by_email: dict) -> list[tuple[int, datetime | None]]:
    """Blocking IMAP sweep of the inbox. Returns (job_id, interview_dt_or_None) for each matched reply.

    A message matches a job by our controlled Message-ID echoed in In-Reply-To/References (the strong
    anchor), else by From == a known hr_email dated after that job's outreach (the lazy fallback).

    Headers first, bodies only for the messages that match, and the headers for the whole window in a
    single FETCH. Which four values decide a match is known up front, so the deciding pass needs nothing
    but headers - fetching (RFC822) per message downloads every body and attachment in the window to
    read them, which measured at over 300 seconds on a real mailbox and held the app at "Waiting for
    application startup" while it ran. One header fetch per message is better but still one round trip
    each, and 300 of those is the same class of stall at a smaller size; IMAP takes a sequence set, so
    all 300 headers come back in one command and only a message that already matched pays for its body.
    """
    out: list[tuple[int, datetime | None]] = []
    seen: set[int] = set()
    with imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=config.IMAP_TIMEOUT) as M:
        M.login(config.SMTP_EMAIL, config.SMTP_PASSWORD)
        M.select("INBOX")
        typ, data = M.search(None, "ALL")
        if typ != "OK":
            return out
        window = data[0].split()[-300:]                  # newest 300 is plenty for a request-driven poll
        if not window:
            return out
        typ, blob = M.fetch(b",".join(window), "(BODY.PEEK[HEADER.FIELDS"
                                             " (FROM IN-REPLY-TO REFERENCES DATE)])")
        if typ != "OK":
            return out
        for part in blob or []:
            if not isinstance(part, tuple) or len(part) < 2:
                continue                                 # the b')' closers between messages
            num = part[0].split(b" ", 1)[0]
            msg = email.message_from_bytes(part[1])
            refs = f"{msg.get('In-Reply-To', '')} {msg.get('References', '')}"
            frm = email.utils.parseaddr(msg.get("From", ""))[1].lower()
            job = next((r for mid, r in by_msgid.items() if mid and mid in refs), None)
            if job is None and frm in by_email:
                job = by_email[frm]
                if not _after_outreach(msg.get("Date"), job.get("outreach_at")):
                    job = None
            if not job or job["id"] in seen:
                continue
            seen.add(job["id"])
            typ, full = M.fetch(num, "(BODY.PEEK[])")
            body = _msg_body(email.message_from_bytes(full[0][1])) \
                if typ == "OK" and full and full[0] else ""
            out.append((job["id"], _extract_interview_dt(body)))
    return out


def _imap_sent(rows: list[dict]) -> list[tuple[int, str, str]]:
    """Blocking sweep of the SENT mailbox: which of these rows already have a mail out, to whom, and its
    Message-ID. Returns (job_id, recipient, message_id) per row the mailbox proves was mailed.

    This is the ground truth behind "nothing was sent" (outreach_at IS NULL). That claim goes wrong in
    two ways the DB cannot see: SMTP accepts the message and the process dies before mark_reached
    records it, or the row is later rewritten 'failed' because the APPLY half broke while the mail had
    already landed. Either way the recruiter has the mail, and without this sweep a retry mails them a
    second time - exactly the duplicate this pipeline exists to avoid. A mail in Sent is proof, so it
    stamps outreach_at and the row joins the follow-up clock like any other.

    The search key is the recipient when the row has one. When it does not, the row cannot be searched
    by address at all, and that is not hypothetical: measured live on job 23, SMTP accepted the mail,
    the process was killed before the row was written, and the address that was mailed existed ONLY in
    the Sent copy - so the sweep had no key, the row stayed 'queued', and the next batch mailed the same
    recruiter again. Those rows are searched by company inside the subject, inside the window that opens
    when the row was created, and the address and Message-ID are read back off the message itself.
    """
    found: list[tuple[int, str, str]] = []
    if not rows:
        return found
    with imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=config.IMAP_TIMEOUT) as M:
        M.login(config.SMTP_EMAIL, config.SMTP_PASSWORD)
        typ, _ = M.select(config.SENT_MAILBOX, readonly=True)
        if typ != "OK":
            print(f"sent sweep: mailbox {config.SENT_MAILBOX} not selectable - set SENT_MAILBOX")
            return found
        for job in rows:
            addr = (job.get("hr_email") or "").lower()
            field, value = ("TO", addr) if addr else ("SUBJECT", job.get("company") or "")
            if not value:
                continue
            typ, data = M.search(None, field, f'"{value}"')
            if typ != "OK" or not data or not data[0].split():
                continue
            num = data[0].split()[-1]                    # newest first-class match for this key
            typ, msg_data = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (TO MESSAGE-ID DATE)])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            if not addr and not _after_outreach(msg.get("Date"), job.get("created_at")):
                continue             # an older mail to the same firm, from a previous day's run
            found.append((job["id"],
                          addr or email.utils.parseaddr(msg.get("To", ""))[1].lower(),
                          (msg.get("Message-ID") or "").strip()))
    return found


def _after_outreach(msg_date: str | None, outreach_at: str | None) -> bool:
    """True unless the message provably predates our outreach - guards the From-only fallback against
    an unrelated earlier mail from the same recruiter.

    The two stamps are in different frames and comparing them raw is a trap: outreach_at comes from
    SQLite's datetime('now','localtime') and is a bare local wall clock, while a mail's Date header
    carries whatever offset the relay stamping it chose. Measured live: Gmail wrote job 23's mail as
    "10:47:16 -0700" for a send that happened at 23:17 local, so dropping the offset compared 10:47
    against a 22:41 row and threw the match away as an older mail - which is exactly how the one row
    this sweep exists to reconcile stayed unreconciled and stayed queued for a second mailing.
    astimezone() puts the mail in the same local frame as the DB stamps before the offset is dropped.
    """
    if not msg_date or not outreach_at:
        return True
    try:
        when = parsedate_to_datetime(msg_date)
        sent = datetime.fromisoformat(outreach_at)
        if when.tzinfo is not None:
            when = when.astimezone().replace(tzinfo=None)
        return when >= sent
    except Exception:
        return True


async def poll_replies() -> dict:
    """Scan the inbox for recruiter replies to outreach and stamp replied_at (write-once fact), then
    reconcile every "nothing was sent" claim against the Sent mailbox.

    Match order: our Message-ID in the reply's In-Reply-To/References, then From == a known hr_email
    dated after that row's outreach. A matched reply that names a date also drops a local .ics invite.
    The Sent sweep runs in the same pass because a row whose mail really went out - even one whose
    status says 'failed' because the apply half broke - has to be reconciled as reached BEFORE the
    follow-up sweep looks at it; otherwise it is nudged as if the first mail never happened.
    DRY_RUN and missing SMTP creds short-circuit before any IMAP connect (no live IMAP under --no-net)."""
    candidates = await database.outreached()
    unreached = await database.unreached()
    if not (candidates or unreached):
        return {"checked": 0, "replied": 0, "verified": 0, "invites": [],
                "note": "no outreach on file"}
    if config.DRY_RUN:
        return {"checked": len(candidates), "replied": 0, "verified": len(unreached), "invites": [],
                "note": "dry-run: IMAP skipped"}
    if not (config.SMTP_EMAIL and config.SMTP_PASSWORD):
        return {"checked": len(candidates), "replied": 0, "verified": len(unreached), "invites": [],
                "note": "SMTP_EMAIL/SMTP_PASSWORD unset - IMAP skipped"}
    by_msgid = {r["email_msgid"]: r for r in candidates if r.get("email_msgid")}
    by_email: dict[str, dict] = {}
    for r in candidates:
        if r.get("hr_email"):
            by_email.setdefault(r["hr_email"].lower(), r)
    matched = await asyncio.to_thread(_imap_scan, by_msgid, by_email) if candidates else []
    invites: list[str] = []
    for job_id, when in matched:
        await database.mark_replied(job_id)
        if when:
            job = await database.get(job_id)
            invites.append(str(_write_ics(job, when)))
    # A row the Sent folder proves was mailed must be marked reached, never re-mailed. mark_reached is
    # COALESCE write-once, so a row that already carries the fact is untouched. The address and the
    # Message-ID come back from the mailbox too, because a row can be missing both - a kill between the
    # SMTP call and the DB write leaves a row with no hr_email, no thread anchor and no way to be
    # reconciled by address on any later sweep.
    verified = await asyncio.to_thread(_imap_sent, list(unreached))
    for job_id, addr, msgid in verified:
        await database.mark_reached(job_id)
        await database.record_sent(job_id, addr, msgid)
        print(f"  sent sweep: job {job_id} already mailed to {addr or '?'} - reached, not re-sent")
    return {"checked": len(candidates), "replied": len(matched), "verified": len(verified),
            "invites": invites}



def _drop_resume(job: dict) -> bool:
    """Delete a row's rendered resume - and only ever a file directly inside RESUME_DIR.

    The path comes out of the database, so a wrong one deletes someone's file. Resolving and checking
    containment is the single guard that makes "delete the resume" safe to run unattended.
    """
    raw = job.get("pdf_path")
    if not raw:
        return False
    try:
        path, root = Path(raw).resolve(), config.RESUME_DIR.resolve()
    except Exception:
        return False
    if path.parent != root:
        print(f"  clean: refusing to delete {path} - outside {root}")
        return False
    try:
        path.unlink(missing_ok=True)
    except Exception as exc:
        print(f"  clean: {path.name} not deleted · {type(exc).__name__}: {exc}")
        return False
    print(f"  clean: dropped resume {path.name}")
    return True


async def clean_failed() -> dict:
    """Clear the rows a killed run left behind, in the shapes they actually come in.

    'failed' is not evidence that nothing happened, and treating it as if it were is the expensive
    mistake. Measured on this machine: job 27's Workday application had genuinely gone out (applied_at
    stamped, session saved) and job 23's recruiter was holding the mail (found in Sent) while both rows
    still read as unfinished.

      never applied, never reached  -> back to 'skipped' like any other filtered row, and its resume is
                                       deleted: it was rendered for an application that never happened
      applied_at set, no mail out   -> back to 'queued', and only outreach is owed. Leaving it 'failed'
                                       strands it, because run_batch picks up nothing but 'queued' - the
                                       row would sit there forever owing a mail nobody would ever send.
                                       The requeue is safe because pipeline skips the submit outright on
                                       applied_at, so the next batch delivers the one thing missing and
                                       no second application
      outreach_at set               -> left exactly as it is, and it is what poll_replies' Sent sweep
                                       produces. The mail is out, so only a reply is owed, never another
                                       send - and the row is already on the follow-up clock

    Runs after poll_replies, on purpose: the Sent sweep is what proves whether a row's outreach is
    spent, and a row is only litter once both halves are known to have gone nowhere.
    """
    if config.DRY_RUN:
        return {"requeued": 0, "replayed": 0, "resumes": 0, "kept": 0, "note": "dry-run: nothing changed"}
    requeued = kept = 0
    replayed: list[int] = []
    resumes: list[str] = []
    for job in await database.fetch("failed"):
        if job.get("outreach_at"):
            kept += 1
            print(f"  clean: job {job['id']} kept - its mail already went out, only a reply is owed")
            continue
        if job.get("applied_at"):
            await database.set_status(job["id"], "queued")
            replayed.append(job["id"])
            print(f"  clean: job {job['id']} requeued for outreach only - applied {job['applied_at']}, "
                  f"no mail on file")
            continue
        # The note is the whole difference between "this was judged not worth applying to" and "this never
        # got anywhere". A bare 'skipped' reads as the first, and the row is revivable by hand - nothing
        # was sent, so there is no spent outreach to protect and no reason it cannot be run again.
        await database.set_status(job["id"], "skipped",
                                  notes="failed with nothing sent · no application, no mail · revive to retry")
        requeued += 1
        if _drop_resume(job):
            await database.clear_asset(job["id"])
            resumes.append(Path(job["pdf_path"]).name)
    if requeued or replayed:
        print(f"  clean: {requeued} failed row(s) marked skipped, {len(resumes)} resume(s) deleted, "
              f"{len(replayed)} requeued for the outreach they still owe")
    return {"requeued": requeued, "replayed": len(replayed), "resumes": len(resumes), "kept": kept}


async def purge_old_jobs(db_path=None) -> dict:
    """Retire the rows a retention clock has run out on, and the mailboxes that die with them.

    Two clocks (see config): a posting nobody was ever told about is kept SCRAPE_KEEP_DAYS days and gone
    on the morning of the next one, while a row that reached a human is a live thread and keeps its
    record until OUTREACH_KEEP_DAYS from the send have passed without an answer. A row with a reply on
    it is never touched by either. The query, and the whole argument for those two windows, is
    database.aged_jobs; what is decided HERE is the mailbox cache, because the vault is keyed by
    EMPLOYER and not by row - a cached address is only retired once the last row at that company goes
    with it, and never for an employer whose thread ended in a reply.

    Runs at boot, after poll_replies, so a reply that landed while the machine was off has already
    protected its row; and after every scrape, the other moment new data arrives and the calendar can
    have moved under a running app. Deliberately silent about company_history: an employer retired here
    stays cooled down for COMPANY_COOLDOWN_DAYS, so an old row leaving the queue can never become a
    second application to the same firm.
    """
    if config.DRY_RUN:
        return {"jobs": 0, "mailboxes": 0, "note": "dry-run: nothing changed"}
    doomed = await database.aged_jobs(config.SCRAPE_KEEP_DAYS, config.OUTREACH_KEEP_DAYS, db_path=db_path)
    if not doomed:
        return {"jobs": 0, "mailboxes": 0}
    await database.delete_jobs([j["id"] for j in doomed], db_path=db_path)
    reached = {_company_key(j["company"]) for j in doomed if j["outreach_at"] or j["applied_at"]}
    live = {_company_key(c) for c in await database.companies(db_path=db_path)}
    mailboxes = [await database.delete_vault_email(key, db_path=db_path) for key in reached - live]
    for job in doomed:
        sent = job["outreach_at"] or job["applied_at"]
        print(f"  purge: job {job['id']} {job['company'][:40]} retired - "
              + (f"sent {sent[:10]}, no reply in {config.OUTREACH_KEEP_DAYS} days" if sent
                 else f"scraped {str(job['created_at'])[:10]}, never sent, "
                      f"past the {config.SCRAPE_KEEP_DAYS}-day window"))
    return {"jobs": len(doomed), "mailboxes": sum(mailboxes)}


async def run_followups() -> dict:
    """One short nudge per aged, unreplied, not-yet-followed outreach - on the channel it went out on.

    The three-way guard lives in database.due_followups; the nudge threads onto the original mail
    (In-Reply-To the stored Message-ID) and stamps followup_at write-once, so a row is nudged once,
    ever. A row with no mailbox was reached over LinkedIn, so that is where its second touch goes:
    mailing it is impossible, and skipping it is what left every DM-only row sitting in the follow-ups
    view as an entry no sweep could ever clear.

    DRY_RUN and missing creds short-circuit the MAIL half only; the LinkedIn half needs a logged-in
    browser session rather than SMTP, so it still runs when the mail path is unavailable.
    """
    due = await database.due_followups(config.FOLLOWUP_DAYS)
    if not due:
        return {"due": 0, "sent": 0, "note": "no aged unreplied outreach"}
    if config.DRY_RUN:
        return {"due": len(due), "sent": 0, "note": "dry-run: no nudge sent"}
    smtp_ok = bool(config.SMTP_EMAIL and config.SMTP_PASSWORD)
    if not smtp_ok and not any(not j.get("hr_email") for j in due):
        return {"due": len(due), "sent": 0, "note": "SMTP_EMAIL/SMTP_PASSWORD unset - mail nudges skipped"}
    sent = 0
    for job in due:
        role, company = job.get("title") or "the role", job.get("company") or "your team"
        try:
            if job.get("hr_email"):
                if not smtp_ok:
                    continue             # its channel is mail, and mail is not available: leave it due
                subject = f"Re: {job.get('title') or 'my application'} - following up"
                body = ["Hi,",
                        f"I wanted to follow up on my earlier note about the {role} position at {company}. "
                        "I remain very interested and would welcome the chance to discuss how I can contribute.",
                        "Thanks for your time - happy to share anything else that would help."]
                await asyncio.to_thread(_send_email, job["hr_email"], subject, body,
                                        job.get("pdf_path"), job.get("email_msgid"))
            elif not await _nudge_linkedin(job, role, company):
                continue
            await database.mark_followed(job["id"])
            sent += 1
        except Exception as exc:
            print(f"followup {job['id']} -> {type(exc).__name__}: {exc}")
    return {"due": len(due), "sent": sent}


async def _nudge_linkedin(job: dict, role: str, company: str) -> bool:
    """The second touch on the LinkedIn row: the named recruiter when one was scraped, else the search.

    Returns the send verdict instead of raising - a dead profile is one row's problem, and the caller's
    except already guards the mail half against the same thing.
    """
    text = ai_engine._cap(
        f"Hi, following up on my note about the {role} role at {company} - I'm still very interested "
        f"and happy to share more detail. Thanks for your time. - Prince Jha", ai_engine.DM_LIMIT)
    if profile := job.get("recruiter_linkedin_url"):
        ok, msg = await send_linkedin_dm(profile, text, job.get("title") or "", company)
    else:
        ok, msg = await send_linkedin_company_dm(company, text, job.get("title") or "")
    print(f"  followup {job['id']}: linkedin nudge -> {'sent' if ok else 'not sent'} · {msg}")
    return ok


_BOND = re.compile(r"[^.\n]*\b(?:bond|service agreement|retention agreement)\b[^.\n]*", re.I)


def _bond_snippet(text: str | None) -> str | None:
    match = _BOND.search(text or "")
    return match.group(0).strip()[:200] if match else None


def _salary(row: dict) -> str | None:
    """jobspy hands back min/max/interval columns; the Internshala scraper hands back one string."""
    if text := str(row.get("salary_text") or "").strip():
        return text
    parts = [f"{int(v):,}" for v in (row.get("min_amount"), row.get("max_amount")) if v and v == v]
    return f"{' - '.join(parts)} {row.get('interval') or 'yearly'}" if parts else None


_TRACKING_PARAMS = frozenset((
    "refid", "trackingid", "trk", "trkinfo", "position", "pagenum", "originalsubdomain", "src",
    "from", "fromage", "vjs", "tk", "advn", "sponsored", "hidesmr", "acatk", "alid", "xkcb",
    "xpse", "xfps", "cvo", "orig_cvo", "eqp", "sjdu", "sgtd", "seen", "rtk", "refresh",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
))


def _norm_url(url) -> str:
    """Canonical URL used as both the in-memory dedupe key and the stored job_url.

    LinkedIn and Indeed hang per-impression tracking on the same posting, so a raw URL is not an
    identity: without this, one job enters the queue once per query that surfaced it and gets applied
    to several times. Identity-bearing params survive (Indeed's ?jk=) — dropping the whole query
    string would collapse every Indeed listing onto a single URL.
    """
    raw = str(url or "").strip()
    parts = urlsplit(raw)
    if not parts.netloc:
        return raw
    kept = sorted((k, v) for k, v in parse_qsl(parts.query) if k.lower() not in _TRACKING_PARAMS)
    return urlunsplit((parts.scheme.lower() or "https", parts.netloc.lower().removeprefix("www."),
                       parts.path.rstrip("/") or "/", urlencode(kept), ""))


def _row_to_job(row: dict) -> dict:
    """One scraped record to the shape parsing.eligible and database.add_jobs both expect.

    date_posted is normalised to an ISO string here so it survives the DB round-trip: pipeline
    re-runs parsing.eligible on the queued row, and the freshness gate needs a date to re-check.
    """
    description = row.get("description") or ""
    posted = parsing.posted_date(row.get("date_posted"))
    job = {
        "title": row.get("title") or "Unknown role",
        "company": row.get("company") or "Unknown company",
        "job_url": _norm_url(row.get("job_url")),
        "platform": (row.get("site") or "").lower(),
        "salary": _salary(row),
        "bond_terms": _bond_snippet(description),
        "description": description,
        "date_posted": posted.isoformat() if posted else None,
        "applicants": row.get("applicants"),        # not a DB column; only the live gate reads it
        "status": "queued",
    }
    # Fit is a PRIORITY, never a gate: run_batch_events spends the day's quota on the highest scores
    # first (database._ORDER["fit"]), and nothing here changes who is eligible. Computed in the one
    # place every board's rows pass through, so no board can insert a NULL score into the queue.
    job["fit_score"], job["track"] = parsing.fit_score(job)
    # The family this req belongs to, stored with the row: the daily mixture is measured against it
    # during the sweep and the dashboard's role filter reads it back long after the sweep is gone.
    job["role"] = parsing.role_of(job)
    return job


def _slug(term: str) -> str:
    """Search term to the hyphen slug Naukri and Internshala both put in their listing paths."""
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", term.lower())).strip("-")


_INTERNSHALA = "https://internshala.com"
_CARD = re.compile(r'<div class="container-fluid individual_internship')
_CARD_FIELDS = {
    "href": re.compile(r"""data-href=['"]([^'"]+)['"]"""),
    "title": re.compile(r'class="(?:job-title-href|job-internship-name|profile)"[^>]*>(.*?)</a>', re.S),
    "company": re.compile(r'class="[^"]*compan(?:y-name|y_name)[^"]*"[^>]*>(.*?)</', re.S),
    "salary_text": re.compile(r'<span class="mobile">\s*(₹[^<]+?)\s*</span>', re.S),
    "date_posted": re.compile(r'class="status[^"]*"[^>]*>.*?<span>\s*(.*?)\s*</span>', re.S),
}


def _internshala_cards(body: str) -> list[dict]:
    """Listing HTML to raw card rows. No network, so the parser is testable against a fixture."""
    rows: list[dict] = []
    for card in _CARD.split(body)[1:]:
        found = {}
        for key, rx in _CARD_FIELDS.items():
            m = rx.search(card)
            found[key] = _text(m.group(1)) if m else None
        href = found.pop("href") or ""
        if not href.startswith("/") or not found.get("title"):
            continue
        rows.append({"site": "internshala", "job_url": f"{_INTERNSHALA}{href}", **found})
    return rows


def _internshala_http(term: str, want: int = 12) -> list[dict]:
    """Blocking Internshala fetch. jobspy has no Internshala backend, so this one is hand-rolled.

    Still the primary path for this board: it works, it costs no browser, and it is the cheapest of the
    four. `_internshala_search` is its Playwright fallback, taken when this returns nothing.

    ponytail: keyword axis only — Internshala's location filter is a separate slug and its listings
    are overwhelmingly remote or Pune/Bengaluru anyway, so the location half of the search matrix is
    skipped here. Add /jobs/keywords-<kw>/<city>/ if city targeting ever matters.

    Detail pages are fetched only for cards that already clear the gate on title, pay and age,
    because each one is a separate request and the gate rejects most cards for free.
    """
    body = _get(f"{_INTERNSHALA}/jobs/keywords-{_slug(term)}/")
    if not body:
        return []
    kept: list[dict] = []
    for row in _internshala_cards(body):
        if len(kept) >= want:
            break
        if not parsing.eligible(_row_to_job(row))[0]:
            continue
        row["description"] = _internshala_jd(row["job_url"])
        kept.append(row)
        time.sleep(random.uniform(1.0, 2.0))
    return _stamp(kept, "internshala")


def _internshala_jd(url: str) -> str:
    """The requirements block off a detail page. Empty string when the layout moved."""
    body = _get(url)
    if not body:
        return ""
    start = body.find('class="internship_details"')
    if start < 0:
        return ""
    end = body.find("similar_internship", start)
    return _text(body[start:end if end > start else start + 20_000])[:8_000]


_NAUKRI = "https://www.naukri.com"
_TUPLE = re.compile(r'<div class="srp-jobtuple-wrapper"')
_TUPLE_FIELDS = {
    "job_url": re.compile(r'<a class="title[^"]*"[^>]*\shref="([^"]+)"'),
    "title": re.compile(r'<a class="title[^"]*"[^>]*\stitle="([^"]*)"'),
    "company": re.compile(r'<a class=" comp-name[^"]*"[^>]*\stitle="([^"]*)"'),
    "experience": re.compile(r'class="expwdth"[^>]*>([^<]*)'),
    "location": re.compile(r'class="locWdth"[^>]*>([^<]*)'),
    "salary_text": re.compile(r'srp-rupee sal">\s*<span[^>]*>([^<]*)'),
    "date_posted": re.compile(r'class="job-post-day[^"]*"\s*>(.*?)</span>', re.S),
    "description": re.compile(r'class="job-desc [^"]*"[^>]*>(.*?)</span>', re.S),
}


def _naukri_cards(body: str) -> list[dict]:
    """Naukri SRP HTML to raw rows. Network-free on purpose, so it is testable against a fixture.

    Regex rather than a DOM parse: the whole file is one already-rendered search page, the six fields
    all live in attributes on distinct classes, and pulling them out costs no dependency.
    """
    rows: list[dict] = []
    for card in _TUPLE.split(body)[1:]:
        found = {}
        for key, rx in _TUPLE_FIELDS.items():
            m = rx.search(card)
            found[key] = _text(m.group(1)) if m else None
        if not (found.get("job_url") or "").startswith("http") or not found.get("title"):
            continue
        experience = found.pop("experience") or ""
        # "Unpaid" / "No fixed salary" is a stated zero, not missing data: rewrite it to a number so
        # the MIN_LPA gate rejects it. A blank or "Not Disclosed" field stays None and still passes.
        if re.search(r"unpaid|no fixed salary|stipend", found.get("salary_text") or "", re.I):
            found["salary_text"] = "0 LPA"
        desc = found.get("description") or ""
        # The card's JD is a ~100-char teaser. Experience is folded into it because the years gate
        # reads the description, and "0-3 Yrs" on the card is the only place that number appears
        # before a detail-page fetch is spent.
        found["description"] = f"Experience required: {experience.replace('Yrs', 'years')}\n{desc}".strip()
        rows.append({"site": "naukri", **found})
    return rows


def _naukri_url(term: str, location: str) -> str:
    """SRP path form, which is the shape Naukri serves without a redirect chain."""
    city = re.sub(r"[^a-z0-9]+", "-", location.split(",")[0].strip().lower()).strip("-")
    slug = _slug(term)
    return f"{_NAUKRI}/{slug}-jobs" if city in ("", "india", "remote") else f"{_NAUKRI}/{slug}-jobs-in-{city}"


async def _naukri_search(term: str, location: str, want: int = 12) -> list[dict]:
    """Naukri ingestion through the logged-in Playwright context instead of jobspy's HTTP client.

    jobspy's Naukri backend is answered with `406 {"message":"recaptcha required"}` on every call from
    here — measured, zero rows — because the endpoint it hits is behind a bot wall. The already
    authenticated persistent context walks straight through it: the same search rendered in the
    browser returns a full 350KB SRP with ~20 job tuples and no captcha challenge, since the session
    cookies Naukri wants are already in the profile. So the page is fetched with the browser and the
    HTML handed to the regex parser, which is what the brief asks for as the fallback branch.

    Detail pages are only opened for cards that already clear the gate, same as Internshala.
    """
    page = None
    try:
        page = await (await _context("naukri")).new_page()
        page.set_default_timeout(_STEP_TIMEOUT)
        await page.goto(_naukri_url(term, location), wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
        await _human(2.0, 4.0)
        body = await page.content()
        if "recaptcha required" in body.lower():
            print("naukri: the browser session is also being challenged - log in again in that profile")
            return []
        kept: list[dict] = []
        for row in _naukri_cards(body):
            if len(kept) >= want:
                break
            if not parsing.eligible(_row_to_job(row))[0]:
                continue
            row["description"] = await _pw_jd(page, row["job_url"], _NAUKRI_JD) or row["description"]
            kept.append(row)
            await _human(1.5, 3.0)
        return _stamp(kept, "naukri")
    except Exception as exc:
        print(f"naukri playwright scrape failed {term!r} @ {location!r}: {type(exc).__name__}: {exc}")
        return []
    finally:
        await _close_quietly(page)


# Attribute-contains selectors, because Naukri's class names carry a rotating build hash
# (`styles_job-desc-container__txpYf`) — pinning the whole name would break on their next deploy.
_NAUKRI_JD = ["[class*='job-desc']", "[class*='dang-inner']", "main"]


# --- universal playwright fallback ----------------------------------------------------------------
# jobspy is now walled on every board, not just Naukri: LinkedIn answers its guest client with a
# throttle, Indeed serves an interstitial, and Internshala has no jobspy backend at all. The four
# already-authenticated persistent contexts walk through all of it, so every board gets the treatment
# Naukri already had - render the search page in the browser, harvest the cards, hand them to the same
# parsing.eligible gate. No login flow anywhere below: _context() reuses browser_sessions/<platform>.
#
# ONE harvester for every board, parameterised by a selector table, instead of one scraper per board:
# the boards differ only in which class names hold the six fields. `title` attributes are read before
# innerText because Indeed and LinkedIn both truncate the visible label with an ellipsis. Returning
# structured dicts (rather than HTML) keeps the whole Python half testable against a fixture.
_CARD_JS = """(cfg) => {
  const text = (card, sels) => {
    for (const s of (sels || [])) {
      let el = null;
      try { el = card.matches(s) ? card : card.querySelector(s); } catch (e) { continue; }
      if (!el) continue;
      const t = (el.getAttribute('title') || el.innerText || el.textContent || '')
        .replace(/\\s+/g, ' ').trim();
      if (t) return t;
    }
    return null;
  };
  for (const cardSel of cfg.card) {
    const rows = [];
    for (const card of document.querySelectorAll(cardSel)) {
      const row = {};
      for (const key of Object.keys(cfg.fields)) row[key] = text(card, cfg.fields[key]);
      for (const s of cfg.url) {
        const a = card.matches(s) ? card : card.querySelector(s);
        if (a && a.href) { row.job_url = a.href; break; }
      }
      // Internshala hangs the posting path off the card element itself, not off an anchor.
      if (!row.job_url) {
        const h = card.getAttribute('data-href');
        if (h) row.job_url = new URL(h, location.origin).href;
      }
      if (row.job_url && row.title) rows.push(row);
    }
    if (rows.length) return rows.slice(0, 60);
  }
  return [];
}"""

_PW_BOARDS = {
    "linkedin": {
        # f_TPR=r86400 is LinkedIn's own last-24h filter, which is exactly config.FRESH_HOURS, and
        # sortBy=DD is newest-first: together they stop detail fetches being spent on postings the
        # freshness gate would drop anyway.
        "search": lambda term, location: (
            "https://www.linkedin.com/jobs/search/?"
            f"keywords={quote_plus(term)}&location={quote_plus(location or 'India')}"
            "&f_TPR=r86400&sortBy=DD"),
        "ready": "div.job-card-container, li[data-occludable-job-id], div.base-card",
        "card": ["div.job-card-container", "li[data-occludable-job-id]", "div.base-card"],
        "url": ["a.job-card-container__link", "a.job-card-list__title", "a.base-card__full-link",
                "a[href*='/jobs/view/']"],
        "fields": {
            "title": ["a.job-card-list__title", "[class*='job-card-list__title']",
                      "h3.base-search-card__title", "a.job-card-container__link"],
            "company": ["[class*='job-card-container__primary-description']",
                        "[class*='job-card-container__company-name']", "h4.base-search-card__subtitle"],
            "location": ["[class*='job-card-container__metadata']", "span.job-search-card__location"],
            "date_posted": ["time[datetime]", "time", "[class*='listdate']"],
        },
        "jd": ["div.jobs-description__content", "[class*='jobs-box__html-content']",
               "div.show-more-less-html__markup", "main"],
    },
    "indeed": {
        # in.indeed.com, not indeed.com: the .com host answers an India query with a country
        # interstitial that renders no cards. fromage=1 is last-24h, sort=date is newest-first.
        "search": lambda term, location: (
            f"https://in.indeed.com/jobs?q={quote_plus(term)}"
            f"&l={quote_plus(location or 'India')}&fromage=1&sort=date"),
        "ready": "div.job_seen_beacon, td.resultContent, a[data-jk]",
        "card": ["div.job_seen_beacon", "div.cardOutline", "td.resultContent"],
        "url": ["h2.jobTitle a", "a.jcs-JobTitle", "a[data-jk]", "a[href*='/rc/clk']",
                "a[href*='/viewjob']"],
        "fields": {
            "title": ["h2.jobTitle span[title]", "h2.jobTitle", "a.jcs-JobTitle"],
            "company": ["[data-testid='company-name']", "span.companyName", "[class*='companyName']"],
            "location": ["[data-testid='text-location']", "div.companyLocation",
                         "[class*='companyLocation']"],
            "date_posted": ["[data-testid='myJobsStateDate']", "span.date", "[class*='date_']"],
            "salary_text": ["[data-testid='attribute_snippet_testid']", "div.salary-snippet-container",
                            "[class*='salary-snippet']"],
        },
        "jd": ["div#jobDescriptionText", "[class*='jobsearch-JobComponent-description']", "main"],
    },
    "internshala": {
        # Same listing URL the httpx primary uses; only the transport differs, so a wall on one is not
        # automatically a wall on the other - the browser carries the session cookies, curl does not.
        "search": lambda term, _location="": f"{_INTERNSHALA}/jobs/keywords-{_slug(term)}/",
        "ready": "div.individual_internship, div.internship_meta",
        "card": ["div.individual_internship", "div.internship_meta"],
        "url": ["a.job-title-href", "a.job-internship-name", "h3.job-internship-name a", "a.profile"],
        "fields": {
            "title": ["a.job-title-href", ".job-internship-name", ".profile"],
            "company": [".company-name", ".company_name", "p.company-name"],
            "location": ["#location_names", ".locations", "[class*='location']"],
            "date_posted": [".status-success", ".status-info", "[class*='status']"],
            "salary_text": [".stipend", "[class*='stipend']", "span.mobile"],
        },
        "jd": ["div.internship_details", "div.detail_view", "main"],
    },
}

_WALLS = ("/authwall", "/login", "/checkpoint", "/uas/login", "/account/login", "captcha")


def _walled(url: str) -> bool:
    """True when a board bounced the scrape to a login or bot wall instead of its results page."""
    return any(wall in (url or "").lower() for wall in _WALLS)


def _stamp(rows: list[dict], platform: str) -> list[dict]:
    """Force `site` on every row a single-board scraper produced. Returns the same list.

    This is the attribution bug's root cause and its fix in one place. `_row_to_job` reads exactly one
    key - `site` - to decide the DB platform column, and the scrape loop merges jobspy's mixed batch
    and a fallback's single-board batch into ONE list. Any fallback row that arrived without `site`
    inherited whatever the DB CHECK constraint happened to accept, which is how Naukri rows were
    written as INDEED. Stamping at the return boundary means a scraper cannot lose its own name.

    Never call this on a jobspy batch: that one genuinely is mixed, and every row already knows its
    board.
    """
    for row in rows:
        row["site"] = platform
    return rows


def _pw_rows(platform: str, harvested: list[dict] | None) -> list[dict]:
    """Harvested card dicts to raw scraper rows. No browser, so the normalising is fixture-testable."""
    rows: list[dict] = []
    seen: set[str] = set()
    for card in harvested or ():
        row = {k: (v.strip() or None) if isinstance(v, str) else v for k, v in card.items()}
        url = row.get("job_url") or ""
        if not url.startswith("http") or not row.get("title") or url in seen:
            continue
        seen.add(url)
        # A stated zero must read as zero, not as missing data - same rule the Naukri parser applies,
        # because "missing metadata always passes" would otherwise let unpaid roles through the gate.
        if re.search(r"unpaid|no fixed salary", row.get("salary_text") or "", re.I):
            row["salary_text"] = "0 LPA"
        row.setdefault("description", "")
        rows.append({**row, "site": platform})
    return rows


async def _pw_jd(page, url: str, selectors: list[str]) -> str:
    """Full JD text off a detail page; empty string when the layout moved or the fetch failed."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT)
        for sel in selectors:
            loc = page.locator(sel).first
            if await loc.count():
                text = (await loc.inner_text()).strip()
                if len(text) > 200:
                    return text[:8_000]
        return ""
    except Exception:
        return ""


async def _pw_search(platform: str, term: str, location: str, want: int = 12) -> list[dict]:
    """One board's search page through its already-authenticated persistent context.

    No login flow: `_context(platform)` reuses the profile under browser_sessions/<platform> that the
    apply run drives, so the session cookies are already there. A bounce to a login wall means that
    profile needs re-authenticating by hand, and is reported rather than retried.

    Detail pages are opened only for cards that already clear the gate on title, pay and age, because
    each one is a full navigation and the gate rejects most cards for free.
    """
    board = _PW_BOARDS[platform]
    page = None
    try:
        page = await (await _context(platform)).new_page()
        page.set_default_timeout(_STEP_TIMEOUT)
        await page.goto(board["search"](term, location), wait_until="domcontentloaded",
                        timeout=_NAV_TIMEOUT)
        if _walled(page.url):
            print(f"{platform}: bounced to a login wall - re-authenticate browser_sessions/{platform}")
            return []
        try:
            await page.wait_for_selector(board["ready"], timeout=_DM_TIMEOUT)
        except Exception:
            pass          # zero results and a moved layout look identical here; the harvest tells which
        await _human(1.5, 3.0)
        # Only the JSON-serialisable half of the table crosses into the page: "search" is a lambda.
        cfg = {key: board[key] for key in ("card", "url", "fields")}
        kept: list[dict] = []
        for row in _pw_rows(platform, await page.evaluate(_CARD_JS, cfg)):
            if len(kept) >= want:
                break
            if not parsing.eligible(_row_to_job(row))[0]:
                continue
            row["description"] = await _pw_jd(page, row["job_url"], board["jd"]) or row["description"]
            kept.append(row)
            await _human(1.0, 2.5)
        if not kept:
            print(f"{platform}: browser scrape found no eligible cards for {term!r} @ {location or 'any'}")
        return _stamp(kept, platform)
    except Exception as exc:
        print(f"{platform} playwright scrape failed {term!r} @ {location!r}: {type(exc).__name__}: {exc}")
        return []
    finally:
        await _close_quietly(page)


async def _linkedin_search(term: str, location: str, want: int = 12) -> list[dict]:
    """LinkedIn jobs SRP in the logged-in browser, for when jobspy's guest client is throttled."""
    return await _pw_search("linkedin", term, location, want)


async def _indeed_search(term: str, location: str, want: int = 12) -> list[dict]:
    """Indeed SRP in the logged-in browser, for when jobspy gets the bot interstitial."""
    return await _pw_search("indeed", term, location, want)


async def _internshala_search(term: str, location: str = "", want: int = 12) -> list[dict]:
    """Internshala listing in the browser, for when the httpx primary (_internshala_http) is walled."""
    return await _pw_search("internshala", term, location, want)


# The per-board fallback routed to when jobspy comes back empty for that board. Naukri keeps its own
# regex parser (page.content() -> _naukri_cards) because it already works and has fixtures; the other
# three share the harvester above.
_PW_SEARCH = {"naukri": _naukri_search, "linkedin": _linkedin_search, "indeed": _indeed_search}


def _scrape_one(term: str, location: str, sites=config.SCRAPE_SITES) -> list[dict]:
    """One jobspy call, blocking. A query that errors is reported and counted as empty.

    `sites` narrows the backends per call: the loop drops a board as soon as its share of the daily
    quota is full, so the remaining requests are spent on the boards that still owe jobs.

    Nothing from jobspy is allowed out of this function. It runs in a worker thread, so a raised
    exception would cross back into the event loop and abort the whole scraping batch - one board's
    406 recaptcha wall or a proxy timeout must cost one query, not the run. The import is inside the
    try for the same reason: a broken jobspy install degrades to zero rows, and the Naukri Playwright
    fallback (which triggers precisely on zero rows) still fills that board.
    """
    try:
        from jobspy import scrape_jobs

        df = scrape_jobs(
            site_name=list(sites),
            search_term=term,
            location=location,
            is_remote=location.lower() == "remote",
            results_wanted=config.RESULTS_PER_QUERY,
            hours_old=config.SCRAPE_HOURS_OLD,
            country_indeed="India",
            linkedin_fetch_description=True,
            description_format="markdown",
            verbose=0,
        )
    except Exception as exc:
        # Includes 406 recaptcha, proxy timeouts, and pandas/parse errors from a board that changed.
        print(f"scrape failed {term!r} @ {location!r} on {tuple(sites)}: {type(exc).__name__}: {exc}")
        return []
    if df is None or df.empty:
        return []
    try:
        df.columns = [c.lower() for c in df.columns]
        return df.where(df.notna(), None).to_dict("records")
    except Exception as exc:
        print(f"scrape returned an unreadable frame for {term!r}: {type(exc).__name__}: {exc}")
        return []


def _plan(terms, locations) -> list[tuple[str, str, str]]:
    """Query order: for each role, Internshala first, then that role across every location.

    Internshala leads because it is the thinnest board and the only one with a dedicated fetcher —
    if it queued behind 180 jobspy calls it would never be reached before the quota filled.
    """
    out = []
    for term in terms:
        out.append(("internshala", term, ""))
        out += [("jobspy", term, location) for location in locations]
    return out


async def scrape_new_jobs(terms=config.SEARCH_TERMS, locations=config.SEARCH_LOCATIONS) -> dict:
    """Role x location sweep across all 4 platforms, gated by parsing.eligible and the daily cap.

    Two splits bound one run, and both backfill at the end. The quota splits evenly across the boards
    — 80 jobs over 4 boards is 20 each — so LinkedIn's volume cannot eat the whole day's budget, and
    it splits across the four role families (config.ROLE_TARGETS) so the day is a mixture instead of
    whichever family the boards answer with first. A board that cannot fill its share or a family the
    market cannot fill (Internshala, fresher full-stack) leaves slots to the overflow backfill, so
    neither split can ever mean an under-filled queue.

    Queries run one at a time with a randomised gap: firing the full matrix back to back is what gets
    an IP blocked. MAX_QUERIES_PER_RUN bounds the sweep, since hunting for a thin board's last few
    jobs would otherwise walk every remaining query.

    Every board gets one cheap attempt (jobspy, or httpx for Internshala). The moment a board answers
    with nothing - the 406/403 bot wall, in practice, on all four now - that board is switched to its
    logged-in Playwright context for the rest of the run, per board and not globally, so a working
    backend keeps being used while a walled one stops being paid for.
    """
    quota = await database.quota_left()
    if quota <= 0:
        return {"queries": "0/0", "scraped": 0, "eligible": 0, "inserted": 0, "walls": {},
                "note": f"daily cap {config.MAX_DAILY_APPLICATIONS} reached"}

    per = max(1, quota // len(config.PLATFORMS))
    # The day's mixture, scaled to whatever quota is actually left: at a full 80 this is the 20/10/25/25
    # split the operator asked for, and a quota of 30 keeps the same shape rather than spending what is
    # left on whichever family the boards happen to answer with.
    shares = {role: max(1, quota * target // config.MAX_DAILY_APPLICATIONS)
              for role, target in config.ROLE_TARGETS.items()}
    taken = {role: 0 for role in config.ROLES}
    queries = _plan(terms, locations)
    # Asked before the first query is spent. Everything the queue already holds - and every company
    # still inside its cooldown - is skipped as it arrives, so the sweep measures its progress in jobs
    # it can actually write and keeps querying until it has the quota of them.
    known, cooled = await database.known_jobs()
    buckets: dict[str, list[dict]] = {p: [] for p in config.PLATFORMS}
    overflow: list[dict] = []
    seen: set[str] = set()
    rejected: list[str] = []
    board_hits: dict[str, int] = {p: 0 for p in config.PLATFORMS}   # raw rows seen per board this run
    done = 0
    pw: set[str] = set()     # boards switched to the browser after their cheap backend returned nothing
    for kind, term, location in queries:
        if all(len(rows) >= per for rows in buckets.values()) or done >= config.MAX_QUERIES_PER_RUN:
            break
        if kind == "internshala":
            need = per - len(buckets["internshala"])
            if need <= 0:
                continue
            rows = [] if "internshala" in pw else await asyncio.to_thread(_internshala_http, term, need)
            if not rows and "internshala" not in pw:
                pw.add("internshala")
                print("internshala: the http fetch returned no rows, switching that board to the "
                      "logged-in browser for the rest of the run")
            if "internshala" in pw:
                rows = await _internshala_search(term, location, need)
        else:
            need = {site: per - len(buckets[site]) for site in config.SCRAPE_SITES}
            if all(n <= 0 for n in need.values()):
                continue
            hungry = tuple(s for s in config.SCRAPE_SITES if need[s] > 0 and s not in pw)
            rows = await asyncio.to_thread(_scrape_one, term, location, hungry) if hungry else []
            for site in hungry:
                if not any((r.get("site") or "").lower() == site for r in rows):
                    # jobspy reached the board and got nothing back. In practice that is the 403/406
                    # bot wall, and a genuinely empty page is no reason to keep paying for the HTTP
                    # path either, since the logged-in browser path is strictly better.
                    pw.add(site)
                    print(f"{site}: jobspy backend returned no rows, switching that board to the "
                          "logged-in browser for the rest of the run")
            for site in config.SCRAPE_SITES:
                if site in pw and need[site] > 0:
                    rows += await _PW_SEARCH[site](term, location, need[site])
        done += 1
        before = sum(len(r) for r in buckets.values())
        for row in rows:
            job = _row_to_job(row)
            url, platform = job["job_url"], job["platform"]
            if platform not in buckets or not url:
                continue
            # A board that answered counts as answering even when every row it served was one we
            # already had: the wall report below reads this, and "already queued" is not "blocked".
            board_hits[platform] = board_hits.get(platform, 0) + 1
            key = (str(job.get("title") or "").strip().lower(),
                   str(job.get("company") or "").strip().lower())
            if url in seen or not key[0] or not key[1] or key in known or key[1] in cooled:
                continue
            seen.add(url)
            known.add(key)      # the same req served twice in one sweep is still one job
            ok, why = parsing.eligible(job)
            role = job.get("role") or ""
            if not ok:
                rejected.append(why)
            elif len(buckets[platform]) < per and (not role or taken[role] < shares.get(role, 0)):
                buckets[platform].append(job)
                if role:
                    taken[role] += 1
            else:
                overflow.append(job)               # backfills slots a thinner board or family never claims
        got = sum(len(r) for r in buckets.values())
        if got > before:
            print(f"[{done}/{len(queries)}] {kind} {term!r} @ {location or 'any'}: +{got - before} "
                  f"eligible ({'/'.join(f'{p[:4]}:{len(r)}' for p, r in buckets.items())} of {per} each)")
        await asyncio.sleep(random.uniform(*config.SCRAPE_PAUSE))

    keep = [job for rows in buckets.values() for job in rows]
    keep += overflow[:max(0, quota - len(keep))]
    # Why a short run was short, said out loud. There are two different reasons and they need two
    # different fixes, and the run that stopped at 43 was the first one, not the second: 40 of 210
    # queries ran before MAX_QUERIES_PER_RUN cut the sweep off.
    if len(keep) < quota:
        if done < len(queries):
            print(f"Query budget spent. Found {len(keep)} eligible jobs across {done} of "
                  f"{len(queries)} queries (MAX_QUERIES_PER_RUN={config.MAX_QUERIES_PER_RUN}); "
                  f"{len(queries) - done} queries never ran. Raise MAX_QUERIES_PER_RUN to reach them.")
        else:
            print(f"Search matrix exhausted. Found {len(keep)} eligible jobs across {done} "
                  f"total queries.")
    # Entry-level rows win the slots, then go in LAST so they take the highest ids: database.fetch
    # orders by id DESC, which is the order run_batch consumes, so freshers get today's budget first.
    entry = {job["job_url"]: parsing.entry_level(job) for job in keep}
    batch = sorted(keep, key=lambda j: not entry[j["job_url"]])[:quota]
    batch.reverse()
    inserted = await database.add_jobs(batch)
    # A board is walled, not merely empty, when it fell back to the logged-in browser AND that path
    # still returned nothing: both the cheap and the authenticated fetch were bounced (406/authwall).
    walls = {b: "browser fallback returned no rows (bot wall / login)"
             for b in sorted(pw) if board_hits.get(b, 0) == 0}
    return {"queries": f"{done}/{len(queries)}", "scraped": len(seen), "eligible": len(keep),
            "per_platform": {p: len(r) for p, r in buckets.items()}, "share": per,
            "per_role": {r: taken[r] for r in config.ROLES},
            "overflow": len(overflow), "entry_level": sum(entry.values()), "inserted": inserted,
            "rejected": len(rejected), "browser_fallback": ",".join(sorted(pw)) or "none",
            "walls": walls, "quota_left": quota - inserted}


async def _selfcheck() -> None:
    assert set(SELECTORS) == set(config.PLATFORMS), "every platform needs a selector table"
    for name, sel in SELECTORS.items():
        assert {"apply", "upload", "advance", "submit"} <= set(sel), name

    assert _salary({"min_amount": 800000, "max_amount": 1200000, "interval": "yearly"}) == "800,000 - 1,200,000 yearly"
    assert _salary({"min_amount": float("nan"), "max_amount": None}) is None
    assert parsing.lpa(_salary({"min_amount": 900000, "max_amount": 1500000, "interval": "yearly"})) == 15
    assert parsing.lpa(_salary({"min_amount": 60000, "interval": "monthly"})) == 7.2
    assert "2 year" in _bond_snippet("Great team. Requires a 2 year service agreement. Apply now.")
    assert _bond_snippet("No strings attached.") is None

    assert len(set(config.SEARCH_TERMS)) == len(config.SEARCH_TERMS), "duplicate search term"
    assert config.SEARCH_LOCATIONS and all(config.SEARCH_LOCATIONS), "blank location kills a query"
    assert config.SCRAPE_PAUSE[0] < config.SCRAPE_PAUSE[1], "sleep range must be (low, high)"
    li = "https://www.linkedin.com/jobs/view/4291234567/?refId=abc&trackingId=xyz&position=3"
    assert _norm_url(li) == "https://linkedin.com/jobs/view/4291234567", _norm_url(li)
    assert _norm_url(li) == _norm_url("https://linkedin.com/jobs/view/4291234567?position=9&trk=q")
    one = _norm_url("https://in.indeed.com/viewjob?jk=aaa111&from=serp&tk=zz")
    two = _norm_url("https://in.indeed.com/viewjob?jk=bbb222&from=serp")
    assert one.endswith("jk=aaa111") and one != two, (one, two)   # identity param must survive
    assert _norm_url(None) == "" and _norm_url("not-a-url") == "not-a-url"
    job = _row_to_job({"site": "LinkedIn", "title": None, "company": None, "job_url": li,
                       "description": "Fresher role. Requires a 2 year service agreement.",
                       "min_amount": 900000, "max_amount": 1500000, "interval": "yearly"})
    assert job["platform"] == "linkedin" and job["title"] == "Unknown role"
    assert job["job_url"] == _norm_url(li) and parsing.lpa(job["salary"]) == 15
    assert job["status"] == "queued" and "2 year" in job["bond_terms"]
    assert job["date_posted"] is None, "a row with no date must not invent one"
    scored = _row_to_job({"site": "linkedin", "title": "DevSecOps Engineer", "company": "Acme",
                          "job_url": two, "description": "CI/CD security gates, Docker, Kubernetes, SAST"})
    assert 0.0 < scored["fit_score"] <= 1.0 and scored["track"] in parsing._TRACKS, scored["track"]
    assert job["fit_score"] == 0.0 and job["track"] == "", \
        "a row with no JD keywords still carries a score, never a missing key"
    dated = _row_to_job({"site": "naukri", "title": "Junior SRE", "job_url": two,
                         "date_posted": "1 day ago"})
    assert dated["date_posted"] == (parsing.date.today() - parsing.timedelta(days=1)).isoformat()
    assert parsing.lpa(_salary({"salary_text": "8 LPA", "min_amount": 900000})) == 8, \
        "a board that gives one salary string wins over the min/max columns"
    fresher = _row_to_job({"site": "indeed", "title": "L1 SOC Analyst", "job_url": two})
    assert parsing.entry_level(fresher) and parsing.eligible(fresher)[0]

    assert _is_hr({"position": "Talent Acquisition Specialist"})
    assert _is_hr({"department": "hr"}) and _is_hr({"position": "Head of People Ops"})
    assert not _is_hr({"position": "Marketing Manager"}), "generic mailbox is not HR"
    assert not _is_hr({"position": ""}) and not _is_hr({}), "blank position is not HR"
    assert not _is_hr({"position": "Chief Thrust Officer"}), "'hr' must not match inside a word"
    assert _corroborated("dtcc.com", "The Depository Trust & Clearing Corporation (DTCC)", "")
    assert _corroborated("infosys.com", "Infosys Limited", "")
    assert _corroborated("tcs.com", "Tata Consultancy Services", ""), "acronym domains are real"
    assert _corroborated("acme.io", "Acme Pvt Ltd", "")
    assert _corroborated("anything.example", "Whoever", "https://careers.anything.example/job/1"), \
        "a posting on the company's own host corroborates itself"
    assert not _corroborated("randomstartup.io", "Cisco Systems", ""), "unrelated domain must fail"
    assert not _corroborated("gmail.com", "Gmail Recruiter", ""), "freemail is never a company domain"
    assert not _corroborated("", "Infosys", "") and not _corroborated("localhost", "Infosys", "")
    assert not _corroborated("infosys.com", "", ""), "no company name, no corroboration"
    hunter_reply = [{"value": "marketing@dtcc.asia", "position": "Marketing", "confidence": 97},
                    {"value": "priya@dtcc.com", "position": "Talent Acquisition", "confidence": 71},
                    {"value": "hr@unrelatedco.io", "position": "HR Manager", "confidence": 99}]
    dtcc = "The Depository Trust & Clearing Corporation (DTCC)"
    assert _pick_hr(hunter_reply, dtcc) == "priya@dtcc.com", _pick_hr(hunter_reply, dtcc)
    assert _pick_hr(hunter_reply[:1], dtcc) is None, "non-HR position must not be emailed"
    assert _pick_hr(hunter_reply[2:], dtcc) is None, "HR at the wrong company must not be emailed"
    assert _pick_hr([{"value": "marie.dubois@acmecorp.eu", "position": "Engineer"}], "AcmeCorp") is None
    assert _pick_hr([], dtcc) is None and _pick_hr([{"position": "HR"}], dtcc) is None

    assert _pick_hr([{"value": "dev@x.com", "position": "Engineer", "confidence": 99},
                     {"value": "hr@acme.com", "position": "Talent Acquisition", "confidence": 40}],
                    "Acme Technologies") == "hr@acme.com"

    assert _HR_LOCAL.search("hr") and _HR_LOCAL.search("hr.india") and _HR_LOCAL.search("careers")
    assert _HR_LOCAL.search("talent.acquisition") and _HR_LOCAL.search("recruitment")
    assert _HR_LOCAL.search("hiring") and _HR_LOCAL.search("jobs") and _HR_LOCAL.search("campus.hr")
    assert not _HR_LOCAL.search("chris"), "'hr' inside a first name is not a role mailbox"
    assert not _HR_LOCAL.search("shreya") and not _HR_LOCAL.search("shrikant")
    assert not _HR_LOCAL.search("dev") and not _HR_LOCAL.search("priya.sharma")
    assert _emails_in_text("write to HR.India@acme.com today") == ["hr.india@acme.com"]
    assert _emails_in_text("<img src=logo@2x.png>") == [], "an asset is not a mailbox"
    assert _emails_in_text("no-reply@acme.com sales@acme.com") == [], "machine/wrong-desk mailboxes"
    # The accommodations desk. Live miss: samsara.com printed `accessibleinterviewing@` in its boilerplate,
    # the domain corroborated, and a cold job pitch was mailed to the people who handle disability
    # accommodation requests. Same paragraph, same class: eeo and veteran desks.
    assert _emails_in_text("accessibleinterviewing@samsara.com") == [], "an accommodations desk is not a recruiter"
    assert _emails_in_text("accommodations@acme.com eeo-reports@acme.com veterans@acme.com") == []
    assert _emails_in_text("accessible.interviewing@acme.com") == []
    assert _emails_in_text("access@acme.com") == ["access@acme.com"], "the stem must not eat real mailboxes"
    assert _jd_email("Need an accommodation? accessibleinterviewing@samsara.com", "Samsara",
                     "https://samsara.com/jobs/1") is None, "a corroborated domain is not a free pass"
    assert _emails_in_text("a@x.com b@y.com a@x.com") == ["a@x.com", "b@y.com"], "deduped, ordered"

    assert _jd_email("Send your CV to careers@acmelabs.com", "Acme Labs") == "careers@acmelabs.com"
    assert _jd_email("Contact priya.sharma@acmelabs.com", "Acme Labs") == "priya.sharma@acmelabs.com", \
        "the company's own domain is trusted whatever the localpart"
    assert _jd_email("Mail hr.acmelabs@gmail.com", "Acme Labs") == "hr.acmelabs@gmail.com", \
        "small firms really do publish a freemail hiring mailbox"
    assert _jd_email("Reach ravi@gmail.com for details", "Acme Labs") is None, \
        "a stranger's freemail in a JD body is not a contact"
    assert _jd_email("Vendor query: bids@unrelatedco.io", "Acme Labs") is None
    assert _jd_email("Apply on the portal", "Acme Labs") is None and _jd_email("", "Acme") is None
    assert _jd_email("hr@acmelabs.com and ravi@acmelabs.com", "Acme Labs") == "hr@acmelabs.com", \
        "an HR mailbox outranks a named employee on the same domain"

    real_hunter, real_dork, real_osint = _hunter_lookup, _dork_hr, _osint_email_search
    real_db = config.DB_PATH

    async def _dork_stub(company, job_url="", page_budget=2):
        return "hr@dorklabs.com"

    async def _dork_dry(company, job_url="", page_budget=2):
        return None

    async def _osint_stub(company_domain, company="", job_url=""):
        return "hr@osintlabs.com"

    async def _osint_dry(company_domain, company="", job_url=""):
        return None

    def _hunter_banned(company, job_url=""):
        raise AssertionError("the vault must answer before a paid Hunter credit is burned")

    # Company-name -> vault key. One employer, one row, however the board spelled the name - but two
    # different employers must never collide, or one company's recruiter gets another company's job.
    assert _company_key("Acme Labs Pvt Ltd") == "acmelabs" == _company_key("  ACME  LABS ")
    assert _company_key("Acme Labs") != _company_key("Acme Technologies"), "distinct firms, distinct keys"
    assert _company_key("Tata Consultancy Services") == "tataconsultancyservices"
    assert _company_key("Pvt Ltd") == "pvtltd", "an all-noise name still needs a key"
    assert _company_key("") == ""
    # Vault trust net: a cache hit still has to corroborate the company on the row.
    assert _trusted("hr@acmelabs.com", "Acme Labs", "https://acmelabs.com/j/1")
    assert _trusted("hr.acmelabs@gmail.com", "Acme Labs"), "small firms do publish freemail HR boxes"
    assert not _trusted("ravi@gmail.com", "Acme Labs"), "a stranger's freemail is not a contact"
    assert not _trusted("hr@othercorp.com", "Acme Labs"), "wrong domain must not be trusted"
    # The gate all six tiers pass. A vault row cached before the deny list existed, or a hunter/OSINT hit
    # that never saw JD prose, reaches the mailer through here and nowhere else.
    assert not _trusted("accessibleinterviewing@samsara.com", "Samsara", "https://samsara.com/j/1"), \
        "a cached accommodations desk is still an accommodations desk"
    assert not _trusted("legal@acmelabs.com", "Acme Labs", "https://acmelabs.com/j/1")
    assert not _trusted("", "Acme Labs") and not _trusted("notanemail", "Acme Labs")
    # Domain derivation: only what the row printed, never the company name squashed into a .com.
    assert _company_domain("Acme Labs", "https://careers.acmelabs.com/j/1") == "careers.acmelabs.com"
    assert _company_domain("Acme Labs", "https://www.linkedin.com/jobs/view/1") == "", \
        "the board's own host is not the company's mail domain"
    assert _company_domain("Acme Labs", "https://in.indeed.com/j/1", "apply via acmelabs.com") == \
        "acmelabs.com", "a domain the JD printed is evidence; a guessed one never is"
    assert _company_domain("Acme Labs", "", "we run Node.js and Python 3.14") == "", \
        "a filename-shaped token is not a domain"
    assert _company_domain("AcmeCorp", "", "great place to work") == "", "no guessing from the name"

    try:
        import tempfile
        vault_db = Path(tempfile.mkdtemp()) / "vault.db"     # the waterfall writes - keep jobs.db out
        await database.init_db(vault_db)
        config.DB_PATH = vault_db
        globals()["_hunter_lookup"] = lambda company, job_url="": \
            f"hr@{_company_key(company)}.com"                 # corroborates whatever firm is asked
        globals()["_dork_hr"] = _dork_stub
        globals()["_osint_email_search"] = _osint_stub
        acme = {"company": "Acme Labs", "job_url": "https://acmelabs.com/jobs/1"}
        assert await _resolve_hr(acme | {"hr_email": "known@acmelabs.com"}) == ("known@acmelabs.com", "db")
        assert await database.get_vault_email("acmelabs") == "known@acmelabs.com", \
            "a mailbox already on the row must backfill the vault for the next job at that employer"
        # One employer per tier: each hit lands in the vault, so reusing a company would short-circuit
        # the tier under test into a cache read.
        jd = {"company": "Jd Labs", "job_url": "https://jdlabs.com/j/1",
              "description": "mail careers@jdlabs.com"}
        assert await _resolve_hr(jd) == ("careers@jdlabs.com", "jd"), "tier 1 beats a paid credit"
        hunt = {"company": "Hunt Labs", "job_url": "https://huntlabs.com/j/1"}
        assert await _resolve_hr(hunt) == ("hr@huntlabs.com", "hunter"), "tier 2 when the JD is silent"
        globals()["_hunter_lookup"] = lambda company, job_url="": None
        dork = {"company": "Dork Labs", "job_url": "https://dorklabs.com/j/1"}
        assert await _resolve_hr(dork) == ("hr@dorklabs.com", "dork"), "tier 3 when Hunter misses"
        globals()["_dork_hr"] = _dork_dry
        osint = {"company": "Osint Labs", "job_url": "https://osintlabs.com/j/1"}
        assert await _resolve_hr(osint) == ("hr@osintlabs.com", "osint"), "tier 4 when the dork misses"
        globals()["_osint_email_search"] = _osint_dry
        dry = {"company": "Dry Labs", "job_url": "https://drylabs.com/j/1"}
        assert await _resolve_hr(dry) == (None, "none"), "all tiers dry means skip the email"
        assert await database.get_vault_email("drylabs") is None, "a miss must not be cached as a hit"

        # The point of the vault: the second job at a company already resolved pays nothing. Hunter is
        # replaced by a raiser, so a cache miss here fails loudly instead of quietly costing a credit.
        globals()["_hunter_lookup"] = _hunter_banned
        assert await _resolve_hr(hunt) == ("hr@huntlabs.com", "vault")
        assert await _resolve_hr({"company": "  HUNT LABS PVT LTD ", "job_url": ""}) == \
            ("hr@huntlabs.com", "vault"), "legal-form noise must land on the same vault row"
        # A cached mailbox that does not corroborate the company on the row is ignored, not mailed.
        await database.save_vault_email("stalelabs", "hr@totallyotherfirm.com", "osint")
        globals()["_hunter_lookup"] = lambda company, job_url="": None
        assert await _resolve_hr({"company": "Stale Labs", "job_url": "https://stalelabs.com/j/1"}) == \
            (None, "none"), "a mismatched cache hit must never reach the send path"
        assert await database.vault_size() == 6, await database.vault_size()
    finally:
        globals()["_hunter_lookup"], globals()["_dork_hr"] = real_hunter, real_dork
        globals()["_osint_email_search"] = real_osint
        config.DB_PATH = real_db

    # OSINT platform table and its one-prompt-per-process gate.
    assert [n for n, _u in _OSINT_SITES] == ["intelx", "maxintel", "osintframework"], _OSINT_SITES
    assert all(u.startswith("https://") for _n, u in _OSINT_SITES)
    assert _osint_state_path("intelx") == config.BROWSER_SESSIONS / "intelx.json"
    assert await _osint_email_search("", "") is None, "no selector at all means no browser is launched"
    # Every searchable site needs its own DOM contract, and the tree-directory one must be declined by
    # name - not left to a generic probe that would ask for a login and then find nothing.
    assert set(_OSINT_DOM) | set(_OSINT_SKIP) == {n for n, _u in _OSINT_SITES}, \
        "a platform is neither navigable nor explicitly skipped"
    assert not set(_OSINT_DOM) & set(_OSINT_SKIP), "a skipped platform must not also carry a contract"
    assert "osintframework" in _OSINT_SKIP and "manual node navigation" in _OSINT_SKIP["osintframework"]
    for _site, _dom in _OSINT_DOM.items():
        assert set(_dom) == {"url", "search", "spinner", "filters", "results"}, (_site, sorted(_dom))
        assert _dom["search"] and _dom["results"], _site
        assert "body" not in _dom["results"], f"{_site} reads the whole page, not its results"
        assert not _dom["url"] or "{q}" in _dom["url"], f"{_site} direct url drops the query"
    assert "?s=" in _OSINT_DOM["intelx"]["url"], "intelx loses its direct search url"
    assert any("Email" in f for f in _OSINT_DOM["intelx"]["filters"]), "the Emails bucket is not targeted"

    # Tier 3 guard: a corroborated host is not enough, the mailbox has to be a hiring desk. These are
    # the exact addresses the live dork surfaced on real Pune companies, and all of them must fail.
    assert _guarded_hr("mail careers@acmelabs.com", "Acme Labs", "https://acmelabs.com/j/1") == \
        "careers@acmelabs.com"
    assert _guarded_hr("info@persistent.com", "Persistent Systems", "") is None, "info@ is not HR"
    assert _guarded_hr("whistleblower@cybage.com business@cybage.com", "Cybage Software", "") is None
    assert _guarded_hr("hr@totallyotherfirm.com", "Acme Labs", "") is None, "wrong domain"
    assert _SERP_ENGINES[0][0] == "google", "the dork must try Google before any fallback engine"
    assert all(base.startswith("https://") for _n, base in _SERP_ENGINES)

    listing = (
        '<div class="container-fluid individual_internship" data-href="/job/detail/jr-devops-acme-1">'
        '<h3 class="job-internship-name"><a class="job-title-href" href="/job/detail/jr-devops-acme-1">'
        'Junior DevOps Engineer</a></h3><p class="company-name">Acme Cloud Labs</p>'
        '<div class="other_detail_item salary_container"><div class="item_body salary">'
        '<span class="desktop">₹ 6,00,000 - 7,20,000</span>'
        '<span class="mobile">₹ 6,00,000 - 7,20,000 /year</span></div></div>'
        '<div class="status-success"><i class="ic-16-reschedule"></i><span>1 day ago</span></div></div>'
        '<div class="container-fluid individual_internship" data-href="/job/detail/stale-2">'
        '<a class="job-title-href" href="/x">Cloud Support Associate</a>'
        '<p class="company-name">Old Corp</p>'
        '<div class="status-inactive"><i class="ic-16-reschedule"></i><span>9 days ago</span></div></div>'
    )
    cards = _internshala_cards(listing)
    assert len(cards) == 2, cards
    fresh, stale = cards
    assert fresh["title"] == "Junior DevOps Engineer" and fresh["company"] == "Acme Cloud Labs"
    assert fresh["job_url"] == f"{_INTERNSHALA}/job/detail/jr-devops-acme-1"
    assert fresh["date_posted"] == "1 day ago" and stale["date_posted"] == "9 days ago"
    assert parsing.lpa(_salary(fresh)) == 7.2, "the mobile salary span is the parseable one"
    ish = _row_to_job(fresh)
    assert ish["platform"] == "internshala" and ish["date_posted"] == \
        (parsing.date.today() - parsing.timedelta(days=1)).isoformat()
    assert parsing.eligible(ish)[0] is True
    assert parsing.eligible(_row_to_job(stale))[0] is False, "9 days old must not reach a detail fetch"
    assert _internshala_cards("<html>nothing here</html>") == []

    # Real Naukri SRP markup, copied off a live logged-in fetch of /junior-devops-engineer-jobs-in-pune.
    srp = (
        '<div class="srp-jobtuple-wrapper" data-job-id="011225501372">'
        '<a class="title " title="Junior DevOps Engineer" target="_blank"'
        ' href="https://www.naukri.com/job-listings-junior-devops-engineer-acme-011225501372">'
        'Junior DevOps Engineer</a>'
        '<a class=" comp-name " title="Acme Cloud Labs" href="https://www.naukri.com/acme-jobs-careers-1">'
        'Acme Cloud Labs</a>'
        '<span title="0-2 Yrs " class="expwdth">0-2 Yrs</span>'
        '<span class="ni-job-tuple-icon ni-job-tuple-icon-srp-rupee sal">'
        '<span title="8-12 Lacs PA " class="">8-12 Lacs PA</span></span>'
        '<span title="Pune, Bengaluru" class="locWdth">Pune, Bengaluru</span>'
        '<span class="job-desc ni-job-tuple-icon ni-job-tuple-icon-srp-description">'
        'CI/CD pipelines, Docker, Kubernetes, AWS.</span>'
        '<span class="job-post-day ">1 day ago</span></div>'
        '<div class="srp-jobtuple-wrapper" data-job-id="011225501999">'
        '<a class="title " title="Senior DevOps Architect"'
        ' href="https://www.naukri.com/job-listings-senior-devops-architect-old-011225501999">x</a>'
        '<a class=" comp-name " title="Old Corp" href="https://www.naukri.com/old-jobs-careers-2">Old</a>'
        '<span class="expwdth" title="8-12 Yrs ">8-12 Yrs</span>'   # attrs in the other order on purpose
        '<span class="job-post-day ">3+ weeks ago</span></div>'
    )
    nk = _naukri_cards(srp)
    assert len(nk) == 2, nk
    good, bad = nk
    assert good["title"] == "Junior DevOps Engineer" and good["company"] == "Acme Cloud Labs"
    assert good["job_url"].endswith("011225501372") and good["site"] == "naukri"
    assert good["location"] == "Pune, Bengaluru" and good["date_posted"] == "1 day ago"
    assert "0-2 years" in good["description"] and "Kubernetes" in good["description"]
    assert parsing.lpa(_salary(good)) == 12, _salary(good)
    nkj = _row_to_job(good)
    assert nkj["platform"] == "naukri" and parsing.eligible(nkj)[0] is True
    assert nkj["date_posted"] == (parsing.date.today() - parsing.timedelta(days=1)).isoformat()
    assert parsing.eligible(_row_to_job(bad))[0] is False, "8-12 Yrs, 3+ weeks old: two reasons to drop"
    assert _naukri_cards("<html>406 recaptcha required</html>") == []
    unpaid = _naukri_cards(
        '<div class="srp-jobtuple-wrapper"><a class="title " title="Intern" href="https://x/1">i</a>'
        '<span class="ni-job-tuple-icon-srp-rupee sal"><span title="Unpaid ">Unpaid</span></span></div>')
    assert parsing.lpa(_salary(unpaid[0])) == 0, "an unpaid role must read as 0, not undisclosed"
    assert parsing.eligible(_row_to_job(unpaid[0]))[0] is False, "unpaid must not reach the queue"
    assert _naukri_url("Junior DevOps Engineer", "Pune, India") == \
        f"{_NAUKRI}/junior-devops-engineer-jobs-in-pune"
    assert _naukri_url("DevOps Engineer", "Remote") == f"{_NAUKRI}/devops-engineer-jobs"
    assert _slug("Junior DevOps / SRE Engineer!") == "junior-devops-sre-engineer"

    # The attribution bug, at its root: _row_to_job reads exactly ONE key to pick the DB platform, so
    # a single-board scraper that returns rows without it gets them written under some other board.
    assert _stamp([{"title": "x"}, {"site": "indeed"}], "naukri") == \
        [{"title": "x", "site": "naukri"}, {"site": "naukri"}], "a fallback row must carry its own board"
    naukri_row = _stamp([{"title": "t", "company": "c", "job_url": "https://x/1"}], "naukri")[0]
    assert _row_to_job(naukri_row)["platform"] == "naukri", "a naukri row must never read as indeed"

    # Every board that has no working HTTP backend needs a browser fallback, and every fallback needs
    # a full selector table - a missing field silently harvests None and the gate then passes it.
    assert set(_PW_BOARDS) == set(config.PLATFORMS) - {"naukri"}, sorted(_PW_BOARDS)
    assert set(_PW_SEARCH) == set(config.SCRAPE_SITES), sorted(_PW_SEARCH)
    assert set(_PW_SEARCH) | set(_PW_BOARDS) == set(config.PLATFORMS), "no board without a fallback"
    for name, board in _PW_BOARDS.items():
        assert {"search", "ready", "card", "url", "fields", "jd"} <= set(board), name
        assert {"title", "company", "location", "date_posted"} <= set(board["fields"]), name
        assert all(sels for sels in board["fields"].values()), name
        url = board["search"]("Junior DevOps Engineer", "Pune, India")
        assert url.startswith("https://") and " " not in url, (name, url)
    assert "f_TPR=r86400" in _PW_BOARDS["linkedin"]["search"]("devsecops", "Pune"), "24h SRP filter"
    assert "in.indeed.com" in _PW_BOARDS["indeed"]["search"]("devsecops", ""), "the .com host redirects"
    assert _PW_BOARDS["internshala"]["search"]("Junior DevOps Engineer") == \
        f"{_INTERNSHALA}/jobs/keywords-junior-devops-engineer/"
    assert _walled("https://www.linkedin.com/authwall?x=1") and _walled("https://x/uas/login")
    assert not _walled(_PW_BOARDS["linkedin"]["search"]("devsecops", "Pune")), "an SRP is not a wall"
    assert not _walled(_PW_BOARDS["indeed"]["search"]("devsecops", "Pune")) and not _walled("")

    # The harvester's Python half, against exactly what page.evaluate hands back.
    harvest = [
        {"job_url": "https://in.indeed.com/viewjob?jk=1&from=web", "title": " Junior DevOps Engineer ",
         "company": "Acme", "location": "Pune", "date_posted": "1 day ago",
         "salary_text": "8-12 Lacs PA"},
        {"job_url": "https://in.indeed.com/viewjob?jk=1&from=web", "title": "same url again"},
        {"job_url": None, "title": "no url"},
        {"job_url": "https://in.indeed.com/viewjob?jk=2", "title": "  "},
        {"job_url": "https://in.indeed.com/viewjob?jk=3", "title": "Intern", "salary_text": "Unpaid"},
    ]
    harvested = _pw_rows("indeed", harvest)
    assert len(harvested) == 2, harvested
    assert harvested[0]["title"] == "Junior DevOps Engineer" and harvested[0]["site"] == "indeed"
    assert parsing.lpa(_salary(harvested[0])) == 12, _salary(harvested[0])
    assert _row_to_job(harvested[0])["platform"] == "indeed"
    assert parsing.eligible(_row_to_job(harvested[0]))[0] is True
    assert parsing.lpa(_salary(harvested[1])) == 0, "unpaid is a stated zero, not missing data"
    assert parsing.eligible(_row_to_job(harvested[1]))[0] is False, "unpaid must not reach the queue"
    assert _pw_rows("linkedin", []) == [] and _pw_rows("linkedin", None) == []
    for key in ("card", "url", "fields"):
        assert key in _CARD_JS, f"the harvester must read cfg.{key}"

    quota, plan = config.MAX_DAILY_APPLICATIONS, _plan(config.SEARCH_TERMS, config.SEARCH_LOCATIONS)
    per = max(1, quota // len(config.PLATFORMS))
    assert per * len(config.PLATFORMS) <= quota, (per, quota)
    assert per >= 1 and per == 20, per                      # 80 // 4, the documented per-board share
    assert set(config.ROLES) == set(config.ROLE_TARGETS), "every family needs a target"
    assert sum(config.ROLE_TARGETS.values()) == config.MAX_DAILY_APPLICATIONS, \
        f"the role targets must spend the whole day: {config.ROLE_TARGETS}"
    assert len(config.SEARCH_TERMS) == 4 * len(config.SOFTWARE_TERMS), \
        "the four term tuples must be equal length or zip drops the tail"
    role_terms = [parsing.role_of({"title": t}) for t in config.SEARCH_TERMS[:4]]
    assert role_terms == list(config.ROLES), \
        f"each cycle of four terms must cover all four families in order, got {role_terms}"
    assert {parsing.role_of({"title": t}) for t in config.SEARCH_TERMS} == set(config.ROLES)
    assert {k for k, _t, _l in plan} == {"internshala", "jobspy"}
    assert plan[0] == ("internshala", config.SEARCH_TERMS[0], "")
    assert len(plan) == len(config.SEARCH_TERMS) * (1 + len(config.SEARCH_LOCATIONS))
    assert set(config.SCRAPE_SITES) < set(config.PLATFORMS), "jobspy covers all but internshala"
    assert set(config.PLATFORMS) - set(config.SCRAPE_SITES) == {"internshala"}

    # LinkedIn outreach. Offline: a fake page answering only the locators a given surface really exposes,
    # so the connect-note-first / message-fallback branching is proven without touching linkedin.com.
    #
    # A chained locator is keyed by its whole path, joined with " >> ": page.locator("main") ->
    # .get_by_role("link", name=re) becomes "main >> link=<pattern>". _key() below builds the same string
    # a fixture declares, so a fixture cannot claim a control the production code does not look up there.
    def _key(*parts: str) -> str:
        return " >> ".join(parts)

    MAIN = _MAIN
    SLUG = "x"                        # the fake page url is /in/x/, so this is what _LI_PROFILE yields
    CONNECT = _key(MAIN, _CONNECT_SEL.format(slug=SLUG))     # the href door, ie the current build
    CONNECT_BTN = _key(MAIN, f"button={_CONNECT_RE.pattern}")  # the older <button>Connect</button>
    MESSAGE = _key(MAIN, f"link={_MESSAGE_RE.pattern}")
    MORE = _key(MAIN, f"button={_MORE_RE.pattern}")
    # Two distinct menu doors, and the keys must stay distinct: the menu is portaled OUT of <main>, so the
    # href door inside it is scoped to _MENU_SEL, never to MAIN. A fixture that only declared CONNECT
    # would prove nothing about the menu layout.
    MENU_CONNECT_HREF = _key(_MENU_SEL, _CONNECT_SEL.format(slug=SLUG))
    MENU_CONNECT = _key(_MENU_SEL, f"menuitem={_CONNECT_RE.pattern}")
    NOTE_BTN = _key(_DIALOG, _NOTE_SEL)
    SEND_INVITE = _key(_DIALOG, _SEND_INVITE)
    # Page-level probes, so their key is the bare selector with no " >> " path. PENDING is read through
    # _visible and must never turn up in page.clicks; PAYWALL and DISMISS drive the arrival-upsell clear.
    PENDING = _PENDING_SEL.format(slug=SLUG)
    PAYWALL = _PAYWALL[0]
    DISMISS = _DISMISS[0]

    class _Loc:
        def __init__(self, page, sel, live, parts=()):
            self.page, self.sel, self.live = page, sel, live
            self.parts = parts or (sel,)
            self.detached = False

        def _child(self, part):
            parts = (*self.parts, part)
            key = _key(*parts)
            return _Loc(self.page, key, key in self.page.live, parts)

        def locator(self, sel):
            return self._child(sel)

        def get_by_role(self, role, name=None, exact=False):
            return self._child(f"{role}={getattr(name, 'pattern', name)}")

        def filter(self, has=None, has_text=None):
            if has_text is not None:
                return self._child(f"text={getattr(has_text, 'pattern', has_text)}")
            return self._child("has")

        @property
        def first(self):
            return self

        async def wait_for(self, state="visible", **_kw):
            if state == "detached":
                if self.sel in self.page.stuck:
                    raise RuntimeError(f"still attached: {self.sel}")
                return
            if not self.live:
                raise RuntimeError(f"not visible: {self.sel}")

        async def click(self):
            await self.wait_for()
            self.page.clicks.append(self.sel)

        async def fill(self, text):
            await self.wait_for()
            if self.sel in _INMAIL_SUBJECT:
                self.page.subject = text
            else:
                self.page.filled = text

        async def count(self):
            return 1 if self.live else 0

        async def inner_text(self):
            return "jd body " + "x" * 300 if self.live else ""

    class _Page:
        def __init__(self, live: tuple[str, ...], url="https://www.linkedin.com/in/x/", no_fill=False,
                     sticky=False, cards=(), stuck=()):
            self.live, self.url, self.no_fill = live, url, no_fill
            self.sticky = sticky        # sticky means goto() lands somewhere else, ie a redirect
            self.cards = list(cards)    # what _HARVEST_JS / _CARD_JS would return off a rendered SRP
            self.stuck = stuck          # locators that never detach, ie a dialog LinkedIn refused to close
            self.clicks: list[str] = []
            self.pressed: list[str] = []   # keyboard.press, ie the Escape that closes a stuck overlay
            self.filled = None
            self.subject = None
            self.typed = None
            self.closed = False
            self.shots: list[str] = []
            self.keyboard = self

        def locator(self, sel):
            loc = _Loc(self, sel, sel in self.live)
            if self.no_fill and sel in _MSG_BOX:
                loc.fill = self._refuse
            return loc

        async def evaluate(self, _js, _arg=None):
            return self.cards

        async def _refuse(self, _text):
            raise RuntimeError("fill() refused on contenteditable")

        async def type(self, text, delay=0):
            self.typed = text

        async def press(self, key):
            self.pressed.append(key)

        async def screenshot(self, path, full_page=False):
            self.shots.append(str(path))

        def set_default_timeout(self, _ms): pass

        async def goto(self, url, **_kw):
            if not self.sticky:
                self.url = url

        async def wait_for_selector(self, _sel, **_kw): pass

        async def wait_for_load_state(self, _state="load", **_kw):
            # Always raises, because on the real site it always does: LinkedIn holds polls and sockets
            # open, so networkidle times out on every profile. Every case below therefore proves that
            # the best-effort wait is swallowed rather than turned into a failed outreach.
            raise TimeoutError(f"{_state} never reached")

        async def close(self): self.closed = True

    _real_human = globals()["_human"]

    async def _no_wait(lo=0.0, hi=0.0):
        return None

    async def _done(value):
        """Wrap a value in an awaitable, so a stub can stand in for `await _context(...)`."""
        return value

    globals()["_human"] = _no_wait
    try:
        # The mandated happy path: Connect inside main -> Add a note -> textarea -> Send invitation.
        page = _Page((MAIN, CONNECT, _DIALOG, NOTE_BTN, _NOTE_BOX, SEND_INVITE))
        ok, msg = await _dm_on_page(page, "x" * 400)
        assert (ok, msg) == (True, "Connected with personalized note"), (ok, msg)
        assert len(page.filled) == NOTE_LIMIT, f"note must be capped at {NOTE_LIMIT}: {len(page.filled)}"
        assert MESSAGE not in page.clicks, "the free connect path must not fall through to InMail"

        # The Follow-promoted layout, measured live: the action bar is Message / Follow / More, Connect is
        # demoted into the overflow menu, and that menu is PORTALED OUT of <main>. So main-scoped Connect
        # is legitimately 0 here (the fixture withholds CONNECT to say exactly that), the menu item is
        # reached under _MENU_SEL, and it is keyed by the same vanityName href - because the item reads
        # "Connect" with no aria-label yet an anchored menuitem name match resolves to 0 page-wide.
        page = _Page((MAIN, MORE, MENU_CONNECT_HREF, _DIALOG, NOTE_BTN, _NOTE_BOX, SEND_INVITE))
        ok, msg = await _dm_on_page(page, "note body")
        assert (ok, msg) == (True, "Connected with personalized note"), (ok, msg)
        assert page.clicks[:2] == [MORE, MENU_CONNECT_HREF], page.clicks
        assert CONNECT not in page.clicks, "the main-scoped door must not be credited on the menu layout"

        # The older menu, which did name its item. Kept as a fallback behind the href, and anchored: the
        # menu's first entry is "Send profile in a message", so a loose match shares the profile instead.
        page = _Page((MAIN, MORE, MENU_CONNECT, _DIALOG, NOTE_BTN, _NOTE_BOX, SEND_INVITE))
        ok, msg = await _dm_on_page(page, "note body")
        assert ok and page.filled == "note body", (ok, msg, page.filled)
        assert page.clicks[:2] == [MORE, MENU_CONNECT], page.clicks

        # ... and when the menu carries no connect item at all, that door misses cleanly and Message is
        # tried. Neither menu key may be credited.
        page = _Page((MAIN, MORE, MESSAGE, _MSG_BOX[0], _SEND_MSG[0]))
        ok, msg = await _dm_on_page(page, "body")
        assert (ok, msg) == (True, "Direct Message sent"), (ok, msg)
        assert not {MENU_CONNECT, MENU_CONNECT_HREF} & set(page.clicks), page.clicks
        # That miss leaves the overflow menu OPEN on top of the action bar, so it is dismissed with Escape
        # before Message is tried - otherwise the menu swallows the click.
        assert page.pressed == ["Escape"], page.pressed

        # An invite already outstanding from an earlier job at the same company. The bar swaps Connect for
        # a Pending anchor, so every connect door legitimately misses - and that is a reached recruiter, not
        # a failure, so the row must not land as failed with a screenshot on a page where nothing is broken.
        page = _Page((MAIN, PENDING))
        ok, msg = await _dm_on_page(page, "body")
        assert ok and "already pending" in msg, (ok, msg)
        # Read, never clicked: the anchor's aria-label is "Pending, click to withdraw invitation", so a
        # click here retracts the invite that already went out.
        assert PENDING not in page.clicks, "the pending anchor must be probed, never pressed"

        # Pending is only ever consulted on the miss path - a profile that connects normally must not pay
        # a probe for it, and must not be reported as pending.
        page = _Page((MAIN, CONNECT, PENDING, _DIALOG, NOTE_BTN, _NOTE_BOX, SEND_INVITE))
        ok, msg = await _dm_on_page(page, "note body")
        assert (ok, msg) == (True, "Connected with personalized note"), (ok, msg)

        # The Premium upsell, measured live sitting OVER a recruiter's profile: "Job Search Smarter with
        # Premium" / "Reactivate Premium: 50% Off". It is cleared on arrival, BEFORE either channel runs -
        # _DIALOG is :visible-filtered, so left up it would win the invite flow's .first and the flow would
        # hunt for "Add a note" inside an upsell, while the message channel reported the unactionable
        # "message composer never appeared" about a composer that was simply behind it.
        page = _Page((MAIN, PAYWALL, DISMISS, CONNECT, _DIALOG, NOTE_BTN, _NOTE_BOX, SEND_INVITE))
        ok, msg = await _dm_on_page(page, "note body")
        assert (ok, msg) == (True, "Connected with personalized note"), (ok, msg)
        assert page.clicks[0] == DISMISS, f"the upsell must be closed first: {page.clicks}"
        # 'Premium' alone, not 'Try Premium': that copy matched none of the three headlines LinkedIn ships.
        assert any(":has-text('Premium')" in p for p in _PAYWALL), _PAYWALL

        # ... and with no Dismiss button on it, Escape closes it rather than the flow giving up.
        page = _Page((MAIN, PAYWALL, CONNECT, _DIALOG, NOTE_BTN, _NOTE_BOX, SEND_INVITE))
        ok, _ = await _dm_on_page(page, "note body")
        assert ok and page.pressed == ["Escape"], (ok, page.pressed)

        # The rail's Connect must stay unreachable. A page whose ONLY Connect is a name match is what
        # "People similar to <person>" ships, and matching it opened an invite dialog for an unrelated third
        # party on a live already-Followed Accenture recruiter. Nothing may be clicked and nothing sent.
        page = _Page((MAIN, CONNECT_BTN, _DIALOG, NOTE_BTN, _NOTE_BOX, SEND_INVITE))
        ok, msg = await _dm_on_page(page, "note body")
        assert not ok, (ok, msg)
        assert CONNECT_BTN not in page.clicks, \
            f"a stranger's invite dialog was opened from the recommendation rail: {page.clicks}"

        # The href door is keyed off the url the flow is standing on, so a regional host, a query string
        # and a trailing slash must all still yield the same vanityName selector.
        page = _Page((MAIN, CONNECT, _DIALOG, NOTE_BTN, _NOTE_BOX, SEND_INVITE),
                     url="https://in.linkedin.com/in/x/?trk=nav")
        assert (await _dm_on_page(page, "n"))[0], "a query string must not break the vanityName key"

        # A url that is not a profile at all must SKIP the href door rather than format an empty slug into
        # it: a[href*='vanityName='] with nothing after it matches every anchor in the recommendation rail.
        # The fixture declares that empty-slug selector live precisely to prove it is never asked for.
        page = _Page((MAIN, _key(MAIN, _CONNECT_SEL.format(slug="")), MESSAGE, _MSG_BOX[0], _SEND_MSG[0]),
                     url="https://www.linkedin.com/company/acme/")
        ok, _ = await _dm_on_page(page, "body")
        assert ok and not any("custom-invite" in c for c in page.clicks), \
            "an empty slug must never be formatted into the invite selector"

        # Direct-send dialog (no note affordance): the invite still goes, and the Message channel is
        # still tried, because an invite is not a delivered message.
        page = _Page((MAIN, CONNECT, _DIALOG, SEND_INVITE, MESSAGE, _MSG_BOX[0], _SEND_MSG[0]))
        ok, msg = await _dm_on_page(page, "body text")
        assert (ok, msg) == (True, "Direct Message sent"), (ok, msg)
        assert page.filled == "body text" and SEND_INVITE in page.clicks, (page.filled, page.clicks)

        # Same, but Message is unavailable too: the note-less invite is reported honestly, not as failure.
        page = _Page((MAIN, CONNECT, _DIALOG, SEND_INVITE))
        ok, msg = await _dm_on_page(page, "body text")
        assert ok and "without a note" in msg, (ok, msg)

        # Connect absent entirely -> Message fallback, capped at DM_LIMIT.
        page = _Page((MAIN, MESSAGE, _MSG_BOX[0], _SEND_MSG[0]))
        ok, msg = await _dm_on_page(page, "x" * 400)
        assert (ok, msg) == (True, "Direct Message sent"), (ok, msg)
        assert len(page.filled) == DM_LIMIT, f"DM must be capped at {DM_LIMIT}: {len(page.filled)}"

        # fill() refused on the contenteditable div -> keyboard fallback still delivers the text.
        page = _Page((MAIN, MESSAGE, _MSG_BOX[0], _SEND_MSG[0]), no_fill=True)
        ok, _ = await _dm_on_page(page, "hello there")
        assert ok and page.typed == "hello there", (ok, page.typed)

        # Invite accepted by the clicks but the dialog never detached: LinkedIn refused it (weekly cap),
        # so it must not be reported as sent - and the message channel is tried instead.
        page = _Page((MAIN, CONNECT, _DIALOG, NOTE_BTN, _NOTE_BOX, SEND_INVITE), stuck=(_DIALOG,))
        ok, msg = await _dm_on_page(page, "note body")
        assert not ok and "stayed open after send" in msg, (ok, msg)

        # Both doors shut. _dm_on_page reports both reasons; the screenshot belongs to the caller now.
        page = _Page((MAIN,))
        ok, msg = await _dm_on_page(page, "x")
        assert not ok and msg.startswith("neither connect-note nor message succeeded"), (ok, msg)
        assert "no connect control" in msg and "no message button" in msg, msg
        assert page.shots == [], "_dm_on_page must not screenshot - the entry points own that"

        global _DM_LOG
        import tempfile
        _DM_LOG = Path(tempfile.mkdtemp()) / "dm_attempted.json"

        who = "https://www.linkedin.com/in/someone-real"
        _dm_mark(who)
        _DM_DONE.clear()
        ok, msg = await send_linkedin_dm(who, "hello")
        assert not ok and "already attempted" in msg, (ok, msg)
        config.DRY_RUN, was = True, config.DRY_RUN
        try:
            ok, _ = await send_linkedin_dm(who, "hello")
        finally:
            config.DRY_RUN = was
        assert not ok, "a rehearsal must report the same refusal a live run would"
        assert (await send_linkedin_dm("https://www.linkedin.com/in/never-touched", ""))[0] is False
        _DM_DONE.clear()

        # Hydration: main never became visible, so this RAISES rather than returning tidily, and the
        # exception is what the entry point's net converts into a screenshot.
        page = _Page(())
        try:
            await _dm_on_page(page, "x")
            raise AssertionError("a main that never hydrates must raise, not return False")
        except AssertionError:
            raise
        except Exception as exc:
            assert "main" in str(exc), exc
        assert not page.clicks, "nothing may be clicked before main hydrates"

        # Composer opens but send is missing -> failure, never a silent "sent".
        page = _Page((MAIN, MESSAGE, _MSG_BOX[0]))
        ok, msg = await _dm_on_page(page, "x")
        assert not ok and "send button missing" in msg, msg
        assert page.filled == "x", "text was typed, and the missing send button is the reported failure"

        # InMail paywall: recognised, dismissed, never filled. A typed-and-submitted upsell sends nothing.
        page = _Page((MAIN, MESSAGE, _PAYWALL[0], _DISMISS[0], _MSG_BOX[0], _SEND_MSG[0]))
        ok, msg = await _dm_on_page(page, "must not be typed")
        assert not ok and page.filled is None and page.typed is None, (msg, page.filled, page.typed)
        assert _DISMISS[0] in page.clicks and not any(s in page.clicks for s in _SEND_MSG), page.clicks

        # The send button on the current composer is an icon-only circle: no text, no aria-label, and a
        # class one rename away from the old one. Both spellings must send, because _SEND_MSG carries both
        # and a profile served the old layout would otherwise report "send button missing" with the text
        # already typed - the one failure that looks like a bug in the message rather than in the selector.
        for send_key in (_SEND_MSG[0], _SEND_MSG[2]):
            page = _Page((MAIN, MESSAGE, _MSG_BOX[0], send_key))
            ok, msg = await _dm_on_page(page, "body", company="Acme")
            assert (ok, msg) == (True, "Direct Message sent"), (send_key, ok, msg)
            assert send_key in page.clicks, (send_key, page.clicks)

        # An unaddressed drawer must be caught even though the composer is present: nothing may be typed
        # into a composer addressed to nobody, whichever order the two surfaces render in.
        page = _Page((MAIN, MESSAGE, _MSG_BOX[0], _DRAWER_RECIPIENT[0], _SEND_MSG[0]))
        ok, msg = await _dm_on_page(page, "must not be typed")
        assert not ok and "no recipient" in msg, msg
        assert page.filled is None and not any(s in page.clicks for s in _SEND_MSG), (page.filled,
                                                                                     page.clicks)

        # Premium InMail: the subject line is mandatory, and company wins over role when both are known.
        page = _Page((MAIN, MESSAGE, _INMAIL_SUBJECT[0], _MSG_BOX[2], _SEND_MSG[0]))
        ok, _ = await _dm_on_page(page, "body text", "DevSecOps Engineer", "Acme Labs")
        assert ok and page.subject == "Regarding open roles at Acme Labs", page.subject
        page = _Page((MAIN, MESSAGE, _INMAIL_SUBJECT[0], _MSG_BOX[2], _SEND_MSG[0]))
        ok, _ = await _dm_on_page(page, "body text", "DevSecOps Engineer")
        assert ok and page.subject == "Regarding the DevSecOps Engineer role", page.subject
        page = _Page((MAIN, MESSAGE, _INMAIL_SUBJECT[0], _MSG_BOX[2], _SEND_MSG[0]))
        assert (await _dm_on_page(page, "body text"))[0] and page.subject is None, \
            "no role and no company means no subject line to invent"

        # The messaging drawer is a sibling of main, so its own compose control is unreachable by
        # construction - but if clicking Message opens it anyway, nothing may be typed into it.
        page = _Page((MAIN, MESSAGE, _DRAWER_RECIPIENT[1], _MSG_BOX[0], _SEND_MSG[0]))
        ok, msg = await _dm_on_page(page, "must not be typed")
        assert not ok and page.filled is None and page.typed is None, "typed into an unaddressed drawer"
        assert not any(s in page.clicks for s in _SEND_MSG), "send was clicked on an empty drawer"

        # Scoping, statically: every outreach trigger is resolved through main_area, never through page,
        # and main is waited for before anything is hunted for inside it.
        src = Path(__file__).read_text(encoding="utf-8")
        flow = src.split("async def _open_connect(", 1)[1].split("\nasync def _dm_on_page(", 1)[0]
        for trigger in ("_CONNECT_RE", "_MESSAGE_RE", "_MORE_RE"):
            assert f"page.locator(\"button\").filter(has_text={trigger}" not in src, \
                f"{trigger} must be looked up inside main by accessible name, never by button text"
        for door in ("_CONNECT_SEL.format(slug=",
                     "_click_named(main_area, _MORE_RE)", "_click_named(main_area, _MESSAGE_RE)"):
            assert door in flow, f"missing door: {door}"
        # Connect is slug-keyed or it does not exist. A name match scoped to main reaches the "People
        # similar to <person>" rail, and an invite is irreversible outreach to someone no job named.
        assert "_click_named(main_area, _CONNECT_RE)" not in flow, \
            "a name-matched Connect door is back; it opens the recommendation rail's invite for a stranger"
        assert flow.count("page.locator") == 2, "only the dialog and the overflow menu are page-level"
        named = src.split("async def _click_named(", 1)[1].split("\nasync def _hydrated_main(", 1)[0]
        assert 'for role in ("link", "button")' in named, \
            "the current build ships Connect/Message as <a href>, so link must be tried first"
        hyd = src.split("async def _hydrated_main(", 1)[1].split("\nasync def _open_connect(", 1)[0]
        assert f"wait_for(state=\"visible\", timeout=_HYDRATE_TIMEOUT)" in hyd, hyd
        assert "networkidle" in hyd and "except Exception:" in hyd, "networkidle must be best-effort"
        assert _HYDRATE_TIMEOUT == 15_000 and _IDLE_TIMEOUT == 5_000
        assert "page.screenshot" not in flow and "page.screenshot" not in src.split(
            "async def _dm_on_page(", 1)[1].split("\nasync def _debug_shot(", 1)[0], \
            "the screenshot net lives in the entry points, not in the flow"
        assert _CONNECT_RE.match(" Connect ") and not _CONNECT_RE.match("Connect with Premium")
        assert _MESSAGE_RE.match("Message") and not _MESSAGE_RE.match("Message Priya Nair")
        assert _MORE_RE.match(" More ") and not _MORE_RE.match("More profiles for you"), \
            "the feed's post-expansion buttons and the rail heading both start with More"
        assert not _CONNECT_RE.match("Invite Preeti S. to connect"), \
            "the primary and the rail share that aria-label shape; only the href tells them apart"
        # The overflow menu, measured on the Follow-promoted layout. Every alternative in these two comma
        # lists must be :visible-filtered, because Playwright resolves a comma list in DOM ORDER and
        # _click/_seen then wait for visibility on whatever won: a profile with an embedded video ships
        # hidden vjs-modal-dialog role=dialog nodes AND a hidden artdeco dropdown div earlier in the
        # document, and either one stealing .first made the real control read as absent.
        assert ":visible" in _DIALOG, _DIALOG
        assert all(":visible" in part for part in _MENU_SEL.split(",")), _MENU_SEL
        # That menu is PORTALED OUT of <main> (main >> [role=menu] counts 0), and its Connect item has no
        # usable accessible name - it reads "Connect", carries no aria-label, and an anchored menuitem name
        # match resolves to 0 page-wide. So the menu door must key on the vanityName href, with the name
        # match kept only as the legacy fallback behind it.
        door3 = flow.split("_click_named(main_area, _MORE_RE)", 1)[1]
        assert door3.index("menu.locator(href)") < door3.index('get_by_role("menuitem"'), \
            "the menu door must try the vanityName href before the name match, which measures 0"
        # Pending: read-only, and slug-keyed like door 1 because the rail lives inside <main> and would carry
        # other people's pending invites. The aria-label reads "Pending, click to withdraw invitation", so a
        # click retracts an invite that already went out - it may only ever reach _visible.
        assert "_visible(page, (_PENDING_SEL" in flow, \
            "Pending must be probed through _visible only, never clicked"
        assert "{slug}" in _PENDING_SEL and "/in/" in _PENDING_SEL, _PENDING_SEL
        # The arrival upsell is cleared before _DIALOG is ever used to find the invite modal; with it still
        # up, :visible would hand the invite flow this modal instead. Cleared a SECOND time before the
        # message channel, because "Add a note" with the monthly allowance spent summons one of its own -
        # measured live intercepting a Message button that was plainly on the action bar.
        dm_flow = src.split("async def _dm_on_page(", 1)[1].split("\nasync def ", 1)[0]
        assert dm_flow.index("_clear_paywall(page)") < dm_flow.index("_connect_with_note"), \
            "the upsell must be closed before either channel runs"
        assert dm_flow.count("_clear_paywall(page)") == 2 and \
            dm_flow.index("_connect_with_note") < dm_flow.rindex("_clear_paywall(page)") \
            < dm_flow.index("_message_fallback"), \
            "the note path's own upsell is left on screen and intercepts the Message button"
        # The note textarea lands in the same slow shadow-root overlay as its dialog, so it gets _fill_first's
        # own _DM_TIMEOUT default. Passing _ACT_TIMEOUT reported "the note textarea never appeared" at 4s.
        assert "_fill_first(page, (_NOTE_BOX,), note_text)" in flow, \
            "the note fill must not be given the click-sized timeout"
        # The invite modal renders IN PLACE, url unchanged, inside a shadow root - and it takes seconds, so
        # its dialog gets the nav-sized wait. With _ACT_TIMEOUT here the flow gave up while it was rendering.
        assert "_seen(dialog, _DM_TIMEOUT)" in flow and _DM_TIMEOUT > _ACT_TIMEOUT
        # The composer's send button is an icon-only circle with no text and no aria-label, so it can only
        # be reached by class or by form-scoped submit. A bare button[type=submit] resolves to 3 with the
        # composer open, and _click_first walks the list in order, so it must never be a candidate.
        assert _SEND_MSG[0] == "button.msg-form__send-btn", _SEND_MSG
        assert "button[type='submit']" not in _SEND_MSG, \
            "unscoped submit hits 3 controls on a profile with the composer open"
        # The composer must be WAITED for before the drawer guard answers: that guard is what stops a
        # message being typed into a composer addressed to nobody, and it was answering in 1.5s about a
        # surface measured to take over 3s to render.
        msg_flow = src.split("async def _message_fallback(", 1)[1].split("\nasync def _dm_on_page(", 1)[0]
        assert msg_flow.index("_visible(page, _MSG_BOX") < msg_flow.index("_visible(page, _DRAWER_RECIPIENT")

        # Recruiter targeting: pure-Python card filtering, no browser involved.
        assert _profile_url("https://www.linkedin.com/in/priya-sharma-123/?miniProfileUrn=urn%3Ax") == \
            "https://www.linkedin.com/in/priya-sharma-123"
        assert _profile_url("https://in.linkedin.com/in/ravi") == "https://www.linkedin.com/in/ravi"
        assert _profile_url("https://www.linkedin.com/company/acme/") == "", "a company is not a person"
        assert _profile_url("/in/relative") == "" and _profile_url("") == ""
        assert _clean_name("Priya SharmaPriya Sharma") == "Priya Sharma", "duplicated label"
        assert _clean_name("Ravi Kumar\nView Ravi Kumar's profile") == "Ravi Kumar"
        assert _clean_name("  \n  ") == ""
        srp_cards = [
            {"href": "https://www.linkedin.com/in/me", "name": "Prince Jha", "card": "Prince Jha"},
            {"href": "https://www.linkedin.com/company/acme", "name": "Acme", "card": "Acme"},
            {"href": "https://www.linkedin.com/in/hidden", "name": "LinkedIn Member",
             "card": "LinkedIn Member Out of your network"},
            {"href": "https://www.linkedin.com/in/dev-anand?trk=x", "name": "Dev Anand",
             "card": "Dev Anand Backend Engineer at Acme"},
            {"href": "https://www.linkedin.com/in/priya-ta", "name": "Priya Nair",
             "card": "Priya Nair Talent Acquisition Specialist at Acme"},
        ]
        url, name = _pick_recruiter(srp_cards, skip={"https://www.linkedin.com/in/me"})
        assert (url, name) == ("https://www.linkedin.com/in/priya-ta", "Priya Nair"), (url, name)
        # Skip moves to the NEXT valid card rather than killing the company outright, which is what makes
        # the once-per-recruiter rule better outreach than a once-per-company one.
        assert _pick_recruiter(srp_cards[3:], skip={"https://www.linkedin.com/in/priya-ta"}) == \
            ("https://www.linkedin.com/in/dev-anand", "Dev Anand")
        assert _pick_recruiter(srp_cards[:1])[0] == "https://www.linkedin.com/in/me", \
            "with no hiring headline anywhere, first valid card still wins"
        assert _pick_recruiter(srp_cards[1:3]) == ("", ""), "companies and hidden members are not people"
        assert _pick_recruiter([]) == ("", "")

        # The company fallback reads the SRP for an href and DMs the profile; it never DMs the SRP.
        page = _Page((), url="https://www.linkedin.com/search/results/people/?keywords=Acme",
                     sticky=True, cards=srp_cards)
        assert await _recruiter_from_search(page, "Acme") == \
            ("https://www.linkedin.com/in/priya-ta", "Priya Nair")
        _real_dm, dm_calls = globals()["send_linkedin_dm"], []

        async def _spy_dm(profile_url, message, role="", company=""):
            dm_calls.append((profile_url, message, role, company))
            return True, "Direct Message sent"

        _real_context = globals()["_context"]

        class _PageCtx:
            def __init__(self, page): self.page = page

            async def new_page(self): return self.page

        globals()["send_linkedin_dm"] = _spy_dm
        globals()["_context"] = lambda _p, headless=None: _done(_PageCtx(page))
        _DM_DONE.clear()
        try:
            ok, msg = await send_linkedin_company_dm("Acme", "hi there", "SOC Analyst")
            assert ok and "Priya Nair" in msg and "/in/priya-ta" in msg, msg
            # The company is threaded through, so a premium InMail modal can fill its subject line.
            assert dm_calls == [("https://www.linkedin.com/in/priya-ta", "hi there",
                                 "SOC Analyst", "Acme")], dm_calls
            assert page.closed, "the search page must be closed before the profile page opens"
            assert not page.clicks, "the search results page must never be clicked on"
            # Second job at the same company: the recruiter just reached is skipped and the next valid card
            # is taken. Measured need - two jobs from one board landed on the same recruiter minutes apart,
            # and the second could only ever hit her now-Pending invite.
            page = _Page((), url="https://www.linkedin.com/search/results/people/?keywords=Acme",
                         sticky=True, cards=srp_cards[3:])
            globals()["_context"] = lambda _p, headless=None: _done(_PageCtx(page))
            ok, msg = await send_linkedin_company_dm("Acme", "hi again")
            assert ok and "/in/dev-anand" in msg, msg
            assert "/in/priya-ta" not in msg, "a recruiter already reached this run must not be re-DMed"
            _DM_DONE.clear()
            dry = _Page((), url="https://www.linkedin.com/search/results/people/?keywords=Nobody",
                        sticky=True, cards=srp_cards[1:3])
            globals()["_context"] = lambda _p, headless=None: _done(_PageCtx(dry))
            ok, msg = await send_linkedin_company_dm("Nobody", "hi")
            assert not ok and "no messageable recruiter" in msg, msg
            # The net one page earlier: a search that resolves nobody leaves the SRP on film, because a
            # layout change there strands the whole channel and the string alone does not say why.
            assert dry.shots == [str(_DEBUG_SHOT)], dry.shots
            assert _DEBUG_SHOT.name in msg, msg
        finally:
            globals()["send_linkedin_dm"] = _real_dm
            globals()["_context"] = _real_context
            _DM_DONE.clear()

        # Public entry point: bad input is rejected before a browser is ever launched.
        for bad_url, bad_msg in ((None, "hi"), ("https://twitter.com/x", "hi"),
                                 ("https://www.linkedin.com/in/x/", "   ")):
            ok, msg = await send_linkedin_dm(bad_url, bad_msg)
            assert not ok and msg.startswith("linkedin dm:"), (bad_url, msg)

        _dry = config.DRY_RUN
        config.DRY_RUN = True
        try:
            ok, msg = await send_linkedin_dm("https://www.linkedin.com/in/probe/", "hi")
            assert ok and "dry-run" in msg, msg
            ok, msg = await send_linkedin_company_dm("Acme", "hi")
            assert ok and "dry-run" in msg, msg
            assert (await send_linkedin_company_dm("", "hi"))[0] is False
        finally:
            config.DRY_RUN = _dry

        # A login-wall bounce is reported, not retried, and the page is still closed.
        page = _Page((), url="https://www.linkedin.com/authwall?x=1", sticky=True)
        _real_context = globals()["_context"]

        class _Ctx:
            async def new_page(self): return page

        async def _fake_context(_platform, headless=None): return _Ctx()

        globals()["_context"] = _fake_context
        try:
            ok, msg = await send_linkedin_dm("https://www.linkedin.com/in/probe/", "hi")
            assert not ok and "login wall" in msg, msg
            assert page.closed, "the page must be closed even on the authwall path"
            assert page.shots == [], "a login wall needs re-auth, not a screenshot of the wall"
        finally:
            globals()["_context"] = _real_context

        # The screenshot net, at the entry point where it now lives. Both shapes of failure must leave
        # exactly one capture and say so in the returned string, because the terminal is all the user sees.
        _real_context = globals()["_context"]
        try:
            for label, live in (("a clean miss", (MAIN,)), ("a thrown timeout", ())):
                shot = _Page(live, url="https://www.linkedin.com/in/probe", sticky=True)
                globals()["_context"] = lambda _p, headless=None, _s=shot: _done(_PageCtx(_s))
                ok, msg = await send_linkedin_dm("https://www.linkedin.com/in/probe/", "hi")
                assert not ok, (label, msg)
                assert msg.startswith("linkedin outreach failed: "), (label, msg)
                assert msg.endswith(f"[Screenshot saved to {_DEBUG_SHOT.name}]"), (label, msg)
                assert shot.shots == [str(_DEBUG_SHOT)], (label, shot.shots)
                assert shot.closed, (label, "the page must still be closed in the finally block")
            # A hydration timeout is a raised exception, and it must reach the net rather than the
            # generic outer handler - that handler would report it without any image.
            assert "not visible: main" in msg, msg
        finally:
            globals()["_context"] = _real_context

        # The universal browser fallback, end to end and offline: harvested cards -> gate -> stamped
        # rows, detail fetch only for keepers, page closed on every path.
        srp = _Page(("main",), cards=[
            {"job_url": "https://in.indeed.com/viewjob?jk=7&from=web",
             "title": "Junior DevOps Engineer", "company": "Acme", "location": "Pune",
             "date_posted": "1 day ago", "salary_text": "8-12 Lacs PA"},
            {"job_url": "https://in.indeed.com/viewjob?jk=8", "title": "Senior DevOps Architect",
             "company": "Old Corp", "location": "Pune", "date_posted": "3+ weeks ago"},
        ])
        _real_context = globals()["_context"]
        globals()["_context"] = lambda _p, headless=None: _done(_PageCtx(srp))
        try:
            rows = await _pw_search("indeed", "devsecops engineer", "Pune", want=5)
            assert len(rows) == 1, rows          # the senior 3-week-old card is dropped before its JD
            assert rows[0]["site"] == "indeed" and _row_to_job(rows[0])["platform"] == "indeed", \
                "the fallback must stamp its own board, or naukri rows show up as INDEED again"
            assert rows[0]["description"].startswith("jd body"), "a keeper gets its detail page fetched"
            assert srp.closed, "a scraper page must be closed in the finally block"

            walled = _Page(("main",), url="https://in.indeed.com/account/login", sticky=True,
                           cards=[{"job_url": "https://x/1", "title": "Junior DevOps Engineer"}])
            globals()["_context"] = lambda _p, headless=None: _done(_PageCtx(walled))
            assert await _indeed_search("devsecops", "Pune") == [], "a login wall harvests nothing"
            assert walled.closed, "the page must be closed on the wall path too"

            # A scraper that throws mid-harvest costs its own query, never the batch - and still closes.
            class _Boom(_Page):
                async def evaluate(self, _js, _arg=None):
                    raise RuntimeError("execution context was destroyed")

            boom = _Boom(("main",))
            globals()["_context"] = lambda _p, headless=None: _done(_PageCtx(boom))
            assert await _linkedin_search("devsecops", "Pune") == []
            assert boom.closed, "a raising harvest must not leak the page"
        finally:
            globals()["_context"] = _real_context
    finally:
        globals()["_human"] = _real_human

    import inspect

    assert "dm_text" not in inspect.signature(execute_platform_apply).parameters, \
        "execute_platform_apply must not DM inline - pipeline.run_job routes outreach, or it double-fires"

    # External ATS. Domain parsing and the session-vs-interceptor decision are proven offline; the
    # interceptor itself is never entered here, because it blocks on a human pressing ENTER.
    assert _ats_domain("https://acme.wd3.myworkdayjobs.com/en-US/careers/job/1") == \
        "acme.wd3.myworkdayjobs.com", "a Workday tenant owns its own login, so it owns its own session"
    assert _ats_domain("https://WWW.Careers.Salesforce.com/en/jobs/9/") == "careers.salesforce.com"
    assert _ats_domain("https://boards.greenhouse.io/acme/jobs/42?t=1") == "boards.greenhouse.io"
    assert _ats_domain("not a url") == "" and _ats_domain("") == "", "no host means no session key"
    assert _ats_domain("https://x.io/a/../b") == "x.io"
    assert _ats_state_path("careers.salesforce.com") == \
        config.BROWSER_SESSIONS / "ats_careers.salesforce.com.json"
    assert _ats_domain("https://a_b:8080/x") == "a-b", "a session key must be one safe path segment"
    assert not {"/", "\\", ":"} & set(_ats_domain("https://a_b:8080/c/d")), "key escapes its directory"
    assert {"apply", "upload", "advance", "submit"} <= set(_ATS_SELECTORS), _ATS_SELECTORS
    assert (await _external_apply("mailto:hr@acme.com", None))[0] is False, "unusable url must not open"
    # Off-board means the employer's own portal. Another board is not one: prompting a human to log into
    # indeed.com - which already has a persistent profile - would key a session file to an aggregator.
    assert _off_board("https://acme.wd3.myworkdayjobs.com/job/1", "www.linkedin.com")
    assert _off_board("https://boards.greenhouse.io/acme/jobs/42", "in.indeed.com")
    assert not _off_board("https://in.indeed.com/viewjob?jk=1", "www.linkedin.com"), "board, not an ATS"
    assert not _off_board("https://www.linkedin.com/jobs/view/1", "in.indeed.com"), "board, not an ATS"
    assert not _off_board("https://www.glassdoor.co.in/job/1", "www.linkedin.com"), "aggregator"
    assert not _off_board("https://www.linkedin.com/x", "www.linkedin.com"), "same host is not off-board"
    assert not _off_board("", "www.linkedin.com") and not _off_board("mailto:a@b.com", "x.com")

    # LinkedIn's own interstitial carries the ATS url in ?url=. Judged by its host it is "still on
    # LinkedIn", which is how three of five live jobs reported "no apply control" with the employer's
    # link sitting in the href. Unwrap first, then judge.
    _GO = "https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fharris.wd1.myworkdayjobs.com%2Fjob%2F7"
    assert _unwrap(_GO) == "https://harris.wd1.myworkdayjobs.com/job/7", _unwrap(_GO)
    assert _off_board(_unwrap(_GO), "www.linkedin.com"), "the unwrapped ATS must read as off-board"
    assert not _off_board(_GO, "www.linkedin.com"), "the wrapper itself is not an employer portal"
    assert _unwrap("https://www.linkedin.com/jobs/view/1") == "https://www.linkedin.com/jobs/view/1"
    assert _unwrap("https://boards.greenhouse.io/a/jobs/1") == "https://boards.greenhouse.io/a/jobs/1"
    assert _unwrap("https://www.linkedin.com/safety/go/?url=javascript%3Aalert(1)").endswith("(1)"), \
        "a non-http inner target is left wrapped rather than navigated to"

    # An off-site listing's Apply control IS the link, so the click moves this tab to the employer's ATS.
    # By then a page-read can only find the ATS linking to itself - measured live: LinkedIn -> Workday,
    # nothing found, the job recorded as "no apply control", the external path never entered, so no
    # session was saved for that host and no human was ever prompted. Where the tab went is checked first.
    class _Tab:
        def __init__(self, url, pages=()):
            self.url = url
            self.context = type("_Ctx", (), {"pages": list(pages)})()

    _board = "www.linkedin.com"
    _landed = "https://vanderlande.wd3.myworkdayjobs.com/en-US/job/1"
    assert await _landed_or_linked(_Tab(_landed), _board) == _landed, \
        "the tab the Apply click moved must be handed to the external path"
    _popped = "https://acme.wd3.myworkdayjobs.com/job/9"
    assert await _landed_or_linked(_Tab("https://www.linkedin.com/jobs/view/1", [_Tab(_popped)]),
                                   _board) == _popped, \
        "a tab the click opened is the same thing as one it moved"
    assert await _landed_or_linked(_Tab("https://www.linkedin.com/jobs/view/1"), _board) == "", \
        "still on the board: nothing is handed over, the link is read off the page instead"

    # Playwright's close() has no timeout of its own and --disable-hang-monitor is in the launch flags,
    # so a wedged renderer left close() pending for good: the batch stopped on that job, the run registry
    # stayed populated, and every .run-btn in the UI - including both delete controls - kept
    # pointer-events:none with no way back short of restarting the server.
    class _Wedged:
        async def close(self):
            await asyncio.sleep(3600)

    await asyncio.wait_for(_close_quietly(_Wedged(), _Wedged(), _Wedged(), timeout=0.2), 10)

    # A guest ATS form refuses submit with its required fields blank, so the identity values have to be
    # there to type. An empty CANDIDATE_EMAIL is a silent apply failure on every external job.
    assert ai_engine.CANDIDATE.split()[0] and ai_engine.CANDIDATE.split()[-1], ai_engine.CANDIDATE
    for _key in ("email", "phone"):
        assert ai_engine.CONTACT.get(_key), f"CANDIDATE_{_key.upper()} unset - guest ATS forms will stall"

    # _apply_flow's two live-measured hazards, on a stub page: a React careers shell that has not
    # rendered yet (jobs.tatacommunications.com answered "Career Portal", 1 word, no controls), and an
    # employer page carrying a Submit button that belongs to a job-alert box, not to the application.
    class _Loc:
        def __init__(self, page, sel):
            self.page, self.sel = page, sel

        @property
        def first(self):
            return self

        async def wait_for(self, **_kw):
            if self.sel not in self.page.live:
                raise TimeoutError(self.sel)

        async def click(self):
            await self.wait_for()
            self.page.clicked.append(self.sel)
            if self.page.landed:
                # An off-site Apply control IS the link: the click moves this same tab to the employer's
                # ATS. That url change is the entire signal the handoff check reads.
                self.page.url = self.page.landed

        async def set_input_files(self, *_a, **_kw):
            raise TimeoutError("no upload field on this form")

        async def input_value(self):
            return ""

        async def fill(self, _value):
            raise TimeoutError("no identity field on this form")

    class _Ctx:
        """Just enough of a browser context for the off-host tab walk to traverse."""

        pages: tuple = ()

    class _Wizard:
        """`live` selectors exist; the page confirms an application only after `confirms` submit clicks."""

        def __init__(self, live, confirms=0, words=200, canvas=False, closed=False, applied=False,
                     frame=None, after_reload=(), dead=False, landed=""):
            self.live, self.confirms, self.words = set(live), confirms, words
            self.canvas, self.clicked, self.closed, self.applied = canvas, [], closed, applied
            self.after_reload = set(after_reload)
            self.url, self.landed, self.context = "https://boards.example.com/job/1", landed, _Ctx()
            # A browser whose driver died: every call still returns a plausible local value (that is the
            # whole trap), and the first thing that has to cross the wire raises Playwright's own message.
            self.dead = dead
            # A Greenhouse embed puts the whole wizard one frame down, where no main-frame locator reaches.
            self.main_frame = self
            self.frames = [self, frame] if frame is not None else [self]

        async def reload(self, **_kw):
            self.live |= self.after_reload
            self.words = 800 if self.after_reload else self.words

        def locator(self, sel):
            return _Loc(self, sel)

        def set_default_timeout(self, _ms):
            pass

        async def wait_for_function(self, _js, **_kw):
            # The real predicate waits for an apply control, not for body text: a Phenom page answered
            # 188 words with its Apply anchor still unrendered, and every selector missed it.
            if self.words <= 40 or "#a" not in self.live:
                raise TimeoutError("no apply control has rendered yet")

        async def evaluate(self, js):
            if self.dead:
                raise TimeoutError("BrowserType.launch_persistent_context: Connection closed while "
                                   "reading from the driver")
            if "flt-glass-pane" in js:
                return self.canvas
            if "applications closed" in js:                # the miss probe, not the required-field sweep
                if self.closed or self.applied:
                    return {"apply": [], "closed": self.closed, "applied": self.applied}
                raise TimeoutError("the miss probe is best-effort and must stay swallowed")
            if "querySelectorAll" in js:
                raise TimeoutError("the required-field sweep is best-effort too")
            if "test(document.body.innerText" in js:
                return bool(self.confirms) and self.clicked.count("#s") >= self.confirms
            if "innerText" in js:
                return "word " * self.words
            raise TimeoutError("the stub answers only the probes it was built for")

    _WSEL = {"apply": ["#a"], "upload": ["#u"], "submit": ["#s"], "advance": ["#n"]}

    async def _nowait(lo=0.0, hi=0.0):
        return None

    _h0 = globals()["_human"]
    globals()["_human"] = _nowait
    try:
        page = _Wizard(("#a", "#s"), confirms=1)
        ok, msg = await _apply_flow(page, _WSEL, "x.pdf")
        assert ok and "submitted" in msg, (ok, msg)
        assert page.clicked.count("#a") == 1, "the board's wizard is walked twice on a live listing"

        # An off-site listing: the click moved this tab to the employer's ATS, so the wizard walk is
        # abandoned there. Measured consequence of not doing it - every external job paid for two whole
        # wizard walks (the board's, with the board's selectors against the ATS's page, then the ATS's
        # own) before the human was prompted, and the miss in between was recorded as "no apply control".
        page = _Wizard(("#a",), words=200, landed="https://acme.wd3.myworkdayjobs.com/j/1")
        ok, msg = await _apply_flow(page, _WSEL, "x.pdf")
        assert not ok and "handed off to acme.wd3.myworkdayjobs.com" in msg, (ok, msg)
        assert page.clicked == ["#a"], f"the board's wizard was walked after the handoff: {page.clicked}"
        assert await _landed_or_linked(page, "boards.example.com") == \
            "https://acme.wd3.myworkdayjobs.com/j/1", "the handoff never reaches the external path"

        # The stray Submit: clicked, never confirmed. Reported as a failure on purpose - recording a job
        # alert subscription as an application retires the row forever with nobody having applied.
        page = _Wizard(("#a", "#s"), confirms=0)
        ok, msg = await _apply_flow(page, _WSEL, "x.pdf")
        assert not ok and "never confirmed" in msg, (ok, msg)
        assert page.clicked.count("#s") > 1, "an unconfirmed Submit must not end the wizard walk"

        # An unrendered shell: the miss has to name the word count, because "no apply control" reads the
        # same for an expired posting, a moved button and a page that simply had not painted yet.
        page = _Wizard((), words=1)
        ok, msg = await _apply_flow(page, _WSEL, "x.pdf")
        assert not ok and "1-word render" in msg, (ok, msg)
        assert await _hydrate(_Wizard((), words=952)) == 952, "a rendered page must report its size"

        # jobs.tatacommunications.com: a Flutter build that paints into a canvas, so no wait and no
        # selector will ever find a control. Named, so nobody spends another round hunting selectors.
        page = _Wizard((), words=30, canvas=True)
        ok, msg = await _apply_flow(page, _WSEL, "x.pdf")
        assert not ok and "canvas-rendered" in msg and "needs a human" in msg, (ok, msg)

        # A closed posting is the one miss no retry can win: measured on a live Zensar row that rendered
        # 707 words and said "no longer accepting applications". The probe knew; the verdict said
        # "expired session", so the row stayed queued for a retry that could never succeed.
        page = _Wizard((), words=707, closed=True)
        ok, msg = await _apply_flow(page, _WSEL, "x.pdf")
        assert not ok and "closed" in msg and "unwinnable" in msg, (ok, msg)

        # And the mirror: a board that already shows the application. Measured on a live Naukri row whose
        # only control read `Applied` while the DB had applied_at NULL - every apply selector hunts for
        # `Apply`, so it reported a miss and the row was retried forever against a page with no button.
        # A win here is what lets run_job_events stamp applied_at and stop.
        page = _Wizard((), words=1129, applied=True)
        ok, msg = await _apply_flow(page, _WSEL, "x.pdf")
        assert ok and "already shows" in msg, (ok, msg)
        # Applied outranks closed - an employer closes a posting after collecting the applications.
        ok, _msg = await _apply_flow(_Wizard((), words=707, closed=True, applied=True), _WSEL, "x.pdf")
        assert ok, "a posting that closed after accepting our application is still an application"

        # The required-control pass, on the four shapes NTT's Phenom guest form actually served. The two
        # plumbing controls must be answered (they were the whole of "wizard stalled before submit") and
        # the two questions must NOT be: a guessed yes/no and a ticked non-consent box are both statements
        # the employer reads as the candidate's own.
        class _Ctl:
            def __init__(self, page, nth):
                self.page, self.nth = page, nth

            async def select_option(self, value, **_kw):
                self.page.acted.append(("select", self.nth, value))

            async def check(self, **_kw):
                self.page.acted.append(("check", self.nth))

        class _Form:
            def __init__(self, todo):
                self.todo, self.acted = todo, []

            def locator(self, _sel):
                return type("L", (), {"nth": lambda _s, n, p=self: _Ctl(p, n)})()

            async def evaluate(self, _js):
                return self.todo

        form = _Form([{"kind": "select", "nth": 0, "value": "MOBILE", "label": "Device Type*",
                       "yesno": False},
                      {"kind": "select", "nth": 1, "value": "Yes", "label": "Do you need sponsorship?*",
                       "yesno": True},
                      {"kind": "check", "nth": 2, "label": "*\nI confirm I have read the privacy notice"},
                      {"kind": "check", "nth": 3, "label": "I require visa sponsorship"}])
        assert await _fill_required(form) == (2, ["Do you need sponsorship?*", "I require visa sponsorship"]), \
            form.acted
        assert form.acted == [("select", 0, "MOBILE"), ("check", 2)], form.acted
        # A page whose DOM will not answer must not take the wizard down with it.
        assert await _fill_required(_Wizard((), words=200)) == (0, []), "the probe must stay best-effort"
        # A stall names the questions, because the operator answers them once by hand and the saved session
        # carries them forward - "extra questions need a human" tells that human nothing.
        ok, msg = await _apply_flow(_Wizard(("#a",), words=700), _WSEL, "x.pdf")
        assert not ok and "no submit or advance control" in msg, (ok, msg)

        # Job 24 (Beghou), the false negative that cost an application already on file: the apply click
        # LANDED, the walk found nothing, and the pre-click probe never ran on that path - so a board
        # rendering div[Applied] read as a stall and the row was retried against a page with no button.
        page = _Wizard(("#a",), words=1129, applied=True)
        ok, msg = await _apply_flow(page, _WSEL, "x.pdf")
        assert ok and "already submitted" in msg, (ok, msg)

        # Job 21 (Samsara), the Greenhouse embed: main frame carries only Apply, the wizard lives one frame
        # down, and a Playwright locator does not cross an iframe boundary.
        inner = _Wizard(("#s",), confirms=1)
        ok, msg = await _apply_flow(_Wizard(("#a",), words=1527, frame=inner), _WSEL, "x.pdf")
        assert ok and "submitted" in msg, (ok, msg)
        assert inner.clicked == ["#s"] and not _Wizard(()).frames[1:], (inner.clicked, msg)

        # Job 22 (HCLTech): in.indeed.com/rc/clk lands on careers.hcltech.com, where the control is an
        # ANCHOR - and indeed's apply list has none, so a page holding two of them reported no control.
        page = _Wizard(("#a", "a:has-text('Apply')", "#s"), confirms=1)
        ok, msg = await _apply_flow(page, {**_WSEL, "apply": ["#gone"]}, "x.pdf")
        assert ok and "submitted" in msg, (ok, msg)
        assert "a:has-text('Apply')" in page.clicked, page.clicked

        # Both indeed rows of one live batch answered 37 words with no control, while a read-only probe of
        # the same two urls minutes later rendered 518 with the Apply anchor there. A thin page is a
        # transient to reload once, not an apply verdict.
        page = _Wizard((), words=37, after_reload=("#a", "#s"), confirms=1)
        ok, msg = await _apply_flow(page, _WSEL, "x.pdf")
        assert ok and "submitted" in msg, (ok, msg)
        # ...but a page that rendered fully and simply has no control is reported on the first pass: a
        # reload there costs every closed posting an extra navigation and changes no verdict.
        page = _Wizard((), words=707, closed=True)
        ok, _msg = await _apply_flow(page, _WSEL, "x.pdf")
        assert page.words == 707, "a fully rendered page must not be reloaded"

        # The crash that took down four rows of one live batch. Three carried "Connection closed while
        # reading from the driver" in their notes and the fourth said "wizard stalled before submit" -
        # the same dead browser, seen from two places, both of which blamed the selectors. Neither may
        # be reported as drift again: the wording has to send the operator to the driver, not the DOM.
        page = _Wizard(("#a", "#s"), confirms=0, dead=True)
        ok, msg = await _apply_flow(page, _WSEL, "x.pdf")
        assert not ok and msg == _DEAD_BROWSER, (ok, msg)
        page = _Wizard((), words=707, dead=True)
        ok, msg = await _apply_miss(page, 707)
        assert not ok and msg == _DEAD_BROWSER, (ok, msg)
        # Liveness itself, in the shape _context actually calls it. A context that still answers is
        # reused; one whose transport is gone must report unhealthy, or the cache would keep handing
        # out a dead browser and every later job would fail identically. The stub's answer lives in
        # cookies() because that is the one call the probe makes, and it is the one that round-trips.
        class _Ctx:
            def __init__(self, dead):
                self.dead = dead

            async def cookies(self):
                if self.dead:
                    raise TimeoutError("Target page, context or browser has been closed")

        assert await _alive(_Ctx(False)) is True
        assert await _alive(_Ctx(True)) is False
    finally:
        globals()["_human"] = _h0

    # No TTY (uvicorn, a service, a redirected shell) means nobody can press ENTER: the job must say so
    # instead of opening a browser nothing will drive and stalling the request forever.
    _real_stdin, _dry = sys.stdin, config.DRY_RUN
    try:
        config.DRY_RUN = False
        sys.stdin = None
        ok, msg = await _ats_intercept("https://acme.myworkdayjobs.com/j/1", "acme.myworkdayjobs.com")
        assert not ok and "needs a human at the keyboard" in msg, msg
        assert not _INTERCEPT_LOCK.locked(), "the lock must not be taken before the TTY check"
        # HITL off is the same refusal on a real TTY: an agent-launched batch owns a pty with nobody
        # watching it, and measured live that blocked the whole run on job 1's Workday prompt.
        sys.stdin, config.HITL = _real_stdin, False
        try:
            ok, msg = await _ats_intercept("https://acme.myworkdayjobs.com/j/1", "acme.myworkdayjobs.com")
            assert not ok and "needs a human at the keyboard" in msg, msg
        finally:
            config.HITL = True
        sys.stdin = None
        config.DRY_RUN = True
        ok, msg = await _ats_intercept("https://acme.myworkdayjobs.com/j/1", "acme.myworkdayjobs.com")
        assert not ok and "dry-run" in msg, msg
    finally:
        sys.stdin, config.DRY_RUN = _real_stdin, _dry
    assert not list(config.BROWSER_SESSIONS.glob("ats_acme.myworkdayjobs.com.json")), \
        "a refused interceptor must not leave a session file claiming the ATS is authenticated"
    # The mandated warning glyphs must survive a console that cannot encode them.
    _real_stdout = sys.stdout
    try:
        import io

        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        _warn("⚠️ EXTERNAL ATS DETECTED (acme.com): Application opened in visible browser.")
        banner = sys.stdout.buffer.getvalue().decode("cp1252")
    finally:
        sys.stdout = _real_stdout
    assert "EXTERNAL ATS DETECTED" in banner and banner.startswith("!"), repr(banner)

    # Reply tracking + follow-up + ICS. The pure parts run offline; the IMAP/SMTP halves short-circuit
    # under DRY_RUN so nothing connects and no live IMAP is needed here.
    assert _extract_interview_dt("Let's meet on 2026-02-12 at 3pm") == datetime(2026, 2, 12, 15, 0)
    assert _extract_interview_dt("Are you free 12 Feb at 09:30?") == datetime(datetime.now().year, 2, 12, 9, 30)
    assert _extract_interview_dt("Call you on Feb 12 in the morning").hour == 10, "no time -> 10:00 default"
    assert _extract_interview_dt("Interview 15/02/2026 at 2 pm") == datetime(2026, 2, 15, 14, 0)
    assert _extract_interview_dt("Thanks for applying, we'll be in touch.") is None
    assert _extract_interview_dt("") is None and _extract_interview_dt("Feb 31 is not a date") is None
    assert _after_outreach("Wed, 12 Feb 2026 10:00:00 +0000", "2026-02-11 09:00:00") is True
    assert _after_outreach("Mon, 10 Feb 2026 10:00:00 +0000", "2026-02-11 09:00:00") is False
    assert _after_outreach(None, "2026-02-11 09:00:00") is True, "no date on the reply must not reject it"

    import tempfile

    ics_path = _write_ics({"id": 7, "company": "Acme", "title": "SOC Analyst", "hr_email": "hr@acme.com"},
                          datetime(2026, 2, 12, 15, 0))
    ics = ics_path.read_text(encoding="utf-8")
    assert "BEGIN:VCALENDAR" in ics and "DTSTART:20260212T150000" in ics, ics
    assert "DTEND:20260212T153000" in ics and "SUMMARY:Interview - Acme (SOC Analyst)" in ics, ics
    ics_path.unlink()

    _dbp, _dry = config.DB_PATH, config.DRY_RUN
    config.DB_PATH, config.DRY_RUN = Path(tempfile.mkdtemp()) / "au_feat.db", True
    try:
        await database.init_db()
        assert (await poll_replies())["note"].startswith("no outreach"), "empty candidate set"
        assert (await run_followups())["note"].startswith("no aged"), "no due nudges"
        await database.add_jobs([{"title": "SOC Analyst", "company": "Acme", "job_url": "https://x/imap1",
                                  "platform": "linkedin", "hr_email": "hr@acme.com", "status": "emailed"}])
        jid = (await database.fetch("emailed"))[0]["id"]
        await database.mark_reached(jid)                 # stamps outreach_at -> a poll candidate
        pr = await poll_replies()
        assert pr["checked"] == 1 and pr["replied"] == 0 and "dry-run" in pr["note"], pr
        # "emailed but the row says failed" is reconciled against the Sent mailbox, so the sweep's
        # candidate set has to be the rows that CLAIM nothing was sent - a mailbox on file, no
        # outreach_at. Under DRY_RUN nothing may connect, but the count must still be reported.
        await database.add_jobs([{"title": "Cloud Assoc", "company": "Beta", "job_url": "https://x/imap2",
                                  "platform": "naukri", "hr_email": "hr@beta.com", "status": "failed"}])
        pr2 = await poll_replies()
        assert pr2["verified"] == 1 and "dry-run" in pr2["note"], pr2
        assert _imap_sent([]) == [], "an empty candidate set must not open a connection"

        # A DM-only row: reached over LinkedIn, no mailbox anywhere. It has to come back as due and its
        # second touch has to go out over LinkedIn - requiring an hr_email in due_followups is exactly
        # what left every such row listed in the follow-ups view as an entry no sweep could ever clear.
        await database.add_jobs([{"title": "SecOps", "company": "Dm Co", "job_url": "https://x/imap3",
                                  "platform": "linkedin", "status": "applied"}])
        dm_only = (await database.fetch("applied"))[0]["id"]
        async with database.connect() as db:
            await db.execute("UPDATE jobs SET outreach_at = datetime('now','localtime', ?),"
                             " recruiter_linkedin_url = 'https://www.linkedin.com/in/dmco' WHERE id = ?",
                             [f"-{config.FOLLOWUP_DAYS + 4} days", dm_only])
        assert dm_only in [r["id"] for r in await database.due_followups(config.FOLLOWUP_DAYS)], \
            "a DM-only row never comes due, so it can never be nudged"
        nudged: list[int] = []

        async def _fake_nudge(job, role, company):
            nudged.append(job["id"])
            return True

        _real_nudge, _real_creds = _nudge_linkedin, (config.SMTP_EMAIL, config.SMTP_PASSWORD)
        globals()["_nudge_linkedin"] = _fake_nudge
        config.SMTP_EMAIL = config.SMTP_PASSWORD = ""
        config.DRY_RUN = False
        try:
            out = await run_followups()
            assert nudged == [dm_only], f"the DM-only nudge went nowhere: {out} {nudged}"
            assert (await database.get(dm_only))["followup_at"], "the LinkedIn nudge was not recorded"
        finally:
            globals()["_nudge_linkedin"] = _real_nudge
            config.SMTP_EMAIL, config.SMTP_PASSWORD = _real_creds
            config.DRY_RUN = True

        # The kill-leftovers sweep. 'failed' is not evidence that nothing happened, so each row is judged
        # on its write-once facts, and there are three shapes: a spent send is never re-sent, a spent
        # submit is never re-applied but does go back in the queue for the mail it still owes, and a row
        # where BOTH halves went nowhere is litter that takes its resume with it.
        _real_res = config.RESUME_DIR
        config.RESUME_DIR = Path(tempfile.mkdtemp())
        try:
            dead = config.RESUME_DIR / "Prince_Jha_resume_Net_Eng_Beta.pdf"
            dead.write_bytes(b"%PDF-1.4 rendered for an application that never happened")
            await database.add_jobs([{"title": "Net Eng", "company": "Beta", "job_url": "https://x/cf1",
                                      "platform": "naukri", "status": "failed"},
                                     {"title": "Spent App", "company": "Beta", "job_url": "https://x/cf2",
                                      "platform": "naukri", "status": "failed"}])
            by_title = {r["title"]: r["id"] for r in await database.fetch()}
            litter, spent_id = by_title["Net Eng"], by_title["Spent App"]
            # pdf_path is written by the pipeline's own status call, not by add_jobs - the scraper has
            # no resume to hand over, which is why it is not one of the insertable columns.
            await database.set_status(litter, "failed", pdf_path=str(dead))
            async with database.connect() as db:                 # a spent submit, still labelled failed
                await db.execute("UPDATE jobs SET applied_at = datetime('now','localtime') WHERE id = ?",
                                 [spent_id])
            config.DRY_RUN = False
            try:
                out = await clean_failed()
            finally:
                config.DRY_RUN = True
            assert not dead.exists(), "a resume rendered for an application that never happened was kept"
            assert not (await database.get(litter))["pdf_path"], "the row still points at a deleted PDF"
            assert (await database.get(litter))["status"] == "skipped", \
                "a row that went nowhere is not filtered like the rows that were filtered out"
            # The third shape: submit spent, mail not sent. Neither reusable nor finished, so it goes
            # back in the queue - applied_at is what makes the batch skip the submit and run only the
            # outreach, so keeping it is what stops this being a second application.
            owed = await database.get(spent_id)
            assert owed["status"] == "queued", \
                "a row owing one mail and no submit is stranded - nothing but 'queued' is ever run"
            assert owed["applied_at"], \
                "the requeue dropped applied_at, so the retry would apply to the same employer again"
            assert owed["outreach_at"] is None, "the owed outreach half was stamped as done"
            assert out["replayed"] == 1 and out["requeued"] >= 1, out
            assert not _drop_resume({"pdf_path": str(Path(tempfile.gettempdir()) / "not_ours.pdf")}), \
                "the resume delete would happily reach outside generated_resumes"
        finally:
            config.RESUME_DIR = _real_res

        # Retention, on a fixture of its own: the legs above leave rows in every shape on purpose, and
        # this one has to be exact - what the two clocks take, what they leave, and the one mailbox that
        # dies with the employer nobody is talking to any more. That mailbox is the only thing the purge
        # ever reaches outside `jobs`, so it is the only one worth asserting on.
        purge_db = Path(tempfile.mkdtemp()) / "purge.db"
        await database.init_db(purge_db)
        await database.add_jobs([{"title": "Net Eng", "company": "Litter Co", "job_url": "https://x/p1",
                                  "platform": "naukri", "status": "queued"},
                                 {"title": "SRE", "company": "Silent Co", "job_url": "https://x/p2",
                                  "platform": "linkedin", "status": "emailed", "hr_email": "hr@silent.com"},
                                 {"title": "SOC", "company": "Answered Co", "job_url": "https://x/p3",
                                  "platform": "linkedin", "status": "emailed",
                                  "hr_email": "hr@answered.com"}],
                                db_path=purge_db)
        rows = {r["company"]: r["id"] for r in await database.fetch(db_path=purge_db)}
        async with database.connect(purge_db) as db:
            # Litter Co: scraped six days ago, never reached anyone - the haul going stale.
            await db.execute("UPDATE jobs SET created_at = datetime('now','localtime','-6 days')"
                             " WHERE company = 'Litter Co'")
            # Silent Co: reached 11 days back, no reply since - the reply window has shut.
            await db.execute("UPDATE jobs SET created_at = datetime('now','localtime','-20 days'),"
                             " applied_at = datetime('now','localtime','-11 days')"
                             " WHERE company = 'Silent Co'")
            # Answered Co: same shape, but the recruiter wrote back.
            await db.execute("UPDATE jobs SET created_at = datetime('now','localtime','-20 days'),"
                             " applied_at = datetime('now','localtime','-11 days'),"
                             " outreach_at = datetime('now','localtime','-11 days')"
                             " WHERE company = 'Answered Co'")
        for name in ("Litter Co", "Silent Co", "Answered Co"):
            await database.save_vault_email(_company_key(name), f"hr@{name.split()[0].lower()}.com",
                                            "hunter", purge_db)
        await database.mark_replied(rows["Answered Co"], purge_db)

        async def _history() -> list:
            async with database.connect(purge_db) as db:
                return sorted(tuple(r) for r in await (await db.execute(
                    "SELECT company_norm, kind FROM company_history")).fetchall())

        history = await _history()
        assert (await purge_old_jobs(db_path=purge_db))["jobs"] == 0, \
            "dry-run deleted a row anyway - the brake is off on the boot path"
        assert len(await database.fetch(db_path=purge_db)) == 3, "dry-run changed the table"
        config.DRY_RUN = False
        try:
            out = await purge_old_jobs(db_path=purge_db)
        finally:
            config.DRY_RUN = True
        assert out == {"jobs": 2, "mailboxes": 1}, out
        assert {r["company"] for r in await database.fetch(db_path=purge_db)} == {"Answered Co"}, \
            "the purge took a row it should have kept, or left one it should have taken"
        assert await database.get_vault_email(_company_key("Silent Co"), purge_db) is None, \
            "the mailbox of an employer who never answered outlived its last row"
        assert await database.get_vault_email(_company_key("Litter Co"), purge_db), \
            "scraped litter is not the vault's to take - only a thread that reached a human is"
        assert await database.get_vault_email(_company_key("Answered Co"), purge_db), \
            "an employer who replied keeps its mailbox"
        assert await _history() == history, \
            "the purge touched company_history, so a retired employer could be approached twice"
    finally:
        config.DB_PATH, config.DRY_RUN = _dbp, _dry

    report = {}
    low, high = config.SCRAPE_PAUSE
    report["search_matrix"] = (f"{len(config.SEARCH_TERMS)} roles x "
                               f"({len(config.SEARCH_LOCATIONS)} locations + internshala) = "
                               f"{len(plan)} queries, capped at {config.MAX_QUERIES_PER_RUN}/run "
                               f"= ~{config.MAX_QUERIES_PER_RUN * (low + high) / 2 / 60:.0f} min of throttle")
    report["quota_split"] = (f"{quota} jobs / {len(config.PLATFORMS)} platforms = {per} each "
                             f"({', '.join(config.PLATFORMS)}), split again across "
                             f"{', '.join(f'{r} {n}' for r, n in config.ROLE_TARGETS.items())}; "
                             f"overflow backfills unfilled shares on both axes")
    report["hr_tiers"] = ("db -> jd regex -> hunter -> playwright dork (google, then ddg/bing), "
                          "all behind _corroborated + HR localpart")
    report["naukri"] = ("jobspy first, then the logged-in playwright context when it returns nothing "
                        "(406 recaptcha wall)")
    report["omni_scraper"] = ("per board: cheap backend once (jobspy, httpx for internshala), then that "
                              "board switches to its logged-in playwright context for the rest of the "
                              f"run - {', '.join(sorted(set(_PW_SEARCH) | set(_PW_BOARDS)))}")
    for platform in (*config.PLATFORMS, "osint"):         # persistent session dirs must be writable
        session_dir = config.BROWSER_SESSIONS / platform
        session_dir.mkdir(parents=True, exist_ok=True)
        probe = session_dir / ".probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    report["browser_sessions"] = (f"{len(config.PLATFORMS)} board dirs + osint writable "
                                  f"at {config.BROWSER_SESSIONS}")

    try:
        import playwright  # noqa: F401
        report["playwright"] = "importable"
    except ImportError as exc:
        report["playwright"] = f"MISSING ({exc})"
    try:
        import jobspy  # noqa: F401
        report["jobspy"] = "importable"
    except Exception as exc:
        report["jobspy"] = f"MISSING ({type(exc).__name__}: {exc})"
    # A board that throws must cost one query, never the batch. Proven offline by standing a fake
    # jobspy in front of the real one: _scrape_one imports inside its try, so the raise is contained.
    import types

    _real_jobspy = sys.modules.get("jobspy")
    _fake = types.ModuleType("jobspy")

    def _boom(**_kw):
        raise RuntimeError("Naukri API response status code 406 - recaptcha required")

    _fake.scrape_jobs = _boom
    sys.modules["jobspy"] = _fake
    try:
        assert _scrape_one("devsecops", "Pune", ("naukri",)) == [], "a 406 must degrade to zero rows"
        del _fake.scrape_jobs                       # and a broken install must not raise either
        assert _scrape_one("devsecops", "Pune", ("naukri",)) == []
    finally:
        sys.modules.pop("jobspy", None)
        if _real_jobspy is not None:
            sys.modules["jobspy"] = _real_jobspy
    report["jobspy_isolation"] = "406 and import failure both degrade to 0 rows, batch survives"

    # A dead context must never be handed out twice; the close handler evicts it so the next call relaunches.
    _CONTEXTS["_probe"] = object()
    _CONTEXTS.pop("_probe", None)
    assert "_probe" not in _CONTEXTS
    report["playwright_leaks"] = ("pages closed in finally (apply, dm, dork, naukri, omni fallbacks), "
                                  "every close bounded by asyncio.wait_for so one wedged renderer "
                                  "cannot leave the run registry populated and the UI locked; osint "
                                  "context closed per dork; 4 board contexts closed once at shutdown()")
    report["linkedin_dm"] = ("profile-targeted: people search -> first non-hidden recruiter href -> "
                             "Connect+note (free, 2nd/3rd degree) then Message; both triggers are <a href> "
                             "anchors the SPA intercepts into IN-PLACE shadow-root overlays (url unchanged) "
                             "resolved inside <main> by ACCESSIBLE NAME (Connect by its vanityName href, "
                             "since its aria-label names the person) so the recommendation rail that also "
                             "lives in main cannot be hit; two live layouts handled - Connect on the action "
                             "bar, or Follow promoted there and Connect demoted into an overflow menu that "
                             "is PORTALED OUT of <main> and whose item has no usable accessible name, so "
                             "that door is keyed by the same vanityName href; an arrival Premium upsell is "
                             "closed before either channel runs (it wins :visible .first otherwise); an "
                             "invite already Pending is reported as reached, never clicked (a click "
                             "withdraws it); one DM per recruiter per run, so a second job at the same "
                             "company gets the next name; <main> waited for visible "
                             "first; composer waited "
                             "for before the drawer guard answers; send is an icon-only circle button "
                             "(msg-form__send-btn, no text, no aria-label); any miss or timeout "
                             f"screenshots to {_DEBUG_SHOT.name}")
    report["external_ats"] = ("LinkedIn's /safety/go/?url= interstitial unwrapped to the real ATS url, "
                              "and the tab the Apply click actually landed on checked against the "
                              "listing's own host the instant the click returns - before any wizard walk "
                              "- so an off-site listing is handed over in seconds instead of after the "
                              "board's walk has ground the ATS page against the board's selectors (an "
                              "off-site Apply control is the link, so the click moves the tab and a "
                              "page-read would only find the ATS linking to itself), then the wizard "
                              "walked headlessly "
                              f"with {config.BROWSER_SESSIONS.name}/"
                              "ats_<host>.json if it exists and as a guest if it does not (greenhouse, "
                              "lever and workday guest forms need no account), else a visible browser + "
                              "ENTER interceptor (TTY-gated, serialised, console polled so a cancel can "
                              "reach it) that writes the session for "
                              "next time, keyed to the LANDED host so a tracker link cannot share one "
                              "cookie jar across employers - and that raises RunAborted, not a plain "
                              "miss, if the operator closes the form without pressing ENTER, because a "
                              "miss is a route the pipeline finishes by mailing the recruiter")
    try:
        import httpx  # noqa: F401
        report["httpx"] = f"importable ({httpx.__version__}) - internshala fetches"
    except Exception as exc:
        report["httpx"] = f"MISSING ({type(exc).__name__}: {exc})"

    if not config.HUNTER_API_KEY:
        report["hunter"] = "HUNTER_API_KEY unset - lookups fall back to db hr_email"
    else:
        try:
            from pyhunter import PyHunter

            info = PyHunter(config.HUNTER_API_KEY).account_information() or {}
            report["hunter"] = f"ok, {info.get('calls', {}).get('left')} calls left"
        except Exception as exc:
            report["hunter"] = f"init failed: {type(exc).__name__}: {exc}"

    if not (config.SMTP_EMAIL and config.SMTP_PASSWORD):
        report["smtp"] = f"{config.SMTP_HOST}:{config.SMTP_PORT} - SMTP_EMAIL/SMTP_PASSWORD unset, sending disabled"
    else:
        try:
            with _smtp() as server:                        # handshake + auth only; no message is sent
                server.login(config.SMTP_EMAIL, config.SMTP_PASSWORD)
            report["smtp"] = f"{config.SMTP_HOST}:{config.SMTP_PORT} auth ok as {config.SMTP_EMAIL} (no mail sent)"
        except Exception as exc:
            report["smtp"] = f"{config.SMTP_HOST}:{config.SMTP_PORT} failed: {type(exc).__name__}: {exc}"

    report["dry_run"] = config.DRY_RUN
    for key, value in report.items():
        print(f"{key:17} {value}")
    print("automations.py selfcheck ok")


if __name__ == "__main__":
    asyncio.run(_selfcheck())
