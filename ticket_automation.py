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
import csv
from datetime import datetime, timedelta, timezone
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
    "Service ticket to be closed",
]


def build_subject_query():
    subject_clause = " OR ".join(f'subject:"{q}"' for q in SUBJECT_QUERIES)
    return f"({subject_clause}) is:unread"


RECOVERY_SUBJECT_QUERIES = [
    "Recovery ticket to be closed",
    "NEW RECOVERY TICKET FOR",
]


def build_recovery_subject_query():
    subject_clause = " OR ".join(f'subject:"{q}"' for q in RECOVERY_SUBJECT_QUERIES)
    return f"({subject_clause}) is:unread"

# While True, sends reply emails (close confirmation, already-completed
# notice, vehicle-number-unclear notice) after processing a ticket.
SEND_REPLIES = True

# While False, recovery closures fill in the whole form (Type, Status,
# Remarks, Location) and take a screenshot, but stop short of clicking
# Submit — nothing gets saved, no reply sent, email stays unread so it's
# retried untouched. Flip to True once the flow's been validated.
RECOVERY_SUBMIT_ENABLED = False

# Independently turn each check on/off. Service tickets are ON by default.
# Recovery tickets are OFF by default (recovery isn't live yet), so the
# GitHub Actions run never touches recovery mails. To test recovery locally
# put these in .env:  CHECK_RECOVERY_TICKETS=true  and (to skip service
# tickets)  CHECK_SERVICE_TICKETS=false
CHECK_SERVICE_TICKETS = os.environ.get("CHECK_SERVICE_TICKETS", "true").lower() == "true"
CHECK_RECOVERY_TICKETS = os.environ.get("CHECK_RECOVERY_TICKETS", "false").lower() == "true"

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


_SUBJECT_VEHICLE_RE = re.compile(r"\b([A-Z]{2}\s?\d{1,2}\s?[A-Z]{1,3}\s?\d{4})\b", re.IGNORECASE)


def _extract_vehicle_from_subject(subject):
    match = _SUBJECT_VEHICLE_RE.search(subject or "")
    return match.group(1).strip() if match else None


def _last4_digits(plate):
    """Just the trailing serial digits (ignoring the district code, which
    is the part most prone to typos), for comparing two plate strings."""
    digits = re.sub(r"\D", "", plate or "")
    return digits[-4:] if len(digits) >= 4 else digits


