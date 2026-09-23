"""
Automated vehicle-service ticket closer.

Watches Gmail (via the Gmail API / OAuth) for unread "UNDER APPROVAL Service
Ticket" emails, parses the vehicle number / repair remarks / status out of
the body, and if the status says to close it, walks through the platform's
Change Status flow and replies "Done" on the same thread.

Run manually for now:  python ticket_automation.py
Keep your laptop + this terminal open; it polls on a loop.

Needs credentials.json (Google Cloud OAuth client) in this folder — see
gmail_auth.py. First run opens a browser once to authorize; after that it
reuses token.json.
"""

import os
import re
import time
import base64
from email.mime.text import MIMEText

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from gmail_auth import get_gmail_service

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PLATFORM_URL = os.environ["PLATFORM_URL"]
PLATFORM_USERNAME = os.environ["PLATFORM_USERNAME"]
PLATFORM_PASSWORD = os.environ["PLATFORM_PASSWORD"]

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))

# Windows default: visible browser via Edge (works around this machine's
# Application Control policy). On the Linux VM, set HEADLESS=true and
# BROWSER_CHANNEL= (empty) in .env instead.
HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true"
BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "msedge") or None

SUBJECT_QUERIES = [
    "UNDER APPROVAL Service Ticket",
    "Service Ticket Status Updated",
    "NEW Service Ticket OPENED",
]


def build_subject_query():
    subject_clause = " OR ".join(f'subject:"{q}"' for q in SUBJECT_QUERIES)
    return f"({subject_clause}) is:unread"

# While True, sends the "Done" reply after successfully closing a ticket.
SEND_DONE_REPLY = True

# Vehicle-number prefix -> vendor location dropdown text (Fyn's own regional
# location, NOT the external garage from the email — confirmed working for
# KA / Bangalore on a real closure).
PREFIX_TO_VENDOR_LOCATION = {
    "KA": "Vendor - Fyn Mobility Bangalore",
    "TN": "Vendor - Fyn Mobility Chennai",
    "TG": "Vendor - Fyn Mobility Hyderabad",
    "TS": "Vendor - Fyn Mobility Hyderabad",  # older Telangana plate format
}


def vendor_location_for(vehicle_number: str) -> str:
    prefix = vehicle_number.strip()[:2].upper()
    try:
        return PREFIX_TO_VENDOR_LOCATION[prefix]
    except KeyError:
        raise ValueError(
            f"No vendor-location mapping for prefix '{prefix}' (vehicle {vehicle_number})."
        )


# ---------------------------------------------------------------------------
# Email body parsing
# ---------------------------------------------------------------------------

def _extract_field(labels, text):
    """labels can be one label or a list of alternate labels to try, in
    order — email formats vary (e.g. 'Vehicle number' vs 'vehicle no')."""
    if isinstance(labels, str):
        labels = [labels]
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*:\s*(.+)", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def parse_ticket_email(body: str):
    """Pull the fields out of a ticket email body. Returns a dict with
    vehicle_number / remarks / status_text, or None if it's missing the
    vehicle number or nothing indicates the ticket should be closed."""
    vehicle_number = _extract_field(["Vehicle number", "Vehicle no"], body)
    repaired_details = _extract_field(["Repaired details", "Issue"], body)
    status_text = _extract_field("Status", body)

    if not vehicle_number:
        return None

    status_lower = (status_text or "").lower()
    body_lower = body.lower()

    should_close = (
        "close the service ticket" in status_lower
        or "close the vehicle service ticket" in status_lower
        or "back to same roster" in status_lower
        or "move this vehicle to rfd" in status_lower
    )

    # Fallback: if the Status field itself didn't match (or there's no
    # Status field at all), check the whole email body for the generic
    # "close the ticket" instruction sentence that often appears in the
    # intro line instead.
    if not should_close:
        should_close = (
            "close the vehicle service ticket" in body_lower
            or "close the service ticket" in body_lower
        )

    if not should_close:
        return None

    return {
        "vehicle_number": vehicle_number,
        "remarks": repaired_details or "Issue Resolved",
        "status_text": status_text or "",
    }


# ---------------------------------------------------------------------------
# Email: fetch, extract body, reply, mark processed (Gmail API)
# ---------------------------------------------------------------------------

def _get_plain_text(payload):
    if payload.get("mimeType") == "text/plain" and "data" in payload.get("body", {}):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode(errors="ignore")
    for part in payload.get("parts", []):
        result = _get_plain_text(part)
        if result:
            return result
    return None


