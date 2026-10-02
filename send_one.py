"""One watched LinkedIn outreach, behind a typed confirmation. The send trigger, kept out of dom_probe.py.

    python send_one.py https://www.linkedin.com/in/someone "Hi Priya, ..."
    python send_one.py --company "Acme" "Hi, ..."          # resolve a recruiter off the people search

Why this file and not a python -c one-liner: the send is irreversible and reaches a real person. Everything
here is a gate in front of automations.send_linkedin_dm - it adds no automation of its own. It prints the
exact text that will leave, in both truncated forms, then waits for the recipient's own words to be typed
back before it does anything. No retry loop, no queue, one attempt: a loop of automated send-retries against
LinkedIn is what gets an account restricted, and the account here is the user's own.
"""
import asyncio
import sys

import automations as A
import config


def _confirm(target: str, message: str) -> bool:
    note, dm = message[:A.NOTE_LIMIT].strip(), message[:A.DM_LIMIT].strip()
    print(f"\ntarget   : {target}")
    print(f"channels : Connect+note first (free, 2nd/3rd degree), then Message")
    print(f"\nas an invite note ({len(note)}/{A.NOTE_LIMIT} chars):\n  {note}")
    print(f"\nas a direct message ({len(dm)}/{A.DM_LIMIT} chars):\n  {dm}")
    if config.DRY_RUN:
        print("\nDRY_RUN is on - nothing would be sent. Set DRY_RUN=false in .env for a real send.")
    print(f"\nThis sends for real and cannot be undone. Type SEND to go ahead, anything else to abort.")
    return input("> ").strip() == "SEND"


async def main() -> int:
    args = sys.argv[1:]
    company = None
    if args and args[0] == "--company":
        company, args = args[1] if len(args) > 1 else "", args[2:]
    if not args or not company and not args[0].startswith("http"):
        print(__doc__)
        return 2
    target, message = (company, args[0]) if company else (args[0], args[1] if len(args) > 1 else "")
    if not (message or "").strip():
        print("no message given")
        return 2
    if not _confirm(f"company search: {company}" if company else target, message):
        print("aborted - nothing sent")
        return 1
    try:
        if company:
            ok, why = await A.send_linkedin_company_dm(company, message)
        else:
            ok, why = await A.send_linkedin_dm(target, message)
        print(f"\n{'SENT' if ok else 'NOT SENT'}: {why}")
        return 0 if ok else 1
    finally:
        await A.shutdown()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
