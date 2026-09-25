"""
Print a per-day summary of ticket automation activity, from the log the
GitHub Actions workflow keeps committing to logs/ticket_log.csv.

Run: python daily_summary.py
"""
import csv
from collections import defaultdict

LOG_FILE = "logs/ticket_log.csv"

OUTCOMES = ["closed", "already_completed", "other_status", "vehicle_mismatch", "no_vehicle_number", "failed"]

counts = defaultdict(lambda: defaultdict(int))

with open(LOG_FILE) as f:
    reader = csv.DictReader(f)
    for row in reader:
        day = row["timestamp_utc"][:10]  # YYYY-MM-DD
        counts[day][row["outcome"]] += 1

header = f"{'Date':<12}" + "".join(f"{o:<18}" for o in OUTCOMES)
print(header)
print("-" * len(header))
for day in sorted(counts):
    c = counts[day]
    print(f"{day:<12}" + "".join(f"{c[o]:<18}" for o in OUTCOMES))

total_closed = sum(c["closed"] for c in counts.values())
print(f"\nTotal tickets closed (all time in log): {total_closed}")
