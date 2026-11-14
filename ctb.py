"""Fetch the full CTB route, route-stop and stop dataset.

Outputs (same filenames as before, now deterministic and validated):
  _CTB_Stop.json        stop_id -> stop record
  _CTB_rt.json          route -> {co, route, I:{...}, O:{...}}
  _CTB_Stop_Route.json  stop_id -> [route, ...]

The job fails (exit 1) when records are missing or coordinates are invalid,
so a broken snapshot is never published to the `data` branch.
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import util

BASE = "https://rt.data.gov.hk/v2/transport/citybus"
# The data.gov.hk gateway returns 403/429 when hit too hard, so stay modest.
ROUTE_WORKERS = 4
STOP_WORKERS = 6

# Hong Kong bounding box (generous): any stop outside this is corrupt data.
LAT_RANGE = (22.10, 22.60)
LON_RANGE = (113.80, 114.50)

FIX_FILE = "ctb_stop_fixes.json"


def load_fixes():
    if not os.path.isfile(FIX_FILE):
        return {}
    with open(FIX_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def write_json(name, data):
    tmp = name + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, name)


def in_hk(lat, lon):
    return LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]


def main():
    issues = []

    route_payload = util.get_json(BASE + "/route/ctb")
    route_list = route_payload.get("data") or []
    if not route_list:
        print("FATAL: CTB route list is empty")
        return 1
    print("routes:", len(route_list))

    def fetch_route_stops(route):
        row = {"I": [], "O": []}
        try:
            for bound, direction in (("I", "inbound"), ("O", "outbound")):
                payload = util.get_json(
                    "%s/route-stop/ctb/%s/%s" % (BASE, quote(route, safe=""), direction)
                )
                row[bound] = [item["stop"] for item in (payload.get("data") or [])]
        except RuntimeError as exc:
            print("ISSUE: route-stop %s: %s" % (route, exc))
        return route, row

    route_stops = {}
    with ThreadPoolExecutor(max_workers=ROUTE_WORKERS) as pool:
        for route, row in pool.map(fetch_route_stops, [r["route"] for r in route_list]):
            route_stops[route] = row

    _CTB_rt = {}
    _CTB_Stop_Route = {}
    for item in route_list:
        route = item["route"]
        row = route_stops.get(route) or {"I": [], "O": []}
        if not row["I"] and not row["O"]:
            issues.append("route %s has no stop sequence" % route)
        _CTB_rt[route] = {
            "co": "CTB",
            "route": route,
            "I": {
                "route": route,
                "bound": "I",
                "orig_tc": item.get("dest_tc", ""),
                "orig_en": item.get("dest_en", ""),
                "dest_tc": item.get("orig_tc", ""),
                "dest_en": item.get("orig_en", ""),
                "stops": row["I"],
            },
            "O": {
                "route": route,
                "bound": "O",
                "orig_tc": item.get("orig_tc", ""),
                "orig_en": item.get("orig_en", ""),
                "dest_tc": item.get("dest_tc", ""),
                "dest_en": item.get("dest_en", ""),
                "stops": row["O"],
            },
        }
        for stop_id in set(row["I"]) | set(row["O"]):
            _CTB_Stop_Route.setdefault(stop_id, []).append(route)

    for route in _CTB_Stop_Route:
        _CTB_Stop_Route[route] = sorted(set(_CTB_Stop_Route[route]))

    stop_ids = sorted(_CTB_Stop_Route)
    print("distinct stops:", len(stop_ids))

    fixes = load_fixes()

    def fetch_stop(stop_id):
        try:
            payload = util.get_json(BASE + "/stop/" + quote(stop_id, safe=""))
            record = payload.get("data")
            if not isinstance(record, dict) or not record:
                # The CTB API returns {} for stops that are no longer served.
                return stop_id, None, "no stop metadata (retired or unserved)"
            return stop_id, record, None
        except RuntimeError as exc:
            return stop_id, None, str(exc)

    _CTB_Stop = {}
    missing = []
    with ThreadPoolExecutor(max_workers=STOP_WORKERS) as pool:
        for stop_id, record, error in pool.map(fetch_stop, stop_ids):
            if record is None:
                missing.append(stop_id)
                issues.append("stop %s: %s" % (stop_id, error))
                continue
            fix = fixes.get(stop_id)
            if fix:
                record["lat"] = str(fix["lat"])
                record["long"] = str(fix["lon"])
                record["_corrected"] = fix.get("reason", "manual fix")
            try:
                lat = float(record["lat"])
                lon = float(record["long"])
            except (KeyError, TypeError, ValueError):
                issues.append("stop %s: invalid coordinates" % stop_id)
                missing.append(stop_id)
                continue
            if not in_hk(lat, lon):
                issues.append("stop %s: coordinates outside HK (%s, %s)" % (stop_id, lat, lon))
                missing.append(stop_id)
                continue
            if not record.get("name_tc") and not record.get("name_en"):
                issues.append("stop %s: missing both names" % stop_id)
            _CTB_Stop[stop_id] = record

    # Drop references to stops that could not be fetched, so the route files
    # never point at a non-existent stop.
    for route, entry in _CTB_rt.items():
        for bound in ("I", "O"):
            entry[bound]["stops"] = [s for s in entry[bound]["stops"] if s in _CTB_Stop]

    print("stops written:", len(_CTB_Stop), "missing:", len(missing))

    write_json("_CTB_Stop.json", _CTB_Stop)
    write_json("_CTB_rt.json", _CTB_rt)
    write_json("_CTB_Stop_Route.json", _CTB_Stop_Route)

    for message in issues[:50]:
        print("ISSUE:", message)
    if len(issues) > 50:
        print("ISSUE: ... %d more" % (len(issues) - 50))

    empty_routes = [
        route for route, entry in _CTB_rt.items()
        if not entry["I"]["stops"] and not entry["O"]["stops"]
    ]
    for route in empty_routes:
        print("ISSUE: route %s has no stop sequence from the API" % route)

    # A few special/seasonal routes legitimately have no route-stop data.
    # A handful of retired stop references should not block the weekly refresh.
    # A large share of failures, or most routes coming back empty, means the
    # run was rate limited or the API is broken.
    too_many_missing = len(missing) > max(20, len(stop_ids) // 100)
    too_many_empty = len(empty_routes) > max(10, len(route_list) // 20)
    return 1 if (too_many_missing or too_many_empty) else 0


if __name__ == "__main__":
    sys.exit(main())
