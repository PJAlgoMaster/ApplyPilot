"""Build a real project, one phase a day, and post each day's work to LinkedIn with its GitHub link.

One press a day, and the press is always the next thing that is owed:

  day 1   the announcement - what is being built, on what, in how many phases, and why
  day 2+  one phase per day. The phase's code is written, RUN, and only posted once it actually runs,
          then committed to the same repository. The post says what now works and what comes next.
  last    the wrap-up: the same link, the real output, and a second image showing the phases.

The project is chosen by the model from the field the operator is actually applying in - security
tooling a SOC, cloud or platform team would run - and it is constrained to Python 3, standard library
only, offline, one command. Those constraints are what make "every phase works" checkable rather than
a claim: the chain runs the thing and reads the exit code before anything is published.

What leaves this machine, and when:

  - the GitHub commits and the LinkedIn posts are the only outward steps, and every one of them is
    behind config.DRY_RUN. In dry-run the project is really planned, really written and really run -
    only the push and the post are skipped.
  - nothing is posted twice on one day: (day, kind) is the primary key of database.daily_posts, so the
    second press finds today's row and reports it.
  - a phase that does not run is never posted as done. It gets one repair turn with the real traceback,
    and if it still fails the day ends as a failed row with the error on the dashboard.

Credentials, and the one that does not work: GITHUB_USER plus GITHUB_TOKEN from .env. A GitHub ACCOUNT
PASSWORD cannot be used here - the API has refused password authentication since 2021 - so the token is
a Personal Access Token with the `repo` scope. It is sent as a Bearer header, never printed, never
logged, and never written anywhere but .env.

The GitHub profile is refreshed on a calendar, not on every run: every PROFILE_REFRESH_DAYS the profile
README is rebuilt from the projects actually finished and pushed to the <user>/<user> repository, and
the day's headline/About draft is shown on the dashboard for the operator to paste into LinkedIn. The
GitHub half is pushed; the LinkedIn half is a draft, because an automated profile edit is the one
action on that site that gets an account restricted, and pasting two sentences takes a second.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import os
import re
import shlex
import sys
from datetime import date
from pathlib import Path
from typing import AsyncIterator

import httpx

import ai_engine
import automations
import config
import database

_API = "https://api.github.com"
_POST_CHARS = 2_800       # LinkedIn caps a post at 3,000; the link and the hashtags are added after
_CARD = (1_200, 1_200)    # LinkedIn's square: it renders uncropped in the feed
_CARD_LINES = 10        # what fits the square under a two-line title and a three-line summary
_CARD_COLS = 62
_PHASES_MIN, _PHASES_MAX = 2, 5

_BLOCKS = ("title", "slug", "pitch", "tech", "run", "phases", "intro", "post", "readme",
           "headline", "about")
_BLOCK = {name: re.compile(rf"<{name}>(.*?)</{name}>", re.S | re.I) for name in _BLOCKS}
_FILE = re.compile(r'<file\s+name="([^"]+)"\s*>(.*?)</file>', re.S | re.I)
_FENCE = re.compile(r"\A\s*```[a-zA-Z]*\s*|\s*```\s*\Z")
_SLUG = re.compile(r"[^a-z0-9]+")
_SECRETISH = ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "APIKEY", "CREDENTIAL")

_SYSTEM = (
    "You are the build partner of a junior DevSecOps and cloud-security engineer in India who is "
    "applying for SOC, cloud security and DevSecOps roles. You pick tools their industry actually "
    "uses and you write the code yourself, at the level of someone two years in: correct, readable, "
    "unremarkable in the best way. Everything is Python 3 with the STANDARD LIBRARY ONLY, it runs "
    "offline against a sample input file that ships inside the repository, and one command runs it. "
    "No API keys, no network calls, no placeholders, no TODO, no invented output, no dependency that "
    "is not in the standard library. The code carries a module docstring, a docstring per function "
    "and type hints where they help; it does not carry a comment on every line. It must finish in "
    "under a minute and print something a security team would actually read."
)

# How the writing has to sound. The operator's objection to the obvious failure mode - a post that
# reads like a model wrote it, or like something pasted out of a tutorial - is a requirement, so the
# tells are named outright rather than described as "be authentic".
_VOICE = (
    "Write it like the engineer who built the thing, typing between two tasks: first person, plain "
    "words, short sentences, one idea per line. Never the announcement voice - no 'thrilled', "
    "'excited', 'delighted', 'delve', 'leverage', 'robust', 'seamless', 'game-changer', 'dive into', "
    "'in today's fast-paced'. No rhetorical questions, no 'it isn't just X, it's Y', no list of "
    "adjectives, no closing line asking for agreement. Name the real command you ran and the real "
    "number it printed. One concrete detail beats three polished ones."
)

# The post angle rotates by calendar day so a month of announcements does not open the same way.
_ANGLES = (
    "the problem this removes from a one-person security team's week",
    "the one design decision that was not obvious, and why it went that way",
    "the mistake a junior makes here on their first attempt, and what the code does instead",
    "where this sits in a real SOC or cloud team's day",
)


def _tail(text: object, limit: int = 200) -> str:
    return " ".join(str(text).split())[-limit:]


def _note(msg: str, state: str = "run", ok: bool | None = None) -> dict:
    """One streamed record. Shaped like pipeline's batch events, so the dashboard's banner and the
    live phase log pick it up with no new plumbing."""
    return {"job_id": 0, "phase": "batch", "state": state, "ok": ok, "msg": msg}


def _block(text: str, name: str) -> str:
    match = _BLOCK[name].search(text or "")
    if not match:
        raise ValueError(f"the model's reply has no <{name}> block")
    return _FENCE.sub("", match.group(1).strip()).strip()


def _slugify(value: str, fallback: str = "daily-project") -> str:
    return _SLUG.sub("-", (value or "").strip().lower()).strip("-")[:40] or fallback


def _phases(text: str) -> list[dict]:
    """The plan's phase lines -> [{'name', 'what'}]. One phase per line, 'name | what it adds'."""
    out: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-*0123456789.) ").strip()
        if not line:
            continue
        name, _, what = line.partition("|")
        name = name.strip(" 	`*")[:60]
        if name:
            out.append({"name": name, "what": what.strip()[:200]})
    if len(out) < _PHASES_MIN:
        raise ValueError(f"the plan has {len(out)} phase(s) - a project is at least {_PHASES_MIN}")
    return out[:_PHASES_MAX]


def parse_plan(text: str) -> dict:
    """The announcement's reply -> the project the rest of this module works on."""
    plan = {name: _block(text, name) for name in ("title", "slug", "pitch", "tech", "run",
                                                  "phases", "intro", "headline", "about")}
    plan["phases"] = _phases(plan["phases"])
    plan["slug"] = _slugify(plan["slug"] or plan["title"], "daily-project")
    plan["run"] = (plan["run"].strip().strip("`") or "python main.py").splitlines()[0][:120]
    if not plan["pitch"].strip() or not plan["intro"].strip():
        raise ValueError("the plan came back without a pitch or an announcement post")
    return plan


def parse_phase(text: str) -> tuple[str, dict[str, str]]:
    """A phase's reply -> (the post, every file of the project as it now stands).

    Whole files, not a diff: a diff has to be applied, and the one thing that must never happen here
    is a half-applied project. The cost is tokens, and tokens are cheaper than a broken build.
    """
    post = _block(text, "post")
    files: dict[str, str] = {}
    for name, body in _FILE.findall(text or ""):
        files[_safe_rel(name)] = _FENCE.sub("", body.strip("\n"))
    if not files:
        raise ValueError("the reply carries no <file> blocks")
    if not any(name.endswith(".py") for name in files):
        raise ValueError("the reply has no Python file")
    return post, files


def parse_finish(text: str) -> tuple[str, str]:
    return _block(text, "post"), _block(text, "readme")


def _plan_prompt(day: str, angle: str) -> str:
    contact = ai_engine.CONTACT
    return (
        f"TODAY: {day}\nANGLE FOR THE ANNOUNCEMENT: {angle}\n\n"
        "Choose today's project: one small but real tool that a SOC, cloud, platform or DevSecOps "
        "team would actually run - a log or config parser, a scanner, a policy or permission linter, "
        "a detector, a report generator, a hardening checker. Not a tutorial clone, not a to-do app, "
        "not a chatbot, not a wrapper around somebody else's API. It must be worth a hiring manager's "
        "two minutes.\n\n"
        f"Split it into {_PHASES_MIN}-{_PHASES_MAX} phases. EVERY phase must leave the tool working "
        "and runnable - never a scaffold that only runs next week. Phase 1 is the smallest thing that "
        "runs end to end and prints something real; each later phase adds exactly one capability.\n\n"
        "Answer with the tags below and NOTHING outside them. Every block in English.\n\n"
        "<title> the project's name, one line, at most 60 characters\n"
        "<slug> kebab-case repository name, at most 40 characters, letters digits and hyphens only\n"
        "<pitch> two sentences, at most 220 characters: what it does and who it is for\n"
        "<tech> the stack, comma separated, and keep it honest - python, stdlib, argparse, sqlite\n"
        "<run> the one command that runs the project, e.g. python main.py\n"
        "<phases> one phase per line, exactly 'Phase name | what this phase adds', in build order\n"
        "<intro> the LinkedIn post that announces this project, under 900 characters. Say what is "
        "being built, what it will be able to do, which technologies you are using and how you are "
        "going to build it - ending with what you are starting on first. At most two emoji. Do NOT "
        "write any URL, the repository link is appended separately\n"
        "<headline> a LinkedIn headline for the author, at most 200 characters\n"
        "<about> a LinkedIn About section for the author, three short paragraphs, first person, no "
        "buzzword padding\n\n"
        + _VOICE + "\n\n"
        f"The author is {ai_engine.CANDIDATE}"
        + (f", GitHub {contact['github']}" if contact.get("github") else "")
        + (f", LinkedIn {contact['linkedin']}" if contact.get("linkedin") else "")
        + f", based in {contact.get('location') or 'India'}, working toward SOC analyst, cloud "
          "security and DevSecOps roles."
    )


