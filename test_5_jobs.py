"""Temporary harness: run N pending jobs from N DIFFERENT companies and prove both halves landed.

    python test_5_jobs.py --dry            # full path, nothing sent - run this first
    python test_5_jobs.py                  # LIVE: real applications, real email, real LinkedIn DMs
    python test_5_jobs.py --no-hitl        # LIVE, unattended: never block on an ATS login prompt
    python test_5_jobs.py --skip=35,39     # LIVE, minus rows a human has to drive (Workday, canvas portals)
    python test_5_jobs.py --outreach-only  # LIVE mail/DM only; the one application per employer is deferred
    python test_5_jobs.py --selfcheck      # scratch DB, no network: proves a rehearsal leaves no stamp behind
    python test_5_jobs.py --scrape --dry    # top the queue up first if fewer than N companies are pending

Terminal success for one job = the application went in AND at least one human was reached (email or DM),
which is exactly what pipeline.run_job_events now writes as 'applied'/'emailed'. Exit code is 0 only if
every selected job reached that state, so this is usable as a gate.

One company per job, always: a queue built from one search term is full of duplicate employers, and five
applications to one company in five minutes is what a recruiter sees as a bot, not as five candidates.
"""
from __future__ import annotations

import asyncio
import sys

import automations
import config
import database
import pipeline

DEFAULT_N = 5
_PENDING = ("new", "queued", "failed")
# outreach_at belongs here for the same reason applied_at does, and it was the more dangerous omission: a
# DRY RUN reaches nobody, but the DM stub returns True, so reached=True and mark_reached stamps the row.
# Left stamped, the pipeline believes that recruiter has been contacted and the LIVE run skips them forever
# - a dry rehearsal silently retiring a real outreach. notes rides along so a rehearsal's verdict does not
# outlive it on the row.
_SNAP = ("status", "applied_at", "outreach_at", "hr_email", "pdf_path", "notes")


async def restore(snaps: list[dict]) -> None:
    """Put the selected rows back exactly as they were.

    Only ever called after a DRY RUN. Nothing was submitted or sent, so leaving applied_at stamped would
    make run_job_events skip the submit on the first LIVE run - the job would look applied and never be -
    and leaving outreach_at stamped would make it skip the mail.
    """
    async with database.connect() as db:
        for s in snaps:
            await db.execute(
                "UPDATE jobs SET status=?, applied_at=?, outreach_at=?, hr_email=?, pdf_path=?, notes=? "
                "WHERE id=?", [*(s[k] for k in _SNAP), s["id"]])
    print(f"dry run: restored {len(snaps)} row(s) - no applied_at or outreach_at left behind")


async def pick_jobs(n: int, skip: set[int] = frozenset()) -> list[dict]:
    """n pending rows, one per company, never-submitted first and freshest first.

    Freshest first is not a preference - a posting that closed cannot be applied to at all, and the
    board then shows no apply control, so an old row is a guaranteed apply failure rather than a slow
    one. A row with applied_at already went into that employer's ATS, so it is picked last and only to
    retry the outreach half - pipeline.run_job_events skips its submit rather than applying twice.

    `skip` exists because a re-run mails that row's recruiter again: the outreach half is not idempotent,
    so proving a fixed apply path belongs on rows nobody has written to yet.
    """
    rows = [r for r in await database.fetch() if r["status"] in _PENDING and r["id"] not in skip]
    # An outreach-only run has exactly one half to do, so a row whose EMPLOYER was already reached owes
    # nothing on it. Row-level would not be enough: two rows can carry the same company, and dropping only
    # the reached row hands back its sibling - which is how NTT DATA got a second touch inside one day, the
    # exact thing "one company per job" exists to stop. A normal run still picks these rows: they owe the
    # application.
    # ponytail: company-level here only. The pipeline's own guard is per-row (outreach_at), so a normal run
    # can still mail a second recruiter at one employer - defensible (different person, different req), and
    # worth a company-level guard in _resolve_hr only if a recruiter ever complains.
    if config.OUTREACH_ONLY:
        done = {(r["company"] or "").strip().lower() for r in rows if r["outreach_at"]}
        rows = [r for r in rows if (r["company"] or "").strip().lower() not in done]
    rows.sort(key=lambda r: (r["applied_at"] is not None, -r["id"]))
    picked: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        key = (row["company"] or "").strip().lower()
        # "Unknown company" is a scrape placeholder, not an employer: the email waterfall has nothing to
        # corroborate a domain against and the DM fallback would search LinkedIn for the literal words.
        if not key or key in seen or key.startswith("unknown"):
            continue
        seen.add(key)
        picked.append(dict(row))
        if len(picked) == n:
            break
    return picked


