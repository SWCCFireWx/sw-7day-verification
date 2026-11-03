#!/usr/bin/env python3
"""
Check observed weather against the 7-Day High Risk trigger criteria, PSA by PSA.

For each completed day (Mountain time) it decides whether each Southwest PSA
observed Wind (W) and/or Burn Environment (B) conditions, using "a few stations
hit it" rather than the PSA average. Results go to data/observed.csv, one row per
PSA per day, with the stations that hit.

Lightning (L) is NOT confirmed automatically. Forecasters record confirmed
lightning days in data/lightning.csv and the website merges them in. This script
only reports how many stations were dry enough (RH below the lightning limit),
as context for that manual call.

Thresholds come from config/criteria.json, which holds named criteria sets and a
schedule of which set is in use from which date (edited from the website's
Criteria tab). Each day is checked with the set in use on that day.

Data sources (all public, no login):
  * PSA boundaries  - NPSG outlooks_forecast service (Day 1 layer geometry)
  * RAWS hourly obs - FEMS climatology GraphQL API (public window = recent ~2 weeks)
  * Instability     - Iowa Environmental Mesonet upper-air archive, 00Z soundings,
                      high-elevation Haines index (700-500 mb)

Usage:
    python scripts/verify_weather.py                # check any unchecked days in the last 10
    python scripts/verify_weather.py --days 13      # look further back (FEMS public limit ~14)
    python scripts/verify_weather.py --rescore      # recompute all saved days with current settings
    MOCK_DIR=path python scripts/verify_weather.py  # offline testing
"""

import argparse
import copy
import csv
import gzip
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OBS_DIR = os.path.join(DATA, "obs_raw")          # saved hourly data per day (for rescoring)
CFG_PATH = os.path.join(ROOT, "config", "criteria.json")
PSA_GEO = os.path.join(DATA, "psa_polygons.geojson")
RAWS_LIST = os.path.join(DATA, "stations_raws.csv")
OUT = os.path.join(DATA, "observed.csv")
LOG = os.path.join(DATA, "verify_log.csv")

TZ = ZoneInfo("America/Denver")
PSA_PREFIX = os.environ.get("PSA_PREFIX", "SW")
MOCK_DIR = os.environ.get("MOCK_DIR")

PSA_SERVICE = "https://fsapps.nwcg.gov/psp/arcgis/rest/services/npsg/outlooks_forecast/MapServer/0/query"
FEMS_GQL = "https://fems.fs2c.usda.gov/api/climatology/graphql"
IEM = "https://mesonet.agron.iastate.edu"
STATES = ["AZ", "NM", "TX", "OK", "CO", "UT"]     # searched, then clipped to the PSA polygons
# Upper-air sites in and around the Southwest (IEM ids, lat, lon)
SOUNDINGS = {
    "KABQ": (35.04, -106.62), "KEPZ": (31.87, -106.70), "KFGZ": (35.23, -111.82),
    "KTUS": (32.23, -110.96), "KAMA": (35.23, -101.71), "KMAF": (31.95, -102.19),
    "KGJT": (39.12, -108.53),
}
REFRESH_DAYS = 30

OUT_COLS = [
    "date", "psa", "criteria_set", "raws_reporting", "stations_needed",
    "observed",            # letters met: "W", "B", "WB" or blank (L is added manually on the site)
    "possible",            # partly met (B? when the sounding was missing)
    "wind_hits", "wind_stations",
    "burn_hits", "burn_stations", "haines", "sounding",
    "dry_for_lightning",   # stations with min RH below the lightning RH limit (context only)
    "max_temp_f", "min_rh", "max_wind_mph", "max_gust_mph",
    "erc", "erc_stations",   # median of station daily max ERC (NFDRS fuel model from config), stations reporting
    "sources_missing", "checked_at_utc",
]


# ------------------------------------------------------------------ settings
def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def criteria_for(d, cfg):
    """The criteria set in use on date d (last schedule entry starting on or before d)."""
    sets = {k: v for k, v in (cfg.get("criteria_sets") or {}).items() if not k.startswith("_")}
    if not sets:
        raise RuntimeError("config/criteria.json has no criteria_sets")
    sched = sorted((e for e in cfg.get("schedule") or [] if e.get("set") in sets), key=lambda e: e["from"])
    name = sched[0]["set"] if sched else next(iter(sets))      # before the first entry, use the first set
    for e in sched:
        if e["from"] <= d.isoformat():
            name = e["set"]
    merged = deep_merge(cfg, sets[name])
    # per-PSA typical max lives at the top level; keep the burn code's lookup working
    merged["burn_environment"]["typical_peak_max_f"] = cfg.get("typical_peak_max_f", {})
    return name, merged