def _phase_prompt(project: dict, phases: list[dict], index: int, files: dict[str, str],
                  error: str = "") -> str:
    done = phases[:index]
    todo = phases[index:]
    listing = "\n\n".join(f"### {name}\n{files[name]}" for name in sorted(files))
    return (
        f"PROJECT: {project['title']} - {project['pitch']}\n"
        f"REPOSITORY: {project['slug']}\n"
        f"RUN WITH: {project['run']}\n\n"
        "PHASES ALREADY SHIPPED AND WORKING:\n"
        + ("\n".join(f"  - {p['name']}: {p['what']}" for p in done) or "  (none, this is the first)")
        + "\n\nTODAY'S PHASE - build exactly this, and nothing from the later phases:\n"
        f"  {todo[0]['name']}: {todo[0]['what']}\n\n"
        "THE PHASES AFTER TODAY, so you do not build them early:\n"
        + "\n".join(f"  - {p['name']}: {p['what']}" for p in todo[1:])
        + "\n\nTHE PROJECT AS IT STANDS RIGHT NOW:\n"
        + (listing or "  (nothing yet - today writes the first files)")
        + "\n\n"
        + (f"YOUR LAST ATTEMPT DID NOT RUN. This is the real output of `{project['run']}`:\n"
           f"{error}\nFix the actual cause and return the whole project again.\n\n" if error else "")
        + "Answer with NOTHING outside the tags below. Every block in English.\n\n"
        "<post> the LinkedIn post for today's phase, under 900 characters, first person. What you "
        "built today, what it can do now that it could not do yesterday, one real command and one "
        "real line of its output, and what the next phase adds. At most two emoji, three or four "
        "hashtags at the end. Do NOT write any URL, the repository link is appended separately\n"
        "<files> EVERY file of the project as it stands after today's phase, each one in its own "
        '<file name="path/name.py"> block, unchanged files included verbatim. The project must run '
        "with the command above at the end of this phase. Keep the README.md in the set and bring it "
        "up to date with what now works\n"
        "</files>\n\n" + _VOICE
    )


def _finish_prompt(project: dict, phases: list[dict], output: str) -> str:
    return (
        f"PROJECT: {project['title']} - {project['pitch']}\n"
        f"REPOSITORY: {project['slug']}\nRUN WITH: {project['run']}\n\n"
        "THE PHASES, ALL OF THEM SHIPPED AND WORKING:\n"
        + "\n".join(f"  {i + 1}. {p['name']}: {p['what']}" for i, p in enumerate(phases))
        + f"\n\nTHE REAL OUTPUT OF `{project['run']}` JUST NOW:\n{output}\n\n"
        "The project is finished. Answer with NOTHING outside the tags below. English.\n\n"
        "<post> the closing LinkedIn post, under 900 characters, first person. What the finished "
        "thing does, the one number or finding from the output above that proves it works, what was "
        "harder than expected, and what you would add next. At most two emoji, three or four "
        "hashtags. Do NOT write any URL, the repository link is appended separately\n"
        "<readme> the final README.md in Markdown: what it does, how to run it, one realistic example "
        "with its real output copied from above, the phases it was built in, and a line on how it "
        "would be used in a real environment\n\n" + _VOICE
    )


async def _ask(prompt: str) -> str:
    """One CLI turn across the model chain, no session.

    No `--resume` on purpose: this is not the resume-tailoring conversation, and appending a daily
    build turn to it would spend that conversation's turn ceiling and hand the resume writer a pile of
    unrelated context.
    """
    if reason := ai_engine._breaker_open():
        raise RuntimeError(f"the model router is down ({reason}) - try the button again later")
    last: object = "the model chain is empty"
    for model in ai_engine.MODEL_CHAIN:
        index, token = next(ai_engine._POOL) if ai_engine._POOL else (0, "")
        try:
            text, _ = await ai_engine._invoke_claude_cli(
                prompt, system=_SYSTEM, model=model, token=token,
                timeout=config.SOCIAL_CLI_TIMEOUT)
            print(f"social: built by {model} (credential {index})")
            return text
        except Exception as exc:
            last = exc
            print(f"social: cli {model} failed: {type(exc).__name__}: {_tail(exc)}")
    raise RuntimeError(f"no model produced an artifact: {last!r}")


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": config.GITHUB_USER or "auto-apply-agent"}


def credential_problem() -> str:
    """Why GitHub cannot be reached, or "" when it can. Never prints the token itself."""
    if not config.GITHUB_USER:
        return "GITHUB_USER is not set in .env"
    if not config.GITHUB_TOKEN:
        return ("GITHUB_TOKEN is not set in .env. A GitHub account password cannot be used - the API "
                "has refused password authentication since 2021. Create a Personal Access Token with "
                "the `repo` scope at github.com/settings/tokens and put it there")
    return ""