async def run_one(job: dict) -> dict:
    """Drive one job through the real generator and report which phases actually landed."""
    phases: dict[str, str] = {}
    async for event in pipeline.run_job_events(job):
        phase, state = event.get("phase", "?"), event.get("state", "?")
        if state in ("ok", "fail"):
            phases[phase] = state
    row = await database.get(job["id"]) or {}
    return {"id": job["id"], "company": job["company"], "title": job["title"],
            "platform": job["platform"], "status": row.get("status"),
            "applied_at": row.get("applied_at"), "hr_email": bool(row.get("hr_email")),
            "apply": phases.get("apply", "-"), "outreach": phases.get("outreach", "-"),
            "ok": phases.get("done") == "ok"}


async def selfcheck() -> int:
    """Prove the two things a rehearsal must never get wrong, on a throwaway DB.

    Both were live bugs: a DRY RUN reaches nobody, but the send stubs return True, so mark_reached stamps
    outreach_at for real - and a stamp left behind makes every later LIVE run skip that recruiter forever.
    """
    import tempfile
    from pathlib import Path

    live, scratch = config.DB_PATH, Path(tempfile.mkdtemp()) / "scratch.db"
    config.DB_PATH = scratch                      # every database helper defaults to it
    try:
        await database.init_db()
        await database.add_jobs([{"title": "Reached Req A", "company": "Reached Co", "platform": "linkedin",
                                  "job_url": "https://example.invalid/1", "status": "queued"},
                                 {"title": "Reached Req B", "company": "Reached Co", "platform": "linkedin",
                                  "job_url": "https://example.invalid/3", "status": "queued"},
                                 {"title": "Fresh Req", "company": "Fresh Co", "platform": "linkedin",
                                  "job_url": "https://example.invalid/2", "status": "queued"}])
        rows = {r["title"]: r["id"] for r in await database.fetch()}
        await database.mark_reached(rows["Reached Req A"])

        config.OUTREACH_ONLY = True
        picked = [j["company"] for j in await pick_jobs(5)]
        # Not just the reached row: its SIBLING at the same employer must go too, or the run touches that
        # company twice in a day through a second req.
        assert picked == ["Fresh Co"], f"an outreach-only run must not spend a slot on a reached employer: {picked}"
        config.OUTREACH_ONLY = False
        assert len(await pick_jobs(5)) == 2, \
            "a reached row still owes its application, so a normal run must keep picking it"

        # The undo has to cover outreach_at, or the rehearsal permanently silences a real recruiter.
        snaps = [{k: j[k] for k in _SNAP} | {"id": j["id"]} for j in await database.fetch()]
        await database.mark_reached(rows["Fresh Req"])
        await database.set_status(rows["Fresh Req"], "failed", notes="dry-run: would email nobody")
        await restore(snaps)
        back = await database.get(rows["Fresh Req"])
        assert not back["outreach_at"], "restore left a rehearsal's outreach_at on the row"
        assert not back["notes"] and back["status"] == "queued", back
        assert (await database.get(rows["Reached Req A"]))["outreach_at"], \
            "restore erased a REAL send - that row's recruiter would now be mailed twice"
    finally:
        config.DB_PATH = live
    print("test_5_jobs selfcheck ok")
    return 0


