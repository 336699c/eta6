#!/usr/bin/env python3
"""Map operator-native stop ids onto the canonical TD stop codes (Phase 2).

Input : static/stops.json produced by build_static.py
Output: static/operator_map.json (+ stats folded into static/manifest.json)

Matching is coordinate + name based:
  * every TD bus stop is indexed on a ~200 m grid;
  * an operator stop is matched to the best TD stop within 80 m;
  * candidates carrying the operator's own TD name tag get the operator name
    for the name score, other stops fall back to the canonical English name;
  * accept when distance <= 15 m, or name similarity >= 0.34 at <= 60 m
    (>= 0.5 beyond 60 m).

Many operator stops may map to one TD code: a TD stop identifies a location
group (both directions, several poles), while operator APIs return one id per
pole. Manual overrides live in overrides/operator_stops.json:

  {"KMB": {"<operatorStopId>": "<tdStopId>"}, ...}
"""

import argparse
import csv
import hashlib
import io
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from build_static import (LAT_RANGE, LON_RANGE, REMOTE_BASE, distance_m,
                          name_score, previous_json, previous_text)
from util import get_json, get_req

# The trailing slash matters: without it the gateway answers 403.
KMB_STOP_URL = "https://data.etabus.gov.hk/v1/transport/kmb/stop/"
CTB_STOP_URL = (
    "https://raw.githubusercontent.com/336699c/eta6/refs/heads/data/"
    "_CTB_Stop.json"
)
NLB_ROUTE_URL = "https://rt.data.gov.hk/v2/transport/nlb/route.php?action=list"
NLB_STOP_URL = (
    "https://rt.data.gov.hk/v2/transport/nlb/stop.php?action=list&routeId=%s"
)
MTR_STOP_URL = "https://opendata.mtr.com.hk/data/mtr_bus_stops.csv"

OPERATORS = ("KMB", "CTB", "NLB", "MTR")

# TD tags that represent each operator. KMB+CTB / LWB+CTB are joint poles.
OPERATOR_TAGS = {
    "KMB": {"KMB", "KMB+CTB", "LWB", "LWB+CTB"},
    "CTB": {"CTB", "KMB+CTB", "LWB+CTB"},
    "NLB": {"NLB"},
    "MTR": {"LRTFeeder"},
}

GRID_CELL = 0.002  # ~200 m
MATCH_RADIUS = 80.0
AUTO_ACCEPT = 15.0
NAME_MIN = 0.34
NLB_WORKERS = 6


def log(message):
    print(message, flush=True)


def valid_location(loc):
    return (LAT_RANGE[0] <= loc[0] <= LAT_RANGE[1]
            and LON_RANGE[0] <= loc[1] <= LON_RANGE[1])


# --------------------------------------------------------------------------
# operator stop lists
# --------------------------------------------------------------------------

