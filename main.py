"""FastAPI entrypoint. Boot with `python main.py` — see the loop note below before using uvicorn's CLI.

Playwright drives Chromium over a subprocess pipe, and only the Windows ProactorEventLoop implements
asyncio.create_subprocess_exec. On a SelectorEventLoop every browser call dies with NotImplementedError
inside playwright/_impl/_transport.py. Measured on uvicorn 0.52.4 / Python 3.14.6 in this repo:

    uvicorn main:app                     -> ProactorEventLoop        (Playwright works)
    uvicorn main:app --reload            -> _WindowsSelectorEventLoop (NotImplementedError)
    uvicorn main:app --reload --loop none -> ProactorEventLoop        (Playwright works)

uvicorn no longer honours the event loop *policy* at all: Server.run() calls
asyncio.run(..., loop_factory=config.get_loop_factory()), and asyncio_loop_factory() hands back
SelectorEventLoop whenever use_subprocess is true — which --reload and --workers>1 both set. So the
policy patch below cannot fix the --reload path; `python main.py` pins the loop factory instead, and
the lifespan guard refuses to serve on a loop that cannot spawn a browser.
"""
import asyncio
import sys
import time
import traceback

if sys.platform == "win32":
    # Mandated policy patch: covers `python main.py`, `python -c "import main"` and any embedder that
    # calls asyncio.run/get_event_loop itself. Deprecated on 3.12+ and ignored by uvicorn's CLI, so it
    # is a floor, not the fix — the guard in lifespan() is what actually holds the line.
    with __import__("warnings").catch_warnings():
        __import__("warnings").simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import AsyncIterator
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

import automations
import config
import database
import parsing
import social

try:
    import pipeline                          # pulls ai_engine + automations; weasyprint stays lazy
except Exception as exc:                     # a broken GTK/jobspy install must degrade to a UI message
    pipeline, _PIPELINE_IMPORT_ERROR = None, f"{type(exc).__name__}: {exc}"
    print(f"pipeline unavailable: {_PIPELINE_IMPORT_ERROR}")
else:
    _PIPELINE_IMPORT_ERROR = ""


def _assert_subprocess_loop() -> str:
    """Fail startup on a loop that cannot spawn Chromium, instead of 90 seconds into a real run.

    A wrong loop is not a soft degradation here: every route worth hitting ends in Playwright, so a
    dashboard that boots and then throws NotImplementedError per click is worse than a refused boot.
    """
    loop = asyncio.get_running_loop()
    name = type(loop).__name__
    if sys.platform == "win32" and not isinstance(loop, asyncio.ProactorEventLoop):
        raise RuntimeError(
            f"{name} cannot spawn subprocesses, so Playwright and the Claude CLI will both fail. "
            "uvicorn picks this loop whenever --reload or --workers>1 is set. "
            "Boot with `python main.py`, or `uvicorn main:app --reload --loop none` if you want reload."
        )
    return name


# One BROWSER at a time - and never a refusal. Every Playwright run drives the same persistent profile
# per platform, cached module-side in automations._CONTEXTS, so two runs at once are two windows on one
# login and a driver restart on one side pulls the context out from under the other. So browser work is
# QUEUED behind one lock instead of being turned away: the operator starts whatever he wants, in the
# order he wants, and each task begins the moment the previous one frees the browser. Everything with no
# browser in it (the inbox poll) skips the lock entirely and runs beside a batch.
# ponytail: one lock for all browser work, not per-platform - the batch already walks every board, so a
# per-platform split only pays off the day two tasks need different boards at the same moment.
_BROWSER = asyncio.Lock()


async def _guarded_catchup() -> AsyncIterator[dict]:
    """A shut lid is not a reason to miss a nudge: poll the inbox, then send what came due, once at boot.

    Both gates are calendar comparisons in SQL (datetime('now','localtime')), so a follow-up that fell
    due on day 5 while the machine was off goes out on the next boot instead of never - which is the
    whole point of counting days rather than uptime. The order is the point and runs in this sequence:

      1. poll the inbox. A reply that arrived while the lid was shut has to strike that row off the
         nudge list before the sweep looks at it, and the same pass reconciles every "nothing was sent"
         claim against the Sent mailbox.
      2. clear what the killed run left behind. It runs AFTER the Sent sweep on purpose, because that
         sweep is what proves whether a row's outreach is already spent - clearing first would requeue
         and re-apply to an employer the row was only ever waiting on a reply from.
      3. send what came due.
      4. retire what has aged out. LAST, and after the inbox poll for the same reason step 2 is: a reply
         that arrived while the machine was shut is what keeps its row out of the purge, and the reply
         can only be on the row once step 1 has read the mailbox.

    A task like any other, under the same browser lock, because it drives the same mailbox and the same
    browser profiles a run does - so it queues behind whatever is already going instead of colliding with
    it. Failures are reported and the next step still runs - a dead socket, a bad credential or a missing
    resume must never stop the app booting.
    """
    for step, call in (("replies", automations.poll_replies),
                       ("cleanup", automations.clean_failed),
                       ("followups", automations.run_followups),
                       ("purge", automations.purge_old_jobs)):
        yield _task("run", f"catch-up · {step}…")
        try:
            out = await call()
        except Exception as exc:
            out = f"failed: {type(exc).__name__}: {exc}"
        print(f"boot catch-up · {step}: {out}")
        yield _task("run", f"catch-up · {step} · {out}")
    yield _task("ok", "catch-up done", ok=True)


async def _release_streams() -> None:
    """Unpark every attached reader, then stop the run worker, so shutdown can actually proceed.

    /pipeline/run-all and /run/attach answer with a chunked body that parks on `await queue.get()` until
    the run worker emits its sentinel - and the dashboard holds that connection open, and re-opens
    /run/attach on every page load while a run is registered. Closing the server does not wake those
    readers, so uvicorn's drain waited for the whole Playwright batch before the lifespan shutdown (and
    therefore automations.shutdown) was even reached. The registry is cleared first so a page that
    renders during teardown paints as idle rather than as a run nobody is driving.
    """
    _TASKS.clear()
    _fan(None)
    for task in list(_LIVE):
        task.cancel()
    if _LIVE:
        await asyncio.wait(_LIVE, timeout=5)