async def main(argv: list[str]) -> int:
    # Job titles carry em dashes and non-latin1 characters, and this console is cp1252: without this the
    # harness dies mid-loop on a print, after the application already went out. write_through because a
    # redirected stdout is block-buffered, and a run that hangs on an ATS prompt would show nothing at all.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    if "--selfcheck" in argv:
        return await selfcheck()
    n = DEFAULT_N
    skip: set[int] = set()
    ids: list[int] = []
    for arg in argv:
        if arg.startswith("--n="):
            n = int(arg.split("=", 1)[1])
        if arg.startswith("--skip="):
            skip = {int(x) for x in arg.split("=", 1)[1].replace(",", " ").split()}
        if arg.startswith("--ids="):
            # Named rows instead of the picker. A row that already half-landed (applied but not reached,
            # or reached but not applied) has to be driven directly: pick_jobs would keep handing back the
            # freshest never-touched rows, and every extra row it picks re-mails another live recruiter.
            ids = [int(x) for x in arg.split("=", 1)[1].replace(",", " ").split()]
    if "--dry" in argv:
        config.DRY_RUN = True                      # read at call time by every send path
    if "--no-hitl" in argv:
        # A pty with nobody watching it is not a human: the ATS interceptor's ENTER wait is unbounded by
        # mandate, so an unattended run blocks on job 1 forever. Off, those jobs report what they need.
        config.HITL = False
    if "--outreach-only" in argv:
        # For a degraded AI engine: reach the humans now, keep the one application per employer for a run
        # that can tailor the resume. The harness scores such a row as REACHED, not passed - the apply half
        # is still owed.
        config.OUTREACH_ONLY = True
    print(f"mode: {'DRY RUN - nothing is sent' if config.DRY_RUN else 'LIVE - real sends'}"
          f" · headless={config.HEADLESS} · hitl={config.HITL}"
          f"{' · OUTREACH ONLY (no submits)' if config.OUTREACH_ONLY else ''}"
          f" · target={n} jobs, {n} companies")

    await database.init_db()
    if "--scrape" in argv and len(await pick_jobs(n, skip)) < n:
        print("topping the queue up first...")
        print(f"  scrape: {await automations.scrape_new_jobs()}")

    jobs = [dict(r) for r in [await database.get(i) for i in ids] if r] if ids \
        else await pick_jobs(n, skip)
    if ids:
        n = len(jobs)                              # an explicit id list IS the target, not a shortfall
    if len(jobs) < n:
        # Honest stop: inventing jobs, or applying twice to one company to reach the count, is worse
        # than reporting the shortfall. --scrape tops the queue up from the boards.
        print(f"\nonly {len(jobs)} pending job(s) from distinct companies - need {n}. "
              f"Run with --scrape, or scrape from the dashboard, then re-run.")
        if not jobs:
            return 2

    quota = await database.quota_left()
    if quota < len(jobs) and not config.OUTREACH_ONLY:      # an outreach-only run spends no application
        print(f"daily cap: {quota} application(s) left of {config.MAX_DAILY_APPLICATIONS} - "
              f"running the first {quota}")
        jobs = jobs[:quota]

    results = []
    snaps = [{k: job[k] for k in _SNAP} | {"id": job["id"]} for job in jobs]
    for i, job in enumerate(jobs, 1):
        print(f"\n{'=' * 78}\n[{i}/{len(jobs)}] job {job['id']} · {job['company']} · {job['title']}")
        try:
            results.append(await run_one(job))
        except Exception as exc:                   # a throw here is a harness bug, not a job outcome
            print(f"  harness: {type(exc).__name__}: {exc}")
            results.append({"id": job["id"], "company": job["company"], "title": job["title"],
                            "platform": job["platform"], "status": "harness-error", "ok": False,
                            "apply": "-", "outreach": "-", "applied_at": None, "hr_email": False})

    print(f"\n{'=' * 78}\nRESULTS")
    for r in results:
        vector = "email" if r["hr_email"] else ("dm" if r["outreach"] == "ok" else "none")
        # An outreach-only run is scored on the half it was asked to do. `ok` stays False on the row - the
        # application is still owed - so the verdict word is REACHED, never PASS: nothing here may read as
        # "this job is done" while no application exists.
        verdict = ("REACHED" if r["outreach"] == "ok" else "MISS") if config.OUTREACH_ONLY \
            else ("PASS" if r["ok"] else "FAIL")
        print(f"  {verdict:<7} · {r['id']:>4} · {r['company'][:26]:<26} "
              f"{r['platform']:<12} apply={r['apply']:<5} outreach={r['outreach']:<5} "
              f"vector={vector:<5} status={r['status']}")
    won = [r for r in results if r["ok"]]
    submitted = [r for r in results if r["applied_at"]]
    reached = [r for r in results if r["outreach"] == "ok"]
    print(f"\n{len(won)}/{len(results)} terminal success · {len(submitted)} application(s) on file · "
          f"{sum(1 for r in results if r['hr_email'])} by email, "
          f"{sum(1 for r in won if not r['hr_email'])} by DM")
    # Before any early return: a rehearsal that leaves outreach_at behind has silenced a real recruiter for
    # good, so the undo cannot sit behind a mode check.
    if config.DRY_RUN:
        await restore(snaps)
    await automations.shutdown()
    if config.OUTREACH_ONLY:
        print(f"{len(reached)}/{len(results)} human(s) reached · submits deferred: every one of these rows "
              f"still owes an application, and retrying it will not re-contact the recruiter")
        return 0 if len(reached) == len(results) == n else 1
    return 0 if (won and len(won) == len(results) == n) else 1


if __name__ == "__main__":
    factory = asyncio.ProactorEventLoop if sys.platform == "win32" else None
    raise SystemExit(asyncio.run(main(sys.argv[1:]), loop_factory=factory))
