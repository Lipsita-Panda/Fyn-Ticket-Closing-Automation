"""
Recovery ticket mail automation - SEPARATE from ticket_automation.py.

Watches Gmail for unread mails whose subject STARTS WITH
    "NEW RECOVERY TICKET FOR"   or   "Recovery ticket to be closed"
(any capitals, and "Re:" / "Fwd:" in front is fine), reads the body, closes
the recovery ticket on the platform (recovery_ticket.py) and replies.

The body looks like:
    The vehicle has been recovered and moved to the HSR WH. Kindly close the
    recovery ticket and raise a service ticket for the brake issue.
    Vehicle Number: KA 04 AD 5004
    Recovery Status: Soft/Hard
    Remars/Remarks:
    Warehouse location:

Run:   python recovery_automation.py              one pass (what GitHub Actions does)
       python recovery_automation.py --loop       keep checking (local use)
       python recovery_automation.py --dry-run    only SHOW what it would do: no platform
                                                   changes, no replies, mails stay unread
"""
import base64
import csv
import io
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

# On GitHub Actions the Google files come from secrets.
for _env, _file in (("GOOGLE_CREDENTIALS_JSON", "credentials.json"), ("GOOGLE_TOKEN_JSON", "token.json")):
    if os.environ.get(_env):
        with open(_file, "w") as _f:
            _f.write(os.environ[_env])

from dotenv import load_dotenv
load_dotenv()          # a local .env (PLATFORM_URL, HEADLESS, BROWSER_CHANNEL ...); harmless on GitHub

from gmail_auth import get_gmail_service
import recovery_ticket as rt

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))

SUBJECT_STARTS = ("new recovery ticket for", "recovery ticket to be closed")
MAX_AGE_DAYS = 2             # only mails from the last 2 days (no surprise clean-up of old mail)
MAX_FAILED_ATTEMPTS = 12     # a mail that keeps failing is retried every 5 min, this many times
SEND_REPLIES = True
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))

DEFAULT_REMARKS = "Recovery Done"
DEFAULT_SERVICE_DESCRIPTION = "General service needs to be done"   # when the mail gives no reason
BAD_VEHICLE_REPLY = "Kindly mention the correct vehicle number in correct format"

GMAIL_QUERY = (
    '(subject:"NEW RECOVERY TICKET FOR" OR subject:"Recovery ticket to be closed") '
    f"is:unread -from:me newer_than:{MAX_AGE_DAYS}d"
)

LOG_FILE = "logs/recovery_log.csv"
# Newest entries on top. Date and time are separate columns (India time).
LOG_COLUMNS = [
    "date_ist", "time_ist",                       # when the automation read the mail
    "vehicle_number", "outcome", "detail",
    "mail_received_date", "mail_received_time",
    "closed_date", "closed_time",                 # when the platform finished closing it
    "replied_date", "replied_time",               # when the reply was sent
    "mail_subject", "recovery_type", "remarks", "location_used",
    "service_requested", "service_description",
    "mail_id", "timestamp_utc",
]


# ---------------------------------------------------------------------------
# Reading the mail
# ---------------------------------------------------------------------------
_ODD_SPACES = re.compile(r"[^\S\n]+")
_INVISIBLE = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")


def _clean_text(text):
    """Non-breaking / doubled spaces -> one normal space; drop invisible characters."""
    return _ODD_SPACES.sub(" ", _INVISIBLE.sub("", text or ""))


_QUOTE_START = re.compile(
    r"(?im)^[ \t]*(?:on\b[^\n]{0,200}(?:\n[^\n]{0,120})?[ \t]*wrote:[ \t]*$"
    r"|-{2,}[ \t]*(?:original message|forwarded message)[ \t]*-{2,}"
    r"|from:[ \t].{0,200}\n[ \t]*sent:)"
)


def _strip_quoted(body):
    """Only what the sender wrote: cut off the quoted earlier mails below
    ('On ... wrote:', '--- Original Message ---', lines starting with '>')."""
    m = _QUOTE_START.search(body)
    text = body[:m.start()] if m else body
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))


def _field(labels, text):
    """The text after 'Label:' on the SAME line. An empty field stays empty
    (it never picks up the next line)."""
    for label in labels:
        for m in re.finditer(rf"{re.escape(label)}[ \t]*:[ \t]*(.*)", text, re.IGNORECASE):
            value = m.group(1).strip().lstrip(":;").strip()    # "Remarks: : done" -> "done"
            if value:
                return value
    return None


_REPLY_PREFIX = re.compile(r"^\s*(?:re|fwd?|fw)\s*:\s*", re.IGNORECASE)