async def _gh(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        res = await client.request(method, _API + path, json=payload, headers=_headers())
    try:
        return res.status_code, res.json()
    except Exception:
        return res.status_code, {"message": res.text[:200]}


async def _put_file(repo: str, path: str, content: bytes | str, message: str) -> None:
    """Create or overwrite one file in one commit. The GET is what makes an overwrite possible: the
    contents API refuses to replace an existing path unless the current blob's sha rides along."""
    blob = content if isinstance(content, bytes) else content.encode("utf-8")
    status, body = await _gh("GET", f"/repos/{repo}/contents/{path}")
    payload: dict = {"message": message, "content": base64.b64encode(blob).decode()}
    if status == 200 and body.get("sha"):
        payload["sha"] = body["sha"]
    status, body = await _gh("PUT", f"/repos/{repo}/contents/{path}", payload)
    if status not in (200, 201):
        raise RuntimeError(f"{path}: github said {status} {_tail(body.get('message'), 120)}")


async def _repo(name: str, description: str, *, reuse: bool = False) -> str:
    """Full name of the repository, created if it is not there yet.

    auto_init is what makes the first PUT safe: an empty repository has no default branch to commit
    to, so the initial README is bought here and immediately replaced by the real one. `reuse` is for
    the one repository that is supposed to already exist - the profile README - and must never be set
    for a project: a project that finds its name taken belongs in a repository of its own.
    """
    status, body = await _gh("POST", "/user/repos",
                             {"name": name, "description": description[:350], "private": False,
                              "auto_init": True, "has_issues": False, "has_wiki": False,
                              "has_projects": False})
    if status in (200, 201):
        return body["full_name"]
    if status == 422 and reuse:
        status, body = await _gh("GET", f"/repos/{config.GITHUB_USER}/{name}")
        if status == 200:
            return body["full_name"]
    raise RuntimeError(f"github refused to create {name}: {status} {_tail(body.get('message'), 160)}")


def plan_readme(plan: dict) -> str:
    """The repository's first README, written the day the project is announced."""
    phases = "\n".join(f"{i + 1}. **{p['name']}** - {p['what']}" for i, p in enumerate(plan["phases"]))
    return (f"# {plan['title']}\n\n{plan['pitch']}\n\n## What it will do\n\n{plan['phases'][0]['what']}"
            f"\n\n## How it is built\n\n{phases}\n\n## Stack\n\n{plan['tech']}\n\n## Run it\n\n```\n"
            f"{plan['run']}\n```\n\n_Being built in public, one phase a day. The README grows with "
            "it._\n")


async def create_repo(plan: dict) -> tuple[str, str]:
    """The project's repository, seeded with the plan. Returns (full_name, html_url)."""
    try:
        full = await _repo(plan["slug"], plan["pitch"])
    except Exception:
        full = await _repo(f"{plan['slug']}-{date.today().isoformat()}", plan["pitch"])
    await _put_file(full, "README.md", plan_readme(plan), f"docs: {plan['title']}")
    return full, f"https://github.com/{full}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _repo_full(url: str) -> str:
    """The `owner/name` the API wants, out of the URL that is stored. The row keeps the link, because
    the link is what the posts and the profile need; the API path is derived, never stored twice."""
    return url.replace("https://github.com/", "").strip("/")


def _safe_rel(name: str) -> str:
    """A path the model asked for, or a refusal. This is a trust boundary: the reply decides file
    names, and a reply is not allowed to write outside the project directory or over a dotfile."""
    raw = (name or "").strip().replace("\\", "/")
    parts = raw.split("/")
    if raw.startswith("/") or ":" in parts[0]:
        raise ValueError(f"refusing to write to an absolute path: {name!r}")
    if not raw or ".." in parts or any(part.startswith(".") or not part.strip() for part in parts):
        raise ValueError(f"refusing to write outside the project: {name!r}")
    return "/".join(parts)


def write_project(slug: str, files: dict[str, str]) -> Path:
    root = config.PROJECTS_DIR / _safe_rel(slug)
    for name, text in files.items():
        out = root / _safe_rel(name)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    return root


async def run_project(slug: str, cmd: str) -> tuple[bool, str]:
    """Run the project the way its README says to, and read the exit code.

    This is the check the whole design turns on: a phase is only ever posted as done because this
    returned True. No shell is involved, so nothing in a generated command can chain into another
    one, and the environment is stripped of anything secret-shaped before the child starts - a
    generated tool has no business reading this machine's tokens.
    """
    argv = shlex.split(cmd) or ["python", "main.py"]
    if Path(argv[0]).name.lower() in ("python", "python3", "py"):
        argv[0] = sys.executable
    env = {k: v for k, v in os.environ.items()
           if not any(word in k.upper() for word in _SECRETISH)}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(config.PROJECTS_DIR / _safe_rel(slug)), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except Exception as exc:
        return False, f"it could not be started: {type(exc).__name__}: {exc}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=config.PROJECT_RUN_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return False, f"it did not finish within {config.PROJECT_RUN_TIMEOUT:.0f}s and was killed"
    text = out.decode("utf-8", "replace").strip()
    ok = proc.returncode == 0 and "Traceback (most recent call last)" not in text
    return ok, text[:6_000]


async def push_files(repo: str, files: dict[str, str], pushed: dict, message: str) -> tuple[dict, str]:
    """Commit what changed since the last phase. Returns (the new file map, "" or the failure).

    The map is content digests, not GitHub's blob shas: the digest is what decides whether a file is
    worth a commit, and it is computable here without a round trip.
    """
    now = {name: _digest(text) for name, text in files.items()}
    changed = [name for name in sorted(files) if pushed.get(name) != now[name]]
    try:
        for name in changed:
            await _put_file(repo, name, files[name], f"{message}: {name}")
    except Exception as exc:
        print(f"social: github failed: {type(exc).__name__}: {exc}")
        return pushed, f"{type(exc).__name__}: {exc}"
    return now, ""


async def push_card(repo: str, images: list[str]) -> str:
    """Second commit, so the repository shows the picture its README links to. Returns "" on success."""
    try:
        for image in images:
            await _put_file(repo, f"docs/{Path(image).name}", Path(image).read_bytes(),
                            "docs: the card for this post")
        return ""
    except Exception as exc:
        print(f"social: the card did not reach {repo}: {type(exc).__name__}: {exc}")
        return f"{type(exc).__name__}: {exc}"


_CARD_TPL = """<!doctype html>
<html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1200px;height:1200px}
body{background:#09090b;color:#e4e4e7;font-family:Inter,"Segoe UI",system-ui,sans-serif;
     padding:76px;position:relative;overflow:hidden}
body::before{content:"";position:absolute;inset:0;background:
  radial-gradient(780px 540px at 4% -10%,rgba(16,185,129,.22),transparent 62%),
  radial-gradient(700px 480px at 106% 2%,rgba(56,189,248,.14),transparent 60%),
  radial-gradient(900px 620px at 50% 118%,rgba(255,255,255,.055),transparent 62%)}
body::after{content:"";position:absolute;inset:0;background-image:
  linear-gradient(rgba(255,255,255,.035) 1px,transparent 1px),
  linear-gradient(90deg,rgba(255,255,255,.035) 1px,transparent 1px);
  background-size:64px 64px;
  -webkit-mask-image:radial-gradient(840px 720px at 24% 0%,#000,transparent 82%)}
.wrap{position:relative;z-index:1;display:flex;flex-direction:column;height:100%}
.chip{display:inline-flex;align-items:center;gap:10px;font:600 15px/1 "JetBrains Mono",Consolas,monospace;
      letter-spacing:.22em;text-transform:uppercase;color:#6ee7b7;
      border:1px solid rgba(16,185,129,.35);background:rgba(16,185,129,.09);
      border-radius:8px;padding:12px 18px;align-self:flex-start}
.top{display:flex;align-items:center;justify-content:space-between;width:100%}
.date{font:500 18px/1 "JetBrains Mono",Consolas,monospace;color:#71717a;letter-spacing:.1em}
h1{font-size:76px;line-height:1.03;font-weight:600;letter-spacing:-.02em;margin-top:52px;
   background-image:linear-gradient(180deg,#fafafa,#a1a1aa);-webkit-background-clip:text;
   background-clip:text;color:transparent}
p.sum{margin-top:30px;font-size:26px;line-height:1.5;color:#a1a1aa;max-width:980px}
.slab{margin-top:46px;border:1px solid #27272a;border-radius:14px;overflow:hidden;
      background:rgba(255,255,255,.022);display:flex;flex-direction:column;min-height:0}
.slabhead{font:500 16px/1 "JetBrains Mono",Consolas,monospace;color:#52525b;
          padding:18px 24px;border-bottom:1px solid #27272a;display:flex;gap:10px;align-items:center}
.dot{width:11px;height:11px;border-radius:50%}
pre{font:400 21px/1.62 "JetBrains Mono",Consolas,monospace;color:#d4d4d8;
    padding:26px 24px;white-space:pre;overflow:hidden}
.foot{margin-top:auto;padding-top:34px;display:flex;align-items:baseline;justify-content:space-between;gap:24px}
.repo{font:500 22px/1 "JetBrains Mono",Consolas,monospace;color:#34d399;
      border-bottom:2px solid rgba(16,185,129,.3);padding-bottom:6px}
.who{font:500 20px/1 "JetBrains Mono",Consolas,monospace;color:#52525b;white-space:nowrap}
</style></head><body><div class="wrap">
<div class="top"><span class="chip">%LABEL%</span><span class="date">%DATE%</span></div>
<h1>%TITLE%</h1>
<p class="sum">%SUMMARY%</p>
<div class="slab"><div class="slabhead"><span class="dot" style="background:#f87171"></span>
<span class="dot" style="background:#fbbf24"></span><span class="dot" style="background:#34d399"></span>
<span style="margin-left:10px">%HEAD%</span></div><pre>%BLOCK%</pre></div>
<div class="foot"><span class="repo">%REPO%</span><span class="who">%WHO%</span></div>
</div></body></html>"""


def _block_text(text: str, limit: int = _CARD_LINES, cols: int = _CARD_COLS) -> str:
    """The terminal window's contents, fitted to the window.

    Three adjustments, each because the card is a picture and a picture cannot scroll: a run longer
    than the window keeps its first lines and its last ones, because the counts that prove the thing
    ran are printed at the end; a line too wide is cut with an ellipsis so it reads as a deliberate
    crop rather than a rendering fault; and a short block is padded, because a terminal with blank
    space under the prompt is what a terminal looks like.
    """
    lines = (text or "").strip().splitlines()
    if len(lines) > limit:
        lines = lines[:2] + ["..."] + lines[-(limit - 3):]
    lines += [""] * max(0, min(limit, 8) - len(lines))
    return "\n".join(line[:cols - 1] + "…" if len(line) > cols else line for line in lines)


def _card_html(card: dict, repo_url: str) -> str:
    who = ai_engine.CONTACT.get("github") or config.GITHUB_USER or ""
    who = who.rstrip("/").rsplit("/", 1)[-1]
    return (_CARD_TPL
            .replace("%DATE%", date.today().isoformat())
            .replace("%LABEL%", html.escape(card["label"][:40]))
            .replace("%TITLE%", html.escape(card["title"][:70]))
            .replace("%SUMMARY%", html.escape(card["summary"][:190]))
            .replace("%HEAD%", html.escape(card["head"][:60]))
            .replace("%BLOCK%", html.escape(_block_text(card["block"])))
            .replace("%REPO%", html.escape(repo_url.replace("https://", "")))
            .replace("%WHO%", html.escape(who)))


async def render_card(card: dict, repo_url: str) -> str:
    """Write one card. Returns its path, or "" when the render failed.

    Rendered in the same headed Chromium the post will use, not a hidden one: it is the window the
    operator is already watching, and a second browser for one screenshot is a second profile lock.
    """
    out = config.SOCIAL_DIR / f"{date.today().isoformat()}-{_slugify(card['label'])}-{_digest(card['title'])}.png"
    page = None
    try:
        page = await (await automations._context("linkedin", headless=False)).new_page()
        await page.set_viewport_size({"width": _CARD[0], "height": _CARD[1]})
        await page.set_content(_card_html(card, repo_url), wait_until="load")
        await page.screenshot(path=str(out))
        return str(out)
    except Exception as exc:
        print(f"social: card render failed: {type(exc).__name__}: {exc}")
        return ""
    finally:
        await automations._close_quietly(page)


_EDITORS = ('div.ql-editor[contenteditable="true"]', 'div[role="textbox"][contenteditable="true"]',
            'div[contenteditable="true"]')

# The Add-project form's fields, each under every name LinkedIn has given it. A label that has moved
# is a field that gets reported missing, never a value typed into whichever box happens to be next -
# the pitch landing in the name box would be a wrong profile, not a failed run. There is no URL field
# on this form any more: the redesign folds links into Media, so the repository goes in there as a
# link card and rides in the description as text, with the GitHub profile after it as a reference.
_PROFILE_FIELDS = (
    ("title", ("Project name", "Project title", "Title")),
    ("description", ("Description", "Project description")),
)
_SKILL_MAX = 5      # LinkedIn's own recommendation for a project, and its own cap in spirit
_LINK_BOX = re.compile(r"Paste or type a link", re.I)
_MEDIA_TRIES = 3    # the preview fetch fails at random; Try again is LinkedIn's own retry for it


def _best_skill(query: str, options: list[str]) -> str:
    """Which of LinkedIn's suggestions to take for a typed stack word, or "" for none of them.

    A skill on this form is a taxonomy entry, not text: the word typed is only a query, and whatever
    comes back is what would be stored. So an exact entry wins, then the taxonomy's canonical form
    (`Python (Programming Language)` over `Python with Data Science`), then any suggestion that
    genuinely begins with the word - and nothing at all when none does, because `argparse` and
    `stdlib` are not skills and the list that answers them with `Apache Ant` is not offering one.
    """
    q = " ".join((query or "").split()).lower()
    if not q:
        return ""
    exact = [o for o in options if o.lower() == q]
    head = [o for o in options if re.match(rf"^{re.escape(q)}\b", o, re.I)]
    qualified = [o for o in head if re.match(rf"^{re.escape(q)}\s*\(", o, re.I)]
    return (exact or qualified or head or [""])[0]


async def _form_fill(page, names: tuple[str, ...], text: str) -> str:
    """Type into the first of `names` that the form actually has. Returns the name it used, or ""."""
    for name in names:
        loc = page.get_by_label(name, exact=False).last
        if await automations._seen(loc, 2_000):
            try:
                await loc.fill(text)
                return name
            except Exception:
                continue
    return ""


async def _form_ready(page) -> bool:
    """The Add-project form is open when its own first field is on screen."""
    for name in _PROFILE_FIELDS[0][1]:
        if await automations._seen(page.get_by_label(name, exact=False).last, 3_000):
            return True
    return False


async def _open_project_form(page, profile: str) -> str:
    """Get to the Add-project form. Returns "" when it is open, or why it could not be.

    The edit-form deep link, and nothing else: it is LinkedIn's own address for this form, it lands on
    it in one navigation, and it is the one entry point that does not depend on where the profile page
    has filed its "Add section" dialog this quarter. When LinkedIn moves it, the operator has the
    dashboard's copy entry, and the day's post - which this is never allowed to cost - has gone out.
    """
    await page.goto(f"{profile}/edit/forms/project/new/", wait_until="domcontentloaded",
                    timeout=automations._NAV_TIMEOUT)
    await automations._human(2.0, 3.5)
    if any(wall in page.url for wall in ("/authwall", "/login", "/checkpoint", "/uas/")):
        return "bounced to a login wall - re-authenticate browser_sessions/linkedin"
    return "" if await _form_ready(page) else "the Add-project form did not open"


async def _add_skill(page, word: str) -> str:
    """Offer one stack word to the skill typeahead and take LinkedIn's own answer for it.

    Read off the typeahead's own listbox and not the page: a native <select> is a listbox full of
    role=option elements too, so the form's 100-year year picker alone would put every one of its
    years in front of this scan - slow enough to stall a finish, and a wrong list to choose from.

    The input is gone the moment a suggestion is taken - the skill becomes a chip and the next one
    starts from a fresh Add skill - so an unanswered word has to be cleared out by hand before the
    next one is typed.
    """
    add = page.get_by_role("button", name="Add skill", exact=True).last
    if not await automations._seen(add, 3_000):
        return ""
    await add.click()
    await automations._human(0.6, 1.2)
    box = page.locator('input[placeholder^="Skill"]').last
    if not await automations._seen(box, 3_000):
        return ""
    await box.click()
    await box.type(word, delay=45)
    menu = page.locator('div[role="listbox"]:visible').last
    picks: list[tuple[str, object]] = []
    if await automations._seen(menu, 2_500):
        await automations._human(0.4, 0.8)      # the list is rendered in two passes
        options = menu.get_by_role("option")
        for i in range(min(await options.count(), 8)):
            opt = options.nth(i)
            if await automations._seen(opt, 300):
                text = " ".join((await opt.inner_text() or "").split())
                if text:
                    picks.append((text, opt))
    if chosen := _best_skill(word, [t for t, _ in picks]):
        await next(loc for t, loc in picks if t == chosen).click()
        await automations._human(0.7, 1.3)
        return chosen
    await box.fill("")
    await page.get_by_label(_PROFILE_FIELDS[0][1][0], exact=False).last.click()
    await automations._human(0.4, 0.9)
    return ""


async def _add_skills(page, tech: str) -> list[str]:
    """The project's stack as LinkedIn skills, at most five, every one of them off its own list.

    The one wait is here, not per word: the Skills block renders a beat later than the name field the
    form is declared open by, and a first word that arrives before the button does must not cost the
    whole stack.
    """
    words = [w.strip() for w in re.split(r"[,\n]", tech or "") if w.strip()][:_SKILL_MAX]
    add = page.get_by_role("button", name="Add skill", exact=True).last
    if not await automations._seen(add, 8_000):
        return []
    picked = []
    for word in words:
        if skill := await _add_skill(page, word):
            picked.append(skill)
    return picked


async def _add_dates(page, project: dict) -> str:
    """The start and end month of the project, out of the days the ledger already has.

    Best effort by design: a date is the one part of this entry the form can lose without the entry
    saying anything untrue, so a picker that refuses one is reported in the message and not treated as
    a failed profile update. Nothing is filled when the row carries no day - the ledger is the only
    thing here that knows when the work happened.
    """
    days = [str(project.get("started_day") or "")[:10], str(project.get("finished_day") or "")[:10]]
    if not any(days):
        return ""
    fields = []
    for sel in await page.locator("select").all():
        if not await automations._seen(sel, 300):
            continue
        if (await sel.locator("option").first.inner_text() or "").strip() in ("Month", "Year"):
            fields.append(sel)
    if len(fields) != 4:
        return "dates skipped (the form has no start/end pickers)"
    said = []
    for day, at in zip(days, (0, 2)):
        try:
            when = date.fromisoformat(day)
        except ValueError:
            continue
        try:
            await fields[at].select_option(label=when.strftime("%B"))
            await fields[at + 1].select_option(label=str(when.year))
        except Exception as exc:
            return f"dates skipped ({type(exc).__name__})"
        said.append(when.strftime("%b %Y"))
    return " - ".join(said)


def _profile_links(project: dict) -> list[str]:
    """The URLs a profile entry carries, in the order they go in: the project's own repository first,
    then the GitHub profile as a standing reference. Duplicates are dropped, so a project that is
    itself the profile README does not put the same link in twice."""
    links = [str(project.get("repo_url") or "")]
    if config.GITHUB_USER:
        links.append(f"https://github.com/{config.GITHUB_USER}")
    return [url for i, url in enumerate(links) if url and url not in links[:i]]


async def _repo_alive(page, url: str) -> str:
    """Open the repository the way a reader would and say why it does not stand up, or "".

    A post scrolls past; a profile entry sits there. That is why the link is opened in a browser
    before it is written anywhere: a repository that answers a 404, that asks for a login, or that
    opens on some other page is reported, and the entry is not written at all. The tab runs in the
    LinkedIn profile, which is not logged into GitHub - so this is exactly what a logged-out reader
    sees, which is the reader that matters for a public repository.
    """
    tab = await page.context.new_page()
    try:
        res = await tab.goto(url, wait_until="domcontentloaded", timeout=automations._NAV_TIMEOUT)
        if res is not None and res.status >= 400:
            return f"the repository link answers {res.status}"
        title = await tab.title() or ""
        if "/login" in tab.url or "page not found" in title.lower():
            return "the repository link does not open - is the repository public?"
        name = _repo_full(url).split("/")[-1].lower()
        if name and name not in tab.url.lower() and name not in title.lower():
            return "the repository link opened, but not on the repository"
        return ""
    except Exception as exc:
        return f"the repository link does not open ({type(exc).__name__}: {str(exc)[:120]})"
    finally:
        await automations._close_quietly(tab)


async def _media_open(page) -> bool:
    """Is the Add-media dialog, or its discard prompt, the thing on top?

    Both states count, and that is the point: a preview that failed leaves the dialog up showing the
    error, and pressing its X then raises a "Discard changes" prompt on top of that. The project form
    is unreachable from either one until the discard is answered.
    """
    dialog = page.get_by_role("dialog").last
    try:
        head = " ".join((await dialog.inner_text() or "").split())[:60].lower()
    except Exception:
        return False
    return (head.startswith("add media") or head.startswith("discard changes")
            or await automations._seen(page.get_by_label(_LINK_BOX).last, 300))


async def _drop_media(page) -> bool:
    """Take the only exit a failed Add-media dialog offers, and say whether it closed.

    That exit is the X and then "Discard", and it takes the whole project form down with it - the
    caller re-opens and refills, minus the link the previewer refused. The alternative is the one
    state this flow must never end in: a dialog that cannot be closed and a form that can never be
    saved.
    """
    if not await _media_open(page):
        return True
    x = page.get_by_role("dialog").last.get_by_role("button", name="Dismiss").last
    if await automations._seen(x, 1_500):
        try:
            await x.click()
            await automations._human(1.0, 1.5)
        except Exception:
            return False
    discard = page.get_by_role("button", name="Discard", exact=True).last
    if await automations._seen(discard, 2_500):
        await discard.click()
        await automations._human(2.0, 2.5)
    return not await _media_open(page)


async def _add_media_link(page, url: str) -> str:
    """Attach one URL to the Media section and report the title LinkedIn's previewer gave it, or "".

    The preview is the gate: the dialog's Save only unlocks once LinkedIn's own fetcher has read the
    page, and that fetcher fails at random for URLs that are perfectly reachable - the same repository
    link answers with a card one minute and "couldn't load a preview" the next. So the fetch gets a few
    rounds through the dialog's own Try again before the link is given up on, and a link given up on is
    skipped, never fatal: the repository URL also rides in the description, in plain text.

    Cleanup is the caller's, not this function's: a dialog left up by a failed preview has exactly one
    exit, and taking it is a decision about the whole form, not about this one link.
    """
    try:
        add = page.get_by_role("button", name="Add media", exact=False).last
        if not await automations._seen(add, 3_000):
            return ""
        await add.click()
        await automations._human(0.8, 1.3)
        item = page.get_by_text("Add a link", exact=True).last
        if not await automations._seen(item, 3_000):
            return ""
        await item.click()
        await automations._human(1.5, 2.2)
        box = page.get_by_label(_LINK_BOX).last
        if not await automations._seen(box, 4_000):
            return ""
        await box.click()
        await box.type(url, delay=25)
        dialog = page.get_by_role("dialog").last
        send = dialog.get_by_role("button", name="Add", exact=True).last
        save = dialog.get_by_role("button", name="Save", exact=True).last
        if not await automations._seen(send, 2_000):
            return ""
        for _ in range(_MEDIA_TRIES):
            await send.click()
            await automations._human(4.0, 5.5)
            if not await save.is_disabled():
                break
            again = dialog.get_by_role("button", name="Try again", exact=True).last
            if await automations._seen(again, 1_500):
                await again.click()
                await automations._human(4.0, 5.5)
                if not await save.is_disabled():
                    break
        if await save.is_disabled():
            return ""
        title = ""
        for field in await dialog.locator("input").all():
            try:
                value = " ".join((await field.input_value() or "").split())
                if value and value != url:
                    title = value[:120]
            except Exception:
                continue
        await save.click()
        await automations._human(2.0, 3.0)
        return title or url
    except Exception:
        return ""


async def add_to_profile(project: dict, save: bool = True) -> tuple[bool, str]:
    """Put the finished project in the Projects section of the operator's own LinkedIn profile.

    A post scrolls; a profile is what someone reads after clicking the name on it. So the day a
    project lands, its repo goes onto the profile - the title, the pitch, the link, the months it was
    built in, and its stack, taken from LinkedIn's own skill list. The form has no URL field of its
    own any more: the repository goes in as a Media link card, the GitHub profile follows it as a
    reference, and the repository URL also rides in the description as text. The link is opened in a
    browser before any of this is written - a profile entry does not scroll past.

    `save=False` fills the form and leaves the window open, which is how this is checked without
    editing a live profile. A failure here is reported and never fatal: the day's post has already
    gone out, and the dashboard keeps the same entry ready to paste by hand.
    """
    if not project.get("repo_url"):
        return False, "profile: the project has no repository to link"
    page = None
    try:
        page = await (await automations._context("linkedin", headless=False)).new_page()
        page.set_default_timeout(automations._STEP_TIMEOUT)
        profile = (ai_engine.CONTACT.get("linkedin")
                   or "https://www.linkedin.com/in/me").rstrip("/")
        if problem := await _repo_alive(page, project["repo_url"]):
            return False, f"profile: {problem} - the entry was not added"
        error = await _open_project_form(page, profile)
        if error:
            return False, f"profile: {error}" + await automations._debug_shot(page)
        texts = {"title": project.get("title", "")[:255],
                 "description": f"{project.get('pitch', '')}\n\n{project['repo_url']}".strip()}
        links = _profile_links(project)
        failed: list[str] = []
        # A refused preview costs the whole form, not just the link, and the retry that works is a
        # fresh dialog rather than the failed one's own Try again - so a link gets two forms to
        # succeed on before it is given up on.
        for _ in range(2 * len(links) + 1):
            filled, missing = [], []
            for key, names in _PROFILE_FIELDS:
                if used := await _form_fill(page, names, texts[key]):
                    filled.append(used)
                else:
                    missing.append(names[0])
            if missing:
                return False, f"profile: the form opened, but not its {', '.join(missing)} field"
            skills = await _add_skills(page, project.get("tech", ""))
            detail = page.get_by_text("Additional details", exact=True).last
            if await automations._seen(detail, 2_000):
                await detail.click()
                await automations._human(0.8, 1.4)
            when = await _add_dates(page, project)
            pending, attached = [u for u in links if failed.count(u) < 2], []
            for url in pending:
                if title := await _add_media_link(page, url):
                    attached.append(title)
                else:
                    failed.append(url)
            if not await _media_open(page):
                break
            # A refused link left its dialog in the one state that cannot be closed: drop it -
            # which takes the form with it - and refill, retrying what has not failed twice. The
            # entry lands with every link the previewer would take; the refused one still rides in
            # the description.
            if not await _drop_media(page):
                return False, ("profile: the media dialog could not be closed"
                               + await automations._debug_shot(page))
            if error := await _open_project_form(page, profile):
                return False, f"profile: {error}" + await automations._debug_shot(page)
        else:
            return False, ("profile: the media dialog would not settle"
                           + await automations._debug_shot(page))
        media = f"links {len(attached)}/{len(links)}"
        if not save:
            return True, (f"profile: form filled ({', '.join(filled)}"
                          + (f", skills {', '.join(skills)}" if skills else "")
                          + (f", {when}" if when else "")
                          + f", {media}"
                          + ") and left open - nothing was saved")
        button = page.get_by_role("button", name="Save", exact=True).last
        if not await automations._seen(button, 8_000):
            return False, "profile: no Save button in the form" + await automations._debug_shot(page)
        await button.click()
        await automations._human(2.0, 3.5)
        # A form that refused the save is still on screen, and it says why - almost always in one
        # line under the field it refused. Reported, because "added" over a form that never closed
        # would be the one wrong message that matters here.
        still = page.get_by_role("dialog").last
        if await automations._seen(still, 3_000):
            why = " ".join((await still.inner_text() or "").split())[:200]
            return False, f"profile: the form stayed open - {why}" + await automations._debug_shot(page)
        said = [*([f"skills {', '.join(skills)}"] if skills else []), *([when] if when else []), media]
        return True, (f"profile: {texts['title']} added to your projects"
                      + (f" ({'; '.join(said)})" if said else ""))
    except Exception as exc:
        return False, f"profile: {type(exc).__name__}: {exc}"
    finally:
        if save:
            await automations._close_quietly(page)


async def post_to_linkedin(text: str, image: "str | list[str]" = "", publish: bool = True) -> tuple[bool, str]:
    """Type the post into the share box on the operator's own profile, then publish it.

    The share box is opened through LinkedIn's own `shareActive` deep link rather than by clicking
    "Start a post": the button is restyled every few months and the deep link is not, so this is the
    one entry point that does not need a selector. Media goes in through the composer's own file
    input, which does not exist until Media has been pressed - set_input_files then drives it
    directly, so the OS dialog never opens, and a list of files becomes the gallery.

    `publish=False` fills everything and leaves the window open for a human to read and press Post.
    That is how this is checked without putting anything in front of an audience.
    """
    if not text.strip():
        return False, "linkedin: nothing to post"
    images = [image] if isinstance(image, str) else [i for i in image if i]
    page = None
    try:
        page = await (await automations._context("linkedin", headless=False)).new_page()
        page.set_default_timeout(automations._STEP_TIMEOUT)
        await page.goto("https://www.linkedin.com/feed/?shareActive=true",
                        wait_until="domcontentloaded", timeout=automations._NAV_TIMEOUT)
        if any(wall in page.url for wall in ("/authwall", "/login", "/checkpoint", "/uas/")):
            return False, "linkedin: bounced to a login wall - re-authenticate browser_sessions/linkedin"
        await automations._human(2.0, 3.5)
        editor = None
        for sel in _EDITORS:
            loc = page.locator(sel).first
            if await automations._seen(loc, 8_000):
                editor = loc
                break
        if editor is None:
            return False, "linkedin: the share box never opened" + await automations._debug_shot(page)
        # Focus rather than click: the composer animates in, and a click that lands while the opening
        # illustration still covers the editor is a hit-test failure, not a missing editor. Focus skips
        # hit-testing entirely, and the character count below proves the caret was really in the box.
        await editor.focus()
        await page.keyboard.insert_text(text)
        await automations._human(1.0, 2.0)
        typed = (await editor.inner_text()).strip()
        if len(typed) < min(80, len(text) // 2):
            print(f"social: the editor took {len(typed)} of {len(text)} characters - retrying with fill")
            await editor.fill(text)
        note = ""
        if images:
            try:
                # The composer carries no file input until Media is pressed: that click creates the
                # hidden element and opens LinkedIn's media Editor over it. The Editor's own Next stays
                # disabled until the upload has landed, so "Next just became enabled" is the signal
                # that the picture is up - clicking before that drops the image and posts text only.
                media = page.get_by_role("button", name="Media", exact=True).last
                if await automations._seen(media, 5_000):
                    await media.click()
                    await page.locator('input[type="file"]').first.wait_for(state="attached", timeout=10_000)
                await page.locator('input[type="file"]').first.set_input_files(images, timeout=20_000)
                nxt = page.get_by_role("button", name="Next", exact=True).last
                for _ in range(30):
                    await asyncio.sleep(1)
                    try:
                        if await nxt.is_enabled(timeout=1_000):
                            await nxt.click()
                            break
                    except Exception:
                        break
                await automations._human(2.0, 3.5)
                attached = await page.locator('img[src^="blob:"]').count()
                if attached < len(images):
                    note = (f" · {attached} of {len(images)} images attached, the rest did not"
                            " - check the post before it goes out")
            except Exception as exc:
                note = f" · the picture did not attach ({type(exc).__name__})"
                print(f"social: {note}")
        if not publish:
            return True, "linkedin: composer filled and left open - nothing was published" + note
        post = page.get_by_role("button", name="Post", exact=True).last
        if not await automations._seen(post, 8_000):
            return False, "linkedin: no Post button in the composer" + await automations._debug_shot(page)
        await post.click()
        await automations._human(3.0, 5.0)
        try:
            await page.locator('div[role="dialog"]').last.wait_for(state="hidden", timeout=20_000)
        except Exception:
            pass
        return True, "linkedin: posted" + note
    except Exception as exc:
        return False, f"linkedin: {type(exc).__name__}: {exc}"
    finally:
        if publish:
            await automations._close_quietly(page)


_PROFILE_TPL = """# %NAME%

**%HEADLINE%**

%ABOUT%

## What I have been building

| Project | What it does | Started |
|---|---|---|
%BUILDS%

## Toolkit

`python` · `linux` · `aws` · `azure` · `docker` · `kubernetes` · `terraform` · `splunk` · `wireshark` ·
`burp suite` · `nmap` · `siem` · `incident response` · `iam` · `ci/cd`

## Reach me

%CONTACT%
"""


def _profile_readme(art: dict, projects: list[dict]) -> str:
    rows = [p for p in projects if p.get("repo_url")]
    table = "\n".join(
        f"| [{p['title']}]({p['repo_url']}) | {(p.get('pitch') or '').replace('|', '/')[:120]} | "
        f"{p.get('started_day', '')} |" for p in rows[:8]) or "| _the next project lands here_ | | |"
    contact = " · ".join(x for x in (ai_engine.CONTACT.get("email"), ai_engine.CONTACT.get("linkedin"),
                                     ai_engine.CONTACT.get("github")) if x)
    return (_PROFILE_TPL
            .replace("%NAME%", ai_engine.CANDIDATE)
            .replace("%HEADLINE%", art.get("headline", ""))
            .replace("%ABOUT%", art.get("about", ""))
            .replace("%BUILDS%", table)
            .replace("%CONTACT%", contact or "open to SOC, cloud security and DevSecOps roles"))


async def refresh_profile(art: dict, projects: list[dict]) -> tuple[str, str]:
    """Rebuild and push the GitHub profile README. Returns (url, error)."""
    try:
        full = await _repo(config.GITHUB_USER, f"{ai_engine.CANDIDATE} - cloud security & DevSecOps",
                           reuse=True)
        await _put_file(full, "README.md", _profile_readme(art, projects), "docs: refresh the profile")
        return f"https://github.com/{full}", ""
    except Exception as exc:
        print(f"social: profile refresh failed: {type(exc).__name__}: {exc}")
        return "", f"{type(exc).__name__}: {exc}"


def refresh_due(rows: list[dict]) -> int:
    """Days until the profile is due again. 0 means today, and it is what the dashboard counts down."""
    last = max((r["day"] for r in rows if r["kind"] == "profile" and r["status"] == "posted"), default="")
    if not last:
        return 0
    return max(0, config.PROFILE_REFRESH_DAYS - (date.today() - date.fromisoformat(last)).days)


def phases_of(project: dict) -> list[dict]:
    try:
        return json.loads(project.get("phases") or "[]")
    except Exception:
        return []


def step_label(project: dict | None) -> str:
    """What today's press is going to do. The dashboard shows it above the button, and it is the only
    place the operator can see which of the three kinds of day this is before clicking."""
    if not project:
        return "announce a new project"
    phases = phases_of(project)
    if not project.get("announced"):
        return f"announce {project['title']} - the last attempt did not get the post out"
    if project["done"] < len(phases):
        return f"phase {project['done'] + 1} of {len(phases)} · {phases[project['done']]['name']}"
    return f"finish {project['title']} - the wrap-up post with the screenshots"


def _posted_today(rows: list[dict], day: str) -> dict | None:
    """Today's real post, whichever kind it was. dry-run and failed rows do not hold the day: the
    press has to be repeatable until something actually goes out."""
    return next((r for r in rows if r["day"] == day and r["status"] == "posted"
                 and r["kind"] in ("announce", "phase", "finish")), None)


async def _fail(day: str, kind: str, message: str, *, title: str = "", summary: str = "", slug: str = "",
                body: str = "", repo_url: str = "") -> dict:
    await database.save_daily_post(day, kind, status="failed", title=title, summary=summary, slug=slug,
                                   body=body, repo_url=repo_url, error=message)
    return _note(message, "fail", False)


async def _announce(day: str, angle: str, project: dict | None) -> AsyncIterator[dict]:
    """Day one of a project: the plan, the repository, the card, and the post that starts it.

    `project` is not None only in the retry lane - the plan and maybe the repository already exist
    from an earlier attempt today, and that press must finish the announcement rather than open a
    second repository for the same project.
    """
    if project:
        plan = {"title": project["title"], "slug": project["slug"], "pitch": project["pitch"],
                "tech": project["tech"], "run": project["run"], "phases": phases_of(project),
                "intro": project["intro"], "headline": project["headline"], "about": project["about"]}
        yield _note(f"reusing today's plan - {plan['title']}, {len(plan['phases'])} phases", "ok", True)
    else:
        yield _note("planning today's project…")
        try:
            plan = parse_plan(await _ask(_plan_prompt(day, angle)))
        except Exception as exc:
            yield await _fail(day, "announce", f"the plan failed: {type(exc).__name__}: {exc}")
            return
        yield _note(f"{plan['title']} · {len(plan['phases'])} phases · {plan['tech']} · "
                    f"angle: {angle}", "ok", True)
        await database.open_project(plan["slug"], title=plan["title"], pitch=plan["pitch"],
                                    tech=plan["tech"], phases=json.dumps(plan["phases"]),
                                    run=plan["run"], intro=plan["intro"],
                                    headline=plan["headline"], about=plan["about"],
                                    day=day, db_path=config.DB_PATH)

    repo, url = "", (project or {}).get("repo_url", "")
    if url:
        yield _note(f"the repository is already open - {url}", "ok", True)
    elif config.DRY_RUN:
        yield _note("dry-run: would open the repository and push the plan", "skip", True)
    elif problem := credential_problem():
        yield await _fail(day, "announce", f"github: {problem}", title=plan["title"],
                          summary=plan["pitch"], slug=plan["slug"], body=plan["intro"])
        return
    else:
        yield _note(f"opening the repository {plan['slug']} with the plan in its README…")
        try:
            repo, url = await create_repo(plan)
        except Exception as exc:
            yield await _fail(day, "announce", f"github: {type(exc).__name__}: {_tail(exc)}",
                              title=plan["title"], summary=plan["pitch"], slug=plan["slug"],
                              body=plan["intro"])
            return
        await database.update_project(plan["slug"], repo_url=url, db_path=config.DB_PATH)
        yield _note(f"pushed to {url}", "ok", True)

    card = await _card({
        "label": "day 1 · the plan", "title": plan["title"], "summary": plan["pitch"],
        "head": plan["tech"],
        "block": "\n".join(f"{i + 1}. {p['name']}" for i, p in enumerate(plan["phases"]))},
        url)
    body = plan["intro"]
    async for event in _publish(day, "announce", body, card, plan["slug"], plan["title"],
                                plan["pitch"], url, announced=True):
        yield event


async def _phase(day: str, project: dict) -> AsyncIterator[dict]:
    """One phase: written, run, committed, and posted - in that order, and only if it runs."""
    phases = phases_of(project)
    index = project["done"]
    phase = phases[index]
    yield _note(f"phase {index + 1} of {len(phases)}: {phase['name']} - asking the model…")
    files = {}
    try:
        post, files, output = await _build(project, phases, index)
    except Exception as exc:
        yield await _fail(day, "phase", f"phase {index + 1} ({phase['name']}) failed: "
                                       f"{type(exc).__name__}: {exc}",
                          title=project["title"], summary=phase["what"], slug=project["slug"])
        return
    lines = sum(len(text.splitlines()) for text in files.values())
    yield _note(f"{len(files)} files, {lines} lines · it runs: {_tail(output.splitlines()[0] if output else '', 90)}",
                "ok", True)

    url = project["repo_url"]
    pushed, error = json.loads(project["files"] or "{}"), ""
    if config.DRY_RUN:
        yield _note("dry-run: would commit this phase to the repository", "skip", True)
    elif problem := credential_problem():
        yield await _fail(day, "phase", f"github: {problem}", title=project["title"],
                          summary=phase["what"], slug=project["slug"], repo_url=url)
        return
    else:
        yield _note(f"committing phase {index + 1} to {url}…")
        pushed, error = await push_files(_repo_full(url), files,
                                         json.loads(project["files"] or "{}"),
                                         f"phase {index + 1}: {phase['name']}")
        if error:
            yield await _fail(day, "phase", f"github: {error}", title=project["title"],
                              summary=phase["what"], slug=project["slug"], repo_url=url)
            return
        yield _note(f"pushed {len(files)} files to {url}", "ok", True)

    card = await _card({
        "label": f"phase {index + 1} of {len(phases)}", "title": project["title"],
        "summary": f"{phase['name']} - {phase['what']}", "head": f"$ {project['run']}",
        "block": output or phase["what"]}, url)
    nxt = phases[index + 1]["name"] if index + 1 < len(phases) else "nothing - this was the last one"
    yield _note(f"card written · next phase: {nxt}", "ok", True)
    async for event in _publish(day, "phase", post, card, project["slug"], project["title"],
                                phase["what"], url,
                                advance={"done": index + 1, "files": json.dumps(pushed),
                                         "output": output}):
        yield event


async def _finish(day: str, project: dict) -> AsyncIterator[dict]:
    """The last day of a project: the final run, the closing post, and its two pictures."""
    phases = phases_of(project)
    yield _note("every phase is in - running the finished project one more time…")
    ok, output = await run_project(project["slug"], project["run"])
    if not ok:
        yield await _fail(day, "finish", f"the finished project did not run: {_tail(output, 300)}",
                          title=project["title"], summary=project["pitch"], slug=project["slug"],
                          repo_url=project["repo_url"])
        return
    try:
        post, readme = parse_finish(await _ask(_finish_prompt(project, phases, output)))
    except Exception as exc:
        yield await _fail(day, "finish", f"the wrap-up failed: {type(exc).__name__}: {exc}",
                          title=project["title"], summary=project["pitch"], slug=project["slug"])
        return
    yield _note(f"the finished project runs: {_tail(output.splitlines()[0] if output else '', 90)}",
                "ok", True)

    url = project["repo_url"]
    # An empty <readme> block is missing content, not content: committing it would replace the real
    # README with nothing on the day the repository is finally worth reading.
    if not readme.strip():
        yield _note("the wrap-up came back without a README - the last phase's README stands", "skip", True)
    elif config.DRY_RUN:
        yield _note("dry-run: would commit the final README", "skip", True)
    elif problem := credential_problem():
        yield await _fail(day, "finish", f"github: {problem}", title=project["title"],
                          summary=project["pitch"], slug=project["slug"], repo_url=url)
        return
    else:
        try:
            await _put_file(_repo_full(url), "README.md", readme, "docs: the finished README")
            yield _note(f"final README pushed to {url}", "ok", True)
        except Exception as exc:
            yield await _fail(day, "finish", f"github: {type(exc).__name__}: {_tail(exc)}",
                              title=project["title"], summary=project["pitch"],
                              slug=project["slug"], repo_url=url)
            return

    card = await _card({
        "label": "finished", "title": project["title"], "summary": project["pitch"],
        "head": f"$ {project['run']}", "block": output}, url)
    proof = await _card({
        "label": f"{len(phases)} phases", "title": project["title"],
        "summary": "what went in, in the order it was built", "head": "the phases",
        "block": "\n".join(f"{i + 1}. {p['name']}" for i, p in enumerate(phases))}, url)
    async for event in _publish(day, "finish", post, [card, proof], project["slug"],
                                project["title"], project["pitch"], url,
                                advance={"status": "finished", "finished_day": day, "output": output},
                                images=[card, proof]):
        yield event

    # The profile entry goes in after the post, and only after it: the post is the day's job, and a
    # profile form that moved is not allowed to cost it. Read the ledger rather than the stream, the
    # same way every other day-end does.
    today = next((r for r in await database.daily_posts(5)
                  if r["day"] == day and r["kind"] == "finish"), None)
    if not today or today["status"] not in ("posted", "dry-run"):
        return
    if config.DRY_RUN:
        yield _note("dry-run: would add this project to the projects section of your LinkedIn profile",
                    "skip", True)
        return
    yield _note("adding the project to the projects section of your LinkedIn profile…")
    ok, msg = await add_to_profile({**project, "finished_day": day})
    yield _note(msg, "ok" if ok else "skip", ok)


async def _card(card: dict, url: str) -> str:
    """Render one card, and put it in the repository when there is one to put it in - the README links
    the picture, and a link into a repository is worth more when the repository shows the picture."""
    image = await render_card(card, url)
    repo = "" if config.DRY_RUN else _repo_full(url)
    if image and repo and config.GITHUB_TOKEN:
        if err := await push_card(repo, [image]):
            print(f"social: the card did not reach the repository: {err}")
    return image


async def _publish(day: str, kind: str, body: str, image: "str | list[str]", slug: str, title: str,
                   summary: str, url: str, *, announced: bool = False,
                   advance: dict | None = None, images: list[str] | None = None) -> AsyncIterator[dict]:
    """The one place a day's post actually goes out. Everything above this is preparation, and every
    outward step in it is behind DRY_RUN."""
    images = images or ([image] if isinstance(image, str) and image else [])
    # One place appends the link, so no kind of day can be the one that forgets it. LinkedIn counts
    # the link as characters like any other, and the whole thing is trimmed under the 3,000 cap here
    # rather than in three callers.
    body = (body.strip() + (f"\n\nCode: {url}" if url else ""))[:_POST_CHARS]
    # The ledger keeps the file NAME, and the dashboard serves it from /social: a row that pointed at
    # an absolute path would be a path on this machine, which is not something a table should carry.
    card = Path(images[0]).name if images else ""
    if config.DRY_RUN:
        await database.save_daily_post(day, kind, status="dry-run", title=title, summary=summary,
                                       slug=slug, body=body, repo_name=_repo_full(url), repo_url=url,
                                       image_path=card)
        yield _note("dry-run: would open the LinkedIn composer and publish · nothing left this machine",
                    "ok", True)
        return

    yield _note("opening the LinkedIn composer in a visible window…")
    ok, msg = await post_to_linkedin(body, images)
    await database.save_daily_post(day, kind, status="posted" if ok else "built", title=title,
                                   summary=summary, slug=slug, body=body, repo_name=_repo_full(url),
                                   repo_url=url, image_path=card, error="" if ok else msg)
    if ok and advance:
        await database.update_project(slug, db_path=config.DB_PATH, **advance)
    if ok and announced:
        await database.update_project(slug, announced=1, db_path=config.DB_PATH)
    yield _note(msg + (f" · {url}" if ok and url else ""), "ok" if ok else "fail", ok)


async def _build(project: dict, phases: list[dict], index: int) -> tuple[str, dict[str, str], str]:
    """Write phase `index`, run it, and hand back (post, files, real output).

    A phase that does not run gets exactly one more turn with its own traceback in the prompt: the
    usual failure is an argument that does not exist yet or a file it forgot to write, and the model
    that wrote the bug fixes it faster than a human reads it. A second failure is reported, never
    posted - "every phase works" has to mean something.
    """
    error = ""
    for attempt in (1, 2):
        post, files = parse_phase(await _ask(_phase_prompt(project, phases, index, _files_of(project),
                                                           error)))
        write_project(project["slug"], files)
        ok, output = await run_project(project["slug"], project["run"])
        if ok:
            return post, files, output
        print(f"social: phase {index + 1} attempt {attempt} did not run: {_tail(output, 200)}")
        if attempt == 2:
            raise RuntimeError(f"`{project['run']}` still fails: {_tail(output, 300)}")
        error = output
    raise RuntimeError("unreachable")


def _files_of(project: dict) -> dict[str, str]:
    """The project's files as they are on disk - what the model is shown before writing the next
    phase, so that a phase adds to the real thing instead of to its memory of it."""
    root = config.PROJECTS_DIR / _safe_rel(project["slug"])
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*")):
        if path.is_file() and not any(part.startswith(".") for part in path.parts):
            try:
                out[path.relative_to(root).as_posix()] = path.read_text("utf-8")[:40_000]
            except Exception:
                continue
    return out


async def daily_post() -> AsyncIterator[dict]:
    """Today's press, whatever today owes: the announcement, a phase, or the wrap-up."""
    day = date.today().isoformat()
    rows = await database.daily_posts(40)
    if today := _posted_today(rows, day):
        yield _note(f"already posted today - {today['kind']}: {today['title']} · {today['repo_url']}",
                    "ok", True)
        return

    project = await database.active_project()
    angle = _ANGLES[date.today().toordinal() % len(_ANGLES)]
    phases = phases_of(project) if project else []
    if not project or not project["announced"]:
        stream = _announce(day, angle, project if project else None)
    elif project["done"] < len(phases):
        stream = _phase(day, project)
    else:
        stream = _finish(day, project)

    async for event in stream:
        yield event

    # Read the ledger rather than trust the stream: "did today's post actually go out" has exactly one
    # source of truth, and every intermediate step above also reports ok.
    if not _posted_today(await database.daily_posts(5), day):
        return
    if due := refresh_due(rows):
        yield _note(f"the GitHub profile is refreshed every {config.PROFILE_REFRESH_DAYS} days - "
                    f"{due} to go", "skip", True)
        return
    if config.DRY_RUN:
        yield _note("the profile refresh is due - dry-run, so nothing was pushed", "skip", True)
        return
    yield _note("15-day profile refresh - rebuilding the profile README…")
    art = {"headline": (project or {}).get("headline", ""), "about": (project or {}).get("about", "")}
    if not art["headline"]:
        fresh = await database.active_project() or {}
        art = {"headline": fresh.get("headline", ""), "about": fresh.get("about", "")}
    profile_url, err = await refresh_profile(art, await database.project_history(20))
    if err:
        yield _note(f"the profile refresh failed: {err}", "skip", True)
        return
    await database.save_daily_post(day, "profile", status="posted", title=art["headline"],
                                   body=art["about"], repo_url=profile_url)
    yield _note(f"profile refreshed - {profile_url} · the headline and About draft are on the "
                "dashboard to paste into LinkedIn", "ok", True)


_PLAN_REPLY = """Sure, here is the plan.

<title>SSH Log Triage</title>
<slug>SSH Log Triage!!</slug>
<pitch>Reads an sshd log and reports the addresses worth banning, with the rule behind each.</pitch>
<tech>python, stdlib, argparse</tech>
<run>python main.py sample/sshd.log</run>
<phases>
Parse the log | reads a real sshd log and counts failures per address
Rank the offenders | sorts by attempts inside a window and prints the top talkers
```
</phases>
<intro>Starting a small sshd log triager today. Python, standard library only, one command.</intro>
<headline>DevSecOps engineer · cloud security</headline>
<about>I build small tools that answer one question well.</about>
"""

_PHASE_REPLY = """Here is phase one.

<post>Phase one of the log triager is in. It parses an sshd log and counts failures per address.</post>
<files>
<file name="main.py">
\"\"\"Count failed logins per address.\"\"\"
print("198.51.100.7 41 failures")
</file>
<file name="README.md">
# SSH Log Triage

Run it with a log path.
</file>
</files>
"""


async def _selfcheck() -> None:
    """Offline checks: the parsers, the path guard, the run gate, the card's escaping, the clocks, and
    the state machine end to end.

    Nothing here touches the network or a browser. The end-to-end legs drive the real daily_post with
    DRY_RUN on and the model replaced, over a real temporary database, so what is proven is the ORDER
    of the chain, the once-a-day gate, that a phase which does not run is never posted, and that the
    project advances one phase per press - not a mocked-up happy path.
    """
    import tempfile
    from datetime import timedelta

    global _ask, render_card, run_project, post_to_linkedin, _put_file, refresh_profile, add_to_profile

    plan = parse_plan(_PLAN_REPLY)
    assert plan["title"] == "SSH Log Triage" and plan["slug"] == "ssh-log-triage", plan
    assert plan["run"] == "python main.py sample/sshd.log", plan["run"]
    assert len(plan["phases"]) == 2 and plan["phases"][1]["name"] == "Rank the offenders", plan["phases"]
    assert plan["phases"][0]["what"].startswith("reads a real sshd log"), plan["phases"]
    assert "```" not in plan["intro"], "a stray fence must not survive into the post"
    try:
        parse_plan(_PLAN_REPLY.replace("<pitch>", "<pitching>"))
        raise AssertionError("a plan missing a block must be rejected, not half-parsed")
    except ValueError as exc:
        assert "<pitch>" in str(exc), exc
    try:
        parse_plan(_PLAN_REPLY.replace("Rank the offenders | sorts by attempts inside a window and "
                                       "prints the top talkers\n", ""))
        raise AssertionError("a one-phase plan is not a project")
    except ValueError as exc:
        assert "phase" in str(exc), exc

    post, files = parse_phase(_PHASE_REPLY)
    assert post.startswith("Phase one") and sorted(files) == ["README.md", "main.py"], files
    assert files["main.py"].startswith('"""Count failed logins'), files["main.py"]
    try:
        parse_phase('<post>x</post><files><file name="../evil.py">x</file></files>')
        raise AssertionError("a file name may not climb out of the project directory")
    except ValueError as exc:
        assert "outside the project" in str(exc), exc
    for bad in ("/etc/passwd", "C:/windows/x.py", ".env", "a/../../b.py"):
        try:
            _safe_rel(bad)
            raise AssertionError(f"{bad} must be refused")
        except ValueError:
            pass
    assert _safe_rel("pkg/tool.py") == "pkg/tool.py"

    card = _card_html({"label": "phase 1 of 3", "title": plan["title"], "summary": "does a thing",
                       "head": "$ python main.py", "block": files["main.py"]},
                      "https://github.com/PJAlgoMaster/ssh-log-triage")
    assert "phase 1 of 3" in card and "github.com/PJAlgoMaster/ssh-log-triage" in card
    assert "&lt;hello&gt;" not in card and "198.51.100.7" in card
    injected = _card_html({"label": "<script>x</script>", "title": "<b>t</b>", "summary": "<i>s</i>",
                           "head": "h", "block": "<img src=x onerror=alert(1)>"}, "")
    assert "<script>" not in injected and "<img" not in injected and "&lt;img" in injected, \
        "the card goes into a real browser - a build must not be able to inject markup into it"

    assert refresh_due([]) == 0, "no profile row yet means the refresh is due now"
    old = (date.today() - timedelta(days=config.PROFILE_REFRESH_DAYS)).isoformat()
    fresh = (date.today() - timedelta(days=1)).isoformat()
    assert refresh_due([{"kind": "profile", "status": "posted", "day": old}]) == 0
    assert refresh_due([{"kind": "profile", "status": "posted", "day": fresh}]) == \
        config.PROFILE_REFRESH_DAYS - 1
    assert refresh_due([{"kind": "profile", "status": "dry-run", "day": old}]) == 0, \
        "a dry-run must not advance the 15-day clock"
    profile = _profile_readme({"headline": "H", "about": "A"},
                              [{"title": "Tool", "repo_url": "https://github.com/x/y",
                                "started_day": "2026-10-01", "pitch": "Does a thing"}])
    assert "[Tool](https://github.com/x/y)" in profile and "H" in profile

    user, token = config.GITHUB_USER, config.GITHUB_TOKEN
    config.GITHUB_USER, config.GITHUB_TOKEN = "", ""
    assert "GITHUB_USER" in credential_problem()
    config.GITHUB_USER = "someone"
    assert "Personal Access Token" in credential_problem(), \
        "the missing-token message has to say what a token is, because a password will not work"
    config.GITHUB_TOKEN = "ghp_neverprinted"
    assert credential_problem() == "" and "ghp_neverprinted" not in credential_problem()
    config.GITHUB_USER, config.GITHUB_TOKEN = user, token

    assert step_label(None).startswith("announce")
    assert step_label({"title": "T", "announced": 0, "done": 0, "phases": "[]"}).startswith("announce T")
    assert step_label({"title": "T", "announced": 1, "done": 0,
                       "phases": json.dumps(plan["phases"])}) == "phase 1 of 2 · Parse the log"
    assert step_label({"title": "T", "announced": 1, "done": 2,
                       "phases": json.dumps(plan["phases"])}).startswith("finish T")

    # Which suggestion a typed stack word becomes. The list is LinkedIn's, so the word is a query and
    # never the stored value: the canonical entry wins over the first one, a word the taxonomy does
    # not carry is dropped rather than forced, and a fuzzy hit that only shares a prefix is not a hit.
    assert _best_skill("python", ["Python with Data Science", "Python (Programming Language)"]) == \
        "Python (Programming Language)"
    assert _best_skill("SQLite", ["SQLite", "SQLite (Database)"]) == "SQLite"
    assert _best_skill("stdlib", []) == ""
    assert _best_skill("argparse", ["Apache Ant", "Argus"]) == "", \
        "a suggestion that does not begin with the word is not that skill"
    assert _best_skill("python", ["Pythonista"]) == "", "a prefix is not the word"
    assert _best_skill("  ", ["Python"]) == ""

    links = _profile_links({"repo_url": "https://github.com/tester/ssh-log-triage"})
    assert links[0] == "https://github.com/tester/ssh-log-triage", \
        "the repository is the first link - the reference never takes its place"
    assert len(links) == len(set(links)) and len(links) <= 2, links
    if config.GITHUB_USER:
        assert links[-1] == f"https://github.com/{config.GITHUB_USER}", links
        assert len(_profile_links({"repo_url": links[-1]})) == 1, \
            "a project that IS the profile README must not carry the same link twice"

    home = tempfile.mkdtemp()
    path = os.path.join(home, "social.db")
    seen_prompts: list[str] = []
    posted_calls: list[tuple] = []
    refreshed: list[list[str]] = []
    profiled: list[tuple] = []

    async def _fake_ask(prompt: str) -> str:
        seen_prompts.append(prompt)
        if "Choose today's project" in prompt:
            return _PLAN_REPLY
        if "The project is finished" in prompt:
            return "<post>It is finished.</post><readme># Done</readme>"
        assert "TODAY'S PHASE" in prompt, prompt[:200]
        return _PHASE_REPLY

    async def _fake_run(slug: str, cmd: str) -> tuple[bool, str]:
        return True, "198.51.100.7 41 failures\n198.51.100.9 12 failures"

    async def _fake_card(item: dict, url: str) -> str:
        assert item["label"] and item["title"], item
        out = Path(home) / f"card-{_digest(item['label'])}.png"
        out.write_bytes(b"\x89PNG\r\n\x1a\n not really a png")
        return str(out)

    async def _fake_post(text: str = "", image="", publish: bool = True) -> tuple[bool, str]:
        names = image if isinstance(image, list) else ([image] if image else [])
        posted_calls.append((text, [Path(n).name for n in names], publish))
        return True, "linkedin: posted"

    async def _fake_put(repo: str, name: str, content, message: str) -> None:
        assert "/" in repo, f"{repo!r} is not owner/name - the repository URL was not stored"
        return None

    async def _fake_refresh(art: dict, projects: list[dict]) -> tuple[str, str]:
        refreshed.append([p["slug"] for p in projects])
        return "https://github.com/tester/tester", ""

    async def _fake_profile(project: dict, save: bool = True) -> tuple[bool, str]:
        profiled.append((project["slug"], save, project.get("finished_day")))
        return True, f"profile: {project['title']} added to your projects"

    async def _boom(prompt: str) -> str:
        raise RuntimeError("the router is down")

    async def _broken(slug: str, cmd: str) -> tuple[bool, str]:
        return False, "Traceback (most recent call last):\nModuleNotFoundError: no module named x"

    await database.init_db(path)
    real = (config.DB_PATH, config.DRY_RUN, config.PROJECTS_DIR, config.GITHUB_USER,
            config.GITHUB_TOKEN, _ask, render_card, run_project, post_to_linkedin, _put_file,
            refresh_profile, add_to_profile)
    config.DB_PATH, config.DRY_RUN = path, True
    config.PROJECTS_DIR = Path(home) / "projects"
    (post_to_linkedin, _put_file, refresh_profile, add_to_profile) = (
        _fake_post, _fake_put, _fake_refresh, _fake_profile)
    try:
        _ask, render_card, run_project = _fake_ask, _fake_card, _fake_run

        announced = [event async for event in daily_post()]
        assert announced[-1]["ok"] is True and "dry-run" in announced[-1]["msg"], announced
        assert any("day 1" in str(e) or "planning today's project" in e["msg"] for e in announced), announced
        assert not any("opening the LinkedIn composer in a visible window" in e["msg"] for e in announced), \
            "dry-run opened the LinkedIn composer - nothing outward-facing may run without the flag off"
        project = await database.active_project(path)
        assert project["slug"] == "ssh-log-triage" and project["done"] == 0, project
        assert project["announced"] == 0 and project["repo_url"] == "", \
            "a dry run must not claim to have opened a repository or announced anything"
        assert step_label(project).startswith("announce SSH Log Triage"), \
            "a dry run did not announce anything, so the next real press still owes the announcement"
        assert len(await database.daily_posts(db_path=path)) == 1, "one row a day"

        await database.update_project("ssh-log-triage", announced=1, db_path=path)
        built = [event async for event in daily_post()]
        assert any("files" in e["msg"] and "it runs" in e["msg"] for e in built), built
        project = await database.active_project(path)
        assert project["done"] == 0, "a dry run must not burn one of the project's phases"
        rows = await database.daily_posts(db_path=path)
        phase_row = next((r for r in rows if r["kind"] == "phase"), None)
        assert phase_row and phase_row["status"] == "dry-run", rows

        _ask, run_project = _boom, _fake_run
        config.DB_PATH = os.path.join(home, "broken.db")
        await database.init_db(config.DB_PATH)
        await database.open_project("x", title="T", pitch="p", tech="t",
                                    phases=json.dumps(plan["phases"]), run="python main.py",
                                    day="2026-10-01", db_path=config.DB_PATH)
        await database.update_project("x", announced=1, db_path=config.DB_PATH)
        failed = [event async for event in daily_post()]
        assert failed[-1]["ok"] is False and "router is down" in failed[-1]["msg"], failed
        assert (await database.active_project(config.DB_PATH))["done"] == 0, \
            "a phase that never ran must not advance the project"

        config.DB_PATH = os.path.join(home, "wonky.db")
        await database.init_db(config.DB_PATH)
        await database.open_project("y", title="T", pitch="p", tech="t",
                                    phases=json.dumps(plan["phases"]), run="python main.py",
                                    day="2026-10-01", db_path=config.DB_PATH)
        await database.update_project("y", announced=1, db_path=config.DB_PATH)
        _ask, run_project = _fake_ask, _broken
        wonky = [event async for event in daily_post()]
        assert wonky[-1]["ok"] is False and "still fails" in wonky[-1]["msg"], wonky
        assert (await database.active_project(config.DB_PATH))["done"] == 0, \
            "a phase that does not run must never be posted as done"

        config.DB_PATH = os.path.join(home, "live.db")
        await database.init_db(config.DB_PATH)
        await database.save_daily_post(date.today().isoformat(), "announce", status="posted",
                                       title="Landed", db_path=config.DB_PATH)
        posted = [event async for event in daily_post()]
        assert len(posted) == 1 and "already posted today" in posted[0]["msg"], posted

        # The wrap-up is the one press that changes the project's own state, so it runs with the flag
        # OFF and only the two outward steps faked: what is checked here is the real transition - the
        # project closing and the row landing - not the fakes.
        config.DB_PATH, config.DRY_RUN = path, False
        config.GITHUB_USER, config.GITHUB_TOKEN = "tester", "ghp_selfcheck"
        await database.update_project("ssh-log-triage", announced=1, done=2, output="ok",
                                      files='{"main.py": "deadbeef"}',
                                      repo_url="https://github.com/tester/ssh-log-triage", db_path=path)
        _ask, render_card, run_project = _fake_ask, _fake_card, _fake_run
        finished = [event async for event in daily_post()]
        assert any(e["msg"].startswith("linkedin: posted") for e in finished), finished
        assert (await database.active_project(path)) is None, "the project must close on the wrap-up"
        history = await database.project_history(db_path=path)
        assert history[0]["status"] == "finished", history[0]
        assert history[0]["finished_day"] == date.today().isoformat(), history[0]
        assert history[0]["output"].startswith("198.51.100.7"), \
            "the closing post is built from the real output of the final run, so it is stored"
        assert len(posted_calls) == 1, f"one press is one post, not {len(posted_calls)}"
        body, names, _ = posted_calls[0]
        assert body.count("https://github.com/tester/ssh-log-triage") == 1, body
        assert len(names) == 2, f"the finished post carries two pictures - the proof and the phases: {names}"
        rows = await database.daily_posts(db_path=path)
        assert any(r["kind"] == "finish" and r["status"] == "posted" for r in rows), rows
        assert refreshed == [["ssh-log-triage"]], \
            f"the profile README is built from the projects that actually finished: {refreshed}"
        assert any("profile refreshed" in e["msg"] for e in finished), \
            "the 15-day refresh is due on the first project, and it goes out after the post"
        assert profiled == [("ssh-log-triage", True, date.today().isoformat())], \
            f"a finished project goes into the profile's projects section with the day it finished, " \
            f"which is what the entry's end month is read from: {profiled}"
        assert finished.index(next(e for e in finished if e["msg"].startswith("profile: "))) > \
            finished.index(next(e for e in finished if e["msg"].startswith("linkedin: posted"))), \
            "the profile entry must not be attempted before the day's post has gone out"
    finally:
        (config.DB_PATH, config.DRY_RUN, config.PROJECTS_DIR, config.GITHUB_USER,
         config.GITHUB_TOKEN, _ask, render_card, run_project, post_to_linkedin, _put_file,
         refresh_profile, add_to_profile) = real

    print("social.py selfcheck ok")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_selfcheck())
