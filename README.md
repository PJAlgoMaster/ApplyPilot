# ApplyPilot

An agent that runs the job hunt for you. It scrapes security, cloud/devops AND software/full-stack
roles, rewrites your own two base resumes into one JD-tailored ATS resume per job with Claude,
submits the application through your own logged-in browser session, DMs a recruiter on LinkedIn, and
cold-emails HR with the PDF attached — then reads the replies and nudges whoever went quiet. Capped at
80 applications/day — a deliberate mixture of the four families below, not whichever one the boards
answer with first.

FastAPI + HTMX + Tailwind (both from CDN) + SQLite + Playwright. No Node, no build step.

Generation runs through the official **Claude Code CLI** in a hidden subprocess (`claude -p`), not
the Anthropic Python SDK: the router in front of the models fingerprints HTTP clients and rejects
the SDK with `401 unauthorized_client_error`, while it accepts the CLI. So `claude` must be
installed and authenticated (`npm i -g @anthropic-ai/claude-code`, then `claude` once to log in).

---

## 1. Install

```bash
python -m venv .venv                # use Python 3.12 or 3.13 — see note below
.venv\Scripts\activate              # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt
playwright install chromium
```

**Python version:** everything except scraping runs on 3.14. `python-jobspy` and `pandas` have no
3.14 wheels yet, so they build from source and fail without a C compiler. Use 3.12/3.13 and the
whole app works, `/scrape` included.

**weasyprint on Windows** needs the GTK runtime installed system-wide once:
<https://weasyprint.org/> → Windows install. Without it, PDF rendering raises on import.

## 2. Add your source resumes

Every generated resume is written *from your own two resumes* — nothing about the candidate is
hardcoded. Drop both files here:

```
source_resumes/cdac_resume.pdf        # structured / academic / credential-forward
source_resumes/personal_resume.pdf    # modern / dense / impact-forward
```

`ai_engine` extracts their text once at import (pypdf, pure Python) and caches it. Claude reads
both, decides which framing fits the employer, and returns `selected_template`: `cdac` for
enterprises, MNCs, banks, GCCs, defence and formal/compliance-heavy JDs; `personal` for startups,
product companies and informally written JDs. That choice picks the PDF layout.

**Both PDFs need a real text layer.** A scanned or exported-as-image resume extracts 0 characters
and is dropped from the source pool (`python ai_engine.py` prints `source[...]: 0 chars -- MISSING`).
Re-export from Word/Docs/LaTeX rather than from a scan or screenshot. With one source missing the
app still runs, it just always routes to the surviving one.

## 3. Configure

```bash
cp .env.example .env
```

Fill in, at minimum:

| Key | Why |
| --- | --- |
| `ROUTER_AUTH_TOKEN_1` (…`_2`, `_3`) | Router tokens for resume + email + DM generation, injected as `ANTHROPIC_AUTH_TOKEN` per CLI call and rotated on failure. Leave all three blank to use the CLI's own login. |
| `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL` | Read by the CLI itself. Model chain is `claude-opus-5` then `claude-opus-4-8`. |
| `CLAUDE_CLI`, `CLI_TIMEOUT` | Optional: full path to `claude.exe` if it is off PATH, and the per-call kill deadline (default 300s; a real call takes ~40s). |
| `CANDIDATE_EMAIL`, `CANDIDATE_PHONE`, … | Resume contact line. Blank fields are omitted, never invented. |
| `SMTP_EMAIL`, `SMTP_PASSWORD` | Gmail sender. **App Password**, not your account password: Google Account → Security → 2-Step Verification → App passwords. `SMTP_HOST`/`IMAP_HOST` stay blank for Gmail, which is the default. |
| `HUNTER_API_KEY` | Optional. Finds HR addresses. Without it, email only fires for rows that already have an `hr_email`. |
| `GITHUB_USER`, `GITHUB_TOKEN` | Optional, for the daily-project chain: the account new repos are opened under, and a **Personal Access Token** with the `repo` scope. A GitHub password will not work — the API has refused password auth since 2021. |