def fetch_kmb():
    """KMB and LWB share one API; one call returns every pole."""
    payload = get_json(KMB_STOP_URL)
    stops = {}
    for row in payload.get("data") or []:
        stop_id = str(row.get("stop") or "").strip()
        if not stop_id:
            continue
        try:
            loc = (float(row["lat"]), float(row["long"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not valid_location(loc):
            continue
        stops[stop_id] = {
            "en": (row.get("name_en") or "").strip(),
            "zh": (row.get("name_tc") or "").strip(),
            "loc": loc,
        }
    return stops, []


def fetch_ctb():
    """CTB stops from the eta6 data branch snapshot."""
    payload = get_json(CTB_STOP_URL)
    if not isinstance(payload, dict):
        raise RuntimeError("CTB stop snapshot is not an object")
    stops = {}
    issues = []
    for stop_id, row in payload.items():
        if not isinstance(row, dict):
            continue
        try:
            loc = (float(row["lat"]), float(row["long"]))
        except (KeyError, TypeError, ValueError):
            issues.append("%s: missing coordinates" % stop_id)
            continue
        if not valid_location(loc):
            issues.append("%s: coordinates outside HK bounds" % stop_id)
            continue
        stops[str(stop_id)] = {
            "en": (row.get("name_en") or "").strip(),
            "zh": (row.get("name_tc") or "").strip(),
            "loc": loc,
        }
    return stops, issues


def fetch_nlb():
    """NLB has no bulk stop list: one call per route, deduplicated by stop id."""
    payload = get_json(NLB_ROUTE_URL)
    routes = payload.get("routes") or payload.get("data") or []
    stop_ids = []
    for route in routes:
        route_id = route.get("routeId")
        if route_id is not None:
            stop_ids.append(str(route_id))

    stops = {}
    issues = []

    def load(route_id):
        try:
            data = get_json(NLB_STOP_URL % route_id)
        except RuntimeError as exc:
            return [], ["route %s: %s" % (route_id, exc)]
        rows, problems = [], []
        for row in data.get("stops") or []:
            try:
                loc = (float(row["latitude"]), float(row["longitude"]))
            except (KeyError, TypeError, ValueError):
                problems.append("route %s: bad coordinates" % route_id)
                continue
            if not valid_location(loc):
                problems.append("route %s: coordinates outside HK bounds" % route_id)
                continue
            rows.append((str(row.get("stopId") or "").strip(), {
                "en": (row.get("stopName_e") or "").strip(),
                "zh": (row.get("stopName_c") or "").strip(),
                "loc": loc,
            }))
        return rows, problems

    with ThreadPoolExecutor(max_workers=NLB_WORKERS) as pool:
        for rows, problems in pool.map(load, stop_ids):
            issues.extend(problems)
            for stop_id, record in rows:
                if stop_id and stop_id not in stops:
                    stops[stop_id] = record
    return stops, issues


def fetch_mtr():
    """MTR Bus / LRT feeder CSV: one row per route-direction, dedupe by stop."""
    body = get_req(MTR_STOP_URL).decode("utf-8-sig", errors="replace")
    stops = {}
    issues = []
    for row in csv.DictReader(io.StringIO(body)):
        stop_id = (row.get("STATION_ID") or "").strip()
        if not stop_id:
            continue
        try:
            loc = (float(row["STATION_LATITUDE"]), float(row["STATION_LONGITUDE"]))
        except (KeyError, TypeError, ValueError):
            issues.append("%s: bad coordinates" % stop_id)
            continue
        if not valid_location(loc):
            issues.append("%s: coordinates outside HK bounds" % stop_id)
            continue
        stops[stop_id] = {
            "en": (row.get("STATION_NAME_ENG") or "").strip(),
            "zh": (row.get("STATION_NAME_CHI") or "").strip(),
            "loc": loc,
        }
    return stops, issues


FETCHERS = {
    "KMB": fetch_kmb,
    "CTB": fetch_ctb,
    "NLB": fetch_nlb,
    "MTR": fetch_mtr,
}


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

class StopIndex:
    def __init__(self, stops_payload):
        self.records = {}
        self.grid = defaultdict(list)
        for code, record in stops_payload.items():
            if record.get("mode") != "bus":
                continue
            loc = record.get("loc")
            if not loc:
                continue
            self.records[code] = record
            self.grid[(int(loc[0] / GRID_CELL), int(loc[1] / GRID_CELL))].append(code)

    def candidates(self, loc, tags):
        cell = (int(loc[0] / GRID_CELL), int(loc[1] / GRID_CELL))
        rows = []
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                for code in self.grid.get((cell[0] + dr, cell[1] + dc), ()):
                    record = self.records[code]
                    dist = distance_m(loc, record["loc"])
                    if dist <= MATCH_RADIUS:
                        rows.append((dist, code, record))
        tagged = [row for row in rows
                  if tags & set((row[2].get("ops") or {}).keys())]
        return tagged or rows


def score_candidate(stop, record, dist):
    names = [record.get("en") or ""]
    names.extend((record.get("ops") or {}).values())
    similarity = max(name_score(stop["en"], name) for name in names)
    score = 0.65 * similarity + 0.35 * (1.0 - dist / MATCH_RADIUS)
    accepted = (
        dist <= AUTO_ACCEPT
        or (similarity >= NAME_MIN and (dist <= 60 or similarity >= 0.5))
    )
    method = "coord" if dist <= AUTO_ACCEPT and similarity < NAME_MIN else "coord+name"
    return similarity, score, accepted, method


def match_operator(operator, operator_stops, index, overrides):
    tags = OPERATOR_TAGS[operator]
    td_to_code = {record["td"]: code for code, record in index.records.items()}
    mapping = {}
    unmatched = []
    method_counts = defaultdict(int)
    bands = {"15": 0, "30": 0, "60": 0, "80": 0}

    for stop_id, stop in sorted(operator_stops.items()):
        override = overrides.get(stop_id)
        if override:
            code = td_to_code.get(str(override))
            if code:
                record = index.records[code]
                mapping[stop_id] = {
                    "td": record["td"],
                    "code": code,
                    "d": round(distance_m(stop["loc"], record["loc"]), 1),
                    "c": 1.0,
                    "m": "override",
                }
                method_counts["override"] += 1
                continue

        best = None
        for dist, code, record in index.candidates(stop["loc"], tags):
            similarity, score, accepted, method = score_candidate(stop, record, dist)
            if not accepted:
                continue
            key = (score, -dist)
            if best is None or key > best[0]:
                best = (key, dist, code, record, score, method)

        if best is None:
            unmatched.append({
                "id": stop_id,
                "en": stop["en"],
                "zh": stop["zh"],
                "loc": [round(stop["loc"][0], 5), round(stop["loc"][1], 5)],
            })
            continue

        _, dist, code, record, score, method = best
        mapping[stop_id] = {
            "td": record["td"],
            "code": code,
            "d": round(dist, 1),
            "c": round(score, 3),
            "m": method,
        }
        method_counts[method] += 1
        for limit in (15, 30, 60, 80):
            if dist <= limit:
                bands[str(limit)] += 1

    return {
        "count": len(operator_stops),
        "matched": len(mapping),
        "ratio": round(len(mapping) / len(operator_stops), 4) if operator_stops else 0,
        "within": bands,
        "methods": dict(method_counts),
        "stops": mapping,
    }, unmatched


def company_ids(previous_map, code):
    rows = []
    for operator, result in (previous_map.get("operators") or {}).items():
        for operator_stop_id, entry in (result.get("stops") or {}).items():
            if entry.get("code") == code:
                rows.append("%s:%s" % (operator, operator_stop_id))
    return sorted(rows)


def merge_deprecated_report(previous_text, rows):
    """Keep earlier findings, refresh the ones seen in this round."""
    merged = {}
    for line in (previous_text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        code = line.split("|", 1)[0].strip()
        if code:
            merged[code] = line
    for code, row in rows.items():
        cells = [
            code,
            row["td"] or "-",
            (row["en"] or "-").replace("|", "/"),
            (row["zh"] or "-").replace("|", "/"),
            ",".join(row["companies"]) or "-",
        ]
        merged[code] = " | ".join(cells)
    return merged


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--static", default="static")
    parser.add_argument("--out", default="")
    parser.add_argument("--overrides", default="overrides/operator_stops.json")
    parser.add_argument("--operators", default=",".join(OPERATORS))
    parser.add_argument("--version", default="")
    parser.add_argument("--previous-dir", default="",
                        help="local directory with the previous round outputs")
    parser.add_argument("--remote-base", default=REMOTE_BASE)
    parser.add_argument("--offline", action="store_true",
                        help="do not read the previous round from GitHub")
    args = parser.parse_args(argv)

    started = datetime.now(timezone.utc)
    stops_path = os.path.join(args.static, "stops.json")
    if not os.path.isfile(stops_path):
        raise SystemExit("missing %s: run build_static.py first" % stops_path)
    with open(stops_path, encoding="utf-8") as handle:
        stops_payload = json.load(handle)

    index = StopIndex(stops_payload)
    log("indexed %d bus stops" % len(index.records))

    overrides = {}
    if os.path.isfile(args.overrides):
        with open(args.overrides, encoding="utf-8") as handle:
            overrides = json.load(handle)
        log("loaded overrides for %s" % ", ".join(sorted(overrides)))
    elif args.overrides:
        log("no overrides file at %s (optional)" % args.overrides)

    wanted = [name.strip().upper() for name in args.operators.split(",") if name.strip()]
    operators = {}
    unmatched = {}
    issues = {}
    for name in wanted:
        fetcher = FETCHERS.get(name)
        if fetcher is None:
            raise SystemExit("unknown operator: %s" % name)
        log("fetching %s stops" % name)
        operator_stops, problems = fetcher()
        log("  %s stops=%d issues=%d" % (name, len(operator_stops), len(problems)))
        result, missing = match_operator(
            name, operator_stops, index, overrides.get(name) or {})
        operators[name] = result
        unmatched[name] = missing
        if problems:
            issues[name] = problems[:50]
        log("  %s matched=%d/%d (%.1f%%)" % (
            name, result["matched"], result["count"], result["ratio"] * 100))

    version = args.version or "v" + started.strftime("%Y%m%d-%H%M")
    output = {
        "version": version,
        "generatedAt": started.isoformat(timespec="seconds"),
        "params": {
            "radiusM": MATCH_RADIUS,
            "autoAcceptM": AUTO_ACCEPT,
            "nameMin": NAME_MIN,
            "nameMinFar": 0.5,
        },
        "operators": operators,
        "unmatched": unmatched,
        "issues": issues,
    }

    out_path = args.out or os.path.join(args.static, "operator_map.json")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path + ".tmp", "w", encoding="utf-8", newline="\n") as handle:
        json.dump(output, handle, ensure_ascii=False, sort_keys=True,
                  separators=(",", ":"))
    os.replace(out_path + ".tmp", out_path)

    # Deprecated stops: published last round, no longer served by any route.
    # Their codes stay reserved in __stops.json and are reported for review.
    remote_base = "" if args.offline else args.remote_base
    previous_stops = previous_json(
        "stops.json", args.previous_dir, remote_base) or {}
    previous_map = previous_json(
        "operator_map.json", args.previous_dir, remote_base) or {}
    previous_report = previous_text(
        "deprecated_stop.txt", args.previous_dir, remote_base)

    deprecated = {}
    for code, record in previous_stops.items():
        if code in stops_payload:
            continue
        deprecated[code] = {
            "code": code,
            "td": str(record.get("td") or ""),
            "en": record.get("en") or "",
            "zh": record.get("zh") or "",
            "companies": company_ids(previous_map, code),
        }
    for code, row in sorted(deprecated.items()):
        log("  deprecated %s %s (%s)" % (
            code, row["en"], ",".join(row["companies"]) or "-"))

    lines = merge_deprecated_report(previous_report, deprecated)
    report_path = os.path.join(args.static, "deprecated_stop.txt")
    with open(report_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("# eta6 deprecated stops -- codes stay reserved in __stops.json\n")
        handle.write("# code | td stop id | name_en | name_zh | operator stop ids\n")
        for code in sorted(lines):
            handle.write(lines[code] + "\n")

    # Fold the stats into manifest.json instead of publishing another file.
    manifest_path = os.path.join(args.static, "manifest.json")
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        with open(out_path, "rb") as handle:
            body = handle.read()
        manifest.setdefault("files", {})["operator_map.json"] = {
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        with open(report_path, "rb") as handle:
            report_body = handle.read()
        manifest["files"]["deprecated_stop.txt"] = {
            "bytes": len(report_body),
            "sha256": hashlib.sha256(report_body).hexdigest(),
        }
        manifest["deprecatedStops"] = {
            "path": "deprecated_stop.txt",
            "count": len(lines),
        }
        manifest["operatorMap"] = {
            "path": "operator_map.json",
            "params": output["params"],
            "operators": {
                name: {key: value for key, value in result.items() if key != "stops"}
                for name, result in operators.items()
            },
            "unmatchedCounts": {name: len(rows) for name, rows in unmatched.items()},
            "issues": issues,
        }
        with open(manifest_path + ".tmp", "w", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
        os.replace(manifest_path + ".tmp", manifest_path)

    log("wrote %s" % os.path.abspath(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
