"""Probe every control the operator reported against a THROWAWAY db + a stub pipeline (no AI, no browser,
no mail). Each check prints PASS/FAIL and the process exits non-zero if any check failed.

  1. the tabs: all / recent / statuses / follow-ups, and 'all' really is every row
  2. one-by-one delete (a row's trash button) and bulk delete (checked boxes)
  3. the queue order the page shows IS the order a batch applies
  4. a run that survives navigating away, plus re-attaching to it from a fresh page load
  5. a per-row run, and run-all scoped to the recent tab
  6. the kill-switch: unsent rows go, rows that left the machine stay, in every section
  7. /analytics with an empty funnel (the ZeroDivisionError regression)
  8. several works at ONCE: a browser batch, an inbox poll (no browser, must not queue) and a row run
     (browser, must queue) in the same panel, a tab switch in the middle of it, and every button still
     clickable the whole way through
  9. the one step a browser run cannot answer itself - "press ENTER when the form is filled" - answered
     from the dashboard instead of a terminal that uvicorn does not have
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["DRY_RUN"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config                                                        # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="probe-"))
config.DB_PATH = TMP / "probe.db"
config.INTERVIEWS_DIR = TMP / "interviews"
config.CATCHUP_ON_BOOT = False          # no IMAP at boot; this probe stays offline

import database                                                      # noqa: E402
import automations                                                   # noqa: E402
import main                                                          # noqa: E402
import pipeline                                                      # noqa: E402
import uvicorn                                                       # noqa: E402
from playwright.async_api import async_playwright                    # noqa: E402

PORT = 8092
URL = f"http://127.0.0.1:{PORT}"
T0 = time.monotonic()
FAILS: list[str] = []


def stamp(what: str) -> None:
    print(f"  t+{time.monotonic() - T0:5.1f}s  {what}", flush=True)


def check(name: str, ok: bool, detail="") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{' · ' + str(detail) if detail != '' else ''}", flush=True)
    if not ok:
        FAILS.append(name)


class StubPipeline:
    """Every phase at PHASE_S, then the row is really marked applied in the DB. Mirrors the real
    signatures - including run_batch_events' `view` scope - and records the order it walked."""
    PHASES = pipeline.PHASES
    PHASE_S = 0.45

    def __init__(self):
        self.order: list[int] = []
        self.park = 0                   # a row id that stops mid-apply for a human, like a real ATS

    async def _phases(self, job):
        for phase in ("ai", "render", "apply", "outreach"):
            if phase == "apply" and job["id"] == self.park:
                # Exactly what a manual apply does: park on the operator's answer, and treat a walk-away
                # as an abort that writes no verdict - the row stays queued, nobody is mailed.
                yield {"job_id": job["id"], "phase": "apply", "state": "human", "msg": "stub needs a human"}
                if not await automations._wait_enter(
                        notice={"phase": "apply", "state": "human", "domain": "acme.com",
                                "msg": "manual apply · acme.com · fill the form, then press ✓"}):
                    raise automations.RunAborted("stub · the operator walked away")
            await asyncio.sleep(self.PHASE_S)
            yield {"job_id": job["id"], "phase": phase, "state": "run", "msg": f"stub {phase}"}

    async def run_batch_events(self, limit=None, view=None, scraped=None, role=None):
        self.order = []
        jobs = await database.fetch("queued", limit=200, order="fit", view=view, db_path=config.DB_PATH)
        if not jobs:
            stamp(f"stub: nothing eligible (view={view})")
            yield {"job_id": 0, "phase": "batch", "state": "ok", "ok": True, "total": 0,
                   "msg": f"nothing queued and eligible in {view or 'the whole queue'}"}
            return
        yield {"job_id": 0, "phase": "batch", "state": "run", "total": len(jobs),
               "msg": f"{len(jobs)} eligible · best-fit first"}
        for job in jobs:
            self.order.append(job["id"])
            stamp(f"stub: batch -> job {job['id']} {job['company']}")
            async for event in self._phases(job):
                yield event
            await database.set_status(job["id"], "applied", db_path=config.DB_PATH)
            yield {"job_id": job["id"], "phase": "done", "state": "ok", "ok": True, "msg": "stub done"}
        yield {"job_id": 0, "phase": "batch", "state": "ok", "ok": True, "msg": "stub batch done"}

    async def run_job_events(self, job):
        self.order = [job["id"]]
        async for event in self._phases(job):
            yield event
        await database.set_status(job["id"], "applied", db_path=config.DB_PATH)
        yield {"job_id": job["id"], "phase": "done", "state": "ok", "ok": True, "msg": "stub done"}