def parse_ticket_email(body: str, subject: str = ""):
    """Pull the fields out of a ticket email body. Returns:
    - a dict with kind="close" (vehicle_number/remarks/status_text) when
      it's a valid close instruction
    - a dict with kind="no_vehicle_number" when neither body nor subject
      had a usable plate number
    - a dict with kind="vehicle_mismatch" when body and subject disagree
      on the vehicle number and even the last 4 digits don't match
    - None when it's simply not a close instruction, or the email is a
      vehicle-replacement request (nothing to do, no reply needed)
    """
    body_lower_full = body.lower()
    if "replace the vehicle" in body_lower_full or "replace this vehicle" in body_lower_full:
        # Vehicle-replacement requests aren't ticket closures — leave
        # unread, no reply, no action at all.
        return None

    vehicle_number = _extract_field(["Vehicle number", "Vehicle no"], body)
    repaired_details = _extract_field(["Repaired details", "Issue"], body)
    status_text = _extract_field("Status", body)

    subject_vehicle = _extract_vehicle_from_subject(subject)

    if not vehicle_number:
        # Body didn't have a usable vehicle number at all — fall back to
        # whatever plate-shaped text is in the subject.
        vehicle_number = subject_vehicle
    elif subject_vehicle and vehicle_number.replace(" ", "").upper() != subject_vehicle.replace(" ", "").upper():
        # Body and subject disagree.
        if _last4_digits(vehicle_number) == _last4_digits(subject_vehicle):
            # Trailing serial digits still match — something else earlier
            # in the plate differs (district number, series letters,
            # etc.), likely a typo — trust the subject's version.
            print(
                f"  Vehicle number mismatch: body says '{vehicle_number}', "
                f"subject says '{subject_vehicle}' — using subject's version."
            )
            vehicle_number = subject_vehicle
        else:
            # Even the trailing digits disagree — genuinely can't tell
            # which vehicle this is about.
            return {"kind": "vehicle_mismatch", "vehicle_number": vehicle_number}

    if not vehicle_number:
        return {"kind": "no_vehicle_number"}

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
        # Not a close instruction — leave it, don't touch the platform.
        return None

    return {
        "kind": "close",
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
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("Subject", "")
        ticket = parse_ticket_email(body, subject)

        if ticket is None:
            print(f"[skip] message {stub['id']} — missing fields or status isn't a close instruction (left unread)")
            # No reply is sent for this case, so leave it unread rather than
            # marking as read — it'll be re-checked on the next poll in case
            # a follow-up reply changes the status to something actionable.
            continue

        if ticket.get("kind") == "no_vehicle_number":
            print(f"[no vehicle number] message {stub['id']} — asking sender to confirm")
            if SEND_REPLIES:
                send_reply(service, msg, "Kindly check the vehicle number and confirm")
            mark_processed(service, stub["id"])
            log_event("UNKNOWN", "no_vehicle_number")
            continue

        if ticket.get("kind") == "vehicle_mismatch":
            vehicle_number = ticket["vehicle_number"]
            print(f"[vehicle mismatch] message {stub['id']} — {vehicle_number} doesn't match subject")
            if SEND_REPLIES:
                send_reply(
                    service, msg,
                    f'vehicle number : {vehicle_number} is not matching with the subject, Kindly check and confirm'
                )
            mark_processed(service, stub["id"])
            log_event(vehicle_number, "vehicle_mismatch")
            continue

        matched.append((msg, ticket))
    return matched


def parse_recovery_email(body: str, subject: str = ""):
    """Pull the fields out of a recovery-ticket email. Vehicle number
    priority is REVERSED from service tickets: subject is checked FIRST,
    body is only the fallback. Same last-4-digit reconciliation applies
    when both exist but disagree.

    Returns a dict with kind="close" / "no_vehicle_number" /
    "vehicle_mismatch", or None if it's not a close instruction.
    """
    # First: is this mail actually asking to close a recovery ticket? If not
    # (notifications, chatter, ...) do nothing at all - no reply, left unread.
    status_text = _extract_field("Status", body) or ""
    should_close = (
        "close the recovery ticket" in status_text.lower()
        or "close the recovery ticket" in body.lower()
    )
    if not should_close:
        return None

    subject_vehicle = _extract_vehicle_from_subject(subject)
    body_vehicle = _extract_field(["Vehicle number", "Vehicle Number"], body)

    if not subject_vehicle:
        vehicle_number = body_vehicle
    elif not body_vehicle:
        vehicle_number = subject_vehicle
    elif subject_vehicle.replace(" ", "").upper() == body_vehicle.replace(" ", "").upper():
        vehicle_number = subject_vehicle
    else:
        if _last4_digits(subject_vehicle) == _last4_digits(body_vehicle):
            print(
                f"  Recovery vehicle number mismatch: subject says '{subject_vehicle}', "
                f"body says '{body_vehicle}' — using subject's version."
            )
            vehicle_number = subject_vehicle
        else:
            return {"kind": "vehicle_mismatch", "vehicle_number": body_vehicle}

    if not vehicle_number:
        return {"kind": "no_vehicle_number"}

    remarks = _extract_field(
        ["Repaired details", "Remars / Remarks", "Remarks", "Remars"], body
    ) or "Vehicle recovered"

    return {
        "kind": "close",
        "vehicle_number": vehicle_number,
        "remarks": remarks,
    }


def fetch_recovery_emails(service):
    """Return a list of (full_message, parsed_ticket) for unread, matching,
    recovery close-instruction emails."""
    query = build_recovery_subject_query()
    results = service.users().messages().list(userId="me", q=query, maxResults=20).execute()
    stubs = results.get("messages", [])

    matched = []
    for stub in stubs:
        msg = service.users().messages().get(userId="me", id=stub["id"], format="full").execute()
        body = _get_plain_text(msg["payload"]) or ""
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("Subject", "")
        ticket = parse_recovery_email(body, subject)

        if ticket is None:
            print(f"[recovery skip] message {stub['id']} — not a close instruction (left unread)")
            continue

        if not RECOVERY_SUBMIT_ENABLED and ticket.get("kind") in ("no_vehicle_number", "vehicle_mismatch"):
            # Recovery is still in test mode: no replies to anyone, mail stays unread.
            print(f"[recovery {ticket['kind']}] message {stub['id']} — test mode, NOT replying (left unread)")
            continue

        if ticket.get("kind") == "no_vehicle_number":
            print(f"[recovery no vehicle number] message {stub['id']} — asking sender to confirm")
            if SEND_REPLIES:
                send_reply(service, msg, "Kindly check the vehicle number and confirm")
            mark_processed(service, stub["id"])
            log_event("UNKNOWN", "recovery_no_vehicle_number")
            continue

        if ticket.get("kind") == "vehicle_mismatch":
            vehicle_number = ticket["vehicle_number"]
            print(f"[recovery vehicle mismatch] message {stub['id']} — {vehicle_number}")
            if SEND_REPLIES:
                send_reply(service, msg, "Kindly mention the correct vehicle number in correct format")
            mark_processed(service, stub["id"])
            log_event(vehicle_number, "recovery_vehicle_mismatch")
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


def send_reply(service, orig_msg, body_text):
    """Reply All with the given text — includes the original sender plus
    everyone on the original To/Cc, minus our own address."""
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

    print(f"  Sending reply {body_text!r} — To: {to_list}  Cc: {cc_list}")

    reply = MIMEText(body_text)
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


class AlreadyCompletedError(Exception):
    """Raised when a vehicle has no open/in-progress/on-hold record because
    it's already Servicing Completed — a normal, reportable state, not a
    real failure."""
    def __init__(self, status_text):
        self.status_text = status_text
        super().__init__(f"Already {status_text}")


class OtherStatusError(Exception):
    """Raised when a vehicle has no open/in-progress/on-hold record and
    isn't Servicing Completed either, but IS found under some other status
    (Under Approval, Rejected, etc.) — also a normal, reportable state."""
    def __init__(self, status_text):
        self.status_text = status_text
        super().__init__(f"Found under status: {status_text}")


# Known platform status values (confirmed via the admin's status filter
# sidebar). The listing rows don't show status text directly, so the only
# way to find "what status is this plate currently under" is to try each
# filter in turn and see which one it shows up in.
STATUS_LABELS = {
    0: "Open",
    1: "Servicing In Progress",
    2: "On Hold",
    3: "Servicing Completed",
    4: "Under Approval",
    5: "Rejected",
}


def _find_status_among(page, vehicle_number, status_nums):
    for status_num in status_nums:
        url = (
            f"{PLATFORM_URL.rstrip('/')}/services/service/"
            f"?q={quote(vehicle_number)}&status__in={status_num}"
        )
        page.goto(url)
        page.wait_for_load_state("networkidle")
        rows = page.get_by_role("row").filter(has_text=_plate_regex(vehicle_number))
        if rows.count() > 0:
            return STATUS_LABELS[status_num]
    return None


def _check_already_completed(page, vehicle_number):
    """When no open record was found (during an actual close attempt),
    check the remaining statuses to report the real reason instead of a
    generic error: Servicing Completed gets its own specific exception,
    anything else found gets a generic one."""
    found_status = _find_status_among(page, vehicle_number, [3, 4, 5])
    if found_status == "Servicing Completed":
        raise AlreadyCompletedError(found_status)
    if found_status:
        raise OtherStatusError(found_status)


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
        _check_already_completed(page, vehicle_number)  # raises if it's already done
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


# ---------------------------------------------------------------------------
# Recovery ticket automation — confirmed-working flow from real recordings
# ---------------------------------------------------------------------------

# Location is always prefix-based for recovery — never pulled from the
# email's Vendor name / Warehouse location field, per instruction.
RECOVERY_LOCATION_DEFAULTS = {
    "KA": "Fyn Mobility Bangalore",
    "TN": "Fyn Mobility Chennai",
    "TG": "Fyn Mobility Hyderabad",
    "TS": "Fyn Mobility Hyderabad",
}


def recovery_location_for(vehicle_number):
    prefix = vehicle_number.strip()[:2].upper()
    try:
        return RECOVERY_LOCATION_DEFAULTS[prefix]
    except KeyError:
        raise ValueError(
            f"No recovery location mapping for prefix '{prefix}' (vehicle {vehicle_number})."
        )


def select_recovery_vehicle(page, vehicle_number):
    page.get_by_role("link", name="Vehicle Recoveries").click()
    page.get_by_role("textbox", name="Search").fill(vehicle_number)
    page.get_by_role("button", name="Search").click()
    page.wait_for_load_state("networkidle")

    # Filter to just "Open" recoveries — a plate can have multiple
    # historical recovery records, and only the open one should be touched.
    page.get_by_role("link", name="Open").click()
    page.wait_for_load_state("networkidle")

    rows = page.get_by_role("row").filter(has_text=_plate_regex(vehicle_number))
    count = rows.count()
    print(f"  Matching rows found: {count}")
    if count == 0:
        raise RuntimeError(f"No open recovery record found for {vehicle_number}.")
    if count > 1:
        raise RuntimeError(
            f"{count} open recovery records found for {vehicle_number} — needs a human to pick the right one."
        )
    rows.first.get_by_role("checkbox").check(timeout=10000)


def set_recovery_location(page, vehicle_number):
    location = recovery_location_for(vehicle_number)
    print(f"  Setting recovery location: {location}")
    # NOTE: the select2 trigger recorded with an empty accessible label —
    # unusual, but confirmed working from the real recording.
    page.get_by_label("", exact=True).click()
    searchbox = page.get_by_role("searchbox")
    searchbox.click()
    searchbox.fill(location)


def close_recovery_ticket(vehicle_number: str, remarks: str) -> bool:
    with sync_playwright() as p:
        launch_kwargs = {"headless": HEADLESS}
        if BROWSER_CHANNEL:
            launch_kwargs["channel"] = BROWSER_CHANNEL
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page()

        login(page)
        select_recovery_vehicle(page, vehicle_number)
        open_change_status(page)

        # Confirmed: Type* is Soft(0)/Hard(1) — always use Soft. Status* is
        # Open(0)/Completed(1) — always use Completed when closing.
        page.get_by_label("Type*", exact=True).select_option(label="Soft")
        page.get_by_label("Status*").select_option(label="Completed")
        page.get_by_role("textbox", name="Remarks*").fill(remarks)
        set_recovery_location(page, vehicle_number)

        page.wait_for_timeout(500)
        page.screenshot(path="debug_recovery_before_submit.png")

        if not RECOVERY_SUBMIT_ENABLED:
            print("  RECOVERY_SUBMIT_ENABLED is False — filled the form but did NOT click Submit.")
            print("  Saved debug_recovery_before_submit.png — check it looks correct.")
            browser.close()
            return False

        page.get_by_role("button", name="Submit").click()
        page.wait_for_load_state("networkidle")

        page.wait_for_timeout(2000)
        page.screenshot(path="debug_recovery_after_close.png")
        print("  Saved debug_recovery_after_close.png — check it matches the closed recovery.")

        browser.close()
        return True


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
# Main loop
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Main loop (local / VM use — continuous polling)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Persistent log (committed back to the repo by the GitHub Actions workflow,
# so activity survives across runs — see the "Commit log" step in
# ticket-automation.yml)
# ---------------------------------------------------------------------------

LOG_FILE = "logs/ticket_log.csv"


def log_event(vehicle_number, outcome, detail=""):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp_utc", "vehicle_number", "outcome", "detail"])
        writer.writerow([
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            vehicle_number,
            outcome,
            detail,
        ])


# ---------------------------------------------------------------------------
# Daily summary email — sent by the first GitHub Actions run after the set
# hour (default 10 PM India time). Recipients come from the DAILY_SUMMARY_TO secret (comma
# separated); if it isn't set, this whole feature stays off.
# ---------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))
DAILY_SUMMARY_TO = os.environ.get("DAILY_SUMMARY_TO", "")
DAILY_SUMMARY_FORCE = os.environ.get("DAILY_SUMMARY_FORCE", "").lower() == "true"


def _summary_hour():
    """Hour of day (India time, 1-23) to send the summary. Set the
    DAILY_SUMMARY_HOUR variable on GitHub to change it; default 22 = 10 PM."""
    try:
        hour = int(os.environ.get("DAILY_SUMMARY_HOUR", "").strip() or 22)
    except ValueError:
        return 22
    return hour if 1 <= hour <= 23 else 22


DAILY_SUMMARY_HOUR = _summary_hour()
SUMMARY_SENT_FILE = "logs/summary_sent.txt"

_FAILED_OUTCOMES = ("failed", "recovery_failed")
_CLOSED_OUTCOMES = ("closed", "recovery_closed")


def _summary_events():
    """Every log row as (time in India, vehicle, outcome, detail)."""
    events = []
    if not os.path.isfile(LOG_FILE):
        return events
    with open(LOG_FILE, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            try:
                when = datetime.fromisoformat(row["timestamp_utc"])
            except (KeyError, TypeError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            events.append((
                when.astimezone(IST),
                (row.get("vehicle_number") or "").strip(),
                (row.get("outcome") or "").strip(),
                (row.get("detail") or "").strip(),
            ))
    events.sort(key=lambda e: e[0])
    return events


def _plate(vehicle):
    return re.sub(r"\s+", "", vehicle).upper()


def _kind(outcome):
    return "recovery" if outcome.startswith("recovery_") else "service"


def build_daily_summary(day, until):
    """Subject and body for `day` (India date), from 12 AM up to `until`."""
    start = datetime(day.year, day.month, day.day, tzinfo=IST)
    events = _summary_events()
    todays = [e for e in events if start <= e[0] < until]

    closed = [e for e in todays if e[2] in _CLOSED_OUTCOMES]
    closed_service = sum(1 for e in closed if _kind(e[2]) == "service")
    closed_recovery = len(closed) - closed_service
    replied = [e for e in todays if e[2] not in _CLOSED_OUTCOMES + _FAILED_OUTCOMES]

    # One entry per failed ticket (a failing ticket is retried every 5
    # minutes, so the same vehicle appears many times in the log).
    failed = {}
    for when, vehicle, outcome, detail in todays:
        if outcome in _FAILED_OUTCOMES:
            info = failed.setdefault(
                (_kind(outcome), _plate(vehicle)),
                {"vehicle": vehicle, "first": when, "reason": ""},
            )
            info["reason"] = detail.splitlines()[0][:100] if detail else "(no reason recorded)"

    # A failure only counts as "still failing" if that vehicle wasn't
    # handled (closed, already completed, ...) afterwards.
    still_failing = []
    for key, info in failed.items():
        handled_later = any(
            (_kind(o), _plate(v)) == key and o not in _FAILED_OUTCOMES and w >= info["first"]
            for w, v, o, _d in events
        )
        if not handled_later:
            still_failing.append(info)

    until_text = until.strftime("%I:%M %p").lstrip("0")
    lines = [
        f"Ticket automation summary for {day.strftime('%d %b %Y')} "
        f"(12:00 AM to {until_text}, India time)",
        "",
        f"Tickets closed: {len(closed)}  (service {closed_service}, recovery {closed_recovery})",
    ]
    if failed:
        lines.append(f"Failed tickets: {len(failed)}  ({len(still_failing)} still failing)")
    if replied:
        lines.append(f"Replied with a question or status notice: {len(replied)}")
    lines.append("")
    if closed:
        lines.append("Closed tickets:")
        for when, vehicle, outcome, _detail in closed:
            lines.append(f"  {when:%I:%M %p}  {vehicle}  [{_kind(outcome)}]")
    else:
        lines.append("No tickets were closed in this period.")
    if still_failing:
        lines += ["", "Still failing (needs a look):"]
        for info in still_failing:
            lines.append(f"  {info['vehicle']} - {info['reason']}")

    subject = f"Ticket automation summary - {day.strftime('%d %b %Y')}"
    return subject, "\n".join(lines)


def send_new_email(service, to_addrs, subject, body_text):
    msg = MIMEText(body_text)
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def send_daily_summary_if_due(service, now=None):
    """Sends today's summary once, in the first run after the set hour (India time).
    A marker file (committed back to the repo with the log) stops it being
    sent twice. With DAILY_SUMMARY_FORCE=true it sends a test copy right now."""
    recipients = [a.strip() for a in DAILY_SUMMARY_TO.split(",") if a.strip()]
    if not recipients:
        return

    now = now or datetime.now(IST)
    today = now.date()

    if DAILY_SUMMARY_FORCE:
        subject, body = build_daily_summary(today, until=now)
        send_new_email(service, recipients, subject + " (test)", body)
        print("  Daily summary: sent a TEST email (forced) — nothing marked as sent.")
        return

    if now.hour < DAILY_SUMMARY_HOUR:
        return

    already_sent = []
    if os.path.isfile(SUMMARY_SENT_FILE):
        with open(SUMMARY_SENT_FILE) as f:
            already_sent = f.read().split()
    # Keyed by date AND hour, so a test send at 1 PM doesn't stop the
    # real 10 PM mail going out the same day.
    sent_key = f"{today}@{DAILY_SUMMARY_HOUR}"
    if sent_key in already_sent:
        return

    until = datetime(today.year, today.month, today.day, DAILY_SUMMARY_HOUR, tzinfo=IST)
    subject, body = build_daily_summary(today, until=until)
    send_new_email(service, recipients, subject, body)

    os.makedirs(os.path.dirname(SUMMARY_SENT_FILE), exist_ok=True)
    with open(SUMMARY_SENT_FILE, "a") as f:
        f.write(f"{sent_key}\n")
    print(f"  Daily summary for {today} (12:00 AM to {until:%I:%M %p}) sent to {', '.join(recipients)}.")


def process_once(service):
    """Run one pass: check for matching unread emails, process each. Used
    both by the continuous local loop below and by run_once.py for
    GitHub Actions, where GitHub's own scheduler handles the waiting."""
    if CHECK_SERVICE_TICKETS:
        matches = fetch_ticket_emails(service)
        print(f"Checked inbox — {len(matches)} matching unread ticket email(s) found.")
        for msg, ticket in matches:
            vehicle_number = ticket["vehicle_number"]
            remarks = ticket["remarks"]
            print(f"Processing {vehicle_number} — remarks: {remarks!r}")
            try:
                close_service_ticket(vehicle_number, remarks)
                if SEND_REPLIES:
                    send_reply(
                        service, msg,
                        f'Vehicle Number : {vehicle_number}\nStatus moved to "Servicing Completed"'
                    )
                mark_processed(service, msg["id"])
                log_event(vehicle_number, "closed")
                print(f"  done: {vehicle_number}")
            except AlreadyCompletedError as e:
                print(f"  Already {e.status_text}: {vehicle_number}")
                if SEND_REPLIES:
                    send_reply(
                        service, msg,
                        f'the current status of the vehicle number : {vehicle_number} is already "Servicing Completed" kindly check and confirm'
                    )
                mark_processed(service, msg["id"])
                log_event(vehicle_number, "already_completed", e.status_text)
            except OtherStatusError as e:
                print(f"  Found under status {e.status_text}: {vehicle_number}")
                if SEND_REPLIES:
                    send_reply(
                        service, msg,
                        f'the current status of the vehicle number : {vehicle_number} is "{e.status_text}", kindly check and confirm'
                    )
                mark_processed(service, msg["id"])
                log_event(vehicle_number, "other_status", e.status_text)
            except Exception as e:
                print(f"  FAILED on {vehicle_number}: {e}")
                log_event(vehicle_number, "failed", str(e))
                # Deliberately not marking as read / replying, so a failed
                # ticket isn't silently dropped.

    if CHECK_RECOVERY_TICKETS:
        recovery_matches = fetch_recovery_emails(service)
        print(f"Checked inbox — {len(recovery_matches)} matching unread recovery email(s) found.")
        for msg, ticket in recovery_matches:
            vehicle_number = ticket["vehicle_number"]
            remarks = ticket["remarks"]
            print(f"Processing recovery {vehicle_number} — remarks: {remarks!r}")
            try:
                submitted = close_recovery_ticket(vehicle_number, remarks)
                if not submitted:
                    # RECOVERY_SUBMIT_ENABLED is off — nothing was actually
                    # saved, so no reply and leave unread for a real retry
                    # once the toggle is switched on.
                    continue
                if SEND_REPLIES:
                    send_reply(service, msg, "Done")
                mark_processed(service, msg["id"])
                log_event(vehicle_number, "recovery_closed")
                print(f"  recovery done: {vehicle_number}")
            except Exception as e:
                print(f"  RECOVERY FAILED on {vehicle_number}: {e}")
                log_event(vehicle_number, "recovery_failed", str(e))
                # Deliberately not marking as read / replying, per instruction:
                # if not closed, leave it unread.


def main():
    service = get_gmail_service()
    checks = []
    if CHECK_SERVICE_TICKETS:
        checks.append("service tickets")
    if CHECK_RECOVERY_TICKETS:
        checks.append("recovery tickets")
    checks_text = " and ".join(checks) if checks else "nothing — both checks are disabled"
    print(f"Watching inbox for {checks_text}... (Ctrl+C to stop)")
    while True:
        try:
            process_once(service)
        except Exception as e:
            print(f"Poll error: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
