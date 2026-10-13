"""
Ticket automation report - pick a date, see what happened that day.

For the selected date (India time) it shows:
  - how many tickets were closed (service + recovery)
  - how many failed, the failure reasons with counts, and each failed ticket
  - tickets the automation replied to with a question / status notice

It reads the log straight from GitHub (the workflow commits
logs/ticket_log.csv there), so there is nothing to download. If GitHub
can't be reached it falls back to a local logs/ticket_log.csv.

Run:
  python daily_summary.py                      asks which date (type q to quit)
  python daily_summary.py 30-09-2026           that date
  python daily_summary.py 2026-09-30
  python daily_summary.py yesterday
  python daily_summary.py 30-09-2026 --local   use the local log file only
"""
import csv
import io
import os
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

LOG_URL = (
    "https://raw.githubusercontent.com/Lipsita-Panda/"
    "Fyn-Ticket-Closing-Automation/main/logs/ticket_log.csv"
)
LOCAL_LOG = "logs/ticket_log.csv"

# Log timestamps are stored in UTC; reports are by India date.
IST = timezone(timedelta(hours=5, minutes=30))

# outcome in the log -> (service or recovery, label shown in the report)
OUTCOME_INFO = {
    "closed": ("service", "Closed"),
    "recovery_closed": ("recovery", "Closed"),
    "failed": ("service", "Failed"),
    "recovery_failed": ("recovery", "Failed"),
    "already_completed": ("service", "Already completed (replied)"),
    "other_status": ("service", "Found under another status (replied)"),
    "vehicle_mismatch": ("service", "Vehicle number mismatch (replied)"),
    "no_vehicle_number": ("service", "No vehicle number (replied)"),
    "recovery_vehicle_mismatch": ("recovery", "Vehicle number mismatch (replied)"),
    "recovery_no_vehicle_number": ("recovery", "No vehicle number (replied)"),
}

