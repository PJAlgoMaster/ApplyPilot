"""Static config. Secrets from env only — never commit keys."""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / "jobs.db"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
RESUME_DIR = BASE_DIR / "generated_resumes"
SOURCE_RESUME_DIR = BASE_DIR / "source_resumes"   # cdac_resume.pdf + personal_resume.pdf live here
BROWSER_SESSIONS = BASE_DIR / "browser_sessions"  # persistent Playwright contexts, one dir per platform
INTERVIEWS_DIR = BASE_DIR / "interviews"
SOCIAL_DIR = BASE_DIR / "social_posts"            # the daily post's card images, one PNG per build
PROJECTS_DIR = BASE_DIR / "projects"              # the multi-day project's working tree, one dir per repo

RUNTIME_DIRS = (STATIC_DIR, TEMPLATES_DIR, RESUME_DIR, SOURCE_RESUME_DIR, BROWSER_SESSIONS,
                INTERVIEWS_DIR, SOCIAL_DIR, PROJECTS_DIR)

MAX_DAILY_APPLICATIONS = 80
MIN_DAILY_APPLICATIONS = 50
MIN_LPA = 4
MAX_BOND_YEARS = 3
MAX_EXPERIENCE_YEARS = 0     # the fresher gate: a floor of 0 ("0-2", "0-4", plain "0") or no floor at all

# Metadata gates. Giants post in bulk, keep reqs open for days and screen thousands, so the
# fresh-and-uncrowded heuristics that protect a startup application are relaxed for them only.
FRESH_HOURS = 24
FRESH_HOURS_ENTERPRISE = 48
MAX_APPLICANTS = 50          # non-enterprise only; boards that do not report a count always pass

PLATFORMS = ("linkedin", "naukri", "indeed", "internshala")
JOB_STATUSES = ("new", "queued", "skipped", "applied", "failed", "emailed")

# The four role families the queue is built from, and how many of each day's jobs each one owns.
# sum(ROLE_TARGETS.values()) == MAX_DAILY_APPLICATIONS is the contract; scrape_new_jobs scales the
# targets down proportionally when less quota is left, and backfills a family the market could not
# fill from the others, so the day is always as mixed as the boards allow.
ROLES = ("software", "fullstack", "security", "devops")
ROLE_TARGETS = {"software": 20, "fullstack": 10, "security": 25, "devops": 25}

"""Search terms per family, zipped one term per family per cycle. scrape_new_jobs walks the plan
role-major and exits the moment the daily cap fills, so a capped run has to meet every family early
and keep meeting them; a family listed after another would only ever be scraped once the first was
satisfied. Order inside each tuple is priority."""
SOFTWARE_TERMS = (
    "Software Developer Fresher", "Junior Software Engineer", "Software Engineer Trainee",
    "Associate Software Engineer", "Java Developer Fresher", "Python Developer Fresher",
    "C++ Developer Fresher", ".NET Developer Fresher", "Junior Backend Developer",
    "Backend Developer Fresher", "Junior Programmer", "Graduate Software Engineer",
    "Junior Application Developer", "Software Development Trainee",
)
FULLSTACK_TERMS = (
    "Full Stack Developer Fresher", "MERN Stack Developer", "Web Developer Fresher",
    "React Developer Fresher", "Node JS Developer Fresher", "Frontend Developer Fresher",
    "Junior Full Stack Developer", "Web Application Developer Fresher", "Website Developer Fresher",
    "MEAN Stack Developer Fresher", "React JS Developer Fresher", "Junior Web Developer",
    "Full Stack Engineer Fresher", "Node JS Backend Developer",
)
SECURITY_TERMS = (
    "SOC Analyst Fresher", "L1 SOC Analyst", "Junior Cyber Security Analyst",
    "Cloud Security Associate", "Information Security Analyst Entry Level", "Junior AppSec Engineer",
    "Incident Response Associate", "Application Security Associate", "IAM Analyst Fresher",
    "Security Operations Center Associate", "Identity and Access Management Associate",
    "Junior Penetration Tester", "Ethical Hacker Fresher", "Vulnerability Assessment Analyst",
)
DEVOPS_TERMS = (
    "Cloud Support Associate", "Junior DevOps Engineer", "Junior DevSecOps Engineer",
    "Junior Cloud Engineer", "DevOps Engineer Fresher", "DevSecOps Fresher",
    "Cloud Operations Associate", "Junior Site Reliability Engineer", "AWS Cloud Associate Fresher",
    "Junior SRE", "Azure Cloud Associate Fresher", "Junior Cloud Administrator",
    "Junior Platform Engineer", "Automation Engineer Fresher",
)
SEARCH_TERMS = tuple(term for cycle in zip(SOFTWARE_TERMS, FULLSTACK_TERMS, SECURITY_TERMS, DEVOPS_TERMS)
                     for term in cycle)
