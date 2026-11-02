#!/usr/bin/env python3
"""
Archive the Predictive Services 7-Day Significant Fire Potential forecast.

Pulls all seven day layers (Day 1 through Day 7) from the NPSG
outlooks_forecast map service, keeps the PSAs for the configured GACC,
and appends one row per PSA per lead day to data/forecasts.csv.
A raw JSON snapshot of each pull is also saved to data/raw/ so the
archive can always be rebuilt or audited.

Standard library only, so the GitHub Action needs no installs.

Usage:
    python scripts/archive_forecast.py            # normal daily run
    python scripts/archive_forecast.py --force    # overwrite today's issue if it already exists
    python scripts/archive_forecast.py --mock DIR # read layer0.json..layer6.json from DIR (testing)
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- settings
SERVICE = ("https://fsapps.nwcg.gov/psp/arcgis/rest/services/"
           "npsg/outlooks_forecast/MapServer")
PSA_PREFIX = os.environ.get("PSA_PREFIX", "SW")      # Southwest PSAs: SW01, SW02, ...
LOCAL_TZ = ZoneInfo("America/Denver")
N_DAYS = 7

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
CSV_PATH = os.path.join(DATA_DIR, "forecasts.csv")
LOG_PATH = os.path.join(DATA_DIR, "pull_log.csv")

COLUMNS = [
    "issue_date",          # local (Mountain) date the forecast was pulled / issued
    "lead_day",            # 1..7
    "valid_date",          # issue_date + lead_day - 1
    "psa",                 # nat_code, e.g. SW05
    "gacc",
    "dryness_code",        # 1 little/no risk, 2 low risk, 3 moderate risk, 0 no data
    "dryness",             # text label
    "high_risk",           # raw service type: CRITICAL, IGNITION, or blank
    "trigger",             # W (wind), B (burn environment), L (lightning), or blank
    "category",            # single combined label used for scoring
    "isvalid",
    "symbol",
    "service_timestamp",   # timestampdate from the service, ISO UTC
    "pulled_at_utc",
]

DRYNESS = {0: "No Data", 1: "Little or No Risk", 2: "Low Risk", 3: "Moderate Risk"}
TRIGGER_LABEL = {"W": "High Risk - Wind", "B": "High Risk - Burn Environment",
                 "L": "High Risk - Lightning", "WB": "High Risk - Wind or Burn Environment"}


def trigger_of(high_risk, symbol):
    """The map labels high-risk PSAs with W, B or L; fall back to the service type."""
    s = str(symbol or "").strip().upper()
    if s in ("W", "B", "L"):
        return s
    if high_risk == "IGNITION":
        return "L"
    if high_risk == "CRITICAL":
        return "WB"          # critical burn environment, letter not provided
    return ""


# ---------------------------------------------------------------- helpers
def fetch_layer(layer_id, retries=4):
    """Return the list of feature attribute dicts for one day layer."""
    params = {
        "where": "1=1",            # filter in Python; avoids SQL dialect surprises
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
    }
    url = f"{SERVICE}/{layer_id}/query?{urllib.parse.urlencode(params)}"
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sw-7day-verification-archiver"})
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = json.load(r)
            if "error" in payload:
                raise RuntimeError(f"service error: {payload['error']}")
            return [f.get("attributes", {}) for f in payload.get("features", [])]
        except Exception as e:  # network blips are common; back off and retry
            last_err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"layer {layer_id} failed after {retries} tries: {last_err}")


def load_mock(mock_dir, layer_id):
    with open(os.path.join(mock_dir, f"layer{layer_id}.json")) as fh:
        payload = json.load(fh)
    return [f.get("attributes", {}) for f in payload.get("features", [])]


def get_ci(attrs, *names):
    """Case-insensitive attribute lookup across alternative field names."""
    lower = {k.lower(): v for k, v in attrs.items()}
    for n in names:
        if n.lower() in lower and lower[n.lower()] not in (None, ""):
            return lower[n.lower()]
    return None


def ms_to_iso(ms):
    if ms in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OSError):
        return str(ms)


def category(dryness_code, trigger):
    if trigger:
        return TRIGGER_LABEL.get(trigger, "High Risk")
    return DRYNESS.get(dryness_code, "No Data")


def read_existing():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, newline="") as fh:
        return list(csv.DictReader(fh))


def write_rows(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    rows.sort(key=lambda r: (r["issue_date"], int(r["lead_day"]), r["psa"]))
    with open(CSV_PATH, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def log_pull(issue_date, pulled_at, n_rows, note):
    new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["issue_date", "pulled_at_utc", "rows", "note"])
        w.writerow([issue_date, pulled_at, n_rows, note])


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="replace today's issue if already archived")
    ap.add_argument("--mock", help="directory with layer0.json..layer6.json for offline testing")
    ap.add_argument("--date", help="override issue date YYYY-MM-DD (testing / backfill of a saved raw file)")
    ap.add_argument("--not-before-hour", type=int, default=None,
                    help="skip quietly if the local (Mountain) hour is earlier than this; "
                         "lets one UTC schedule hit ~6 pm in both MST and MDT")
    args = ap.parse_args()

    now_utc = datetime.now(timezone.utc)
    local_now = now_utc.astimezone(LOCAL_TZ)
    if args.not_before_hour is not None and local_now.hour < args.not_before_hour:
        print(f"Local time {local_now:%H:%M %Z} is before {args.not_before_hour}:00; skipping this run.")
        return 0
    pulled_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    issue = date.fromisoformat(args.date) if args.date else now_utc.astimezone(LOCAL_TZ).date()
    issue_s = issue.isoformat()

    existing = read_existing()
    if any(r["issue_date"] == issue_s for r in existing) and not args.force:
        print(f"{issue_s} already archived; keeping the first (morning) pull. Use --force to replace.")
        return 0

    raw_snapshot = {"issue_date": issue_s, "pulled_at_utc": pulled_at, "layers": {}}
    new_rows = []
    for layer_id in range(N_DAYS):
        lead = layer_id + 1
        feats = load_mock(args.mock, layer_id) if args.mock else fetch_layer(layer_id)
        keep = [a for a in feats
                if str(get_ci(a, "nat_code") or "").upper().startswith(PSA_PREFIX.upper())]
        raw_snapshot["layers"][f"day{lead}"] = keep

        for a in keep:
            dcode = get_ci(a, "drynesscode")
            try:
                dcode = int(dcode)
            except (TypeError, ValueError):
                dcode = 0
            hr = str(get_ci(a, "type") or "").strip().upper()
            hr = hr if hr in ("CRITICAL", "IGNITION") else ""
            sym = get_ci(a, "symbol") or ""
            trig = trigger_of(hr, sym)
            new_rows.append({
                "issue_date": issue_s,
                "lead_day": lead,
                "valid_date": (issue + timedelta(days=lead - 1)).isoformat(),
                "psa": str(get_ci(a, "nat_code")).upper(),
                "gacc": get_ci(a, "gacc", "gacc_name") or "",
                "dryness_code": dcode,
                "dryness": DRYNESS.get(dcode, "No Data"),
                "high_risk": hr,
                "trigger": trig,
                "category": category(dcode, trig),
                "isvalid": get_ci(a, "isvalid") if get_ci(a, "isvalid") is not None else "",
                "symbol": sym,
                "service_timestamp": ms_to_iso(get_ci(a, "timestampdate")),
                "pulled_at_utc": pulled_at,
            })

    if not new_rows:
        log_pull(issue_s, pulled_at, 0, f"no {PSA_PREFIX} PSAs returned")
        print(f"ERROR: service returned no PSAs starting with {PSA_PREFIX}", file=sys.stderr)
        return 1

    # dedupe in case a PSA polygon is split into several parts
    seen, deduped = set(), []
    for r in new_rows:
        k = (r["lead_day"], r["psa"])
        if k not in seen:
            seen.add(k)
            deduped.append(r)

    os.makedirs(RAW_DIR, exist_ok=True)
    with open(os.path.join(RAW_DIR, f"{issue_s}.json"), "w") as fh:
        json.dump(raw_snapshot, fh, indent=1)

    kept = [r for r in existing if r["issue_date"] != issue_s]
    write_rows(kept + deduped)
    log_pull(issue_s, pulled_at, len(deduped), "forced replace" if args.force else "ok")
    psas = sorted({r["psa"] for r in deduped})
    print(f"Archived {len(deduped)} rows for {issue_s}: {len(psas)} PSAs x {N_DAYS} days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