class _Server(uvicorn.Server):
    """Unpark the dashboard's stream readers the instant Ctrl-C lands, before uvicorn's drain runs.

    /pipeline/run-all and /run/attach answer with a chunked body parked on `await queue.get()`, and the
    only thing that wakes those readers is the None sentinel _release_streams fans out - which runs in
    the lifespan shutdown, which uvicorn reaches only AFTER it has finished waiting for connections to
    close. The drain was therefore waiting on a connection for the very call that ends the drain: it
    burned the whole graceful timeout and then cancelled the reader, which the operator saw as
    "Cancel 1 running task(s), timeout graceful shutdown exceeded" followed by a CancelledError traceback
    out of the ASGI app on an otherwise ordinary Ctrl-C. Fanning the sentinel from the exit handler puts
    the unpark on the near side of the drain, so the generators return, the connections close by
    themselves, and the timeout path is a backstop instead of the routine. Guarded on should_exit so a
    second Ctrl-C still means force, and idempotent with the same two calls in _release_streams.
    """

    def handle_exit(self, sig, frame):
        if not self.should_exit:
            _TASKS.clear()
            _fan(None)
        super().handle_exit(sig, frame)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"event loop: {_assert_subprocess_loop()} (subprocess-capable)")
    await database.init_db()
    # Sanitize the queue that already exists: rows written before (company, title) became the dedupe
    # key are still in there, and this is also where the UNIQUE index over that pair gets created.
    if dropped := await database.remove_duplicates():
        print(f"deduplicated queue: dropped {dropped} rows sharing a (company, title) with an older row")
    if config.CATCHUP_ON_BOOT:
        # Deliberately NOT awaited. The catch-up's first step sweeps the whole inbox and that is minutes
        # of IMAP on a real mailbox - and for as long as it runs, uvicorn never reaches "Application
        # startup complete", so every connection is refused and the app reads as dead at launch.
        # Measured live: that is exactly what the operator hit. None of it is needed to serve a page,
        # it is a catch-up rather than a precondition, and the browser lock already stops it colliding
        # with a run the operator starts while it is still going. Tracked in _LIVE like every other
        # task, so the shutdown path cancels it instead of waiting out the mailbox.
        print("catch-up: running in the background, the app is serving already")
        _start("catchup", "catchup", _guarded_catchup)
    yield
    print("shutting down: releasing readers, stopping the run, closing browsers")
    await _release_streams()
    await automations.shutdown()
    print("shutdown complete")


app = FastAPI(title="ApplyPilot", version="3.0", lifespan=lifespan)

for _d in config.RUNTIME_DIRS:
    _d.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
# The daily build's card images, served so the dashboard can show the picture that actually went out
# with each post instead of only naming it.
app.mount("/social", StaticFiles(directory=config.SOCIAL_DIR), name="social")
templates = Jinja2Templates(directory=config.TEMPLATES_DIR)
templates.env.filters.update(lpa=parsing.lpa, bond_years=parsing.bond_years)
templates.env.globals.update(cfg=config)


def _rows(request: Request, jobs, banner: str, ok: bool) -> HTMLResponse:
    """tbody fragment for the routes that answer with the queue and a one-line result banner."""
    return templates.TemplateResponse(
        request=request, name="_rows.html",
        context={"jobs": jobs, "banner": banner, "ok": ok}
    )


def _filter(status: str | None, view: str | None, scraped: str | None = None,
            role: str | None = None) -> None:
    """Reject an unknown filter rather than silently serving the unfiltered queue.

    `scraped` is the All tab's date picker. It is a bound parameter, not SQL text, but an unparseable
    one would still quietly show every row under a chosen date, which is worse than an error - so it is
    parsed here and a non-date is a 400 like any other bad filter. `role` is the same bargain: keys of
    config.ROLES only, so a typo is a 400 and not a page that looks like the family is empty.
    """
    if status and status not in config.JOB_STATUSES:
        raise HTTPException(400, f"unknown status: {status}")
    if view and view not in database.VIEWS:
        raise HTTPException(400, f"unknown view: {view}")
    if scraped:
        try:
            date.fromisoformat(scraped)
        except ValueError:
            raise HTTPException(400, f"not a date: {scraped}") from None
    if role and role not in config.ROLES:
        raise HTTPException(400, f"unknown role: {role}")


async def _queued(status: str | None = None, view: str | None = None,
                  scraped: str | None = None, role: str | None = None) -> list[dict]:
    """The queue in the batch's own order - best fit first - so the row at the TOP of the page is the
    row a run starts on. Both the page and the run read the same query; they cannot drift apart."""
    return await database.fetch(status, order="fit", view=view, scraped=scraped, role=role)


# The scrape-day strip as view name -> how many days back. One place, because the tab strip, the
# run-all rule above and the retention window all have to agree on what 'd3' means.
_DAY_VIEWS = {"recent": 0, **{f"d{n}": n for n in range(1, config.SCRAPE_KEEP_DAYS + 1)}}


def _day_tabs(stats: dict, view: str | None) -> list[tuple]:
    """The scrape-day strip: today, then the five days behind it.

    One tab per day a scraped row can still exist - config.SCRAPE_KEEP_DAYS - so nothing in the table is
    older than a tab that shows it, and the oldest tab holds what the morning's purge took off the other
    five: the litter is already gone from it, and the rows still there are the ones a recruiter was
    written to, which is exactly the row worth opening the app for. Label is the offset, because that is
    what the operator counts in; the date and the age are the tooltip, because that is what he checks
    against his own calendar. 'recent' is TODAY, not the newest day in the table: a tab named for today
    that quietly showed yesterday's haul could not answer the one question it exists for - did the scrape
    run?
    """
    today = date.today()
    days = [("recent" if not back else f"d{back}", "recent" if not back else f"-{back}d",
             today - timedelta(days=back)) for back in _DAY_VIEWS.values()]
    return [(label, f"/?view={name}", stats.get(name, 0),
             f"scraped {day.isoformat()} · "
             + ("today" if day == today else f"{(today - day).days} day(s) ago"),
             view == name)
            for name, label, day in days]