def subject_core(subject):
    s = _clean_text(subject)
    while True:
        t = _REPLY_PREFIX.sub("", s, count=1)
        if t == s:
            return s.strip()
        s = t


def is_recovery_subject(subject):
    """Subject STARTS WITH one of the two phrases (ignoring Re:/Fwd: and capitals)."""
    return subject_core(subject).casefold().startswith(SUBJECT_STARTS)


PLATE_RE = re.compile(r"\b([A-Z]{2})\s*-?\s*(\d{1,2})\s*-?\s*([A-Z]{1,3})\s*-?\s*(\d{4})\b", re.IGNORECASE)


def _plate(match):
    return " ".join(g.upper() for g in match.groups())


def _compact(text):
    return re.sub(r"[\s-]+", "", text or "").upper()


def _last4(text):
    digits = re.sub(r"\D", "", text or "")
    return digits[-4:]


def choose_vehicle(subject, text):
    """The subject's plate comes first; the body's is only used when the
    subject has none. Returns (plate, problem). problem is None when fine."""
    sm = PLATE_RE.search(subject or "")
    subject_plate = _plate(sm) if sm else None
    raw = _field(["Vehicle number", "Vehicle no"], text)
    if subject_plate:
        if not raw:
            return subject_plate, None
        if _compact(raw) == _compact(subject_plate) or _last4(raw) == _last4(subject_plate):
            return subject_plate, None      # same plate (spacing / small typo): the subject wins
        return None, "the vehicle number in the mail doesn't match the subject"
    bm = PLATE_RE.search(raw) if raw else None
    if bm:
        return _plate(bm), None
    return None, "no readable vehicle number" if raw else "no vehicle number"


CLOSE_RE = re.compile(r"close\s+the\s+(?:vehicle\s+)?recovery\s+ticket", re.IGNORECASE)
# A service ticket is created ONLY when the mail says "close the recovery ticket
# AND raise/create a service ticket" - the request has to follow the close
# instruction directly. A stray mention ("no need to raise a service ticket",
# "the vendor will raise one later") does not count.
SERVICE_RE = re.compile(
    r"close\s+the\s+(?:vehicle\s+)?recovery\s+ticket"
    r"[\s,&]*(?:(?:and|also|then|plus)[\s,&]*)*"
    r"(?:please\s+|kindly\s+)?(?:raise|create|open|generate)\s+(?:a\s+|an\s+|the\s+|new\s+)?service\s+ticket"
    r"(?:[ \t]+for[ \t]+(?P<desc>[^.\n]*))?",
    re.IGNORECASE,
)
_NOT_A_VALUE = {"-", "--", "na", "n/a", "nil", "none", "null"}


def parse_mail(subject, body):
    """Returns None when the mail is not a request to close a recovery ticket
    (notifications, chatter, replies that only quote an old request), else a
    dict with kind 'close' or 'bad_vehicle'."""
    text = _strip_quoted(_clean_text(body))
    if not CLOSE_RE.search(text):
        return None

    plate, problem = choose_vehicle(_clean_text(subject), text)
    if problem:
        return {"kind": "bad_vehicle", "problem": problem}

    remarks = _field(["Remars/Remarks", "Remars / Remarks", "Remarks", "Remark", "Remars"], text)
    if not remarks or remarks.strip().casefold() in _NOT_A_VALUE:
        remarks = DEFAULT_REMARKS

    location = _field(["Warehouse location", "Warehouse", "Location"], text)
    if location:
        location = location.strip().rstrip(".,;:").strip()          # "NXG MOBILITY." -> "NXG MOBILITY"
    if location and location.casefold() in _NOT_A_VALUE:
        location = None

    status = (_field(["Recovery Status"], text) or "").casefold()
    hard = "hard" in status and "soft" not in status       # "Soft/Hard" left as is -> Soft
    recovery_type = "1" if hard else "0"

    service = SERVICE_RE.search(text)
    description = DEFAULT_SERVICE_DESCRIPTION
    if service and (service.group("desc") or "").strip(" \t.,;:"):
        description = service.group("desc").strip(" \t.,;:")

    return {
        "kind": "close", "vehicle": plate, "remarks": remarks, "location_text": location,
        "recovery_type": recovery_type, "service": bool(service), "service_description": description,
    }


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------
def _plain_text(payload):
    if payload.get("mimeType") == "text/plain" and "data" in payload.get("body", {}):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode(errors="ignore")
    for part in payload.get("parts", []):
        found = _plain_text(part)
        if found:
            return found
    return None