def fetch_ticket_emails(service):
    """Return a list of (full_message, parsed_ticket) for unread, matching,
    close-instruction emails."""
    query = build_subject_query()
    results = service.users().messages().list(userId="me", q=query, maxResults=20).execute()
    stubs = results.get("messages", [])

    matched = []
    for stub in stubs:
        msg = service.users().messages().get(userId="me", id=stub["id"], format="full").execute()
        body = _get_plain_text(msg["payload"]) or ""
        ticket = parse_ticket_email(body)
        if ticket is None:
            print(f"[skip] message {stub['id']} — missing fields or status isn't a close instruction")
            mark_processed(service, stub["id"])  # don't keep reprocessing irrelevant mail
            continue
        matched.append((msg, ticket))
    return matched


def _split_addrs(header_value):
    return [a.strip() for a in header_value.split(",") if a.strip()]


def _dedupe_addrs(addr_list):
    seen = set()
    out = []
    for a in addr_list:
        key = a.lower()
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


def reply_done(service, orig_msg):
    """Reply All with 'Done' — includes the original sender plus everyone
    on the original To/Cc, minus our own address."""
    headers = {h["name"]: h["value"] for h in orig_msg["payload"]["headers"]}
    subject = headers.get("Subject", "")
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    message_id_header = headers.get("Message-ID", "")

    my_address = service.users().getProfile(userId="me").execute()["emailAddress"]

    def is_me(addr):
        return my_address.lower() in addr.lower()

    to_list = _split_addrs(headers.get("From", "")) + _split_addrs(headers.get("To", ""))
    cc_list = _split_addrs(headers.get("Cc", ""))

    to_list = _dedupe_addrs([a for a in to_list if not is_me(a)])
    cc_list = _dedupe_addrs([a for a in cc_list if not is_me(a)])

    # Safety: if filtering out our own address left nobody to send to (e.g.
    # this workflow sends the close-instruction from the same account we're
    # using), fall back to the original sender even if that's ourselves —
    # better than silently "sending" to an empty recipient list.
    if not to_list:
        to_list = _split_addrs(headers.get("From", ""))

    print(f"  Sending Done reply — To: {to_list}  Cc: {cc_list}")

    reply = MIMEText("Done")
    reply["To"] = ", ".join(to_list)
    if cc_list:
        reply["Cc"] = ", ".join(cc_list)
    reply["Subject"] = subject
    if message_id_header:
        reply["In-Reply-To"] = message_id_header
        reply["References"] = message_id_header

    raw = base64.urlsafe_b64encode(reply.as_bytes()).decode()
    body = {"raw": raw, "threadId": orig_msg["threadId"]}
    service.users().messages().send(userId="me", body=body).execute()