def _run_all_visible(status: str | None, view: str | None, scraped: str | None,
                     untouched: dict[str, int], role: str | None = None) -> bool:
    """Whether the batch button belongs on the page the operator is looking at.

    Never on 'all': every row in the table is a week of hauls at once, and the URL carries no day to
    say which one a run was meant for. On a scrape day it is the whole point - today's tab always
    offers it, because that is the haul that just came in; an older day offers it only while it still
    holds config.RUN_ALL_MIN_UNTOUCHED rows that were never applied to and never written to, so the
    button appears where there is a batch to run and not where it would find two jobs. `untouched`
    comes off the same scrape-day query that feeds the picker.

    The other scoped queues (a status, a role family, the owed tab, follow-ups, DMs) are not 'all' -
    they are already a narrowing the operator chose - so they keep the button they had.
    """
    today = date.today()
    day = None
    if view in _DAY_VIEWS:
        day = today - timedelta(days=_DAY_VIEWS[view])
    elif scraped:
        day = date.fromisoformat(scraped)
    if day is None:
        return view is not None or status is not None or role is not None
    return day == today or untouched.get(day.isoformat(), 0) >= config.RUN_ALL_MIN_UNTOUCHED


def _tabs(stats: dict, status: str | None, view: str | None, role: str | None = None) -> list[tuple]:
    """The queue tabs as (label, href, count, hint, active). Built here because Jinja has no list
    comprehensions and the counts already live in stats.

    'all' is the one tab with no predicate at all - nothing in this app hides a row. Then the four role
    families, because the day's haul is a deliberate MIX of them and "which role is this job" is the
    first thing read off a queue; then the scrape-day strip (see _day_tabs). 'not applied' is the other
    direction: what is still owed, which is not a slice of 'queued' - a row whose mail went out and
    whose application never landed reads as 'emailed' and would sit in no queue at all. 'followups' is
    mailed-and-unanswered, i.e. what a nudge is coming for. The rest are plain statuses.
    """
    tabs = [("all", "/", stats.get("total", 0), "every row, nothing filtered",
             not status and not view and not role)]
    tabs += [(r, f"/?role={r}", stats.get(r, 0),
              f"{r} roles · one of the four families the day's haul is split across", role == r)
             for r in config.ROLES]
    tabs += _day_tabs(stats, view)
    tabs.append(("not applied", "/?view=notapplied", stats.get("notapplied", 0),
                 "work still owed on either half - no application, or no mail out", view == "notapplied"))
    tabs += [(s, f"/?status={s}", stats.get(s, 0), f"{s} rows", status == s) for s in config.JOB_STATUSES]
    tabs.append(("followups", "/?view=followups", stats.get("followups", 0),
                 "mailed or DMed, no reply yet", view == "followups"))
    tabs.append(("dms", "/?view=dms", stats.get("dms", 0),
                 "reached on LinkedIn only - the nudge goes back out there", view == "dms"))
    return tabs


# --- streamed pipeline progress -------------------------------------------------------------------
# One record per phase, records separated by an ASCII record separator. htmx 2 does not progressively
# process a chunked body (it swaps once, when the response ends), so the reader in base.html splits on
# \x1e and hands each record to htmx.swap. Every record is nothing but hx-swap-oob elements, so the
# no-JS path is not broken, only un-live: htmx applies every update at once when the stream closes.
_SEP = "\x1e"
_WAIT = "wait"
_LIVE: set[asyncio.Task] = set()
_PHASES = pipeline.PHASES if pipeline else ()
# A finished task keeps its line on the panel this long, so the result of a scrape that ended while the
# operator was on another page is still there to read when he comes back.
_TASK_TTL = 900

# The work lives HERE, not in the response that started it. Two measured reasons:
#  - Starlette cancels a StreamingResponse generator when the client disconnects, so a browser that
#    navigates to another page would otherwise detach the only thing painting that run - and switching
#    tabs mid-run is exactly what the operator does; and
#  - a run takes minutes, so the run has to be visible from EVERY page and re-attachable from any of
#    them, with the page itself free to keep starting other work.
# KEYED, not single: one key = one task, so a second click on the same key attaches to the task already
# running instead of starting a second one (two batches over one queue would apply to the same employer
# twice), while a DIFFERENT key is simply a second task - it queues behind the browser lock and shows up
# on the same panel.
_TASKS: dict[str, dict] = {}
_SUBS: set[asyncio.Queue] = set()


def tasks_state() -> list[dict]:
    """Every task worth painting - the live ones plus anything that finished inside the TTL, oldest first."""
    now = time.time()
    for key, rec in list(_TASKS.items()):
        if rec["state"] == "done" and now - rec["finished"] > _TASK_TTL:
            _TASKS.pop(key, None)
    return sorted(_TASKS.values(), key=lambda r: r["queued"])


def live_tasks() -> list[dict]:
    """The tasks still running or waiting for a resource - what the panel's pulse and clock mean."""
    return [rec for rec in tasks_state() if rec["state"] != "done"]


def job_phases(job_id: int) -> dict:
    """One job's checklist out of whichever task owns it ('job:<id>' when run on its own, 'batch' inside
    a run-all), so a row paints its four phases mid-run and empty when nothing is touching it."""
    for rec in _TASKS.values():
        if job_id in rec["phases"]:
            return dict(rec["phases"][job_id])
    return {}


# Registered as globals so every render path - the dashboard, a fragment swap, a streamed record -
# paints the same set of tasks without the caller having to pass anything down.
templates.env.globals.update(tasks_state=tasks_state, live_tasks=live_tasks, job_phases=job_phases)


def _fan(event: dict | None) -> None:
    """Hand one event to every attached reader. put_nowait: a reader must never stall the run."""
    for queue in _SUBS:
        queue.put_nowait(event)


def _task(state: str, msg: str, ok: bool | None = None, warn: bool = False) -> dict:
    """A record about a TASK rather than about a job: queued / running / its result line."""
    return {"job_id": 0, "phase": "task", "state": state, "ok": ok, "warn": warn, "msg": msg}