def list_candidates(service):
    ids, token = [], None
    while True:
        resp = service.users().messages().list(
            userId="me", q=GMAIL_QUERY, maxResults=50, pageToken=token
        ).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            return ids


def _split_addrs(value):
    return [a.strip() for a in (value or "").split(",") if a.strip()]


def _dedupe(addrs):
    seen, out = set(), []
    for a in addrs:
        if a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out


def send_reply(service, orig_msg, body_text):
    """Reply All: the sender plus everyone on To/Cc, minus our own address."""
    headers = {h["name"]: h["value"] for h in orig_msg["payload"]["headers"]}
    subject = headers.get("Subject", "")
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    me = service.users().getProfile(userId="me").execute()["emailAddress"].lower()

    to_list = _dedupe([a for a in _split_addrs(headers.get("From")) + _split_addrs(headers.get("To")) if me not in a.lower()])
    cc_list = _dedupe([a for a in _split_addrs(headers.get("Cc")) if me not in a.lower()])
    if not to_list:
        to_list = _split_addrs(headers.get("From"))

    print(f"  Sending reply {body_text!r} - To: {to_list}  Cc: {cc_list}")
    reply = MIMEText(body_text)
    reply["To"] = ", ".join(to_list)
    if cc_list:
        reply["Cc"] = ", ".join(cc_list)
    reply["Subject"] = subject
    if headers.get("Message-ID"):
        reply["In-Reply-To"] = headers["Message-ID"]
        reply["References"] = headers["Message-ID"]
    raw = base64.urlsafe_b64encode(reply.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw, "threadId": orig_msg["threadId"]}).execute()


def mark_processed(service, msg_id):
    service.users().messages().modify(userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}).execute()


# ---------------------------------------------------------------------------
# The log (logs/recovery_log.csv, newest on top)
# ---------------------------------------------------------------------------
def _ist_date(dt):
    return dt.astimezone(IST).strftime("%Y-%m-%d")


def _ist_time(dt):
    return dt.astimezone(IST).strftime("%H:%M:%S")


def _stamp(prefix):
    now = datetime.now(timezone.utc)
    return {f"{prefix}_date": _ist_date(now), f"{prefix}_time": _ist_time(now)}


def _write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def log_event(vehicle, outcome, detail="", info=None):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    now = datetime.now(timezone.utc)
    row = {
        "date_ist": _ist_date(now), "time_ist": _ist_time(now),
        "vehicle_number": vehicle, "outcome": outcome, "detail": detail,
        **(info or {}),
        "timestamp_utc": now.isoformat(timespec="seconds"),
    }
    buf = io.StringIO(newline="")
    csv.writer(buf).writerow([row.get(col, "") for col in LOG_COLUMNS])
    line = buf.getvalue()
    if os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > 0:
        with open(LOG_FILE, newline="", encoding="utf-8", errors="replace") as f:
            header, _, rest = f.read().partition("\n")
        text = header + "\n" + line + rest
    else:
        text = ",".join(LOG_COLUMNS) + "\r\n" + line
    _write_atomic(LOG_FILE, text)


def failed_attempts(mail_id):
    if not os.path.isfile(LOG_FILE):
        return 0
    with open(LOG_FILE, newline="", encoding="utf-8", errors="replace") as f:
        return sum(1 for r in csv.DictReader(f) if r.get("mail_id") == mail_id and r.get("outcome") == "failed")


def held_after_unclear_submit(mail_id):
    """True if an earlier try on this mail submitted the form but the recovery
    stayed Open. If that form also asked for a service ticket, a retry could
    create a second one - so it is not retried automatically."""
    if not os.path.isfile(LOG_FILE):
        return False
    with open(LOG_FILE, newline="", encoding="utf-8", errors="replace") as f:
        return any(r.get("mail_id") == mail_id and r.get("outcome") == "failed"
                   and (r.get("detail") or "").startswith(rt.SUBMITTED_BUT_OPEN) for r in csv.DictReader(f))