Leave `DRY_RUN=1` until you've watched one full run.

## 4. Log in to each platform (one time)

Playwright reuses your cookies from `browser_sessions/<platform>/`. Open each one, log in
manually, solve any captcha, close the window. The agent never handles credentials.

```bash
playwright open --user-data-dir=browser_sessions/linkedin     https://www.linkedin.com/login
playwright open --user-data-dir=browser_sessions/naukri       https://www.naukri.com/nlogin/login
playwright open --user-data-dir=browser_sessions/indeed       https://secure.indeed.com/account/login
playwright open --user-data-dir=browser_sessions/internshala  https://internshala.com/login/student
```

Sessions expire every few weeks — when a row fails with `no apply control … expired session`,
re-run the matching line above.

## 5. Run

```bash
uvicorn main:app --reload
```

Dashboard: <http://127.0.0.1:8000> · API docs: `/docs` · runtime state: `/health`

## Daily flow

1. **`$ scrape-jobs`** — LinkedIn + Indeed + Naukri + Internshala × 56 fresher terms ×
   {Pune, Remote, Bengaluru, Hyderabad, Noida, India}. The terms are zipped one per family per cycle
   (software / full-stack / security / cloud-devops) so even a run the daily cap ends early has asked
   every board for all four. Only rows passing the eligibility gate are inserted, as `queued`, sliced
   to whatever quota is left today and held to that family's share of it.
2. **Review the queue.** Salary ≥ 4 LPA and bond ≤ 3y render emerald; violations render red. Each row
   carries its family chip, and the top of the queue has one counted filter per family.
3. **`$ execute-application-pipeline`** on a row, or **`$ run-all-queued`** for the batch.
   Per job: template routed from the JD → tailored resume PDF → platform submit → recruiter DM
   (LinkedIn) → HR cold email → status `emailed` / `applied` / `failed`.

Batch runs are sequential on purpose — parallel submits from one IP is what trips platform
anti-automation. Expect minutes, not seconds, for a large batch.

Resumes land in `generated_resumes/Prince_Jha_resume_{Role}_{Company}.pdf`.

## Guardrails

| Guard | Value | Where |
| --- | --- | --- |
| Daily applications | 80 (20 software / 10 full-stack / 25 security / 25 cloud-devops) | `config.MAX_DAILY_APPLICATIONS` + `config.ROLE_TARGETS`, enforced in SQL against today's `applied_at` |
| Minimum salary | 4 LPA | `parsing.eligible` — a *hidden* salary passes, a stated sub-4 LPA does not |
| Experience floor | fresher only | `config.MAX_EXPERIENCE_YEARS = 0` — "fresher" or a range starting at 0 (0-2, 0-4, 0-5) passes; 1-3 does not |
| Maximum bond | 3 years | `parsing.bond_years`, read from a bond/service-agreement sentence in the JD |
| Human pacing | 2.5–5.0 s random per action | `automations._human` |
| LinkedIn DM | 300 chars, hard-capped | `ai_engine` schema + `automations.DM_LIMIT` |
| Candidate facts | name, 0-day notice, "Self-Directed Technical Report" framing | `ai_engine.CANDIDATE_RULES`, name re-stamped after generation |
| Dry run | `DRY_RUN=1` | Full code path, zero submits, zero emails |
| Human gate | `HITL=1` | A manual apply or a platform login parks the run and waits — answered from the dashboard's ✓ button, or ENTER on the console |

## Self-checks

Each module verifies itself with no server and no live spam:

```bash
python database.py      # schema, dedupe, quota, additive migration of an old jobs.db
python parsing.py       # INR salary + bond parsing
python ai_engine.py     # PDF extraction per source, CLI argv/env/timeout + JSON extraction, one live call, renders both templates
python automations.py   # SMTP auth, Hunter credits, session dirs, salary/bond extraction
python pipeline.py      # orchestration + status transitions, AI and PDF stubbed
```

