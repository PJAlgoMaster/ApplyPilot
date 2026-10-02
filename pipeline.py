"""Per-job orchestration: claude-session assets -> ATS PDF -> platform submit -> routed outreach -> status.

Owns the terminal status write for a job. Never raises into the request handler: every failure
comes back as (False, reason) with the row marked 'failed'.

Outreach is routed here and nowhere else. Gmail wins when an HR mailbox is known (a reply lands in an
inbox a human actually reads); a LinkedIn DM is the fallback when it is not. automations no longer
DMs from inside the apply flow, or both channels would fire on the same job.

The AI engine is the one stage allowed to fail without losing the job: quota exhaustion and a
persistent content block are outages, not bugs, and a base resume in an HR inbox today beats a
tailored one never sent. On that path _fallback_assets supplies a static PDF plus a generic pitch and
the OSINT + Playwright phases run exactly as they would have.
"""
from __future__ import annotations

import asyncio
import json
import traceback
from pathlib import Path

import ai_engine
import automations
import config
import database
import parsing

# Generic pitch for the fallback path: no JD-specific claims, since nothing read the JD. Wording
# follows the standing rule that the GitHub DevSecOps work is a self-directed technical report.
_FALLBACK_PITCH = [
    "I am applying for the {title} opening at {company}. My focus is DevSecOps and application "
    "security: shift-left scanning in CI/CD, container and cloud hardening, and SIEM-side detection "
    "work. My resume is attached.",
    "Recent hands-on proof: self-directed technical reports covering SAST/DAST pipeline gates "
    "(SonarQube, Trivy, OWASP ZAP), Kubernetes and container hardening, and a security audit writeup "
    "of a live web target, published with remediation notes on GitHub.",
    "I am an immediate joiner and open to relocation. If the role is still live, I would value a "
    "short call to walk through the audit work in detail.",
]