def _mail_info(msg, subject, req=None):
    read_at = datetime.now(timezone.utc)
    try:
        received = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc)
        rdate, rtime = _ist_date(received), _ist_time(received)
    except (KeyError, TypeError, ValueError):
        rdate = rtime = ""
    info = {
        "date_ist": _ist_date(read_at), "time_ist": _ist_time(read_at),
        "mail_received_date": rdate, "mail_received_time": rtime,
        "mail_subject": subject, "mail_id": msg.get("id", ""),
    }
    if req and req.get("kind") == "close":
        info.update({
            "recovery_type": "Hard" if req["recovery_type"] == "1" else "Soft",
            "remarks": req["remarks"],
            "service_requested": "yes" if req["service"] else "no",
            "service_description": req["service_description"] if req["service"] else "",
        })
    return info


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------
def process_once(service, dry_run=False):
    ids = list_candidates(service)
    print(f"Checked inbox - {len(ids)} unread recovery-subject mail(s) from the last {MAX_AGE_DAYS} days.")
    for mid in ids:
        msg = service.users().messages().get(userId="me", id=mid, format="full").execute()
        if "SENT" in msg.get("labelIds", []):
            continue
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        subject = _clean_text(headers.get("Subject", ""))
        body = _clean_text(_plain_text(msg["payload"]) or "")

        if not is_recovery_subject(subject):
            print(f"[skip] {mid} - subject doesn't start with a recovery phrase: {subject[:60]!r}")
            continue
        req = parse_mail(subject, body)
        if req is None:
            print(f"[skip] {mid} - not a request to close a recovery ticket (left unread)")
            continue
        info = _mail_info(msg, subject, req)

        if req["kind"] == "bad_vehicle":
            print(f"[vehicle problem] {mid} - {req['problem']}")
            if dry_run:
                print(f"  DRY RUN: would reply {BAD_VEHICLE_REPLY!r} and mark it read.")
                continue
            times = {}
            if SEND_REPLIES:
                send_reply(service, msg, BAD_VEHICLE_REPLY)
                times.update(_stamp("replied"))
            mark_processed(service, mid)
            log_event("UNKNOWN", "vehicle_problem", req["problem"], {**info, **times})
            continue

        plate = req["vehicle"]
        if dry_run:
            print(
                f"  DRY RUN {plate}: would close as {'Hard' if req['recovery_type'] == '1' else 'Soft'}, "
                f"remarks {req['remarks']!r}, location {req['location_text'] or '(default for ' + plate[:2] + ')'}, "
                f"service ticket: {('yes - ' + repr(req['service_description'])) if req['service'] else 'no'}"
            )
            continue

        if req["service"] and "--retry-held" not in sys.argv and held_after_unclear_submit(mid):
            print(f"[on hold] {plate} - the form was submitted earlier but the recovery stayed Open, and it asked for a "
                  f"service ticket, so a retry could create a second one. Check the platform; add --retry-held to force a retry.")
            continue

        attempts = failed_attempts(mid)
        if attempts >= MAX_FAILED_ATTEMPTS:
            print(f"[giving up] {plate} - failed {attempts} times already; left unread for a person to look at.")
            continue

        print(f"Processing {plate} - {info['recovery_type']}, remarks {req['remarks']!r}, "
              f"location {req['location_text'] or '(default)'}, service ticket {req['service']}")
        try:
            result = rt.close_recovery_ticket(
                plate, remarks=req["remarks"], create_service=req["service"],
                description=req["service_description"], headless=HEADLESS,
                recovery_type=req["recovery_type"], location_text=req["location_text"],
            )
            if not result["ok"]:
                raise RuntimeError(result["message"].replace("\n", " "))
        except Exception as e:
            reason = (str(e).strip().splitlines() or [repr(e)])[0][:300]
            print(f"  FAILED on {plate}: {reason}  (attempt {attempts + 1} of {MAX_FAILED_ATTEMPTS}; left unread)")
            log_event(plate, "failed", reason, info)
            continue

        times = _stamp("closed")
        note = result.get("location_note", "")
        if SEND_REPLIES:
            try:
                send_reply(service, msg, result["message"])
                times.update(_stamp("replied"))
            except Exception as e:
                note = f"closed, but the reply could not be sent: {e}"
        try:
            mark_processed(service, mid)
        except Exception as e:
            note = (note + " | " if note else "") + f"closed, but could not mark the mail read: {e}"
        log_event(plate, result["outcome"], note, {**info, **times, "location_used": result["location_used"]})
        print(f"  done: {plate} - {result['message']}")


def main():
    dry_run = "--dry-run" in sys.argv or os.environ.get("DRY_RUN", "").lower() == "true"
    service = get_gmail_service()
    print("Recovery mail automation" + (" - DRY RUN (nothing will be changed)" if dry_run else ""))
    if "--loop" in sys.argv:
        while True:
            try:
                process_once(service, dry_run)
            except Exception as e:
                print(f"Poll error: {e}")
            time.sleep(POLL_SECONDS)
    else:
        process_once(service, dry_run)


if __name__ == "__main__":
    main()