# ------------------------------------------------------------------ http
def http(url, body=None, retries=3, timeout=90):
    """GET (or POST JSON when body is given). Returns text."""
    if MOCK_DIR:
        return mock_http(url, body)
    last = None
    for i in range(retries):
        try:
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, headers={
                "User-Agent": "sw-7day-verification (github actions)",
                "Content-Type": "application/json", "Accept": "application/json, */*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(4 * (i + 1))
    raise RuntimeError(f"{url[:120]} failed: {last}")


def mock_http(url, body):
    """Offline test hook: map each request to a file in MOCK_DIR."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "outlooks_forecast" in url:
        name = "psa.geojson"
    elif "fems" in url:
        qtext = body["query"]
        if "stationMetaData" in qtext:
            name = "fems_stations.json"
        elif "nfdrsObs" in qtext:
            start = qtext.split('startDateRange: "')[1][:10]
            name = f"fems_nfdr_{start}.json"
        else:
            start = qtext.split('startDateTimeRange: "')[1][:10]
            name = f"fems_obs_{start}.json"
    elif "raob" in url:
        name = f"raob_{q['station'][0]}_{q['ts'][0]}.json"
    else:
        raise RuntimeError("no mock for " + url)
    p = os.path.join(MOCK_DIR, name)
    if not os.path.exists(p):
        raise RuntimeError(f"mock missing: {name}")
    return open(p).read()


# ------------------------------------------------------------------ geometry
def point_in_ring(x, y, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def point_in_geom(x, y, geom):
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        if poly and point_in_ring(x, y, poly[0]) and not any(point_in_ring(x, y, h) for h in poly[1:]):
            return True
    return False


def centroid(geom):
    pts = []
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        pts.extend(poly[0])
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def dist_km(lat1, lon1, lat2, lon2):
    r = math.radians
    a = math.sin(r(lat2 - lat1) / 2) ** 2 + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def stale(path):
    return (not os.path.exists(path)) or (time.time() - os.path.getmtime(path) > REFRESH_DAYS * 86400)


# ------------------------------------------------------------------ reference data
def load_psas():
    if stale(PSA_GEO):
        params = {"where": "1=1", "outFields": "nat_code", "returnGeometry": "true",
                  "maxAllowableOffset": "0.005", "outSR": "4326", "f": "geojson"}
        gj = json.loads(http(PSA_SERVICE + "?" + urllib.parse.urlencode(params)))
        merged = {}
        for f in gj.get("features", []):
            props = {k.lower(): v for k, v in (f.get("properties") or {}).items()}
            code = str(props.get("nat_code") or "").upper()
            g = f.get("geometry")
            if code.startswith(PSA_PREFIX) and g:
                parts = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
                merged.setdefault(code, []).extend(parts)
        if not merged:
            raise RuntimeError("no PSA polygons returned")
        out = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"psa": k}, "geometry": {"type": "MultiPolygon", "coordinates": v}}
            for k, v in sorted(merged.items())]}
        with open(PSA_GEO, "w") as fh:
            json.dump(out, fh)
    with open(PSA_GEO) as fh:
        return {f["properties"]["psa"]: f["geometry"] for f in json.load(fh)["features"]}


def assign_psa(lat, lon, psas):
    for code, geom in psas.items():
        if point_in_geom(lon, lat, geom):
            return code
    return None


def gql(query):
    payload = json.loads(http(FEMS_GQL, {"query": query}))
    if payload.get("errors"):
        raise RuntimeError(f"FEMS error: {str(payload['errors'])[:300]}")
    return payload["data"]


def load_raws(psas):
    if stale(RAWS_LIST):
        rows = []
        for st in STATES:
            page = 0
            while True:
                q = f'''query {{ stationMetaData(stateId: "{st}", page: {page}, per_page: 1000) {{
                    _metadata {{ page page_count total_count }}
                    data {{ station_id station_name latitude longitude elevation state }} }} }}'''
                d = gql(q)["stationMetaData"]
                for s in d.get("data") or []:
                    try:
                        lat, lon = float(s["latitude"]), float(s["longitude"])
                    except (TypeError, ValueError, KeyError):
                        continue
                    psa = assign_psa(lat, lon, psas)
                    if psa:
                        rows.append({"station_id": s["station_id"], "name": s.get("station_name", ""),
                                     "lat": lat, "lon": lon, "elev_ft": s.get("elevation") or "",
                                     "state": s.get("state", st), "psa": psa})
                page += 1
                if page >= int((d.get("_metadata") or {}).get("page_count") or 1):
                    break
        if not rows:
            raise RuntimeError("no RAWS stations found inside the PSAs")
        rows = list({str(r["station_id"]): r for r in rows}.values())
        write_csv(RAWS_LIST, rows, ["station_id", "name", "lat", "lon", "elev_ft", "state", "psa"])
    return read_csv(RAWS_LIST)


# ------------------------------------------------------------------ daily data
def local_day_utc(d):
    start = datetime(d.year, d.month, d.day, tzinfo=TZ).astimezone(timezone.utc)
    return start, start + timedelta(days=1) - timedelta(seconds=1)


def fetch_raws_obs(d, station_ids):
    s, e = local_day_utc(d)
    rows = []
    for i in range(0, len(station_ids), 60):
        chunk = ",".join(str(x) for x in station_ids[i:i + 60])
        page = 0
        while True:
            q = f'''query {{ weatherObs(attribute: "temperature",
                startDateTimeRange: "{s:%Y-%m-%dT%H:%M:%SZ}", endDateTimeRange: "{e:%Y-%m-%dT%H:%M:%SZ}",
                stationIds: "{chunk}", timeZone: "America/Denver", page: {page}, per_page: 5000) {{
                _metadata {{ page page_count total_count }}
                data {{ station_id observation_time temperature relative_humidity wind_speed peak_gust_speed }} }} }}'''
            d_ = gql(q)["weatherObs"]
            rows.extend(d_.get("data") or [])
            page += 1
            if page >= int((d_.get("_metadata") or {}).get("page_count") or 1):
                break
    return rows


def fetch_erc(d, station_ids, fuel_model="Y"):
    """Daily max observed ERC per station for local day d (FEMS NFDRS outputs)."""
    out = {}
    for i in range(0, len(station_ids), 60):
        chunk = ",".join(str(x) for x in station_ids[i:i + 60])
        page = 0
        while True:
            q = f'''query {{ nfdrsObs(stationIds: "{chunk}", fuelModels: "{fuel_model}", nfdrType: "O",
                startDateRange: "{d.isoformat()}", endDateRange: "{d.isoformat()}", startHour: 0, endHour: 24,
                dateTimeFormat: "LocalStationTime", page: {page}, per_page: 5000) {{
                _metadata {{ page page_count total_count }}
                data {{ station_id nfdr_date energy_release_component }} }} }}'''
            d_ = gql(q)["nfdrsObs"]
            for r in d_.get("data") or []:
                v = num(r.get("energy_release_component"))
                if v is not None and 0 <= v < 200:
                    sid = str(r.get("station_id"))
                    out[sid] = max(out.get(sid, v), v)
            page += 1
            if page >= int((d_.get("_metadata") or {}).get("page_count") or 1):
                break
    return out


def haines_high(profile):
    """High-elevation Haines from a sounding profile list (pres, tmpc, dwpc)."""
    lv = {}
    for p in profile:
        try:
            pr = round(float(p.get("pres")))
        except (TypeError, ValueError):
            continue
        if pr in (700, 500) and p.get("tmpc") is not None:
            lv[pr] = p
    if 700 not in lv or 500 not in lv or lv[700].get("dwpc") is None:
        return None
    a = float(lv[700]["tmpc"]) - float(lv[500]["tmpc"])
    b = float(lv[700]["tmpc"]) - float(lv[700]["dwpc"])
    sa = 1 if a <= 17 else (2 if a <= 21 else 3)
    sb = 1 if b <= 14 else (2 if b <= 20 else 3)
    return sa + sb


def fetch_haines(d):
    """00Z the next UTC day = late afternoon local on day d."""
    ts = (datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1)).strftime("%Y%m%d%H%M")
    out = {}
    for sid in SOUNDINGS:
        try:
            j = json.loads(http(f"{IEM}/json/raob.py?" + urllib.parse.urlencode({"ts": ts, "station": sid})))
            profs = j.get("profiles") or []
            if profs:
                h = haines_high(profs[0].get("profile") or [])
                if h is not None:
                    out[sid] = h
        except Exception as e:
            print(f"  sounding {sid}: {e}")
    return out


# ------------------------------------------------------------------ evaluation
def typical_peak_max(elev_ft):
    """Rough Southwest June average max (F) from elevation; override per PSA in the config."""
    try:
        e = float(elev_ft)
    except (TypeError, ValueError):
        e = 5000.0
    return 106.0 - 3.5 * (e - 1000.0) / 1000.0


def num(v):
    try:
        x = float(v)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def evaluate(d, saved, raws, psas, cfg_all):
    set_name, cfg = criteria_for(d, cfg_all)
    W, B = cfg["wind"], cfg["burn_environment"]
    SN, AH = cfg["stations_needed"], cfg["afternoon_hours"]
    ltg_rh = cfg.get("lightning", {}).get("rh_below", 20)
    overrides = {k: v for k, v in B.get("typical_peak_max_f", {}).items() if not k.startswith("_")}

    st_meta = {str(r["station_id"]): r for r in raws}
    by_station = defaultdict(list)
    for o in saved.get("raws", []):
        by_station[str(o.get("station_id"))].append(o)

    summ = {}
    for sid, obs in by_station.items():
        meta = st_meta.get(sid)
        if not meta:
            continue
        maxt = minrh = maxws = maxg = None
        sus_hours = gust_hours = 0
        for o in obs:
            t, rh, ws, g = (num(o.get(k)) for k in ("temperature", "relative_humidity", "wind_speed", "peak_gust_speed"))
            if rh is not None and not (0 <= rh <= 100):
                rh = None
            try:
                hr = datetime.fromisoformat(str(o.get("observation_time")).replace("Z", "+00:00")).astimezone(TZ).hour
            except ValueError:
                hr = 15
            aft = AH["start"] <= hr <= AH["end"]
            if aft and t is not None and t < 135:
                maxt = t if maxt is None else max(maxt, t)
            if aft and rh is not None:
                minrh = rh if minrh is None else min(minrh, rh)
            if ws is not None and ws < 120:
                maxws = ws if maxws is None else max(maxws, ws)
            if g is not None and g < 150:
                maxg = g if maxg is None else max(maxg, g)
            if rh is not None and rh <= W["rh_max"]:
                if ws is not None and ws >= W["sustained_mph"]:
                    sus_hours += 1
                if g is not None and g >= W["gust_mph"]:
                    gust_hours += 1
        summ[sid] = dict(psa=meta["psa"], name=meta["name"], elev=meta["elev_ft"], maxt=maxt, minrh=minrh,
                         maxws=maxws, maxg=maxg, wind=(sus_hours >= 1 or gust_hours >= W["gust_hours"]))

    rows = []
    for psa in sorted(psas):
        sts = [s for s in summ.values() if s["psa"] == psa]
        n = len(sts)
        need = SN["small_psa_needed"] if 0 < n <= SN["small_psa_station_count"] else SN["default"]
        wind_st = [s["name"] for s in sts if s["wind"]]
        burn_st = []
        for s in sts:
            typ = overrides.get(psa) or typical_peak_max(s["elev"])
            if (s["maxt"] is not None and s["minrh"] is not None
                    and s["maxt"] >= float(typ) + B["degrees_above_typical"] and s["minrh"] <= B["rh_max"]):
                burn_st.append(s["name"])
        hn, snd = None, ""
        if saved.get("haines"):
            cx, cy = centroid(psas[psa])
            best = min(saved["haines"], key=lambda k: dist_km(cy, cx, *SOUNDINGS[k]))
            hn, snd = saved["haines"][best], best

        observed, possible = "", ""
        if n and len(wind_st) >= need:
            observed += "W"
        if n and len(burn_st) >= need:
            if hn is not None and hn >= B["haines_min"]:
                observed += "B"
            elif hn is None:
                possible += "B?"

        vals = lambda k: [s[k] for s in sts if s[k] is not None]
        ercs = sorted(v for sid, v in (saved.get("erc") or {}).items() if st_meta.get(sid, {}).get("psa") == psa)
        erc_med = "" if not ercs else round((ercs[len(ercs) // 2] + ercs[(len(ercs) - 1) // 2]) / 2, 1)
        rows.append({
            "date": d.isoformat(), "psa": psa, "criteria_set": set_name, "raws_reporting": n, "stations_needed": need,
            "observed": observed, "possible": possible,
            "wind_hits": len(wind_st), "wind_stations": "; ".join(wind_st),
            "burn_hits": len(burn_st), "burn_stations": "; ".join(burn_st),
            "haines": "" if hn is None else hn, "sounding": snd,
            "dry_for_lightning": sum(1 for s in sts if s["minrh"] is not None and s["minrh"] < ltg_rh),
            "max_temp_f": max(vals("maxt"), default=""), "min_rh": min(vals("minrh"), default=""),
            "max_wind_mph": max(vals("maxws"), default=""), "max_gust_mph": max(vals("maxg"), default=""),
            "erc": erc_med, "erc_stations": len(ercs),
            "sources_missing": " ".join(saved.get("missing", [])),
            "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return rows


# ------------------------------------------------------------------ io
def read_csv(p):
    if not os.path.exists(p):
        return []
    with open(p, newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(p, rows, cols):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def log(d, msg):
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["date", "run_utc", "message"])
        w.writerow([d, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), msg])


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="how many past days to check for gaps")
    ap.add_argument("--rescore", action="store_true", help="recompute every saved day with current settings")
    ap.add_argument("--today", help="pretend today is YYYY-MM-DD (testing)")
    args = ap.parse_args()

    with open(CFG_PATH) as fh:
        cfg = json.load(fh)
    os.makedirs(OBS_DIR, exist_ok=True)

    psas = load_psas()
    raws = load_raws(psas)
    print(f"{len(psas)} PSAs, {len(raws)} RAWS inside them")

    existing = read_csv(OUT)
    done = {r["date"] for r in existing}
    today = date.fromisoformat(args.today) if args.today else datetime.now(TZ).date()

    if args.rescore:
        todo = sorted(f[:-8] for f in os.listdir(OBS_DIR) if f.endswith(".json.gz"))
    else:
        todo = [(today - timedelta(days=k)).isoformat() for k in range(args.days, 0, -1)]
        todo = [d for d in todo if d not in done]

    results = defaultdict(list)
    for r in existing:
        results[r["date"]].append(r)

    for ds in todo:
        d = date.fromisoformat(ds)
        raw_path = os.path.join(OBS_DIR, f"{ds}.json.gz")
        if os.path.exists(raw_path):
            with gzip.open(raw_path, "rt") as fh:
                saved = json.load(fh)
        else:
            saved, missing = {}, []
            try:
                saved["raws"] = fetch_raws_obs(d, [r["station_id"] for r in raws])
            except Exception as e:
                print(f"{ds} RAWS: {e}")
                saved["raws"] = []
            try:
                saved["erc"] = fetch_erc(d, [r["station_id"] for r in raws], cfg.get("erc_fuel_model", "Y"))
            except Exception as e:
                print(f"{ds} ERC: {e}")
                saved["erc"], missing = {}, missing + ["erc"]
            saved["haines"] = fetch_haines(d)
            if not saved["haines"]:
                missing.append("soundings")
            saved["missing"] = missing
            if not saved["raws"]:
                log(ds, "no RAWS data; will retry next run")
                print(f"{ds}: no RAWS data returned, skipping (will retry)")
                continue
            keep = ("station_id", "observation_time", "temperature", "relative_humidity", "wind_speed", "peak_gust_speed")
            saved["raws"] = [{k: o.get(k) for k in keep} for o in saved["raws"]]
            with gzip.open(raw_path, "wt") as fh:
                json.dump(saved, fh, separators=(",", ":"))
        rows = evaluate(d, saved, raws, psas, cfg)
        results[ds] = rows
        met = [f"{r['psa']}:{r['observed']}" for r in rows if r["observed"]]
        log(ds, f"ok; criteria={rows[0]['criteria_set'] if rows else ''}; missing={saved.get('missing')}; met={' '.join(met) or 'none'}")
        print(f"{ds} ({rows[0]['criteria_set'] if rows else ''}): {len(rows)} PSAs checked; met: {', '.join(met) or 'none'}")

    all_rows = [r for k in sorted(results) for r in sorted(results[k], key=lambda x: x["psa"])]
    write_csv(OUT, all_rows, OUT_COLS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