def _fallback_assets(job: dict) -> tuple[dict, Path]:
    """Static base resume + generic pitch, for when the AI engine is down. Blocking (file copy).

    The PDF is COPIED to the mandated per-job name rather than handed over as the source path: the
    filename is what a recruiter sees on the attachment, and 'cdac_resume.pdf' in an inbox reads as a
    mass mailing. resume_pdf_path is the same namer the tailored path uses, so nothing downstream
    (status write, dashboard link, dedupe by name) has to know which path produced the file.

    The copy goes through the same metadata pass the rendered path uses, because the sources are not
    clean either - one carries no author at all and the other still calls itself 'Microsoft Word -
    Document1' from whatever last saved it. A recruiter opening the fallback attachment should see
    the same owner as the tailored one, not the provenance of the file it was copied from.
    """
    key = next(iter(ai_engine.available_sources()), None)
    if not key:
        raise RuntimeError("AI engine failed and no source resume is readable - nothing to send")
    src = config.SOURCE_RESUME_DIR / ai_engine.SOURCE_FILES[key]
    dest = ai_engine.resume_pdf_path(job["title"], job["company"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(ai_engine._strip_producer(src.read_bytes(), {
        "/Author": ai_engine.CANDIDATE,
        "/Title": f"{ai_engine.CANDIDATE} - {job['title']}",
    }, doc_id=dest.stem))
    title, company = job["title"], job["company"]
    pitch = [p.format(title=title, company=company) for p in _FALLBACK_PITCH]
    return {
        "selected_template": key,
        "resume_data": {},                       # nothing to render: the PDF is already final
        "email_subject": f"{title} - Prince Jha, immediate joiner (resume attached)",
        "hr_email_pitch": pitch,
        "platform_dm": ai_engine._cap(
            f"Hi, I applied for the {title} role at {company}. DevSecOps focus - CI/CD security "
            f"gates, container hardening, SIEM detection - with published self-directed technical "
            f"reports to back it. Happy to share details. - Prince Jha", ai_engine.DM_LIMIT),
        "meta": {"fallback": True, "source": key},
    }, dest


async def _outreach(job: dict, pdf_path, assets: dict) -> tuple[bool, str | None, str, str | None]:
    """Route one job's outreach. Returns (reached, hr_email, message, message_id).

    Precedence: known hr_email -> Gmail; else recruiter_linkedin_url -> DM; else the email waterfall
    (vault -> jd -> hunter -> dork -> osint, inside find_and_email_hr) and, if it comes back dry, the
    LinkedIn DM. A mailbox discovered mid-run takes the email path, so a job is never both emailed and
    DMed.

    message_id is the Message-ID of a mail that actually went out (None on a DM or a dry/failed send);
    it is the thread anchor poll_replies matches an incoming reply against.

    The guarantee: every path that does not send an email falls through to a DM. An unreachable job is
    one where BOTH channels were tried and both failed - never one where the email tier was dry and
    nothing else was attempted.
    """
    hr_email = None
    if job.get("hr_email"):
        sent, hr_email, msg, msgid = await automations.find_and_email_hr(
            job, pdf_path, assets["hr_email_pitch"], assets["email_subject"])
        print(f"  outreach: gmail (hr_email on file) -> {'sent' if sent else 'not sent'} · {msg}")
        if sent:
            return True, hr_email, msg, msgid
        # A known mailbox that bounces or has no SMTP configured is still an unreached job, so the DM
        # below runs. hr_email is kept: it is a real address and belongs on the row either way.

    elif profile := job.get("recruiter_linkedin_url"):
        sent, msg = await automations.send_linkedin_dm(profile, assets["platform_dm"],
                                                      job.get("title") or "",
                                                      job.get("company") or "")
        print(f"  outreach: linkedin dm ({profile}) -> {'sent' if sent else 'not sent'} · {msg}")
        return sent, None, msg, None

    else:
        sent, hr_email, msg, msgid = await automations.find_and_email_hr(
            job, pdf_path, assets["hr_email_pitch"], assets["email_subject"])
        print(f"  outreach: gmail (resolver) -> {'sent' if sent else 'not sent'} · {msg}")
        if sent:
            return True, hr_email, msg, msgid

    # Email did not go out. Explicitly flagged, then the DM runs as the fallback vector: named
    # recruiter first if the row carries one, company search otherwise.
    print("  outreach: email skipped -> linkedin dm is the fallback vector for this job")
    if profile := job.get("recruiter_linkedin_url"):
        dm_sent, dm_msg = await automations.send_linkedin_dm(profile, assets["platform_dm"],
                                                            job.get("title") or "",
                                                            job.get("company") or "")
        print(f"  outreach: linkedin dm ({profile}) -> {'sent' if dm_sent else 'not sent'} · {dm_msg}")
    else:
        dm_sent, dm_msg = await automations.send_linkedin_company_dm(job.get("company", ""),
                                                                    assets["platform_dm"],
                                                                    job.get("title") or "")
        print(f"  outreach: linkedin dm (company search) -> "
              f"{'sent' if dm_sent else 'not sent'} · {dm_msg}")
    return dm_sent, hr_email, f"{msg}; {dm_msg}", None


_CLOSING_SECONDS = 20     # how long a Ctrl-C'd run may hold the console to get the mail out


async def _closing_mail(job: dict, pdf_path, assets: dict) -> None:
    """Send the mail for a job whose application was interrupted, then get out of the way.

    Called from the unwind path when the operator closes the server with an employer's form still open.
    That recruiter is owed a heads-up on the way out - the alternative is a job nobody was ever told
    about - and the two facts are what keep it honest: outreach_at is stamped (write-once, so no later
    run can mail this person a second time), applied_at is NOT, and no verdict is written at all. That
    combination is precisely what the card's red 'you have not applied' alert is built from, and that is
    the point of it: the application is still owed, and it is owed to the operator to go and submit it.

    Bounded on purpose. The waterfall behind _outreach measures 43-58 seconds when it has to go hunting,
    and nobody who just pressed Ctrl-C wants to watch a terminal for a minute. Twenty seconds covers an
    address already on the row and the LinkedIn DM; a lookup still running at the deadline is dropped,
    the row is left exactly as it was, and the next run reaches that recruiter normally.

    ponytail: the stamp lands after the send, so a process killed *between* the two - forced exit rather
    than Ctrl-C, which is the path where uvicorn's 5s drain cuts this short - can leave a mail out with
    outreach_at unset, and the next run mails that recruiter again. The same window exists on the normal
    path; closing it needs a send-intent row written before the SMTP call, which is a schema change for
    a case nobody has hit.
    """
    if job.get("outreach_at"):
        return
    try:
        reached, hr_email, msg, msgid = await asyncio.wait_for(
            _outreach(job, pdf_path, assets), timeout=_CLOSING_SECONDS)
    except Exception as exc:
        print(f"  closing: no mail sent · {type(exc).__name__}: {exc}")
        return
    if not reached:
        print(f"  closing: no mail sent · {msg}")
        return
    await database.mark_reached(job["id"])
    await database.set_status(job["id"], None, hr_email=hr_email, email_msgid=msgid,
                              notes="closed with the application unfinished · the mail went out, you "
                                    "have not applied - submit it on the portal, then stamp it")
    print(f"  closing: mail sent · {msg} · application still owed")


def _ev(job_id: int, phase: str, state: str, msg: str = "", **extra) -> dict:
    return {"job_id": job_id, "phase": phase, "state": state, "msg": msg, **extra}


def _cached_assets(job: dict) -> dict | None:
    """The generated copy this row already owns, or None if there is nothing usable to reuse.

    Both halves must be present and both must be the SAME build: the column without the PDF describes a
    resume nobody can be sent, and the PDF without the column is a file with no subject line and no
    pitch to mail it with. The name is derived - resume_pdf_path is a pure function of (title, company)
    and render_pdf writes to exactly that path - so a file found there is this row's, not a namesake's.
    Checked against _REQUIRED rather than trusted: a payload from an older schema would otherwise fail
    deep inside the outreach with a KeyError, which reads as a bug in the mailer instead of a stale row.
    """
    blob, pdf_path = job.get("assets_json"), ai_engine.resume_pdf_path(job["title"], job["company"])
    if not blob or not pdf_path.exists():
        return None
    try:
        assets = json.loads(blob)
    except ValueError:
        return None
    if not isinstance(assets, dict) or any(k not in assets for k in ai_engine._REQUIRED):
        return None
    return {"assets": assets, "pdf_path": pdf_path}


async def _prepare_assets(job: dict) -> dict:
    """The AI + render (or fallback) half of a job, side-effect-safe to run AHEAD of its submit.

    Returns {"ok": True, "assets", "pdf_path", "events"} or {"ok": False, "at", "exc", "events"}.
    `events` are the ai/render phase events to REPLAY in order, so overlapping this stage never
    reorders a job's own phases. Nothing here submits or contacts anyone: the only writes are the
    resumable cli_session_id, the cached copy, and the PDF on disk, all of which a later submit can
    rely on.

    The AI engine is the one stage allowed to fail without losing the job: an outage drops to the
    static base resume (the fallback guard below) and the submit half runs exactly as it would have.
    A render/asset failure is a real bug and comes back ok=False blamed on 'render'.
    """
    job_id = job["id"]
    events: list[dict] = []
    if (cached := _cached_assets(job)) is not None:
        # Built for this exact row on a run that stopped before it finished - Ctrl-C at a manual-apply
        # prompt, or a kill - and the next run was asked to pick that up rather than pay for it twice.
        # Both halves have to be there for this to be worth anything, and both are checkable: the copy is
        # on the row, and the PDF name is a pure function of (title, company), so "already built" is a
        # fact about this run rather than a hope. What it also buys is that the attachment stays
        # byte-identical to the one an existing reply thread is already keyed against.
        print(f"job {job_id} · {job['company']} · {job['title']}")
        print(f"  resume: reusing {cached['pdf_path'].name} and its copy - already built, not rebuilding")
        events.append(_ev(job_id, "ai", "skip", "reusing what the interrupted run already generated"))
        events.append(_ev(job_id, "render", "skip", cached["pdf_path"].name))
        return {"ok": True, **cached, "events": events}
    events.append(_ev(job_id, "ai", "run", _PHASE_HINT["ai"]))
    print(f"job {job_id} · {job['company']} · {job['title']}")
    try:
        try:
            assets = await ai_engine.generate_tailored_assets(
                job["title"], job["company"], job.get("description") or job["title"],
                session_id=job.get("cli_session_id"),
            )
        except Exception as exc:
            traceback.print_exc()
            print(f"  AI generation failed, falling back to base resume · {_short(exc)}")
            assets, pdf_path = await asyncio.to_thread(_fallback_assets, job)
            print(f"  mode: fallback on source '{assets['meta']['source']}' (generic pitch, no tailoring)")
            events.append(_ev(job_id, "ai", "skip", f"AI down, base resume instead · {_short(exc)}"))
            events.append(_ev(job_id, "render", "skip", f"copied {Path(pdf_path).name}"))
        else:
            meta = assets.get("meta") or {}
            print(f"  claude-session: turn {meta.get('turn')} on {meta.get('model')}"
                  f" ({'seeded' if meta.get('seeded') else 'appended'})")
            session_id = meta.get("session_id")
            if session_id and session_id != job.get("cli_session_id"):
                await database.set_session_id(job_id, session_id)    # resumable even if a later step fails
            events.append(_ev(job_id, "ai", "ok", f"turn {meta.get('turn')} on {meta.get('model')}"))
            events.append(_ev(job_id, "render", "run", _PHASE_HINT["render"]))
            pdf_path = await asyncio.to_thread(                     # weasyprint is blocking
                ai_engine.render_pdf, assets, job["company"], job["title"]
            )
            events.append(_ev(job_id, "render", "ok", Path(pdf_path).name))
        print(f"  resume: {pdf_path}")
        try:
            await database.set_assets(job_id, json.dumps(assets))
        except (TypeError, ValueError):
            # Non-fatal by design. The cache only saves a rebuild; a value the encoder refuses costs
            # this job nothing it can feel, and must never fail one that already has its PDF in hand.
            print("  (generated copy not cached - a future run will rebuild it)")
        return {"ok": True, "assets": assets, "pdf_path": pdf_path, "events": events}
    except Exception as exc:
        traceback.print_exc()
        return {"ok": False, "at": "render", "exc": exc, "events": events}


PHASES = ("ai", "render", "apply", "outreach")
_PHASE_HINT = {"ai": "tailoring resume · email · DM in claude-session", "render": "weasyprint → PDF",
               "apply": "playwright submit", "outreach": "gmail / linkedin dm"}


def _short(exc: Exception, limit: int = 140) -> str:
    one = " ".join(f"{type(exc).__name__}: {exc}".split())
    return one if len(one) <= limit else one[:limit - 1] + "…"


async def _submit_events(job: dict, prep: dict):
    """The submit + routed-outreach half, given pre-made assets. Yields ai/render (replayed from
    prep) then apply/outreach/done. STRICTLY SERIAL across jobs: parallel submits from one IP trip
    anti-automation, so overlap in run_batch_events only ever covers _prepare_assets, never this."""
    job_id = job["id"]

    def ev(phase: str, state: str, msg: str = "", **extra) -> dict:
        return _ev(job_id, phase, state, msg, **extra)

    for e in prep["events"]:                     # replay the ai/render phases in the order they ran
        yield e
    if not prep["ok"]:                           # asset prep failed: blame its phase, do not submit
        await database.set_status(job_id, "failed", phase_fail="resume",
                                  notes=f"{prep['at']} raised: {_short(prep['exc'])}")
        yield ev(prep["at"], "fail", _short(prep["exc"]))
        yield ev("done", "fail", _short(prep["exc"]), ok=False)
        return
    assets, pdf_path = prep["assets"], prep["pdf_path"]
    at = "apply"
    try:
        # A row carrying applied_at already went into this employer's ATS on an earlier run. Submitting
        # again would put a second application in front of the same recruiter, which cannot be undone,
        # so the submit is skipped and only the outreach half is retried.
        if resubmit := bool(job.get("applied_at")):
            applied, apply_msg = True, f"already submitted {job['applied_at']} - not applying twice"
            print(f"  apply: skip · {apply_msg}")
            yield ev("apply", "ok", apply_msg)
        elif config.OUTREACH_ONLY:
            # Deliberately reported as a MISS, not a skip: applied is what stamps applied_at and what makes
            # the row terminal, so a deferred submit has to leave the row retryable or the one shot at this
            # employer is silently thrown away. The outreach half below still runs and still stamps
            # outreach_at, which is what stops that retry from mailing the recruiter a second time.
            applied, apply_msg = False, "outreach-only run - submit deferred, applied_at left unstamped"
            print(f"  apply: skip · {apply_msg}")
            yield ev("apply", "skip", apply_msg)
        else:
            yield ev("apply", "run", f"submitting on {job.get('platform') or '?'}")
            try:
                applied, apply_msg = await automations.execute_platform_apply(job, pdf_path)
            except BaseException:
                # Three ways to land here and one thing owed by all of them: Ctrl-C at the ENTER prompt
                # (RunAborted), a bare cancel from the shutdown path, or a plain crash. The form is
                # unfinished and the recruiter has heard nothing, so the mail goes out on the way past -
                # and no verdict is written, which leaves the row in 'not applied' carrying the red
                # alert. Then the unwind continues untouched: RunAborted still has to reach _run to
                # be reported as the operator's own stop rather than as a crash.
                await _closing_mail(job, pdf_path, assets)
                raise
            print(f"  apply: {'ok' if applied else 'no'} · {apply_msg}")
            yield ev("apply", "ok" if applied else "fail", apply_msg)
        if applied and not resubmit:
            await database.mark_submitted(job_id)   # the fact, stamped before outreach can change it

        at = "outreach"
        msgid = None
        # The mirror of the applied_at skip above: a row that already reached a human keeps that fact in
        # outreach_at, so a retry of the failed apply half does not mail the same recruiter twice.
        if reoutreach := bool(job.get("outreach_at")):
            reached, hr_email = True, job.get("hr_email")
            out_msg = f"already reached {job['outreach_at']} - not contacting twice"
            print(f"  outreach: skip · {out_msg}")
            yield ev("outreach", "ok", out_msg)
        else:
            yield ev("outreach", "run", _PHASE_HINT["outreach"])
            reached, hr_email, out_msg, msgid = await _outreach(job, pdf_path, assets)
            yield ev("outreach", "ok" if reached else "fail", out_msg)
        if reached and not reoutreach:
            await database.mark_reached(job_id)     # the send is as irreversible as the submit

        # A reached human is a real outcome and must show as one: 'emailed' with a mailbox on file, else
        # 'applied' (the DM-only convention). Reaching nobody after the submit LANDED is 'applied' too -
        # the application is in that employer's ATS whatever the mail did, and 'failed' was a lie about
        # the half that mattered more: measured on job 27, submitted to Toyota, mailed nobody, and the
        # card shouted FAILED over a job that had been applied to. The verdict names the strongest true
        # thing; the half that broke is the outreach chip on the card, which reads the fact (no
        # outreach_at) and goes red without this line having to say anything about it. Only a run that
        # reached nobody AND submitted nothing is 'failed'.
        # The one exception is OUTREACH_ONLY: there the application is deliberately owed, so a mail-only
        # run stays 'failed' on purpose (config documents this) and the normal retry applies later.
        # applied_at / outreach_at are the write-once facts that keep any retry from double-applying or
        # double-mailing regardless of this label.
        done = applied and reached
        if reached and (applied or not config.OUTREACH_ONLY):
            status = "emailed" if hr_email else "applied"
        elif applied:
            status = "applied"
        else:
            status = "failed"
        # Which halves this run tried and did not land, for the per-phase chips on the card. The label
        # above cannot carry it any more - a submitted row that reached nobody reads 'applied' - so
        # without this the failure would be visible nowhere. "" clears the chips; None (the default of
        # set_status) would leave a stale red on a half that just landed.
        broken = ",".join(name for name, landed in (("apply", applied), ("outreach", reached))
                          if not landed)
        # The verdict for both halves, on the row. template/email_msgid persist the reply-tracking
        # anchors (COALESCE, so a retry that reaches nobody never wipes a known thread id).
        await database.set_status(job_id, status, hr_email=hr_email, pdf_path=str(pdf_path),
                                  touch_applied_at=False, template=assets.get("selected_template"),
                                  email_msgid=msgid, phase_fail=broken,
                                  notes=f"apply: {apply_msg} | outreach: {out_msg}")
        print(f"  status: {status}")
        yield ev("done", "ok" if done else "fail", f"{apply_msg}; {out_msg}", ok=done)
    except Exception as exc:
        traceback.print_exc()
        await database.set_status(job_id, "failed", phase_fail=at, notes=f"{at} raised: {_short(exc)}")
        yield ev(at, "fail", _short(exc))        # blame the phase that actually broke, not the last one
        yield ev("done", "fail", _short(exc), ok=False)


async def run_job_events(job: dict | int):
    """run_job's body, yielded one event per phase so the dashboard can show what the bot is doing.

    Event: {"job_id", "phase", "state", "msg"}; phase in PHASES + ("done",), state in
    ("run", "ok", "fail", "skip"), and "ok" (bool) on the terminal event. run_job drains this, so the
    streamed and the blocking path are the same code — a phase cannot be observable and wrong.

    Two stages: _prepare_assets (AI + render, overlappable) then _submit_events (submit + outreach,
    strictly serial). Single-job behaviour is byte-identical to running them back to back.
    """
    if isinstance(job, int):                     # main.py passes the row; a job id is accepted too
        row = await database.get(job)
        if not row:
            yield {"job_id": job, "phase": "done", "state": "fail", "ok": False,
                   "msg": f"job {job} not found"}
            return
        job = row
    prep = await _prepare_assets(job)
    async for event in _submit_events(job, prep):
        yield event


async def run_job(job: dict | int) -> tuple[bool, str]:
    """1. ping claude-session  2. render PDF  3. submit the application  4. route outreach.

    Stage 1 is fault-isolated: if the AI engine is exhausted, content-blocked or behind a dead proxy,
    the job drops to the static base resume and keeps going. Everything after it still raises into the
    outer handler - a PDF that will not render or a DB write that fails is a bug worth seeing.

    Blocking wrapper over run_job_events, for callers that only want the verdict (run_batch, the CLI).
    """
    ok, msg = False, "pipeline produced no result"
    async for event in run_job_events(job):
        if event["phase"] == "done":
            ok, msg = event["ok"], event["msg"]
    return ok, msg


def _batch_status(view: str | None) -> str | None:
    """The status filter a view's batch runs under. None means every status, and only one view wants it.

    Every other queue is a narrowing of 'queued'. 'notapplied' is not: it is the set of rows that still
    OWE something, and a row whose mail went out while its application never landed carries the verdict
    'emailed' - so filtering that view to status='queued' would hide precisely the rows the operator
    opened the tab to finish, and the tab would list work that its own run-all then refused to touch.
    The predicate already excludes 'skipped', so nothing ineligible is dragged back in.
    """
    return None if view == "notapplied" else "queued"


async def run_batch_events(limit: int | None = None, view: str | None = None,
                           scraped: str | None = None, role: str | None = None):
    """Queued + eligible jobs, best-fit first, up to the remaining daily quota, streamed job by job.

    `view` narrows the run to a named queue view (database._VIEWS) so "apply all queued" on the recent
    tab applies today's scrape and nothing else; `scraped` narrows it to one scrape day, which is the
    same promise for the All tab's date filter; `role` to one family, so run-all under the dashboard's
    role filter starts on the same rows the page is showing. The fetch below is the SAME clause set and
    the SAME order the dashboard lists with, so the row at the top of the page is the row the batch
    starts on.

    ponytail: submits stay sequential on purpose — parallel submits from one IP is what trips platform
    anti-automation. The ONE overlap is a depth-1 prefetch: while a job submits+outreaches, the NEXT
    eligible job's assets (AI + render) are generated in the background, so the slow claude-session
    turn for job N+1 runs during job N's submit instead of after it. The prefetch is always awaited
    before that job is submitted, so nothing reorders and no job is ever submitted twice.
    """
    quota = await database.quota_left()
    if quota <= 0:
        yield {"job_id": 0, "phase": "batch", "state": "fail", "ok": False,
               "msg": f"daily cap {config.MAX_DAILY_APPLICATIONS} reached"}
        return
    budget = min(quota, limit) if limit else quota

    eligible: list[dict] = []
    for job in await database.fetch(_batch_status(view), limit=max(budget * 3, 30), order="fit",
                                    view=view, scraped=scraped, role=role):
        if len(eligible) >= budget:
            break
        ok, why = parsing.eligible(job)
        if not ok and job.get("applied_at"):
            # Eligibility gates what is worth APPLYING to, and this row is not going to apply again -
            # mark_submitted is write-once and _submit_events takes its resubmit branch. Its application
            # is already sitting in an employer's ATS, so there is nothing left here to decide; what it
            # still owes is the mail, and only the outreach half can deliver that. Measured on job 27:
            # clean_failed requeued it for exactly that mail, this gate re-skipped it because the posting
            # had aged out, and the row went back and forth between the two sweeps forever - buried in the
            # litter tab, invisible to the owed view, refused by revive, with a live application out and
            # nobody ever told about it.
            yield {"job_id": job["id"], "phase": "done", "state": "skip", "ok": False,
                   "msg": f"{why} · already applied {job['applied_at'][:10]}, "
                          f"running anyway for the outreach it still owes"}
            eligible.append(job)
            continue
        if not ok:
            # The reason is persisted, not just streamed. It was already computed and already shown once -
            # and then dropped, so the only way to learn why a row was rejected was to read the log of the
            # run that rejected it, which is gone by the next morning. `notes` is what the card renders as
            # the verdict line, and the reason IS the verdict here.
            await database.set_status(job["id"], "skipped", notes=why)
            yield {"job_id": job["id"], "phase": "done", "state": "skip", "ok": False, "msg": why}
            continue
        eligible.append(job)

    if not eligible:
        yield {"job_id": 0, "phase": "batch", "state": "ok", "ok": True, "total": 0,
               "msg": f"nothing queued and eligible in {view or 'the whole queue'}"}
        return
    # One event carrying the count, before the first job: the run banner can then say 3/12 instead of
    # only naming the job in flight, and a reader that attached late still learns the total.
    yield {"job_id": 0, "phase": "batch", "state": "run", "total": len(eligible),
           "msg": f"{len(eligible)} eligible · best-fit first"}

    ran = won = 0
    prefetch: dict | None = None                 # job i's assets, prepared during job i-1's submit
    for i, job in enumerate(eligible):
        prep = prefetch if prefetch is not None else await _prepare_assets(job)
        nxt = eligible[i + 1] if i + 1 < len(eligible) else None
        prefetch_task = asyncio.create_task(_prepare_assets(nxt)) if nxt else None
        try:
            async for event in _submit_events(job, prep):
                yield event
                if event["phase"] == "done":
                    ran += 1
                    won += bool(event["ok"])
            prefetch = await prefetch_task if prefetch_task else None
        except BaseException:
            # Anything that unwinds this job - the operator walking away from a manual apply, a cancel
            # from the shutdown path, a bare GeneratorExit - leaves the look-ahead generation running
            # with nothing left to submit it to. Cancel it here rather than let it spend a claude
            # session and write a PDF for a job this batch will never reach.
            if prefetch_task and not prefetch_task.done():
                prefetch_task.cancel()
            raise
    yield {"job_id": 0, "phase": "batch", "state": "ok" if won == ran else "fail", "ok": won == ran,
           "msg": f"{won}/{ran} succeeded · quota left {await database.quota_left()}"}


async def run_batch(limit: int | None = None) -> tuple[list[dict], str]:
    """Blocking wrapper over run_batch_events. Skipped rows are not results: they never ran."""
    results: list[dict] = []
    summary = ""
    async for event in run_batch_events(limit):
        if event["phase"] == "batch":
            summary = event["msg"]
        elif event["phase"] == "done" and event["state"] != "skip":
            results.append({"id": event["job_id"], "ok": event["ok"], "msg": event["msg"]})
    return results, summary


async def _selfcheck() -> None:
    """Orchestration only: AI + PDF stubbed, DRY_RUN blocks real submits and mail."""
    import tempfile
    from pathlib import Path

    config.DB_PATH = Path(tempfile.mkdtemp()) / "pipeline.db"
    # The fallback path writes a real PDF through ai_engine.resume_pdf_path, which is RESUME_DIR. Left
    # pointing at the live one, every selfcheck run drops a fake 'AcmeCorp' resume into the operator's
    # generated_resumes/ - and, since the resume cache checks for exactly that file, it also makes the
    # reuse assertions below pass for the wrong reason (a leftover from the previous run).
    config.RESUME_DIR = Path(tempfile.mkdtemp())
    config.DRY_RUN = True
    await database.init_db()
    await database.add_jobs([
        {"title": "DevSecOps Engineer", "company": "AcmeCorp", "job_url": "https://x/1",
         "platform": "linkedin", "salary": "14 LPA", "bond_terms": "no bond",
         "description": "SAST/DAST, Trivy, Kubernetes.", "status": "queued"},
        {"title": "Trainee", "company": "LowPay", "job_url": "https://x/2", "platform": "naukri",
         "salary": "2 LPA", "bond_terms": "none", "description": "", "status": "queued"},
    ])

    pdf = Path(tempfile.mkdtemp()) / "Prince_Jha_resume_DevSecOps_Engineer_AcmeCorp.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")

    async def _assets(*_a, **_k):
        return {"selected_template": "personal", "resume_data": {"name": "Prince Jha"},
                "email_subject": "s", "hr_email_pitch": ["a", "b", "c"], "platform_dm": "d",
                "meta": {"model": "claude-opus-5", "token_index": 1, "session_id": "sess-1",
                         "session_name": database.CLAUDE_SESSION, "turn": 1, "seeded": True}}

    ai_engine.generate_tailored_assets = _assets
    ai_engine.render_pdf = lambda *_a, **_k: pdf

    # The real resolver is a LIVE hunter/dork/osint sweep - 50 to 60 seconds here, and its verdict is
    # whatever the open internet says today. A dork either turns up a generic hr@ address for the
    # fixture company or it does not, and that alone flips this row between 'applied' and 'emailed':
    # the run failed and then passed on two consecutive attempts with no code change between them. The
    # resolver has its own scenarios further down. Declaring the miss here is what makes the assertion
    # below about the thing it names - the AI stub, the render stub and a dry-run submit.
    async def _no_hr(*_a, **_k):
        return False, None, "no HR email (declared miss for this scenario) - email skipped", None

    _real_resolver = automations.find_and_email_hr
    automations.find_and_email_hr = _no_hr
    try:
        good = next(j for j in await database.fetch("queued") if j["company"] == "AcmeCorp")
        ok, msg = await run_job(good)
        row = await database.get(good["id"])
        assert ok, msg
        assert row["status"] == "applied", row["status"]      # dry-run submits, no HR email to send
        assert row["pdf_path"] == str(pdf)
        assert row["cli_session_id"] == "sess-1", "CLI session must be saved for retries"
        print(f"run_job -> {ok} · {msg}")

        ok, msg = await run_job(good["id"])              # a bare job id must work too (mandate wording)
        assert ok, msg
        assert (await run_job(999_999))[0] is False, "a missing id must not raise"
    finally:
        automations.find_and_email_hr = _real_resolver

    # Event stream: the streamed and the blocking paths are ONE implementation, so a phase badge cannot
    # disagree with what ran. Order matters (the UI paints left to right) and 'done' must come last.
    # Channels are stubbed here - this gate is about event shape, not about sending anything.
    _real_mail_shape = automations.find_and_email_hr

    async def _quiet_mail(job, *_a, **_k):
        return True, "hr@acme.com", "emailed hr@acme.com (stub)", "<stub@host>"

    automations.find_and_email_hr = _quiet_mail
    try:
        await database.set_status(good["id"], "queued")
        events = [e async for e in run_job_events(good)]
    finally:
        automations.find_and_email_hr = _real_mail_shape
    assert [(e["phase"], e["state"]) for e in events] == [
        ("ai", "run"), ("ai", "ok"), ("render", "run"), ("render", "ok"),
        ("apply", "run"), ("apply", "ok"), ("outreach", "run"), ("outreach", "ok"),
        ("done", "ok")], [(e["phase"], e["state"]) for e in events]
    assert all(e["job_id"] == good["id"] and e["msg"] for e in events), events
    assert events[-1]["ok"] is True and "ok" not in events[0], "only the terminal event carries a verdict"

    # Outreach routing. Channels are stubbed so precedence is provable without sending anything.
    calls: list[str] = []
    _real_mail = automations.find_and_email_hr
    _real_dm = automations.send_linkedin_dm
    _real_company_dm = automations.send_linkedin_company_dm
    mail_result = (True, "hr@acme.com", "emailed hr@acme.com (db)", "<db@host>")

    async def _mail(job, *_a, **_k):
        calls.append("gmail")
        return mail_result

    async def _dm(profile_url, message, role="", company=""):
        # role/company fill a premium InMail's subject line; company wins when both are known
        calls.append(f"dm:{profile_url}:{role}:{company}")
        return True, f"linkedin dm: sent ({len(message)} chars)"

    async def _company_dm(company, message, role=""):
        calls.append(f"company_dm:{company}:{role}")
        return True, "linkedin dm: sent via company search"

    automations.find_and_email_hr, automations.send_linkedin_dm = _mail, _dm
    automations.send_linkedin_company_dm = _company_dm
    assets = await _assets()
    profile = "https://www.linkedin.com/in/acme-recruiter/"
    try:
        calls.clear()                                    # hr_email only -> gmail
        reached, hr_email, _, _mid = await _outreach({**good, "hr_email": "hr@acme.com"}, pdf, assets)
        assert (reached, hr_email, calls) == (True, "hr@acme.com", ["gmail"]), calls

        calls.clear()                                    # both -> gmail wins, DM must not fire
        await _outreach({**good, "hr_email": "hr@acme.com", "recruiter_linkedin_url": profile}, pdf, assets)
        assert calls == ["gmail"], f"gmail must win outright: {calls}"

        calls.clear()                                    # recruiter url only -> DM, no email attempt
        reached, hr_email, _, _mid = await _outreach({**good, "recruiter_linkedin_url": profile}, pdf, assets)
        assert reached and hr_email is None, (reached, hr_email)
        assert calls == [f"dm:{profile}:{good['title']}:{good['company']}"], \
            f"the role and the company must both reach the DM: {calls}"

        calls.clear()                                    # neither -> resolver email, DM never needed
        reached, hr_email, _, _mid = await _outreach(good, pdf, assets)
        assert reached and hr_email == "hr@acme.com" and calls == ["gmail"], calls

        calls.clear()                                    # neither, resolver misses -> company DM
        mail_result = (False, None, "no HR email (vault/jd/hunter/dork/osint all missed) - email skipped", None)
        reached, hr_email, msg, _mid = await _outreach(good, pdf, assets)
        assert reached and hr_email is None, (reached, hr_email)
        assert calls == ["gmail", f"company_dm:{good['company']}:{good['title']}"], calls
        assert "email skipped" in msg and "company search" in msg, msg

        # The guarantee: a mailbox on the row whose SEND fails is still an unreached job, so the DM has
        # to run anyway. This used to return early and the job got no outreach vector at all.
        calls.clear()
        mail_result = (False, "hr@acme.com", "email to hr@acme.com failed: SMTPAuthenticationError", None)
        reached, hr_email, msg, _mid = await _outreach({**good, "hr_email": "hr@acme.com"}, pdf, assets)
        assert reached and calls == ["gmail", f"company_dm:{good['company']}:{good['title']}"], calls
        assert hr_email == "hr@acme.com", "a real address stays on the row even when the send failed"

        calls.clear()                                    # ...and the named recruiter is preferred to a search
        reached, _hr, _msg, _mid = await _outreach(
            {**good, "hr_email": "hr@acme.com", "recruiter_linkedin_url": profile}, pdf, assets)
        assert reached and calls == ["gmail", f"dm:{profile}:{good['title']}:{good['company']}"], calls

        calls.clear()                                    # every channel dead -> honest failure
        mail_result = (False, None, "no HR email (vault/jd/hunter/dork/osint all missed) - email skipped", None)

        async def _dead_dm(_company, _message, _role=""):
            calls.append("company_dm")
            return False, "linkedin dm: no Message and no Connect button on this page"

        automations.send_linkedin_company_dm = _dead_dm
        reached, _hr, _msg, _mid = await _outreach(good, pdf, assets)
        assert not reached and calls == ["gmail", "company_dm"], calls
    finally:
        automations.find_and_email_hr = _real_mail
        automations.send_linkedin_dm = _real_dm
        automations.send_linkedin_company_dm = _real_company_dm

    # A DM-only win is still a real outcome: 'applied', never 'failed', and never 'emailed'.
    async def _dm_only_mail(job, *_a, **_k):
        return False, None, "no HR email - email skipped", None

    async def _dm_only(company, message, role=""):
        return True, "linkedin dm: sent via company search"

    automations.find_and_email_hr = _dm_only_mail
    automations.send_linkedin_company_dm = _dm_only
    try:
        await database.set_status(good["id"], "queued")
        ok, msg = await run_job(good)
        assert ok, msg
        assert (await database.get(good["id"]))["status"] == "applied", "a DM-only win is 'applied'"
    finally:
        automations.find_and_email_hr = _real_mail
        automations.send_linkedin_company_dm = _real_company_dm

    _, summary = await run_batch()
    assert (await database.get(2))["status"] == "skipped", "2 LPA must be gated out"
    assert [e["phase"] async for e in run_batch_events()] == ["batch"], "an empty queue is one summary"
    print(f"run_batch -> {summary}")

    def _boom(*_a, **_k):
        raise RuntimeError("weasyprint exploded")

    ai_engine.render_pdf = _boom
    await database.set_status(good["id"], "queued")
    ok, msg = await run_job(good)
    assert not ok and "RuntimeError" in msg, msg
    assert (await database.get(good["id"]))["status"] == "failed"

    # A mid-flight crash must blame the phase that broke, or the dashboard leaves a spinner on forever.
    await database.set_status(good["id"], "queued")
    broke = [(e["phase"], e["state"]) async for e in run_job_events(good)]
    assert broke[-2:] == [("render", "fail"), ("done", "fail")], broke

    # AI outage: the job must NOT abort. It falls back to a static base resume under the mandated
    # per-job filename, and the OSINT + Playwright phases still run. A dead AI engine costs tailoring,
    # never the application. render_pdf stays broken here on purpose - the fallback must not call it.
    async def _dead_ai(*_a, **_k):
        raise RuntimeError("403 quota exceeded on every token; 400 content-blocked after scrub")

    ai_engine.generate_tailored_assets = _dead_ai
    hit: list[str] = []
    _real_apply = automations.execute_platform_apply

    async def _watch_apply(job, pdf):
        hit.append(f"apply:{Path(pdf).name}")
        return True, "dry run: would submit"

    automations.execute_platform_apply = _watch_apply
    automations.find_and_email_hr = _mail
    mail_result = (True, "hr@acme.com", "emailed hr@acme.com (fallback resume)", "<fb@host>")
    try:
        await database.set_status(good["id"], "queued")
        ok, msg = await run_job(good)
        row = await database.get(good["id"])
        assert ok, f"an AI outage must not fail the job: {msg}"
        assert row["status"] == "emailed", row["status"]
        sent = Path(row["pdf_path"])
        assert sent.name == "Prince_Jha_resume_DevSecOps_Engineer_AcmeCorp.pdf", sent.name
        assert sent.exists() and sent.stat().st_size > 0, "the fallback must attach a real file"
        assert hit == [f"apply:{sent.name}"], f"the platform submit was skipped: {hit}"
        print(f"fallback -> {row['status']} · {sent.name} ({sent.stat().st_size} bytes)")

        # The dashboard must read the fallback as 'skipped', not 'failed': the job still went out.
        await database.set_status(good["id"], "queued")
        fb = [(e["phase"], e["state"]) async for e in run_job_events(good)]
        assert fb[:3] == [("ai", "run"), ("ai", "skip"), ("render", "skip")], fb
        assert fb[-1] == ("done", "ok"), fb

        # Genuinely pending again. Both facts have to go: applied_at and outreach_at are deliberately
        # write-once, so an earlier test's stamp would make the phase under test skip itself.
        async def _repend(job_id: int) -> None:
            await database.set_status(job_id, "queued")
            async with database.connect() as db:
                await db.execute(
                    "UPDATE jobs SET applied_at = NULL, outreach_at = NULL WHERE id = ?", [job_id])

        # BOTH halves or it is not done - but the label now names the strongest true thing. A submitted
        # application that reached nobody is 'applied' (it IS in that employer's ATS; 'failed' was a lie
        # about the half that mattered more) and applied_at is stamped anyway so the retry cannot apply
        # to that employer a second time. What says the outreach broke is the outreach chip on the card,
        # which reads outreach_at - NULL here - and the ok=False on the run itself.
        async def _no_mail(*_a, **_k):
            return False, None, "no HR email (vault/jd/hunter/dork/osint all missed) - email skipped", None

        async def _no_dm(*_a, **_k):
            return False, "linkedin outreach failed: neither connect-note nor message succeeded"

        automations.find_and_email_hr = _no_mail
        automations.send_linkedin_company_dm = _no_dm
        try:
            await _repend(good["id"])
            hit.clear()
            ok, msg = await run_job(good)
            row = await database.get(good["id"])
            assert not ok, "an application that reached nobody is not a terminal success"
            assert row["status"] == "applied", row["status"]
            assert row["outreach_at"] is None, "the card cannot go red on a half with a stamp on it"
            assert row["applied_at"], "the submit happened - not recording it re-applies on the retry"
            assert len(hit) == 1, hit
            stamped = row["applied_at"]

            # The retry of that row: outreach runs again, the submit must NOT.
            hit.clear()
            again = [(e["phase"], e["state"]) async for e in run_job_events(await database.get(good["id"]))]
            assert hit == [], f"the same employer got a second application: {hit}"
            assert ("apply", "ok") in again and ("apply", "run") not in again, again
            assert (await database.get(good["id"]))["applied_at"] == stamped, "applied_at moved"
            assert await database.applied_today() >= 1, "applied_at is not what the daily cap counts"
            assert (await database.get(good["id"]))["outreach_at"] is None, \
                "nobody was reached, so nothing may be stamped - else the retry never mails at all"
        finally:
            automations.find_and_email_hr = _mail
            automations.send_linkedin_company_dm = _real_company_dm

        # The other half of the same story: a row whose mail landed but whose apply failed. The retry must
        # re-apply and NOT re-mail - a second application is a duplicate, a second mail is a stranger's
        # inbox twice. Measured before outreach_at existed: two rounds reached the same five people.
        async def _no_apply(*_a, **_k):
            return False, "no apply control after 188-word render (external-apply or expired session)"

        _apply_before = automations.execute_platform_apply
        automations.execute_platform_apply = _no_apply
        try:
            await _repend(good["id"])
            ok, _msg = await run_job(good)
            row = await database.get(good["id"])
            assert not ok and row["status"] == "emailed", (ok, row["status"])
            assert row["outreach_at"] and not row["applied_at"], dict(row)
            sent_at = row["outreach_at"]

            mailed: list[str] = []

            async def _count_mail(*a, **k):
                mailed.append("x")
                return await _mail(*a, **k)

            automations.find_and_email_hr = _count_mail
            retry = [(e["phase"], e["state"]) async for e in run_job_events(await database.get(good["id"]))]
            assert mailed == [], f"a recruiter was contacted a second time: {mailed}"
            assert ("outreach", "ok") in retry and ("outreach", "run") not in retry, retry
            assert ("apply", "run") in retry, "the failed half must still be retried"
            assert (await database.get(good["id"]))["outreach_at"] == sent_at, "outreach_at moved"
        finally:
            automations.execute_platform_apply = _apply_before
            automations.find_and_email_hr = _mail

        # OUTREACH_ONLY defers the one application this employer will ever accept. The submit must not be
        # attempted AND applied_at must stay NULL - a stamp there retires the row and throws that shot away.
        submits: list[str] = []

        async def _would_apply(*_a, **_k):
            submits.append("x")
            return True, "submitted"

        _apply_before, config.OUTREACH_ONLY = automations.execute_platform_apply, True
        automations.execute_platform_apply = _would_apply
        try:
            await _repend(good["id"])
            ok, _msg = await run_job(good)
            row = await database.get(good["id"])
            assert submits == [], "outreach-only still submitted the application"
            assert not row["applied_at"], "a deferred submit stamped applied_at and retired the row"
            assert row["outreach_at"], "the mail went out and was not recorded - a retry mails them twice"
            assert not ok and row["status"] == "failed", "an owed application must not read as terminal"
            assert "deferred" in (row["notes"] or ""), row["notes"]
        finally:
            automations.execute_platform_apply, config.OUTREACH_ONLY = _apply_before, False

        no_sources = ai_engine.available_sources                 # no readable resume at all: honest failure
        ai_engine.available_sources = lambda: {}
        try:
            await database.set_status(good["id"], "queued")
            ok, msg = await run_job(good)
            assert not ok and "nothing to send" in msg, msg
            assert (await database.get(good["id"]))["status"] == "failed"
        finally:
            ai_engine.available_sources = no_sources
    finally:
        automations.execute_platform_apply = _real_apply
        automations.find_and_email_hr = _real_mail
    print("pipeline.py selfcheck ok · exceptions contained, statuses correct")


if __name__ == "__main__":
    async def _main() -> None:
        # The real HR resolver runs here (only sending is stubbed/DRY_RUN), and its tier-3 dork launches
        # the osint browser - so the driver has to be stopped or the process exits on a live node.exe.
        try:
            await _selfcheck()
        finally:
            await automations.shutdown()

    asyncio.run(_main())