def _track(rec: dict, event: dict) -> None:
    """Fold an event into the task record every page renders, before any reader is woken by it.

    Ordering is the point: _run calls this synchronously before _fan, so whatever a reader paints from
    the registry is at least as new as the event that woke it - and a reader that attached late (a page
    loaded mid-run) paints exactly the state the first reader has.
    """
    phase, job_id = event["phase"], event.get("job_id") or 0
    if event.get("ok") is not None:
        rec["ok"] = event["ok"]
    if event.get("warn"):
        rec["warn"] = True
    if phase == "task":                          # this task's own line: queued / running / its result
        rec.update(log=event["msg"], state=event["state"])
        return
    if phase == "batch":
        rec.update(log=event["msg"], phase="batch", phase_state=event["state"])
        if "total" in event:
            rec["total"] = event["total"]
        return
    if phase == "done":
        rec["phases"].pop(job_id, None)
        rec["done"] += 1
    else:
        rec["phases"].setdefault(job_id, {p: _WAIT for p in _PHASES})[phase] = event["state"]
    rec.update(job=job_id, phase=phase, phase_state=event["state"], log=event["msg"])


def _start(key: str, kind: str, factory, *, browser: bool = True,
           job: int = 0, total: int = 0, phases: dict | None = None) -> dict:
    """Register a task under `key` and own it; a second call while that key is live attaches instead.

    The dedupe is load-bearing, not a nicety: nothing is disabled while work is in flight any more, so a
    double click, a second tab and a stale page all reach this line - and two batches over one queue
    would apply to the same employer twice. Same key = the task already running, nothing new started.
    """
    if (rec := _TASKS.get(key)) and rec["state"] != "done":
        return rec
    for stale, old in list(_TASKS.items()):      # the panel stays readable, the dict does not grow
        if old["state"] == "done" and time.time() - old["finished"] > _TASK_TTL:
            _TASKS.pop(stale, None)
    rec = _TASKS[key] = {
        "key": key, "kind": kind, "state": "wait", "ok": None, "warn": False,
        "phase": "", "phase_state": "", "job": job, "done": 0, "total": total,
        "phases": phases or {}, "log": "queued", "queued": time.time(), "started": 0.0,
        "finished": 0.0, "lock": _BROWSER if browser else None,
    }
    task = asyncio.create_task(_run(rec, factory))
    _LIVE.add(task)                              # strong ref: a bare create_task can be collected
    task.add_done_callback(_LIVE.discard)
    return rec


async def _run(rec: dict, factory) -> None:
    """Own one task: wait for its resource, then drain the factory into the registry and out to readers.

    A task of its own, so a reader detaching (the browser navigated away) stops nothing else, and so a
    task queued behind a long batch waits in the background while the page that started it goes anywhere.
    """
    lock, got = rec["lock"], False

    def _human(notice: dict) -> None:
        """A run is parked inside "press ENTER when the form is filled" - the one thing a browser cannot
        answer itself. The notice becomes an ordinary event on the job it belongs to, so the panel paints
        the two buttons and the row a "needs you" chip, on every page, until the run moves on. The job id
        is filled in HERE: automations knows the form and the domain, only the task knows which row."""
        notice["job_id"] = rec["job"]
        _track(rec, notice)
        _fan(notice)

    try:
        if rec["lock"] is not None:              # only browser work can need a human at all
            automations.ON_HUMAN = _human
        if lock is not None:
            await lock.acquire()                 # FIFO: this starts the moment the previous task frees
            got = True
        rec.update(state="run", started=time.time(), log="starting…")
        _fan(_task("run", "starting…"))
        async for event in factory():
            _track(rec, event)
            _fan(event)
    except automations.RunAborted as stop:
        # A BaseException, so it lands here and not in the `except Exception` below - that one prints a
        # traceback and paints a red failure for what is really the operator pressing Ctrl-C at an ENTER
        # prompt. Saying "stopped by you" is the honest report: the batch halted where the human left it,
        # no row was given a verdict, and the next run resumes from the queue as it stands.
        print(f"\ntask stopped by you at the manual apply: {stop}")
        rec.update(ok=True, log=f"stopped by you · {stop} · no verdict written, this job is still queued")
    except Exception as exc:
        traceback.print_exc()
        rec.update(ok=False, log=f"{type(exc).__name__}: {exc}")
    finally:
        if rec["lock"] is not None and automations.ON_HUMAN is _human:
            automations.ON_HUMAN = None          # ours to clear: a net task running beside us never set it
        if got:
            lock.release()                       # only what was acquired: a cancelled waiter holds none
        rec.update(state="done", finished=time.time(), phase="", phase_state="")
        _fan(_task("done", rec["log"], rec["ok"], rec["warn"]))
        if not live_tasks():
            _fan(None)                           # None is the end-of-stream sentinel for every reader


async def _pump() -> AsyncIterator[dict]:
    """One reader's view of the registry: everything live right now, then every event until the last task
    ends. Starting work is _start's job - this never starts anything, which is what makes it safe to call
    on every page load and from any page."""
    queue: asyncio.Queue = asyncio.Queue()
    _SUBS.add(queue)                             # before the first await: nothing can start or end unseen
    try:
        live = live_tasks()
        if not live:
            yield {"job_id": 0, "phase": "idle", "state": "ok", "ok": True, "msg": "no run in flight"}
            return
        first = live[-1]                         # first paint: this page sees every task as it stands now
        yield _task(first["state"], first["log"], first["ok"], first["warn"])
        while (event := await queue.get()) is not None:
            yield event
    finally:
        _SUBS.discard(queue)
    # Whatever this reader got, the last record is the final line: the registry has no live task left, so
    # the page cannot be left showing work that has finished.
    done = [rec for rec in tasks_state() if rec["state"] == "done"]
    last = max(done, key=lambda r: r["finished"]) if done else None
    yield _task("ok", last["log"] if last else "run finished", ok=True)


def _record(job: dict | None, phases: dict[str, str] | None, log: str,
            msg: str = "", ok: bool | None = None) -> bytes:
    """One streamed record: the job's row (when there is one) plus the status line and the run banner,
    all OOB."""
    html = templates.get_template("_stream.html").render(
        job=job, phases=phases, log=log, msg=msg, ok=ok, oob=True, cfg=config)
    return (html + _SEP).encode("utf-8")


