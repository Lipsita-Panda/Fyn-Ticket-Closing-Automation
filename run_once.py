"""
One-shot entry point for GitHub Actions.

Unlike ticket_automation.py's main() (an infinite loop for local/VM use),
this runs exactly one pass and exits — GitHub's own cron schedule handles
the "check every 5 minutes" part instead of us looping and sleeping.

Expects credentials.json and token.json contents to arrive as environment
variables (GOOGLE_CREDENTIALS_JSON, GOOGLE_TOKEN_JSON) rather than files
already on disk, since GitHub Actions runners start fresh every time. This
writes them out to the expected filenames before running.
"""
import os

# Recreate the files gmail_auth.py expects, from GitHub Secrets passed in
# as env vars, before importing anything that reads them.
if os.environ.get("GOOGLE_CREDENTIALS_JSON"):
    with open("credentials.json", "w") as f:
        f.write(os.environ["GOOGLE_CREDENTIALS_JSON"])

if os.environ.get("GOOGLE_TOKEN_JSON"):
    with open("token.json", "w") as f:
        f.write(os.environ["GOOGLE_TOKEN_JSON"])

from gmail_auth import get_gmail_service
from ticket_automation import process_once

service = get_gmail_service()
process_once(service)