`python automations.py` connects and authenticates to SMTP but sends nothing.
`python ai_engine.py` prints an extracted character count per source PDF — the fastest way to see
whether a resume has a usable text layer. It makes one real CLI call if `claude` is installed,
otherwise it renders both templates from an offline fixture.

## Files

| File | Role |
| --- | --- |
| `main.py` | Routes: `/`, `/jobs/{id}/pipeline`, `/scrape`, `/pipeline/run-all`, `/health` |
| `pipeline.py` | `run_job` / `run_batch` — owns the status write, contains every exception |
| `automations.py` | Playwright per-platform apply + DM, Hunter.io lookup, SMTP send, jobspy ingestion |
| `ai_engine.py` | Source-PDF ingestion, `claude -p` subprocess engine (token rotation + model failover + JSON extraction), tailored assets, ATS PDF render |
| `database.py` | Async SQLite; `job_url` is the dedupe key, `cli_session_id` resumes a job's CLI context, `role` is derived on every insert and backfilled at boot |
| `parsing.py` | Salary/bond parsing, the single eligibility gate, and the role-family classifier |
| `config.py` | Limits and env-sourced secrets |
| `templates/` | `base` · `dashboard` · `_rows` · `_row` · `resume_cdac` + `resume_personal` (the PDFs) |
| `source_resumes/` | Your two base resumes — the only source of candidate facts |

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `no source resumes readable — add …` | `source_resumes/` empty, or both PDFs are image-only. Re-export with a text layer (§2). |
| `source[personal] …: 0 chars -- MISSING` | That one PDF has no text layer. Template routing falls back to the other source; fix by re-exporting. |
| `scrape failed · ModuleNotFoundError: No module named 'jobspy'` | Python 3.14 venv. Recreate on 3.12/3.13. |
| `claude CLI exit 1: …` | CLI not logged in or the token was rejected. Run `claude` once interactively, or check `ROUTER_AUTH_TOKEN_*`. |
| `claude CLI hung, killed after 300.0s` | The CLI stalled; the job is marked failed and its `cli_session_id` is kept so a retry resumes it. Raise `CLI_TIMEOUT` if calls are legitimately slow. |
| `claude CLI produced no valid assets across (…)` | Both models returned unparseable output. `_extract_json` prints each attempt's reason above this line. |
| `no apply control (external-apply listing or expired session)` | Session expired (re-login above), or the listing applies on the company's own site — apply manually. |
| `wizard stalled before submit` | Employer added screening questions. Finish that one by hand. |
| Every row fails at the same step | Site DOM changed. Edit the candidate list in `automations.SELECTORS` — that's what the lists are there for. |
| `no HR email (hunter miss, none stored)` | No Hunter key, out of credits, or nothing found. The application still submitted; only the email was skipped. |
| SMTP `535` / auth failure | Using the account password. Generate an App Password. |
| `cannot load library 'gobject-2.0-0'` | GTK runtime missing (weasyprint). |
| Buttons do nothing in the browser | htmx blocked by SRI/CSP or offline — the CDN scripts must load. |

## Limits worth knowing

- Company domains are never guessed for cold email. Hunter is queried by company name; a wrong
  guessed domain would email a stranger.
- Gmail throttles around 500 recipients/day; the 80/day cap sits well under it.
- One resume PDF per role+company pair — re-running a job overwrites its PDF.
- Nothing about the candidate is hardcoded except the name, the 0-day notice, and the technical-report
  framing. Everything else is read out of `source_resumes/` — swap those PDFs and the output follows.
- `.env`, `jobs.db`, `browser_sessions/`, `generated_resumes/`, and `source_resumes/` are gitignored.
  Browser session dirs contain live login cookies; source resumes contain personal contact details.
  Treat both like credentials.