async def _stream() -> AsyncIterator[bytes]:
    """Pipeline events -> OOB HTML records. Mid-run events paint badges; 'done' paints the final row."""
    async for event in _pump():
        phase = event["phase"]
        if phase in ("idle", "end", "task"):     # nothing in flight, a task's own line, or the last one
            yield _record(None, None, event["msg"], ok=event.get("ok"))
            continue
        if phase == "batch":
            yield _record(None, None, f"batch · {event['msg']}", ok=event.get("ok"))
            continue
        row_id = event.get("job_id") or 0
        job = await database.get(row_id)
        if phase == "done":                      # final view: status pill, result line, button back
            yield _record(job, None, f"job {row_id} · {event['state']} · {event['msg']}",
                          msg=event["msg"], ok=event.get("ok"))
            continue
        # The checklist comes from the registry, not from this reader's own bookkeeping, so a page that
        # attached mid-run paints the same phases as the one that started it.
        yield _record(job, job_phases(row_id),
                      f"job {row_id} · {phase} {event['state']} · {event['msg']}")


def _streaming() -> StreamingResponse:
    # X-Accel-Buffering is for a reverse proxy that would otherwise buffer the whole body and defeat
    # the point; no-store keeps a chunked HTML body out of the browser cache.
    return StreamingResponse(_stream(), media_type="text/html; charset=utf-8",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


def _blocked(job_id: int, reason: str, state: str = "fail") -> dict:
    return {"job_id": job_id, "phase": "done", "state": state, "ok": False, "msg": reason}


async def _guarded_job(job: dict) -> AsyncIterator[dict]:
    """Eligibility and quota as events instead of an early return. The browser lock is the task's, held
    by _run for as long as this generator is being drained - so a run of one row queues behind a batch
    exactly like a batch queues behind it, and neither has to be refused to keep them apart."""
    job_id = job["id"]
    ok, reason = parsing.eligible(job)
    if not ok and job.get("applied_at"):
        # The same exception the batch makes, for the same measured reason: eligibility gates what is
        # worth APPLYING to, and this row is not going to apply again. Its application is already in an
        # employer's ATS, so there is nothing left here to decide - what it owes is the mail, and
        # skipping it would bury that in a tab nothing ever runs. It is reported, then let through; the
        # pipeline takes its resubmit branch and only does the outreach.
        yield {"job_id": job_id, "phase": "apply", "state": "skip", "ok": True,
               "msg": f"{reason} · already applied {job['applied_at'][:10]} - running for the outreach it owes"}
        ok = True
    if not ok:
        await database.set_status(job_id, "skipped", notes=reason)
        yield _blocked(job_id, reason, "skip")
    elif await database.quota_left() <= 0:
        yield _blocked(job_id, f"daily cap {config.MAX_DAILY_APPLICATIONS} reached")
    elif pipeline is None:
        await database.set_status(job_id, "queued")
        yield _blocked(job_id, f"queued · pipeline unavailable: {_PIPELINE_IMPORT_ERROR}")
    else:
        async for event in pipeline.run_job_events(job):   # run_job_events owns the status write
            yield event


async def _guarded_batch(view: str | None = None, scraped: str | None = None,
                         role: str | None = None) -> AsyncIterator[dict]:
    """$run-all-queued: sequential, one row at a time, each row's badges live as it runs.

    `view` scopes the batch to whatever the page is filtered by, so the recent tab's run-all applies
    the latest scrape's queue and nothing else, and `scraped` does the same for the All tab's date
    filter, `role` for the family filter - a run-all on a page showing one scrape day, or one role
    family, must not quietly apply the whole table.
    """
    if pipeline is None:
        yield {"job_id": 0, "phase": "batch", "state": "fail", "ok": False,
               "msg": f"pipeline unavailable: {_PIPELINE_IMPORT_ERROR}"}
    else:
        async for event in pipeline.run_batch_events(view=view, scraped=scraped, role=role):
            yield event


async def _guarded_social() -> AsyncIterator[dict]:
    """$daily-post: build something, push it to GitHub, post it to LinkedIn, streamed step by step.

    The browser lock, like every other Playwright run: this drives the LinkedIn profile an outreach run
    also drives, so it waits its turn instead of opening a second window on one login. No quota and no
    eligibility gate - nothing here spends the daily application cap.
    """
    async for event in social.daily_post():
        yield event


async def _guarded_scrape() -> AsyncIterator[dict]:
    """All four boards, then the calendar sweep, as one task with one result line.

    Under the browser lock, like everything else: when jobspy is walled the scrape falls back to the
    logged-in Playwright profiles - the very profiles an apply run drives.
    """
    yield _task("run", "scraping four boards…")
    try:
        result = await automations.scrape_new_jobs()
        # New data in is the moment the calendar gets a chance to matter: a fresh haul is exactly when
        # the old ones should go, and this is the only other place time passes for a machine left on.
        result["purged"] = (await automations.purge_old_jobs())["jobs"]
    except Exception as exc:
        yield _task("fail", f"scrape failed · {type(exc).__name__}: {exc}", ok=False)
        return
    # A walled board answers 0 rows for a reason that has nothing to do with the market. The board is
    # named in the line and the line is flagged, because an empty result the operator cannot tell from a
    # quiet day is the bug this notice exists for.
    walls = result.get("walls") or {}
    msg = "scrape · " + " · ".join(f"{k}={v}" for k, v in result.items())
    if walls:
        msg += " · bot wall · " + ", ".join(f"{board}: {why}" for board, why in walls.items())
    yield _task("ok", msg, ok=True, warn=bool(walls))


async def _guarded_replies() -> AsyncIterator[dict]:
    """Check the inbox for recruiter replies and stamp replied_at (write-once). Request-driven, no
    scheduler. DRY_RUN skips the IMAP connect. A reply naming a date drops a local .ics invite.

    No browser anywhere in it, so this task skips the lock and runs BESIDE a batch instead of waiting
    behind one - which is the whole reason the lock is the browser's rather than the pipeline's."""
    yield _task("run", "checking the inbox…")
    try:
        result = await automations.poll_replies()
    except Exception as exc:
        yield _task("fail", f"reply poll failed · {type(exc).__name__}: {exc}", ok=False)
        return
    yield _task("ok", "replies · " + " · ".join(f"{k}={v}" for k, v in result.items()), ok=True)


async def _guarded_followups() -> AsyncIterator[dict]:
    """Send one nudge per aged, unreplied, not-yet-followed outreach with an HR mailbox. One per row,
    ever (followup_at is write-once). DRY_RUN sends nothing.

    The nudge for a LinkedIn row is typed into the same profile an outreach run drives, so this one
    takes the browser lock."""
    yield _task("run", "sending one nudge per aged row…")
    try:
        result = await automations.run_followups()
    except Exception as exc:
        yield _task("fail", f"followups failed · {type(exc).__name__}: {exc}", ok=False)
        return
    yield _task("ok", "followups · " + " · ".join(f"{k}={v}" for k, v in result.items()), ok=True)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, status: str | None = None, view: str | None = None,
                    scraped: str | None = None, role: str | None = None):
    _filter(status, view, scraped, role)
    jobs = await _queued(status, view, scraped, role)
    stats = await database.stats()
    tabs = _tabs(stats, status, view, role)
    today = date.today()
    days = await database.scrape_days()
    for day in days:
        # How old the haul is, in days, next to the date - the two things the operator reads off the
        # filter: which scrape this is, and whether it is fresh enough to be worth working.
        day["age"] = (today - date.fromisoformat(day["day"])).days
    untouched = {d["day"]: d["untouched"] for d in days}
    # The daily build ledger. One read feeds the whole panel: today's post (which is what retires the
    # button), the history list, and the 15-day profile clock. github_problem is empty unless .env is
    # missing the token, and then it is printed on the panel rather than discovered at the click. The
    # press does whatever the project in flight is owed today - the announcement, the next phase, or
    # the wrap-up - so the panel says which one that is before the button is pressed, not after.
    posts = await database.daily_posts(20)
    posted_today = next((p for p in posts if p["day"] == today.isoformat()
                         and p["kind"] in ("announce", "phase", "finish")
                         and p["status"] == "posted"), None)
    project = await database.active_project()
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "active_nav": "/",
            "jobs": jobs,
            "status_filter": status,
            "view_filter": view,
            "scraped_filter": scraped,
            "role_filter": role,
            "scrape_days": days,
            "run_all": _run_all_visible(status, view, scraped, untouched, role),
            "by_status": stats,
            "tabs": tabs,
            # The active tab's own label, so the run-all button describes the queue it is about to run
            # in the operator's words (recent / -3d / not applied) instead of the view's internal name.
            "view_label": next((t[0] for t in tabs if t[4] and t[0] != "all"), ""),
            "applied_today": await database.applied_today(),
            "quota_left": await database.quota_left(),
            "funnel": (await database.response_rates())["funnel"],
            "eligible_count": sum(1 for j in jobs if parsing.eligible(j)[0]),
            "posts": posts,
            # The projects section of the LinkedIn profile is filled by _finish, but the entry itself has
            # to be readable when that form moves under it - so the finished ones are on the panel with
            # their exact three fields, ready to paste. Same read the GitHub profile uses.
            "projects": await database.project_history(),
            "posted_today": posted_today,
            "project": project,
            "project_phases": social.phases_of(project),
            "today_step": social.step_label(project),
            "refresh_in": social.refresh_due(posts),
            "github_problem": social.credential_problem(),
        },
    )