# Order is priority, not preference: scrape_new_jobs walks role-major over this tuple and exits as
# soon as the daily cap is filled, so whatever sits at the front is what actually gets scraped.
SEARCH_LOCATIONS = ("Pune, India", "Remote", "Bengaluru, India", "Hyderabad, India",
                    "Noida, India", "India")
SCRAPE_SITES = ("linkedin", "indeed", "naukri")    # jobspy backends; internshala is hand-rolled
RESULTS_PER_QUERY = int(os.getenv("RESULTS_PER_QUERY", "20"))
SCRAPE_HOURS_OLD = int(os.getenv("SCRAPE_HOURS_OLD", "48"))   # widest freshness gate; source-side filter
SCRAPE_PAUSE = (3.5, 7.5)    # randomised gap between queries; back-to-back requests get an IP blocked
# Per-run query ceiling. The quota is split evenly across the 4 platforms, so a thin platform can
# never fill its share and the sweep would otherwise walk the whole matrix hunting for it.
MAX_QUERIES_PER_RUN = int(os.getenv("MAX_QUERIES_PER_RUN", "64"))

DRY_RUN = os.getenv("DRY_RUN", "0") == "1"        # skip real submits/sends, keep the full code path
HEADLESS = os.getenv("HEADLESS", "0") == "1"      # logged-in flows want a visible window
# Every unbounded "press ENTER" wait in the app is gated on this: an ATS login, an OSINT login, a dead
# credential pool. Correct with a human at the keyboard, a permanent hang with a pty nobody is watching -
# which is what an agent-launched batch is. Lives here because ai_engine cannot import automations.
HITL = os.getenv("HITL", "1") == "1"
# Outreach without the submit. An application to a company is ONE shot: the row is in that employer's ATS
# forever and a second, better-tailored one is not a thing. So when the AI engine is degraded (an exhausted
# router means every resume is the untailored base PDF), the apply half is deferred rather than spent -
# applied_at stays unstamped, the row stays 'failed', and the normal retry path applies later with a
# tailored resume. outreach_at is still stamped, so that retry cannot mail the same recruiter twice.
OUTREACH_ONLY = os.getenv("OUTREACH_ONLY", "0") == "1"

# Days of silence before a follow-up nudge. Counted by the CALENDAR, not by uptime: the age gate is a
# datetime('now','localtime') comparison inside SQLite, so a nudge that came due while the laptop was
# shut still lands on the next boot or the next click - see CATCHUP_ON_BOOT.
FOLLOWUP_DAYS = int(os.getenv("FOLLOWUP_DAYS", "5"))
COMPANY_COOLDOWN_DAYS = int(os.getenv("COMPANY_COOLDOWN_DAYS", "14"))

# Two retention clocks, because the two kinds of scraped row are worth different amounts. A posting
# nobody was ever told about is the day's haul, and the haul goes stale: kept SCRAPE_KEEP_DAYS days,
# gone on the morning of the next one. A row that reached a human is a live thread - an application in
# an ATS or a mail in a recruiter's inbox - and it keeps its record until the reply window shuts, so
# the purge waits OUTREACH_KEEP_DAYS from the send before retiring it. A row with a reply on it is
# never touched by either clock: the recruiter is the point of the whole run.
SCRAPE_KEEP_DAYS = int(os.getenv("SCRAPE_KEEP_DAYS", "5"))
OUTREACH_KEEP_DAYS = int(os.getenv("OUTREACH_KEEP_DAYS", "10"))

