"""One-time interactive sign-in per platform, straight into the persistent Playwright profiles.

    python login.py             # every platform whose profile holds no cookies yet
    python login.py linkedin    # just one
    python login.py --status    # report profile state, launch nothing

You sign in by hand in the window this opens. The script types nothing, reads no credentials, and
stores none: the browser profile under browser_sessions/<platform> is the only artifact, and it is
git-ignored. After this runs once per platform, automations.execute_platform_apply reuses the same
profile and lands already authenticated.
"""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
from pathlib import Path

import automations
import config

LOGIN_URLS = {
    "linkedin": "https://www.linkedin.com/login",
    "naukri": "https://www.naukri.com/nlogin/login",
    "indeed": "https://secure.indeed.com/account/login",
    "internshala": "https://internshala.com/login/student",
}
DOMAINS = {"linkedin": "linkedin.com", "naukri": "naukri.com",
           "indeed": "indeed.com", "internshala": "internshala.com"}


def profile_cookies(platform: str, scratch: Path | None = None) -> int:
    """Cookies the persistent profile already holds for that platform's own domain.

    Proof a sign-in actually happened. A bare "directory is non-empty" test passes as soon as
    Chromium writes its first profile file, which says nothing about being logged in.

    Cookie *names* are plaintext in the Chromium store; values are DPAPI-encrypted and never read
    here. The live database is locked while the browser runs, so it is copied first.
    """
    db = config.BROWSER_SESSIONS / platform / "Default" / "Network" / "Cookies"
    if not db.exists():
        return 0
    copy = (scratch or Path(config.BASE_DIR)) / f".cookies_{platform}.sqlite"
    try:
        shutil.copyfile(db, copy)
        con = sqlite3.connect(copy)
        try:
            count = con.execute("SELECT COUNT(*) FROM cookies WHERE host_key LIKE ?",
                                (f"%{DOMAINS[platform]}",)).fetchone()[0]
            return count
        finally:
            con.close()  # Windows explicitly requires the connection to close before deletion
    except (OSError, sqlite3.Error):
        return 0
    finally:
        copy.unlink(missing_ok=True)


def status() -> dict[str, int]:
    return {p: profile_cookies(p) for p in config.PLATFORMS}


async def sign_in(platform: str) -> int:
    """Open the platform's own login page in its persistent profile and wait for a human."""
    context = await automations._context(platform)
    page = await context.new_page()
    await page.goto(LOGIN_URLS[platform], wait_until="domcontentloaded", timeout=60_000)
    print(f"\n  {platform}: sign in in the browser window, then come back here.")
    await asyncio.to_thread(input, f"  press Enter once {platform} shows you logged in > ")
    live = [c for c in await context.cookies() if DOMAINS[platform] in (c.get("domain") or "")]
    await page.close()
    return len(live)


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--status" in sys.argv:
        for platform, count in status().items():
            print(f"{platform:13} {count:4} cookies {'ready' if count else '-- NOT SIGNED IN'}")
        return 0

    config.HEADLESS = False                      # a login flow needs a window you can see
    before = status()
    todo = [p for p in (args or config.PLATFORMS) if p in LOGIN_URLS]
    if unknown := [p for p in args if p not in LOGIN_URLS]:
        print(f"unknown platform(s): {', '.join(unknown)} — known: {', '.join(LOGIN_URLS)}")
        return 2
    if not args:
        todo = [p for p in todo if not before[p]]
        if not todo:
            print("every profile already holds cookies — nothing to do (--status to inspect)")
            return 0

    print(f"signing in to: {', '.join(todo)}  (profiles under {config.BROWSER_SESSIONS})")
    try:
        for platform in todo:
            try:
                print(f"{platform:13} {await sign_in(platform)} live cookies")
            except Exception as exc:
                print(f"{platform:13} FAILED {type(exc).__name__}: {exc}")
    finally:
        await automations.shutdown()             # closing the context is what flushes the profile

    after = status()
    for platform in config.PLATFORMS:
        moved = after[platform] - before[platform]
        state = "ready" if after[platform] else "-- NOT SIGNED IN"
        print(f"{platform:13} {after[platform]:4} cookies ({moved:+d}) {state}")
    missing = [p for p in config.PLATFORMS if not after[p]]
    print(f"\nlogin.py done · {len(config.PLATFORMS) - len(missing)}/{len(config.PLATFORMS)} profiles ready"
          + (f" · still to do: {', '.join(missing)}" if missing else " · all platforms authenticated"))
    return 1 if missing else 0


def _selfcheck() -> None:
    """No browser, no network: the offline halves must hold before anyone opens a window."""
    assert set(LOGIN_URLS) == set(config.PLATFORMS) == set(DOMAINS), "one login URL per platform"
    for platform, url in LOGIN_URLS.items():
        assert url.startswith("https://"), f"{platform} login page must be https"
        assert DOMAINS[platform] in url, f"{platform} login URL leaves its own domain: {url}"
    counts = status()
    assert set(counts) == set(config.PLATFORMS) and all(isinstance(v, int) for v in counts.values())
    assert profile_cookies("linkedin") >= 0
    config.BROWSER_SESSIONS = Path(config.BASE_DIR) / "no_such_sessions_dir"
    assert profile_cookies("linkedin") == 0, "a missing profile must read as 0, not raise"
    print(f"login.py selfcheck ok · {counts}")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        raise SystemExit(asyncio.run(main()))