def _posted_email(body: bytes) -> str:
    """The `email` field out of a streamed run's form body, or "" when there is none.

    parse_qs rather than FastAPI's Form(...): one optional field does not justify a python-multipart
    dependency, and the streaming routes read the body directly anyway. A run button outside a form
    posts no body at all, which is the ordinary case and must stay a no-op.
    """
    if not body:
        return ""
    try:
        return (parse_qs(body.decode("utf-8", "replace")).get("email") or [""])[0].strip()
    except Exception:
        return ""


@app.post("/jobs/{job_id}/pipeline")
async def run_pipeline(job_id: int, request: Request):
    """Resume gen + Playwright submit + platform DM + HR cold email, streamed phase by phase.

    Chunked HTML: one hx-swap-oob record per phase, so the row's badges move while the bot works
    instead of after it. The row is swapped a final time with its real status when the job ends.

    Registered as a task, so the run survives this page: switching tabs, walking the dashboard or
    closing the response detaches a reader and nothing else. A second click on the same row attaches to
    the task already running it (never a second run of one job), and any other row starts its own task
    that waits its turn for the browser.

    An optional `email` field rides in the posted body, from the row's own box: the human knows the
    recruiter's address and the waterfall came back empty, which is the reason that row is red. It is
    written onto the row (replacing a dud) and cached in the vault BEFORE the run starts, so the
    outreach half finds it on the row and every later job at that employer gets it for free. An empty
    or malformed value is ignored and the run proceeds as an ordinary retry of the search.
    """
    job = await database.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if typed := _posted_email(await request.body()):
        if kept := await database.set_hr_email(job_id, typed):
            await database.save_vault_email(automations._company_key(job["company"]), kept, "human")
            print(f"hr email: job {job_id} given {kept} by hand · cached for {job['company']}")
            job = await database.get(job_id)         # the run must see the address it was given
        else:
            print(f"hr email: job {job_id} refused {typed!r} - not an address")
    # A single-job run opens with a silent phase - resume generation can take half a minute - so the
    # record is seeded with the checklist and the panel paints it now: the row shows its four phases
    # waiting instead of nothing at all happening for the first 30 seconds after the click.
    _start(f"job:{job_id}", "job", lambda: _guarded_job(job),
           phases={job_id: {p: _WAIT for p in _PHASES}}, job=job_id, total=1)
    return _streaming()


@app.post("/scrape")
async def scrape(status: str | None = None, view: str | None = None,
                 scraped: str | None = None, role: str | None = None):
    """Ingest eligible roles from all four boards, capped by remaining daily quota.

    A task, not a fragment: it queues behind whatever browser work is in flight (a walled board falls
    back to the same logged-in profiles an apply run drives) and it survives the page that started it,
    so the operator can keep starting other work while it runs. The counts land on the panel.
    """
    _filter(status, view, scraped, role)
    _start("scrape", "scrape", _guarded_scrape)
    return _streaming()


@app.post("/pipeline/run-all")
async def run_all(view: str | None = None, scraped: str | None = None, role: str | None = None):
    """Sequential batch over queued eligible jobs, streamed one row at a time, best fit first."""
    _filter(None, view, scraped, role)
    _start("batch", "batch", lambda: _guarded_batch(view, scraped, role))
    return _streaming()