def mark_processed(service, msg_id):
    service.users().messages().modify(
        userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()


# ---------------------------------------------------------------------------
# Browser automation — confirmed-working flow from real recordings
# ---------------------------------------------------------------------------

def close_service_ticket(vehicle_number: str, remarks: str) -> bool:
    with sync_playwright() as p:
        # On Windows this machine's Application Control policy blocked
        # Playwright's own bundled Chromium, so we used the trusted Edge
        # browser instead (channel="msedge"), visibly (headless=False) for
        # easier debugging. On a Linux server (no such policy, nobody
        # watching), leave BROWSER_CHANNEL unset and HEADLESS=true in .env
        # to use Playwright's own headless Chromium instead.
        launch_kwargs = {"headless": HEADLESS}
        if BROWSER_CHANNEL:
            launch_kwargs["channel"] = BROWSER_CHANNEL
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page()

        login(page)
        select_vehicle(page, vehicle_number)
        open_change_status(page)
        handle_status_step(page, vehicle_number, remarks)

        page.wait_for_timeout(2000)
        page.screenshot(path="debug_after_close.png")
        print("  Saved debug_after_close.png — check it matches the closed ticket.")

        browser.close()
        return True


def login(page):
    page.goto(PLATFORM_URL)
    page.get_by_role("textbox", name="Username:").fill(PLATFORM_USERNAME)
    page.get_by_role("textbox", name="Password:").fill(PLATFORM_PASSWORD)
    page.get_by_role("button", name="Log in").click()
    page.wait_for_load_state("networkidle")


def _plate_regex(vehicle_number):
    """The platform displays plates with spaces (e.g. 'TG 08 T 6632'). Build
    a pattern matching the plate's characters in order regardless of
    spacing."""
    compact = re.sub(r"\s+", "", vehicle_number)
    return re.compile(r"\s*".join(re.escape(ch) for ch in compact), re.IGNORECASE)


from urllib.parse import quote


def select_vehicle(page, vehicle_number):
    """Go straight to the filtered listing. status__in=0,1,2 means Open,
    Servicing In Progress, On Hold — sidesteps vehicles with multiple
    historical service records under the same plate."""
    search_url = (
        f"{PLATFORM_URL.rstrip('/')}/services/service/"
        f"?q={quote(vehicle_number)}&status__in=0,1,2"
    )
    print(f"  Navigating to: {search_url}")
    page.goto(search_url)
    page.wait_for_load_state("networkidle")

    rows = page.get_by_role("row").filter(has_text=_plate_regex(vehicle_number))
    count = rows.count()
    print(f"  Matching rows found: {count}")

    if count == 0:
        page.screenshot(path="debug_no_rows.png")
        raise RuntimeError(f"No open/in-progress/on-hold record found for {vehicle_number}.")
    if count > 1:
        raise RuntimeError(
            f"{count} open records found for {vehicle_number} — needs a human to pick the right one."
        )
    rows.first.get_by_role("checkbox").check(timeout=10000)


def open_change_status(page):
    page.get_by_label("Action: --------- Change").select_option("change_status")
    page.get_by_role("button", name="Go").click()
    page.wait_for_load_state("networkidle")


def get_status_options(page):
    return page.get_by_label("Status*").locator("option").all_text_contents()


def select_status(page, status_text):
    page.get_by_label("Status*").select_option(label=status_text)


def fill_remarks(page, remarks_text):
    """Remarks* is required — fill it with the real repair details from the
    email every time."""
    page.get_by_role("textbox", name="Remarks*").fill(remarks_text)


def handle_status_step(page, vehicle_number, remarks):
    options = get_status_options(page)
    print(f"  Status options: {options}")

    if "Servicing Completed" in options:
        print("  Selecting Servicing Completed directly...")
        select_status(page, "Servicing Completed")
        fill_remarks(page, remarks)
        page.get_by_role("button", name="Submit").click()
        print("  Submitted. Setting location...")
        set_location_and_confirm(page, vehicle_number)
        print("  Location confirmed.")
        return

    if "Servicing In Progress" in options:
        print("  Selecting Servicing In Progress first...")
        select_status(page, "Servicing In Progress")
        fill_remarks(page, remarks)
        page.get_by_role("button", name="Submit").click()
        page.wait_for_load_state("networkidle")
        print("  Submitted Servicing In Progress. Re-opening ticket...")

        # Re-search and reopen Change Status — Completed should be available now.
        select_vehicle(page, vehicle_number)
        open_change_status(page)
        options_after = get_status_options(page)
        print(f"  Status options after re-open: {options_after}")
        if "Servicing Completed" not in options_after:
            raise RuntimeError(
                f"Still no Servicing Completed option for {vehicle_number} "
                f"after moving to Servicing In Progress: {options_after}"
            )
        print("  Selecting Servicing Completed...")
        select_status(page, "Servicing Completed")
        fill_remarks(page, remarks)
        page.get_by_role("button", name="Submit").click()
        print("  Submitted. Setting location...")
        set_location_and_confirm(page, vehicle_number)
        print("  Location confirmed.")
        return

    raise RuntimeError(
        f"No Servicing Completed or Servicing In Progress option for {vehicle_number}: {options}"
    )


def set_location_and_confirm(page, vehicle_number):
    location = vendor_location_for(vehicle_number)

    page.locator("#select2-id_vehicle_location-container").get_by_text("Select Vehicle Location").click()
    modal = page.locator("#vehicleLocationModal")
    modal.get_by_role("searchbox").fill(location)
    modal.get_by_text(location, exact=False).first.click()
    page.get_by_role("button", name="Confirm & Submit").click()
    page.wait_for_load_state("networkidle")


# ---------------------------------------------------------------------------
# Main loop (local / VM use — continuous polling)
# ---------------------------------------------------------------------------

def process_once(service):
    """Run one pass: check for matching unread emails, process each. Used
    both by the continuous local loop below and by run_once.py for
    GitHub Actions, where GitHub's own scheduler handles the waiting."""
    matches = fetch_ticket_emails(service)
    print(f"Checked inbox — {len(matches)} matching unread ticket email(s) found.")
    for msg, ticket in matches:
        vehicle_number = ticket["vehicle_number"]
        remarks = ticket["remarks"]
        print(f"Processing {vehicle_number} — remarks: {remarks!r}")
        try:
            close_service_ticket(vehicle_number, remarks)
            if SEND_DONE_REPLY:
                reply_done(service, msg)
            mark_processed(service, msg["id"])
            print(f"  done: {vehicle_number}")
        except Exception as e:
            print(f"  FAILED on {vehicle_number}: {e}")
            # Deliberately not marking as read / replying, so a failed
            # ticket isn't silently dropped.


def main():
    service = get_gmail_service()
    print("Watching inbox for service-ticket emails... (Ctrl+C to stop)")
    while True:
        try:
            process_once(service)
        except Exception as e:
            print(f"Poll error: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