_PLATE = re.compile(r"\b[A-Z]{2}\s?\d{1,2}\s?[A-Z]{1,3}\s?\d{4}\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Loading the log
# ---------------------------------------------------------------------------

def load_log(force_local=False):
    """Returns a list of csv rows, or None if no log could be found."""
    if not force_local:
        try:
            req = urllib.request.Request(LOG_URL, headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            print("(log read from GitHub)")
            return list(csv.DictReader(io.StringIO(text, newline="")))
        except Exception as e:
            print(f"(couldn't read the log from GitHub: {e})")

    if os.path.isfile(LOCAL_LOG):
        print(f"(log read from local file {LOCAL_LOG})")
        with open(LOCAL_LOG, newline="", encoding="utf-8", errors="replace") as f:
            return list(csv.DictReader(f))
    return None


def to_ist(ts):
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def plate_key(vehicle):
    """'TS08UL7428' and 'TS 08 UL 7428' are the same vehicle."""
    return re.sub(r"\s+", "", vehicle or "").upper()


def build_events(rows):
    events = []
    for r in rows:
        try:
            when = to_ist(r["timestamp_utc"])
        except (KeyError, TypeError, ValueError):
            continue
        outcome = (r.get("outcome") or "").strip()
        kind, label = OUTCOME_INFO.get(outcome, ("other", outcome or "unknown"))
        events.append({
            "when": when,
            "vehicle": (r.get("vehicle_number") or "").strip(),
            "kind": kind,
            "label": label,
            "detail": (r.get("detail") or "").strip(),
        })
    events.sort(key=lambda e: e["when"])
    return events


# ---------------------------------------------------------------------------
# Reasons
# ---------------------------------------------------------------------------

def raw_reason(detail):
    """First line of the recorded error, trimmed."""
    detail = (detail or "").strip()
    if not detail:
        return "(no reason recorded)"
    return detail.splitlines()[0].strip()[:140]


def group_reason(detail):
    """Same reason with the vehicle number / counts masked, so similar
    failures for different vehicles count together."""
    text = _PLATE.sub("<vehicle>", raw_reason(detail))
    return re.sub(r"\b\d+\b", "N", text)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def dotted(label, count, note=""):
    dots = "." * max(2, 40 - len(label))
    line = f"  {label} {dots} {count:>4}"
    return f"{line}   {note}" if note else line


def split_by_kind(items):
    s = sum(1 for e in items if e["kind"] == "service")
    r = sum(1 for e in items if e["kind"] == "recovery")
    o = len(items) - s - r
    text = f"service {s}, recovery {r}"
    return f"{text}, other {o}" if o else text


def print_report(events, day):
    todays = [e for e in events if e["when"].date() == day]

    print()
    print("=" * 72)
    print(f" TICKET AUTOMATION REPORT - {day.strftime('%d %b %Y')} (India time)")
    print("=" * 72)

    if not todays:
        print("\nNo activity was logged on this date.")
        if events:
            first = events[0]["when"].date().strftime("%d %b %Y")
            last = events[-1]["when"].date().strftime("%d %b %Y")
            print(f"The log covers {first} to {last}.")
        return

    closed = [e for e in todays if e["label"] == "Closed"]
    failed = [e for e in todays if e["label"] == "Failed"]
    others = [e for e in todays if e["label"] not in ("Closed", "Failed")]

    # --- group failed attempts into distinct tickets -----------------------
    groups = {}
    for e in failed:
        key = (e["kind"], plate_key(e["vehicle"]))
        g = groups.setdefault(key, {
            "kind": e["kind"], "vehicle": e["vehicle"], "attempts": 0,
            "first": e["when"], "last": e["when"], "detail": "",
        })
        g["attempts"] += 1
        g["last"] = e["when"]
        g["detail"] = e["detail"]

    # A failure is "resolved" if the same vehicle was handled afterwards
    # (closed, or answered with an already-completed / status notice...).
    for key, g in groups.items():
        later = [
            e for e in events
            if e["label"] != "Failed"
            and (e["kind"], plate_key(e["vehicle"])) == key
            and e["when"] >= g["first"]
        ]
        g["resolved_by"] = later[0]["label"] if later else None

    still_failing = [g for g in groups.values() if g["resolved_by"] is None]
    resolved = [g for g in groups.values() if g["resolved_by"] is not None]

    # --- summary -------------------------------------------------------------
    print("\nSUMMARY")
    print(dotted("Closed", len(closed), f"({split_by_kind(closed)})"))
    failed_kinds = Counter(g["kind"] for g in groups.values())
    print(dotted(
        "Failed tickets", len(groups),
        f"(service {failed_kinds['service']}, recovery {failed_kinds['recovery']})",
    ))
    print(dotted("    still failing", len(still_failing)))
    print(dotted("    later resolved", len(resolved)))
    print(dotted("    total failed attempts", len(failed), "(a failing ticket is retried every 5 min)"))
    by_label = defaultdict(list)
    for e in others:
        by_label[e["label"]].append(e)
    for label in sorted(by_label):
        items = by_label[label]
        print(dotted(label, len(items), f"({split_by_kind(items)})"))
    print(dotted("Total log entries", len(todays)))

    # --- closed --------------------------------------------------------------
    print("\n" + "-" * 72)
    print(f"CLOSED ({len(closed)})")
    print("-" * 72)
    if closed:
        for e in closed:
            print(f"  {e['when']:%H:%M}  {e['vehicle']:<16} [{e['kind']}]")
    else:
        print("  none")

    # --- failed --------------------------------------------------------------
    print("\n" + "-" * 72)
    print(f"FAILED ({len(groups)} ticket(s), {len(failed)} attempt(s))")
    print("-" * 72)
    if groups:
        reason_counts = Counter(group_reason(g["detail"]) for g in groups.values())
        print("  Reasons (number of tickets):")
        for reason, count in reason_counts.most_common():
            print(f"    {count}x  {reason}")
        print("\n  List:")
        for g in sorted(groups.values(), key=lambda g: g["first"]):
            status = "STILL FAILING" if g["resolved_by"] is None else f"resolved later: {g['resolved_by']}"
            window = f"{g['first']:%H:%M}" if g["attempts"] == 1 else f"{g['first']:%H:%M}-{g['last']:%H:%M}"
            print(f"    {g['vehicle']:<16} [{g['kind']}]  {g['attempts']} attempt(s), {window}  -> {status}")
            print(f"        {raw_reason(g['detail'])}")
    else:
        print("  none")

    # --- replied / other -----------------------------------------------------
    if others:
        print("\n" + "-" * 72)
        print(f"OTHER OUTCOMES ({len(others)})")
        print("-" * 72)
        for e in others:
            extra = f"  {raw_reason(e['detail'])}" if e["detail"] else ""
            print(f"  {e['when']:%H:%M}  {e['vehicle']:<16} [{e['kind']}]  {e['label']}{extra}")
    print()


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------

def parse_date_text(text):
    t = (text or "").strip().lower()
    today = datetime.now(IST).date()
    if t in ("", "today"):
        return today
    if t == "yesterday":
        return today - timedelta(days=1)
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force_local = "--local" in sys.argv

    rows = load_log(force_local)
    if rows is None:
        print(
            "\nNo log found. The log file (logs/ticket_log.csv) is created the "
            "first time the automation processes a ticket."
        )
        return
    events = build_events(rows)

    if args:
        day = parse_date_text(args[0])
        if day is None:
            print(f"Couldn't understand the date '{args[0]}'. Try 30-09-2026, 2026-09-30, today or yesterday.")
            return
        print_report(events, day)
        return

    while True:
        text = input("Date (30-09-2026 / 2026-09-30 / today / yesterday, blank = today, q = quit): ")
        if text.strip().lower() in ("q", "quit", "exit"):
            break
        day = parse_date_text(text)
        if day is None:
            print("  Couldn't understand that date - try again.")
            continue
        print_report(events, day)


if __name__ == "__main__":
    main()
