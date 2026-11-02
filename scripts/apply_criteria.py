#!/usr/bin/env python3
"""
Apply criteria changes submitted from the website's Criteria tab.

The website opens a GitHub issue whose body contains a ```json block with the
edited criteria sets, schedule and per-PSA typical max temps. The apply-criteria
workflow passes that body in the ISSUE_BODY environment variable. This script
checks every value, then writes config/criteria.json (keeping settings the
website doesn't edit). It writes a short summary to criteria_result.md for the
workflow to post back on the issue.

Exit code 0 = applied, 1 = rejected (summary explains why).
"""

import json
import os
import re
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT, "config", "criteria.json")
RESULT = os.path.join(ROOT, "criteria_result.md")
PSA_PREFIX = os.environ.get("PSA_PREFIX", "SW")

LIMITS = {
    "wind": {"sustained_mph": (1, 80), "gust_mph": (1, 120), "gust_hours": (1, 24), "rh_max": (1, 60)},
    "burn_environment": {"degrees_above_typical": (0, 40), "rh_max": (1, 60), "haines_min": (2, 6)},
    "stations_needed": {"default": (1, 20), "small_psa_station_count": (0, 20), "small_psa_needed": (1, 20)},
}
INTEGERS = {"gust_hours", "haines_min", "default", "small_psa_station_count", "small_psa_needed"}


def fail(msg):
    with open(RESULT, "w") as fh:
        fh.write(f"**Not applied.** {msg}\n\nNothing was changed. Fix it on the Criteria tab and save again.\n")
    print("REJECTED:", msg)
    sys.exit(1)


def clean_text(s, n):
    return re.sub(r"[\x00-\x1f<>`]", "", str(s or "")).strip()[:n]


def main():
    body = os.environ.get("ISSUE_BODY", "")
    m = re.search(r"```json\s*(\{.*?\})\s*```", body, re.S)
    if not m:
        fail("Couldn't find the criteria block in the issue. Please save from the website rather than editing the issue text.")
    try:
        sub = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        fail(f"The criteria block isn't valid (JSON error: {e}).")

    # ---- criteria sets
    sets_in = sub.get("criteria_sets")
    if not isinstance(sets_in, dict) or not sets_in:
        fail("At least one criteria set is required.")
    if len(sets_in) > 20:
        fail("Too many criteria sets (20 max).")
    sets = {}
    for raw_name, s in sets_in.items():
        name = clean_text(raw_name, 40)
        if not name or name.startswith("_"):
            fail(f"'{raw_name}' isn't a usable set name.")
        if name in sets:
            fail(f"Two sets are named '{name}'.")
        if not isinstance(s, dict):
            fail(f"Set '{name}' is malformed.")
        out = {"description": clean_text(s.get("description"), 200)}
        for section, fields in LIMITS.items():
            sec = s.get(section) or {}
            out[section] = {}
            for field, (lo, hi) in fields.items():
                v = sec.get(field)
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    fail(f"Set '{name}': {section.replace('_', ' ')} {field.replace('_', ' ')} is missing or not a number.")
                if not (lo <= v <= hi):
                    fail(f"Set '{name}': {section.replace('_', ' ')} {field.replace('_', ' ')} = {v:g} is outside {lo}-{hi}.")
                out[section][field] = int(round(v)) if field in INTEGERS else (int(v) if v == int(v) else round(v, 1))
        sets[name] = out

    # ---- schedule
    sched_in = sub.get("schedule")
    if not isinstance(sched_in, list) or not sched_in:
        fail("The schedule needs at least one entry saying which set is in use.")
    sched, seen = [], set()
    for e in sched_in:
        frm = str((e or {}).get("from", ""))
        try:
            date.fromisoformat(frm)
        except ValueError:
            fail(f"Schedule date '{frm}' isn't a valid date (YYYY-MM-DD).")
        st = clean_text(e.get("set"), 40)
        if st not in sets:
            fail(f"Schedule entry on {frm} uses set '{st}', which doesn't exist.")
        if frm in seen:
            fail(f"Two schedule entries start on {frm}. Keep one.")
        seen.add(frm)
        sched.append({"from": frm, "set": st, "note": clean_text(e.get("note"), 200)})
    sched.sort(key=lambda e: e["from"])

    # ---- per-PSA typical peak max
    tpm_in = sub.get("typical_peak_max_f") or {}
    if not isinstance(tpm_in, dict):
        fail("Typical peak max temperatures are malformed.")
    tpm = {}
    for psa, v in tpm_in.items():
        if psa.startswith("_"):
            continue
        if not re.fullmatch(PSA_PREFIX + r"\d{1,3}[A-Z]?", psa.upper()):
            fail(f"'{psa}' isn't a {PSA_PREFIX} PSA code.")
        if v in (None, ""):
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            fail(f"Typical peak max for {psa} isn't a number.")
        if not (40 <= v <= 130):
            fail(f"Typical peak max for {psa} = {v:g}°F is outside 40-130.")
        tpm[psa.upper()] = round(v, 1)

    # ---- write, keeping everything the website doesn't edit
    with open(CFG_PATH) as fh:
        cfg = json.load(fh)
    cfg["criteria_sets"] = sets
    cfg["schedule"] = sched
    note = cfg.get("typical_peak_max_f", {}).get("_note")
    cfg["typical_peak_max_f"] = ({"_note": note} if note else {}) | dict(sorted(tpm.items()))
    with open(CFG_PATH, "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")

    # ---- summary for the issue comment
    today = date.today().isoformat()
    active = next((e["set"] for e in reversed(sched) if e["from"] <= today), sched[0]["set"])
    lines = ["**Applied.** The website will show the new criteria within a few minutes.", "",
             f"- Criteria set in use today: **{active}**",
             f"- Sets: {', '.join(sets)}",
             "- Schedule: " + "; ".join(f"{e['set']} from {e['from']}" for e in sched)]
    if tpm:
        lines.append("- Typical peak max set for: " + ", ".join(f"{k} {v:g}°F" for k, v in tpm.items()))
    with open(RESULT, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