@app.get("/run/attach")
async def attach_run():
    """Re-attach this page to whatever is running. Every page load calls this, so navigating inside the
    app - or a mid-run refresh - picks the live tasks back up instead of appearing to have lost them.
    It never starts anything; starting is each button's own route."""
    return _streaming()


@app.post("/social/post")
async def social_post():
    """The daily build: generate it, commit it to GitHub, render its card, post it to LinkedIn.

    Streamed like the batch runs, so the steps land in the panel one at a time instead of after the
    browser work. Public and irreversible once it fires, which is why it carries a confirm on the
    dashboard and why the LinkedIn window is deliberately visible: the post itself is typed into the
    operator's own profile, and the click that publishes it is theirs to watch.
    """
    _start("social", "social", _guarded_social)
    return _streaming()


@app.post("/replies/poll")
async def poll_replies(status: str | None = None, view: str | None = None,
                       scraped: str | None = None, role: str | None = None):
    """Check the inbox for recruiter replies and stamp replied_at (write-once). Request-driven, no
    scheduler. DRY_RUN skips the IMAP connect. A reply naming a date drops a local .ics invite."""
    _filter(status, view, scraped, role)
    _start("replies", "replies", _guarded_replies, browser=False)
    return _streaming()


@app.post("/followups/run")
async def run_followups(status: str | None = None, view: str | None = None,
                        scraped: str | None = None, role: str | None = None):
    """Send one nudge per aged, unreplied, not-yet-followed outreach with an HR mailbox. One per row,
    ever (followup_at is write-once). DRY_RUN sends nothing."""
    _filter(status, view, scraped, role)
    _start("followups", "followups", _guarded_followups)
    return _streaming()


@app.get("/analytics", response_class=HTMLResponse)
async def analytics(request: Request):
    """Funnel + per platform/track/template reply rates. Steers where scraping effort should go."""
    return templates.TemplateResponse(
        request=request, name="analytics.html",
        context={"active_nav": "/analytics", "rates": await database.response_rates()},
    )


@app.delete("/jobs/clear", response_class=HTMLResponse)
async def clear_jobs(request: Request):
    """Kill-switch: wipe the queue back to empty - every row that never left this machine.

    A row that was really SUBMITTED (applied_at) or really MAILED (outreach_at) is not deleted. Those
    are facts about the world, not bookkeeping: the application is in that employer's ATS forever and
    the mail is in a recruiter's inbox forever, so deleting the row would not undo anything, it would
    only make this app forget and do it a second time. Such rows stay exactly where the operator asked
    to still find them - the applied / emailed / follow-ups sections - and keep spending the quota they
    already spent. Nothing else is touched: email_vault and company_history survive, so no discovered
    mailbox is re-bought and no cooled-down employer is approached again.

    On success HTMX is told to reload the whole page rather than swap the tbody: the wipe changes the
    stat tiles and the quota badge too, and a fragment swap would leave both showing pre-wipe numbers.
    A failure returns the normal banner fragment so the reason is visible.
    """
    try:
        deleted = await database.clear_all_jobs()
    except Exception as exc:
        return _rows(request, await _queued(), f"clear failed · {type(exc).__name__}: {exc}", False)
    print(f"kill-switch: deleted {deleted} rows; generated_resumes/ left on disk")
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@app.post("/jobs/delete", response_class=HTMLResponse)
async def delete_checked(request: Request):
    """Checkbox selection -> one DELETE for exactly those rows. Nothing is inferred from the page.

    The ids arrive as a comma-joined list the button builds from the checkboxes that are actually
    ticked, so an unticked box can never be part of the statement - the destructive path does not
    depend on how any framework serialises a checkbox. The body is parsed with the stdlib because
    request.form() would drag in python-multipart (not installed, and not worth installing for one
    field): the payload is a single urlencoded key either way.
    """
    body = (await request.body()).decode("utf-8", "replace")
    ids = [int(t) for t in parse_qs(body).get("ids", [""])[0].split(",") if t.strip().isdigit()]
    if not ids:
        return _rows(request, await _queued(), "delete · nothing checked", False)
    try:
        deleted = await database.delete_jobs(ids)
    except Exception as exc:
        return _rows(request, await _queued(), f"delete failed · {type(exc).__name__}: {exc}", False)
    print(f"delete: {deleted} row(s) of {len(ids)} named")
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@app.delete("/jobs/{job_id}", response_class=HTMLResponse)
async def delete_job(job_id: int, request: Request):
    """One row, from its own trash button. Registered AFTER /jobs/clear so the literal path wins.

    Unlike the kill-switch this can remove a row that already left the machine, because the operator
    named that one row deliberately - unticking a bad scrape is the whole point of the control. What
    survives it is company_history: the employer stays cooled down, so a thrown-away row cannot buy a
    second application.
    """
    try:
        deleted = await database.delete_jobs([job_id])
    except Exception as exc:
        return _rows(request, await _queued(), f"delete failed · {type(exc).__name__}: {exc}", False)
    if not deleted:
        return _rows(request, await _queued(), f"delete · job {job_id} not found", False)
    print(f"delete: job {job_id} removed by hand")
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@app.post("/jobs/{job_id}/revive", response_class=HTMLResponse)
async def revive_job(job_id: int, request: Request):
    """Put a rejected row back in the queue, on the operator's say-so.

    'skipped' is written by heuristics - an eligibility rule, or clean_failed's sweep - and a heuristic
    that is wrong about one row is the normal case, not a bug. The only way back used to be delete and
    re-scrape, which loses the id everything else is keyed on. The old verdict is carried into the note
    rather than dropped: the next run overwrites `notes` with its own, and until then the card should
    still say what this row was put aside for.

    Refused only for a row that owes nothing at all - both facts on it, nothing left for any run to do.
    A row with ONE fact is genuinely waiting: an application and no mail still owes the outreach, and a
    mail and no application still owes the submit. Refusing those was what made a spent row a dead end,
    since 'skipped' is written by sweeps that never look at it again and its run button only re-ran the
    gate that rejected it.
    """
    job = await database.get(job_id)
    if not job:
        return _rows(request, await _queued(), f"revive · job {job_id} not found", False)
    if job.get("applied_at") and job.get("outreach_at"):
        return _rows(request, await _queued(),
                     f"revive · job {job_id} owes nothing - applied {job['applied_at'][:10]} and reached "
                     f"{job['outreach_at'][:10]}, so there is nothing left to run", False)
    was = job.get("notes") or "no reason recorded"
    await database.set_status(job_id, "queued", notes=f"revived by hand · was {job['status']}: {was}")
    print(f"revive: job {job_id} requeued by hand (was {job['status']} · {was})")
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@app.post("/jobs/{job_id}/applied", response_class=HTMLResponse)
async def applied_by_hand(job_id: int, request: Request):
    """The operator submitted the employer's form themselves. Record that fact and nothing else.

    The other half of the manual-apply button: the red 'you have not applied' alert needs a way to stop
    being true once the form really has been sent, or run-all keeps re-attempting that employer and the
    warning turns into something the operator scrolls past. It stamps applied_at through the same
    write-once path the pipeline uses, so the next run reads 'already submitted' and refuses to apply
    twice. The status label is left as the last verdict - this route knows one fact, not a whole verdict,
    and inventing one here is how a label stops matching its row.
    """
    job = await database.get(job_id)
    if not job:
        return _rows(request, await _queued(), f"applied · job {job_id} not found", False)
    if job.get("applied_at"):
        return _rows(request, await _queued(),
                     f"applied · job {job_id} was already stamped {job['applied_at']}", False)
    await database.mark_submitted(job_id)
    print(f"applied: job {job_id} stamped by hand from the dashboard")
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@app.post("/jobs/{job_id}/human/{answer}")
async def human_answer(job_id: int, answer: str):
    """The dashboard's ENTER: a run parked on a manual apply / a platform login continues or stops here.

    The run is not waiting on this response - it is parked inside the wait, holding the browser - so the
    POST only sets the gate and every attached reader paints the next record. `done` saves the session
    and carries on; `abandon` is the honest "I did not send that form", which raises RunAborted so no
    verdict is written and the recruiter is not mailed about an application that never happened. That is
    the same outcome as Ctrl-C at the console prompt, and the only way to reach it from a browser.
    """
    if answer not in ("done", "abandon"):
        raise HTTPException(400, "answer must be done or abandon")
    if not automations.release_human(job_id, answer == "done"):
        raise HTTPException(409, f"no run is waiting on a human for job {job_id}")
    print(f"human: job {job_id} answered from the dashboard · {answer}")
    return _streaming()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "event_loop": type(asyncio.get_running_loop()).__name__,
        "browser_busy": _BROWSER.locked(),
        "human": automations.human_waiting(),
        "tasks": [{"key": r["key"], "kind": r["kind"], "state": r["state"], "job": r["job"],
                   "phase": r["phase"], "done": r["done"], "total": r["total"]}
                  for r in tasks_state()],
        "watchers": len(_SUBS),
        "db": config.DB_PATH.name,
        "applied_today": await database.applied_today(),
        "quota_left": await database.quota_left(),
        "by_status": await database.stats(),
        "platforms": config.PLATFORMS,
        "filters": {"min_lpa": config.MIN_LPA, "max_bond_years": config.MAX_BOND_YEARS,
                    "max_experience_years": config.MAX_EXPERIENCE_YEARS,
                    "fresh_hours": config.FRESH_HOURS,
                    "fresh_hours_enterprise": config.FRESH_HOURS_ENTERPRISE,
                    "max_applicants": config.MAX_APPLICANTS},
        "search": {"terms": len(config.SEARCH_TERMS), "role_targets": dict(config.ROLE_TARGETS),
                   "locations": len(config.SEARCH_LOCATIONS),
                   "queries": len(config.SEARCH_TERMS) * (1 + len(config.SEARCH_LOCATIONS)),
                   "per_platform_quota": max(1, config.MAX_DAILY_APPLICATIONS // len(config.PLATFORMS)),
                   "max_queries_per_run": config.MAX_QUERIES_PER_RUN},
        "dry_run": config.DRY_RUN,
    }