# What makes an old day tab worth a batch. Today's tab always offers run-all - that is the haul just in.
# A tab from four days ago offers it only while it still holds this many rows that were never applied to
# and never written to, i.e. work a run would actually start on, so five old tabs cannot each sit there
# offering a click that finds two jobs. 0 restores the button everywhere.
RUN_ALL_MIN_UNTOUCHED = int(os.getenv("RUN_ALL_MIN_UNTOUCHED", "10"))

ANTHROPIC_AUTH_TOKEN = os.getenv("ANTHROPIC_AUTH_TOKEN", "")   # single-token fallback; ai_engine prefers ROUTER_AUTH_TOKEN_1..3
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")

# The daily build -> GitHub -> LinkedIn chain (social.py). GITHUB_USER is the account the repos are
# created under; GITHUB_TOKEN is a Personal Access Token with the `repo` scope. A GitHub PASSWORD does
# not work here and has not since 2021 - the REST API refuses password authentication outright.
GITHUB_USER = os.getenv("GITHUB_USER", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
# Writing a whole runnable script plus its README takes longer than one resume turn, and the CLI is
# allowed to take it: too short and every daily post fails on a slow model instead of landing.
SOCIAL_CLI_TIMEOUT = float(os.getenv("SOCIAL_CLI_TIMEOUT", "180"))
# How long one phase of a project is allowed to run for. Every phase has to actually work before it is
# posted, and a generated tool that hangs must cost a timeout, not an afternoon.
PROJECT_RUN_TIMEOUT = float(os.getenv("PROJECT_RUN_TIMEOUT", "90"))
# How often the GitHub profile README is rebuilt from the builds actually shipped. The LinkedIn half
# of the same refresh is a draft on the dashboard, never an automated profile edit.
PROFILE_REFRESH_DAYS = int(os.getenv("PROFILE_REFRESH_DAYS", "15"))

# Gmail SMTP. GMAIL_ADDRESS / GMAIL_APP_PASSWORD are the documented names; SMTP_EMAIL /
# SMTP_PASSWORD stay as fallbacks because the deployed .env already uses them and works.
# GMAIL_APP_PASSWORD must be a 16-char App Password from myaccount.google.com/apppasswords -
# a raw Google account password is rejected by smtp.gmail.com, and 2FA accounts cannot use one at
# all. Google displays app passwords in four spaced groups; the spaces are display-only, so strip
# whitespace before AUTH or the login fails with "Username and Password not accepted".
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_EMAIL = (os.getenv("GMAIL_ADDRESS") or os.getenv("SMTP_EMAIL", "")).strip()
SMTP_PASSWORD = "".join((os.getenv("GMAIL_APP_PASSWORD") or os.getenv("SMTP_PASSWORD", "")).split())

IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
# The Sent copy is the ground truth for "did this mail actually go out": SMTP can accept a message and
# the process die before the fact was written, and the row is then left saying nothing was sent.
SENT_MAILBOX = os.getenv("SENT_MAILBOX", '"[Gmail]/Sent Mail"')
IMAP_TIMEOUT = int(os.getenv("IMAP_TIMEOUT", "20"))     # seconds; a dead socket must not hang a boot

# Reply polling and follow-ups are driven by the calendar, so they run once at boot: the machine is
# off most of the day, and a nudge that came due on day 5 has to go out the next time this is started
# rather than never. Poll first - an answer that arrived while the lid was shut must strike that row
# off the nudge list before the sweep runs. Set CATCHUP_ON_BOOT=0 for click-only behaviour.
CATCHUP_ON_BOOT = os.getenv("CATCHUP_ON_BOOT", "1") == "1"