async def add(title: str, company: str) -> int:
    await database.add_jobs([{"title": title, "company": company, "job_url": f"http://p/{company.lower()}",
                              "platform": "naukri", "status": "queued"}], config.DB_PATH)
    return await jid(company)


async def jid(company: str) -> int:
    return [r["id"] for r in await database.fetch(db_path=config.DB_PATH) if r["company"] == company][0]


async def seed() -> dict[str, int]:
    await database.init_db(config.DB_PATH)
    for title, company in (("SOC Analyst", "Alpha"), ("Cloud Support Associate", "Beta"),
                           ("DevOps Engineer Fresher", "Gamma"), ("Junior Cloud Engineer", "Zeta"),
                           ("Junior SRE", "Eta"), ("Cloud Security Associate", "Theta"),
                           ("Application Security Associate", "Iota"), ("Junior Platform Engineer", "Kappa")):
        await add(title, company)
    ids = {c: await jid(c) for c in ("Alpha", "Beta", "Gamma", "Zeta", "Eta", "Theta", "Iota", "Kappa")}
    # Beta was really submitted; Gamma was really mailed. Both are irreversible facts, so both must
    # survive a wipe and still show up in the applied / emailed / follow-ups sections afterwards.
    await database.set_status(ids["Beta"], "applied", hr_email="hr@beta.com", db_path=config.DB_PATH)
    await database.set_status(ids["Gamma"], "emailed", hr_email="hr@gamma.com", db_path=config.DB_PATH)
    await database.mark_reached(ids["Gamma"], config.DB_PATH)
    # Zeta came in with an older scrape, so it is queued but NOT part of the latest day.
    async with database.connect(config.DB_PATH) as db:
        await db.execute("UPDATE jobs SET created_at = datetime('now','localtime','-3 days')"
                         " WHERE id = ?", [ids["Zeta"]])
    return ids


async def tab_counts(page) -> dict:
    out = {}
    for name in ("all", "recent", "queued", "applied", "emailed", "followups"):
        path = {"all": "/", "recent": "/?view=recent", "followups": "/?view=followups"}.get(
            name, f"/?status={name}")
        await page.goto(URL + path)
        out[name] = await page.locator("article").count()
    return out


async def page_order(page) -> list[int]:
    return await page.evaluate(
        "[...document.querySelectorAll('#rows article')].map(a => Number(a.id.replace('job-','')))")


async def db_rows() -> dict:
    return {r["id"]: r["status"] for r in await database.fetch(db_path=config.DB_PATH)}


async def wait_idle(page, seconds: float = 25) -> bool:
    for _ in range(int(seconds * 2)):
        if await page.evaluate("document.body.dataset.running") is None:
            return True
        await page.wait_for_timeout(500)
    return False