if __name__ == "__main__":
    import os

    # Line-buffered even when stdout is a pipe or a file. A signal exit does not flush Python's block
    # buffer, so under a redirect - a log file, systemd - the teardown lines and the last job's verdicts
    # were being discarded at exactly the moment they are worth reading, while the same run in a console
    # showed them all because a console is line-buffered already. Set here and not at import, so
    # importing main (the audit does) never reaches in and reconfigures its caller's stdout.
    sys.stdout.reconfigure(line_buffering=True)

    # The loop is pinned here, not left to uvicorn's flag parsing, because the wrong loop breaks every
    # Playwright call. asyncio.run's loop_factory arg is how uvicorn selects a loop internally too.
    # PORT is env-overridable so a stale server on 8000 does not block a boot: PORT=8001 python main.py
    factory = asyncio.ProactorEventLoop if sys.platform == "win32" else None
    # timeout_graceful_shutdown bounds uvicorn's drain, which is unbounded by default. Without it a
    # Ctrl-C waited on the parked stream readers below for as long as the Playwright batch ran - the hang
    # that ended in a force-killed PowerShell. uvicorn 0.52 cancels the leftover tasks on timeout and
    # still runs the lifespan shutdown (only a SECOND Ctrl-C sets force_exit and skips it), so the
    # ordered teardown in _release_streams/shutdown() below is reached either way - and with the unpark
    # above it is reached in well under the bound instead, the timeout being only the backstop now.
    # HOST/PORT are env-driven so the same entrypoint serves as a local console app and as a service
    # behind a supervisor or a container: nothing about the run logic changes with where it listens.
    cfg = uvicorn.Config("main:app", host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")),
                         log_level="info", timeout_graceful_shutdown=10)
    print(f"listening on http://{cfg.host}:{cfg.port} · health at /health · Ctrl-C to stop")
    try:
        asyncio.run(_Server(cfg).serve(), loop_factory=factory)
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl-C on Windows reaches asyncio.run as a CancelledError once uvicorn's own handler has run,
        # and asyncio.run re-raises it as a traceback over an already-clean shutdown. Nothing is left to
        # do here: lifespan has closed the browser contexts and the DB by this point.
        print("\nShutdown complete")
        sys.exit(0)