async def main_probe() -> None:
    ids = await seed()
    main.pipeline = StubPipeline()
    stub = main.pipeline
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=PORT, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("dialog", lambda d: asyncio.create_task(d.accept()))
            await page.goto(URL)

            print("\n=== 1. tabs ===")
            counts = await tab_counts(page)
            check("all tab shows every row", counts["all"] == 8, counts)
            check("recent tab is the latest scrape day only (Zeta excluded)",
                  counts["recent"] == 7, counts)
            check("status filter", counts["queued"] == 6, counts)
            check("followups tab is mailed-and-unanswered", counts["followups"] == 1, counts)
            await page.goto(URL)
            chips = await page.evaluate(
                "[...document.querySelectorAll('.card a[href=\"/\"], .card a[href^=\"/?\"]')]"
                ".map(a => a.textContent.replace(/\\s+/g, ''))")
            check("the tabs carry their counts", {"all8", "recent7", "followups1"} <= set(chips), chips)

            print("\n=== 2. delete one by one, then by checkbox ===")
            await page.click(f'#job-{ids["Eta"]} button[hx-delete]')
            await page.wait_for_timeout(1800)
            rows = await db_rows()
            check("the trash button deletes exactly that row",
                  ids["Eta"] not in rows and len(rows) == 7, sorted(rows))
            check("…and the page came back with the row gone",
                  await page.locator("article").count() == 7,
                  await page.locator("article").count())
            await page.check(f'#rows input[name=job][value="{ids["Theta"]}"]')
            await page.click('button[hx-post="/jobs/delete"]')
            await page.wait_for_timeout(1800)
            rows = await db_rows()
            check("the checkbox delete removes only the ticked row",
                  ids["Theta"] not in rows and ids["Zeta"] in rows and len(rows) == 6, sorted(rows))

            print("\n=== 3. the page's order IS the batch's order ===")
            # run-all is offered on a SCOPED queue, never on the 'all' tab (main._run_all_visible), so the
            # batch is pressed from the queued tab - the one tab whose rows are exactly what a batch walks.
            await page.goto(URL + "/?status=queued")
            shown = await page_order(page)
            before = await db_rows()
            queued_shown = [i for i in shown if before.get(i) == "queued"]
            check("one checkbox per row on the page",
                  await page.locator('#rows input[name=job]').count() == len(shown), len(shown))
            await page.click('[data-run^="/pipeline/run-all"]')
            stamp("clicked run-all")
            await page.wait_for_timeout(1200)
            check("the banner appeared and says it is a batch",
                  "batch run" in await page.locator("#run-banner").inner_text(),
                  (await page.locator("#run-banner").inner_text())[:90])
            check("the page is flagged as running",
                  await page.evaluate("document.body.dataset.running") == "1")
            check("every run button stays live while it runs (nothing is refused mid-run)",
                  await page.evaluate(
                      "getComputedStyle(document.querySelector('.run-btn')).pointerEvents") != "none")
            await wait_idle(page)
            rows = await db_rows()
            check("the batch applied the queued rows in the order the page listed them",
                  stub.order == queued_shown, (stub.order, queued_shown))
            check("every queued row on that page is now applied",
                  all(rows[i] == "applied" for i in queued_shown),
                  [(i, rows[i]) for i in queued_shown])
            # data-running clears in the reader's finally, BEFORE the end-of-stream htmx.ajax that
            # re-reads the rows lands - so this one gets a bounded moment instead of racing that
            # round-trip. Counted per row rather than page-wide: a stray 'applied' elsewhere on the page
            # must not stand in for the rows this batch actually walked. Still honest - it fails if the
            # repaint never arrives.
            cleared = 0
            for _ in range(20):
                cleared = await page.evaluate(
                    "ids => ids.filter(i => { const a = document.querySelector(`#job-${i}`);"
                    " return !a || ![...a.querySelectorAll('span')]"
                    ".some(s => s.textContent.trim() === 'queued'); }).length", queued_shown)
                if cleared == len(queued_shown):
                    break
                await page.wait_for_timeout(250)
            check("…and the page itself repainted them without a manual reload",
                  cleared == len(queued_shown), f"{cleared}/{len(queued_shown)}")
            check("the finished task keeps its line for a while, so the result is readable",
                  "batch run" in await page.locator("#run-banner").inner_text(),
                  (await page.locator("#run-banner").inner_text())[:90])
            check("the controls are live again",
                  await page.evaluate("document.body.dataset.running") is None)

            print("\n=== 4. a run survives navigating away, and a fresh page re-attaches ===")
            nu, xi, omicron = (await add("Cloud Operations Associate", "Nu"),
                               await add("Incident Response Associate", "Xi"),
                               await add("Junior Site Reliability Engineer", "Omicron"))
            await page.goto(URL + "/?view=recent")
            await page.click('[data-run^="/pipeline/run-all"]')
            await page.wait_for_timeout(900)
            stamp("navigating to /analytics mid-run (the stream is cut)")
            await page.goto(f"{URL}/analytics")
            check("the run banner is on the analytics page too",
                  "batch run" in await page.locator("#run-banner").inner_text(),
                  (await page.locator("#run-banner").inner_text())[:90])
            await page.wait_for_timeout(900)
            stamp("reloading the dashboard mid-run: it must re-attach to the same run")
            await page.goto(URL)
            check("a page loaded mid-run is flagged as running",
                  await page.evaluate("document.body.dataset.running") == "1")
            live_before = await page.locator("#stream-log").inner_text()
            await page.wait_for_timeout(2000)
            live_after = await page.locator("#stream-log").inner_text()
            check("the reloaded page is streaming again (the log moved)",
                  bool(live_after) and live_after != live_before, (live_before, live_after))
            check("it re-attached and then finished on its own", await wait_idle(page))
            rows = await db_rows()
            check("the run finished although the browser left it twice",
                  all(rows[i] == "applied" for i in (nu, xi, omicron)),
                  [(i, rows[i]) for i in (nu, xi, omicron)])

            print("\n=== 5. one row at a time, and run-all scoped to the recent tab ===")
            rho = await add("Junior DevOps Engineer", "Rho")
            sigma = await add("IAM Analyst Fresher", "Sigma")
            # A fresh backdated queued row: still in the queue, but from an older scrape. It is the
            # witness that a recent-scoped batch cannot reach outside the tab it was clicked on - Zeta
            # cannot serve that role any more, step 3 already applied it.
            tau = await add("Cloud Network Associate", "Tau")
            async with database.connect(config.DB_PATH) as db:
                await db.execute("UPDATE jobs SET created_at = datetime('now','localtime','-3 days')"
                                 " WHERE id = ?", [tau])
            await page.goto(URL + "/?view=recent")
            check("the recent tab leaves the older scrape out",
                  tau not in await page_order(page), await page_order(page))
            await page.click(f'#job-{rho} [data-run]')
            await page.wait_for_timeout(700)
            check("a one-row run says so in the banner",
                  "single job run" in await page.locator("#run-banner").inner_text(),
                  (await page.locator("#run-banner").inner_text())[:90])
            check("the clicked row shows its phase checklist immediately",
                  await page.locator(f'#job-{rho} span:text-is("waiting")').count() >= 1)
            await wait_idle(page)
            rows = await db_rows()
            check("applying one row touched only that row",
                  rows[rho] == "applied" and stub.order == [rho], (stub.order, rows[rho]))
            scope = await page.evaluate(
                "document.querySelector('[data-run^=\"/pipeline/run-all\"]').dataset.run")
            check("run-all on the recent tab is scoped to the recent tab",
                  scope == "/pipeline/run-all?view=recent", scope)
            await page.click('[data-run^="/pipeline/run-all"]')
            await wait_idle(page)
            rows = await db_rows()
            check("the scoped batch applied the recent row and left the older one alone",
                  rows[sigma] == "applied" and rows[tau] == "queued",
                  {k: rows[k] for k in (sigma, tau)})

            print("\n=== 6. the wipe ===")
            lam, mu = await add("Vulnerability Assessment Analyst", "Lambda"), await add(
                "Build Engineer Junior", "Mu")
            before = await db_rows()
            spent = await database.applied_today(config.DB_PATH)
            await page.goto(URL)
            await page.evaluate("window.__survived = 'yes'")
            await page.click('[hx-delete="/jobs/clear"]')
            await page.wait_for_timeout(2800)
            check("the wipe reloaded the page instead of leaving stale rows",
                  not await page.evaluate('window.__survived === "yes"'))
            rows = await db_rows()
            check("every row that never left the machine is gone",
                  not {lam, mu} & set(rows), sorted(rows))
            check("the submitted row and the mailed row both survived",
                  rows.get(ids["Beta"]) == "applied" and rows.get(ids["Gamma"]) == "emailed", sorted(rows))
            check("the wipe handed back no quota that was really spent",
                  spent > 0 and await database.applied_today(config.DB_PATH) == spent,
                  (spent, await database.applied_today(config.DB_PATH)))
            counts = await tab_counts(page)
            check("'all' still equals the rows in the db after a wipe",
                  counts["all"] == len(rows), (counts, len(rows)))
            check("the mailed row is still in the emailed tab", counts["emailed"] == 1, counts)
            check("…and still in the follow-ups tab", counts["followups"] == 1, counts)
            check("…and the submitted row is still in the applied tab", counts["applied"] == len(rows) - 1,
                  counts)

            print("\n=== 7. /analytics with an empty funnel ===")
            resp = await page.goto(f"{URL}/analytics")
            check("analytics renders with data", resp.status == 200, resp.status)
            await database.delete_jobs(list(rows), config.DB_PATH)
            check("db emptied for the regression check", not await db_rows())
            resp = await page.goto(f"{URL}/analytics")
            check("analytics renders at emailed=0 (no ZeroDivisionError)",
                  resp.status == 200 and "reply rate" in await page.content(), resp.status)

            print("\n=== 8. several works at once, and a tab switch in the middle ===")
            up, phi = (await add("Security Analyst Fresher", "Upsilon"),
                       await add("Cloud Engineer Trainee", "Phi"))
            await page.goto(URL + "/?view=recent")
            await page.click('[data-run^="/pipeline/run-all"]')
            await page.wait_for_timeout(600)
            stamp("batch is running · clicking the inbox poll and a row's own run button")
            await page.click('[data-run^="/replies/poll"]')
            await page.wait_for_timeout(300)
            await page.click(f'#job-{phi} [data-run]')
            await page.wait_for_timeout(700)
            lines = await page.evaluate(
                "[...document.querySelectorAll('#run-banner .card > div')]"
                ".map(d => d.innerText.replace(/\\s+/g, ' '))")
            check("the panel holds every task at once, not one run",
                  len(lines) >= 3 and all(any(k in l for l in lines)
                                          for k in ("batch run", "check replies", "single job run")), lines)
            check("the inbox poll does not queue behind the browser batch",
                  not any("check replies" in l and "waiting" in l for l in lines), lines)
            check("the second browser run waits its turn instead of being refused",
                  any("single job run" in l and "waiting for the browser" in l for l in lines), lines)
            check("the row button that was just pressed is still clickable",
                  await page.evaluate(
                      "getComputedStyle(document.querySelector('.run-btn')).pointerEvents") != "none")
            stamp("switching to /analytics mid-run: neither task may stop")
            await page.goto(URL + "/analytics")
            check("the work is visible from the other page too",
                  "batch run" in await page.locator("#run-banner").inner_text())
            check("the batch kept working while the dashboard was gone",
                  await wait_idle(page, 40))
            rows = await db_rows()
            check("both rows were applied, the batch and the queued row run alike",
                  rows.get(up) == "applied" and rows.get(phi) == "applied",
                  {k: rows.get(k) for k in (up, phi)})
            await page.goto(URL)
            panel = await page.locator("#run-banner").inner_text()
            check("the finished lines are still readable after the run",
                  "batch run" in panel and "single job run" in panel, panel[:120])
            check("nothing is flagged as running once it is all over",
                  await page.evaluate("document.body.dataset.running") is None)

            print("\n=== 9. a run that needs a human, answered from the dashboard ===")
            psi, chi = (await add("Security Engineer Trainee", "Psi"),
                        await add("Cloud Analyst Trainee", "Chi"))
            stub.park = psi
            await page.goto(URL + "/?view=recent")
            await page.click(f'#job-{psi} [data-run]')
            # From the analytics page, which never showed that row: the panel is what has to carry the
            # answer, because the operator is looking at the form in the browser window, not at that tab.
            await page.goto(URL + "/analytics")
            done = page.locator('#run-banner button:has-text("form done")')
            for _ in range(20):
                if await done.count():
                    break
                await page.wait_for_timeout(250)
            check("the parked run offers its ENTER on a page that never showed the row",
                  await done.count() == 1 and await done.is_visible(), await done.count())
            check("…and a way out beside it",
                  await page.locator('#run-banner button:has-text("didn\'t send")').count() == 1)
            await page.goto(URL + "/?view=recent")
            check("the row says whose turn it is",
                  await page.locator(f'#job-{psi} :text-is("needs you")').count() == 1)
            check("…and it has not been applied while it waits", (await db_rows()).get(psi) == "queued")
            await page.goto(URL + "/analytics")
            await done.click()
            check("✓ released the run and it finished on its own", await wait_idle(page, 40))
            check("…and the application really landed", (await db_rows()).get(psi) == "applied",
                  (await db_rows()).get(psi))
            check("the buttons went with it",
                  await page.locator('#run-banner button:has-text("form done")').count() == 0)

            stub.park = chi
            await page.goto(URL + "/?view=recent")
            await page.click(f'#job-{chi} [data-run]')
            stop = page.locator('#run-banner button:has-text("didn\'t send")')
            for _ in range(20):
                if await stop.count():
                    break
                await page.wait_for_timeout(250)
            # The confirm is the whole point of the button: abandoning must be deliberate.
            check("the walk-away button asks first",
                  await page.evaluate("[...document.querySelectorAll('#run-banner button')]"
                                      ".some(b => b.dataset.confirm?.includes('verdict'))"))
            await stop.click()
            check("✗ ended the run", await wait_idle(page, 40))
            check("…and wrote no verdict: the row is still queued",
                  (await db_rows()).get(chi) == "queued", (await db_rows()).get(chi))
            check("nothing is left flagged as running",
                  await page.evaluate("document.body.dataset.running") is None)
            check("no run is left waiting on a human", automations.human_waiting() is None,
                  automations.human_waiting())

            await browser.close()
    finally:
        server.should_exit = True
        await task

    print("\n=== summary ===")
    print(f"checks failed: {len(FAILS)} {FAILS}" if FAILS else "every check passed")
    print(f"temp dir kept for inspection: {TMP}")


if __name__ == "__main__":
    asyncio.run(main_probe(), loop_factory=asyncio.ProactorEventLoop if sys.platform == "win32" else None)
    sys.exit(1 if FAILS else 0)
