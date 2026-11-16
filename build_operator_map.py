#!/usr/bin/env python3
"""Build native-pole stop codes and route-specific operator stop arrays.

A public bus code identifies at most one native pole per company. TD stops are
only linked to those codes; they no longer define bus-stop identity. Codes are
persistent in __stop_codes.json. New bus codes use three characters and roll
over to four when the three-character space is exhausted. GMB is handled by
build_static.py and never enters this allocator.

Route-stop sequences provide conservative evidence for TD links, co-op pole
sharing, per-operator bounds, and repeated-stop disambiguation. Ambiguous
one-to-many matches are deliberately left in separate codes.
"""

import argparse
import csv
import hashlib
import io
import json
import math
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote

from build_static import (ALPHABET, ALPHABET_SIZE, BUS_CODE_LEN, GMB_CODE_LEN,
                          LAT_RANGE, LON_RANGE, NEARBY_LIMIT, NEARBY_RADIUS,
                          REMOTE_BASE, TD_PREFIX, WALK_SPEED, VariantIdMapper,
                          build_nearby, code_from_index, distance_m,
                          index_from_code, name_score, nested_names, previous_json,
                          previous_text, route_slug, stop_company_names, stop_name,
                          write_json)
from util import get_json, get_req

KMB_STOP_URL = "https://data.etabus.gov.hk/v1/transport/kmb/stop/"
KMB_ROUTE_URL = "https://data.etabus.gov.hk/v1/transport/kmb/route/"
KMB_ROUTE_STOP_URL = "https://data.etabus.gov.hk/v1/transport/kmb/route-stop/%s/%s/%s"
CTB_BASE = "https://raw.githubusercontent.com/336699c/eta6/refs/heads/data/"
CTB_STOP_URL = CTB_BASE + "_CTB_Stop.json"
CTB_ROUTE_URL = CTB_BASE + "_CTB_rt.json"
NLB_ROUTE_URL = "https://rt.data.gov.hk/v2/transport/nlb/route.php?action=list"
NLB_STOP_URL = "https://rt.data.gov.hk/v2/transport/nlb/stop.php?action=list&routeId=%s"
MTR_STOP_URL = "https://opendata.mtr.com.hk/data/mtr_bus_stops.csv"

OPERATORS = ("KMB", "CTB", "NLB", "MTR")
OPERATOR_TAGS = {
    "KMB": {"KMB", "KMB+CTB", "LWB", "LWB+CTB"},
    "CTB": {"CTB", "KMB+CTB", "LWB+CTB"},
    "NLB": {"NLB"},
    "MTR": {"LRTFeeder"},
}
AGENCY_OPERATOR = {"KMB": "KMB", "LWB": "KMB", "CTB": "CTB",
                   "NLB": "NLB", "LRTFeeder": "MTR"}
GRID_CELL = 0.002
MATCH_RADIUS = 80.0
AUTO_ACCEPT = 15.0
NAME_MIN = 0.34
GAP_FILL_QUALITY = 0.8
NLB_WORKERS = 6
KMB_ROUTE_WORKERS = 8
BUS_3_CAPACITY = ALPHABET_SIZE ** BUS_CODE_LEN

NATIVE_ROUTE_PREFIX = "native:"
NATIVE_STABLE_PREFIX = "NATIVE|"
NATIVE_MERGE_RADIUS = 200.0
OPERATOR_ORDER = ("KMB", "LWB", "CTB", "NLB", "GMB", "MTR", "LRTFeeder")


def log(message):
    print(message, flush=True)


def native_key(operator, stop_id):
    return "%s:%s" % (operator, stop_id)


def split_native_key(key):
    return key.split(":", 1)


def valid_location(loc):
    return (LAT_RANGE[0] <= loc[0] <= LAT_RANGE[1]
            and LON_RANGE[0] <= loc[1] <= LON_RANGE[1])


# --------------------------------------------------------------------------
# Native stops and route sequences
# --------------------------------------------------------------------------

def fetch_kmb():
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
        if valid_location(loc):
            stops[stop_id] = {"en": (row.get("name_en") or "").strip(),
                              "zh": (row.get("name_tc") or "").strip(), "loc": loc}
    return stops, []


def fetch_ctb():
    payload = get_json(CTB_STOP_URL)
    if not isinstance(payload, dict):
        raise RuntimeError("CTB stop snapshot is not an object")
    stops, issues = {}, []
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
        stops[str(stop_id)] = {"en": (row.get("name_en") or "").strip(),
                               "zh": (row.get("name_tc") or "").strip(), "loc": loc}
    return stops, issues


_NLB_CACHE = {}
_MTR_BODY = None


def _nlb_payload(route_id):
    key = str(route_id)
    if key not in _NLB_CACHE:
        _NLB_CACHE[key] = get_json(NLB_STOP_URL % key)
    return _NLB_CACHE[key]


def fetch_nlb():
    payload = get_json(NLB_ROUTE_URL)
    routes = payload.get("routes") or payload.get("data") or []
    route_ids = [str(row["routeId"]) for row in routes if row.get("routeId") is not None]
    stops, issues = {}, []

    def load(route_id):
        try:
            data = _nlb_payload(route_id)
        except RuntimeError as exc:
            return [], ["route %s: %s" % (route_id, exc)]
        rows, problems = [], []
        for row in data.get("stops") or []:
            try:
                loc = (float(row["latitude"]), float(row["longitude"]))
            except (KeyError, TypeError, ValueError):
                problems.append("route %s: bad coordinates" % route_id)
                continue
            rows.append((str(row.get("stopId") or "").strip(), {
                "en": (row.get("stopName_e") or "").strip(),
                "zh": (row.get("stopName_c") or "").strip(), "loc": loc}))
        return rows, problems

    with ThreadPoolExecutor(max_workers=NLB_WORKERS) as pool:
        for rows, problems in pool.map(load, route_ids):
            issues.extend(problems)
            for stop_id, record in rows:
                if stop_id and valid_location(record["loc"]) and stop_id not in stops:
                    stops[stop_id] = record
    return stops, issues


def _mtr_rows():
    global _MTR_BODY
    if _MTR_BODY is None:
        _MTR_BODY = get_req(MTR_STOP_URL).decode("utf-8-sig", errors="replace")
    return csv.DictReader(io.StringIO(_MTR_BODY))


def fetch_mtr():
    stops, issues = {}, []
    for row in _mtr_rows():
        stop_id = (row.get("STATION_ID") or "").strip()
        if not stop_id:
            continue
        try:
            loc = (float(row["STATION_LATITUDE"]), float(row["STATION_LONGITUDE"]))
        except (KeyError, TypeError, ValueError):
            issues.append("%s: bad coordinates" % stop_id)
            continue
        if valid_location(loc):
            stops[stop_id] = {"en": (row.get("STATION_NAME_ENG") or "").strip(),
                              "zh": (row.get("STATION_NAME_CHI") or "").strip(), "loc": loc}
    return stops, issues


FETCHERS = {"KMB": fetch_kmb, "CTB": fetch_ctb, "NLB": fetch_nlb, "MTR": fetch_mtr}


def bilingual(en, zh):
    return {"en": en or "", "zh": zh or en or ""}


def fetch_kmb_routes():
    rows = get_json(KMB_ROUTE_URL).get("data") or []

    def load(row):
        route = str(row.get("route") or "")
        bound = str(row.get("bound") or "O").upper()
        service = str(row.get("service_type") or "1")
        direction = "inbound" if bound == "I" else "outbound"
        try:
            payload = get_json(KMB_ROUTE_STOP_URL % (
                quote(route, safe=""), direction, quote(service, safe="")))
            stops = [str(item.get("stop") or "") for item in payload.get("data") or []]
            stops = [stop for stop in stops if stop]
            error = None
        except RuntimeError as exc:
            stops, error = [], str(exc)
        return {"route": route, "bound": bound, "serviceType": service,
                "orig": bilingual(row.get("orig_en"), row.get("orig_tc")),
                "dest": bilingual(row.get("dest_en"), row.get("dest_tc")),
                "stops": stops}, error

    routes, issues = [], []
    with ThreadPoolExecutor(max_workers=KMB_ROUTE_WORKERS) as pool:
        for route, error in pool.map(load, rows):
            if route["stops"]:
                routes.append(route)
            if error:
                issues.append("%s %s/%s: %s" % (
                    route["route"], route["bound"], route["serviceType"], error))
    return routes, issues


def fetch_ctb_routes():
    payload = get_json(CTB_ROUTE_URL)
    routes = []
    for number, row in sorted(payload.items()):
        for bound in ("I", "O"):
            item = row.get(bound) or {}
            stops = [str(stop) for stop in item.get("stops") or [] if stop]
            if not stops:
                continue
            # CTB publishes no service type. Its service shape is inferred from
            # the sequence it supplies: a repeated terminal means a loop, and
            # the bound distinguishes the two directions. Nothing is assumed
            # per route number.
            routes.append({"route": str(number), "bound": bound, "serviceType": None,
                           "loop": len(stops) > 1 and stops[0] == stops[-1],
                           "orig": bilingual(item.get("orig_en"), item.get("orig_tc")),
                           "dest": bilingual(item.get("dest_en"), item.get("dest_tc")),
                           "stops": stops})
    return routes, []


ROUTE_FETCHERS = {"KMB": fetch_kmb_routes, "CTB": fetch_ctb_routes}


def fetch_nlb_routes():
    """NLB supplies one row per direction plus its own overnight/special flags.

    The bound is inferred from the operator's own sequences: when two rows of
    one route are reverses of each other the first is outbound and the second
    inbound. No route number is special-cased.
    """
    payload = get_json(NLB_ROUTE_URL)
    rows = payload.get("routes") or payload.get("data") or []
    built, issues = [], []
    for row in rows:
        route_id = row.get("routeId")
        number = str(row.get("routeNo") or "").strip()
        if route_id is None or not number:
            continue
        try:
            data = _nlb_payload(route_id)
        except RuntimeError as exc:
            issues.append("route %s: %s" % (route_id, exc))
            continue
        stops = [str(item.get("stopId") or "").strip() for item in data.get("stops") or []]
        stops = [stop for stop in stops if stop]
        if len(stops) < 2:
            continue
        name_en = str(row.get("routeName_e") or "")
        name_zh = str(row.get("routeName_c") or "")
        orig_en, _, dest_en = name_en.partition(">")
        orig_zh, _, dest_zh = name_zh.partition(">")
        if int(row.get("specialRoute") or 0):
            service = "special"
        elif int(row.get("overnightRoute") or 0):
            service = "overnight"
        else:
            service = None
        built.append({"route": number, "bound": "O", "serviceType": service,
                      "loop": len(stops) > 1 and stops[0] == stops[-1],
                      "orig": bilingual(orig_en.strip(), orig_zh.strip()),
                      "dest": bilingual(dest_en.strip(), dest_zh.strip()),
                      "stops": stops})
    groups = defaultdict(list)
    for route in built:
        groups[route["route"]].append(route)
    for group in groups.values():
        for position, route in enumerate(group):
            others = [other for other in group if other is not route]
            if any(route["stops"] == list(reversed(other["stops"]))
                   for other in others):
                route["bound"] = "I" if position else "O"
    return built, issues


def fetch_mtr_routes():
    """MTR feeder sequences come from the same CSV, ordered by STATION_SEQNO."""
    groups = defaultdict(list)
    for row in _mtr_rows():
        route = (row.get("ROUTE_ID") or "").strip()
        direction = (row.get("DIRECTION") or "O").strip().upper()
        stop_id = (row.get("STATION_ID") or "").strip()
        if not route or not stop_id:
            continue
        try:
            sequence = int(row.get("STATION_SEQNO") or 0)
        except ValueError:
            sequence = 0
        groups[(route, direction)].append((sequence, stop_id))
    routes = []
    for (route, direction), items in sorted(groups.items()):
        items.sort()
        stops = [stop for _, stop in items]
        if len(stops) < 2:
            continue
        routes.append({"route": route, "bound": "I" if direction == "I" else "O",
                       "serviceType": None,
                       "loop": len(stops) > 1 and stops[0] == stops[-1],
                       "orig": bilingual("", ""), "dest": bilingual("", ""),
                       "stops": stops})
    return routes, []


ROUTE_FETCHERS.update({"NLB": fetch_nlb_routes, "MTR": fetch_mtr_routes})


# --------------------------------------------------------------------------
# TD candidates and sequence alignment
# --------------------------------------------------------------------------

class StopIndex:
    def __init__(self, stops_payload):
        self.records, self.grid, self.by_td = {}, defaultdict(list), defaultdict(list)
        for key, record in stops_payload.items():
            if record.get("mode") != "bus" or not record.get("loc"):
                continue
            self.records[key] = record
            self.by_td[str(record.get("td") or "")].append(key)
            loc = record["loc"]
            self.grid[(int(loc[0] / GRID_CELL), int(loc[1] / GRID_CELL))].append(key)

    def candidates(self, operator, stop, override=None):
        if override is not None:
            # An explicit override is authoritative: it replaces proximity and
            # name matching entirely, including the match radius.
            override = str(override)
            result = []
            for key in self.by_td.get(override, ()):
                record = self.records[key]
                result.append({"td": override,
                               "d": round(distance_m(stop["loc"], record["loc"]), 1),
                               "c": 1.0, "m": "override", "q": 1.0})
            result.sort(key=lambda row: (row["d"], row["td"]))
            return result
        rows = []
        cell = (int(stop["loc"][0] / GRID_CELL), int(stop["loc"][1] / GRID_CELL))
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                for key in self.grid.get((cell[0] + dr, cell[1] + dc), ()):
                    record = self.records[key]
                    dist = distance_m(stop["loc"], record["loc"])
                    if dist <= MATCH_RADIUS:
                        rows.append((dist, key, record))
        tagged = [row for row in rows if OPERATOR_TAGS[operator]
                  & set(stop_company_names(row[2]))]
        rows = tagged or rows
        result = []
        for dist, key, record in rows:
            names = [stop_name(record, "en")]
            names.extend(stop_company_names(record).values())
            similarity = max(name_score(stop["en"], name) for name in names)
            quality = 0.65 * similarity + 0.35 * (1.0 - dist / MATCH_RADIUS)
            accepted = (dist <= AUTO_ACCEPT or
                        (similarity >= NAME_MIN and (dist <= 60 or similarity >= 0.5)))
            if accepted:
                result.append({"td": str(record["td"]), "d": round(dist, 1),
                               "c": round(quality, 3),
                               "m": "coord" if dist <= AUTO_ACCEPT and similarity < NAME_MIN
                               else "coord+name", "q": quality})
        result.sort(key=lambda row: (-row["q"], row["d"], row["td"]))
        return result


def align_sequences(td_ids, operator_stop_ids, candidates):
    """Global alignment; native supersets are cheap, missed TD stops are not."""
    n, m = len(td_ids), len(operator_stop_ids)
    quality = []
    for stop_id in operator_stop_ids:
        quality.append({row["td"]: row["q"] for row in candidates.get(stop_id, [])})
    dp = [[None] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = (0.0, 0)
    for i in range(1, n + 1):
        dp[i][0] = (dp[i - 1][0][0] - 1.25, dp[i - 1][0][1])
        back[i][0] = "T"
    for j in range(1, m + 1):
        dp[0][j] = (dp[0][j - 1][0] - 0.12, 0)
        back[0][j] = "N"

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            options = [(dp[i][j - 1][0] - 0.12, dp[i][j - 1][1], "N"),
                       (dp[i - 1][j][0] - 1.25, dp[i - 1][j][1], "T")]
            q = quality[j - 1].get(td_ids[i - 1])
            if q is not None:
                options.append((dp[i - 1][j - 1][0] + 2.0 + q,
                                dp[i - 1][j - 1][1] + 1, "M"))
            best = max(options, key=lambda row: (row[0], row[1], row[2] == "M"))
            dp[i][j], back[i][j] = best[:2], best[2]

    pairs = []
    i, j = n, m
    while i or j:
        action = back[i][j]
        if action == "M":
            pairs.append((i - 1, j - 1, quality[j - 1][td_ids[i - 1]]))
            i, j = i - 1, j - 1
        elif action == "T":
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return {"pairs": pairs, "matched": len(pairs), "score": dp[n][m][0]}


def direction_score(variant, route):
    return (name_score((variant.get("orig") or {}).get("en", ""), route["orig"]["en"])
            + name_score((variant.get("dest") or {}).get("en", ""), route["dest"]["en"]))


def stitched_tracks(routes):
    """Ordered joins of the opposite bounds of one route.

    TD can publish a loop as a single pattern while the operator publishes its
    two halves, or a truncated loop pattern that only the pair covers. Joining
    O and I (either order) gives the alignment a track that follows the
    operator's own sequences; no route-specific knowledge is involved.
    """
    tracks, seen = [], set()
    for first in routes:
        for second in routes:
            if first is second or first["bound"] == second["bound"]:
                continue
            # A joined track is only the same service if both bounds carry the
            # same service type; mixing them would invent a service.
            if (first.get("serviceType") or "") != (second.get("serviceType") or ""):
                continue
            first_stops, second_stops = list(first["stops"]), list(second["stops"])
            if not first_stops or not second_stops:
                continue
            if first_stops[-1] == second_stops[0]:
                second_stops = second_stops[1:]
            stops = first_stops + second_stops
            key = tuple(stops)
            if key in seen:
                continue
            seen.add(key)
            tracks.append({
                "route": first["route"],
                "bound": "%s+%s" % (first["bound"], second["bound"]),
                "serviceType": first.get("serviceType"),
                "loop": len(stops) > 1 and stops[0] == stops[-1],
                "orig": first.get("orig") or {},
                "dest": second.get("dest") or {},
                "stops": stops,
                "segments": [
                    {"bound": first["bound"], "start": 0, "length": len(first_stops)},
                    {"bound": second["bound"], "start": len(first_stops),
                     "length": len(second_stops)},
                ],
            })
    return tracks


def align_routes(variants, native_routes, candidates):
    alignments = {}
    stats = defaultdict(lambda: {"variants": 0, "positions": 0})
    for variant_id, variant in sorted(variants.items()):
        td_ids = [key[len(TD_PREFIX):] for key in variant.get("stops") or []
                  if key.startswith(TD_PREFIX)]
        if len(td_ids) < 2 or len(td_ids) != len(variant.get("stops") or []):
            continue  # GMB-only or degenerate variant
        minimum = min(2, len(td_ids)) if len(td_ids) <= 3 else math.ceil(len(td_ids) * 0.45)
        for agency in variant.get("co") or []:
            operator = AGENCY_OPERATOR.get(agency)
            if operator not in native_routes:
                continue
            routes = [route for route in native_routes[operator]
                      if route["route"] == variant.get("route")]
            best = None
            for route in routes:
                aligned = align_sequences(td_ids, route["stops"],
                                          candidates.get(operator, {}))
                if aligned["matched"] < minimum:
                    continue
                rank = (aligned["score"] + direction_score(variant, route),
                        aligned["matched"], -abs(len(td_ids) - len(route["stops"])),
                        route["bound"], route.get("serviceType") or "")
                if best is None or rank > best[0]:
                    best = (rank, route, aligned)
            # A loop or partial loop has no single native sequence that covers
            # the TD pattern. Try the operator's joined bounds only then.
            if best is not None and best[2]["matched"] < len(td_ids):
                for track in stitched_tracks(routes):
                    aligned = align_sequences(td_ids, track["stops"],
                                              candidates.get(operator, {}))
                    if aligned["matched"] < minimum:
                        continue
                    rank = (aligned["score"] + direction_score(variant, track),
                            aligned["matched"], -abs(len(td_ids) - len(track["stops"])),
                            track["bound"], track.get("serviceType") or "")
                    if rank > best[0]:
                        best = (rank, track, aligned)
            if best is not None:
                alignments.setdefault(variant_id, {})[operator] = {
                    "route": best[1], "pairs": best[2]["pairs"],
                    "matched": best[2]["matched"], "score": round(best[2]["score"], 3),
                    "td_ids": td_ids}
                stats[operator]["variants"] += 1
                stats[operator]["positions"] += best[2]["matched"]
    return alignments, stats


def propagate_coop_alignments(alignments, variants, stats):
    """Fill missing co-op positions only between consistent native anchors.

    If every shared TD position has the same target/donor native-index offset,
    that offset is reliable sequence evidence even when a pole is beyond the
    ordinary coordinate radius. No offset is guessed across divergent segments.
    """
    for variant_id, by_operator in alignments.items():
        if len(by_operator) < 2:
            continue
        for target, target_aligned in by_operator.items():
            target_used = {native_index for _, native_index, _ in target_aligned["pairs"]}
            target_inferred = target_aligned.get("inferred") or set()
            target_at_td = {td_index: native_index
                            for td_index, native_index, _ in target_aligned["pairs"]
                            if (td_index, native_index) not in target_inferred}
            for donor, donor_aligned in by_operator.items():
                if donor == target:
                    continue
                donor_inferred = donor_aligned.get("inferred") or set()
                donor_at_td = {td_index: (native_index, quality)
                               for td_index, native_index, quality in donor_aligned["pairs"]
                               if (td_index, native_index) not in donor_inferred}
                shared = [(target_at_td[i], donor_at_td[i][0]) for i in target_at_td
                          if i in donor_at_td]
                if len(shared) < 2:
                    continue
                offsets = {left - right for left, right in shared}
                if len(offsets) != 1:
                    continue
                offset = next(iter(offsets))
                target_size = len(target_aligned["route"]["stops"])
                added = []
                for td_index, (donor_index, quality) in sorted(donor_at_td.items()):
                    if td_index in target_at_td:
                        continue
                    native_index = donor_index + offset
                    if not (0 <= native_index < target_size) or native_index in target_used:
                        continue
                    added.append((td_index, native_index, min(0.8, quality)))
                    target_used.add(native_index)
                if added:
                    target_aligned["pairs"].extend(added)
                    target_aligned["pairs"].sort()
                    target_aligned["matched"] += len(added)
                    stats[target]["positions"] += len(added)
                    target_at_td.update({i: j for i, j, _ in added})
    return alignments


def complete_alignments(alignments):
    """Pair skipped TD positions with unused native stops between anchors.

    The operator's own sequence is authoritative. When the DP skipped a TD
    position and left a native stop unused inside the same matched interval,
    the two are bound positionally. Stops outside the chosen sequence are
    never substituted, so a gap with no native stop stays empty.
    """
    added = 0
    for by_operator in alignments.values():
        for operator, aligned in by_operator.items():
            pairs = sorted(aligned["pairs"])
            td_count = len(aligned["td_ids"])
            native_count = len(aligned["route"]["stops"])
            used_td = {index for index, _, _ in pairs}
            used_native = {index for _, index, _ in pairs}
            anchors = [(-1, -1)]
            anchors += [(index, native) for index, native, _ in pairs]
            anchors.append((td_count, native_count))
            filled = []
            for (td_left, native_left), (td_right, native_right) in zip(anchors, anchors[1:]):
                td_gap = [index for index in range(td_left + 1, td_right)
                          if index not in used_td]
                native_gap = [index for index in range(native_left + 1, native_right)
                              if index not in used_native]
                for td_index, native_index in zip(td_gap, native_gap):
                    filled.append((td_index, native_index, GAP_FILL_QUALITY))
            if filled:
                aligned["pairs"] = sorted(pairs + filled)
                aligned["matched"] += len(filled)
                aligned.setdefault("inferred", set()).update(
                    (td_index, native_index) for td_index, native_index, _ in filled)
                added += len(filled)
    return added


# Persistent native codes


class NativeCodeRegistry:
    def __init__(self, state=None, historical_codes=(), collision_codes=(),
                 capacity3=BUS_3_CAPACITY):
        state = state if isinstance(state, dict) else {}
        self.groups = {}
        self.native_to_code = {}
        for code, row in (state.get("groups") or {}).items():
            operators = {str(op): str(stop) for op, stop in
                         (row.get("operators") or {}).items()}
            self.groups[code] = {"operators": operators}
            for op, stop in operators.items():
                self.native_to_code[native_key(op, stop)] = code
        self.td_codes = {str(td): code for td, code in
                         (state.get("tdCodes") or {}).items()}
        self.reserved = set(state.get("reserved") or []) | set(historical_codes)
        self.reserved -= set(self.groups)
        self.collisions = set(collision_codes)
        self.capacity3 = capacity3
        self.next_index = 0
        for code in self.groups:
            if code and all(char in ALPHABET for char in code):
                try:
                    self.next_index = max(self.next_index, index_from_code(code) + 1)
                except ValueError:
                    pass

    def _can_use(self, code, operator):
        return code not in self.groups or operator not in self.groups[code]["operators"]

    def _activate(self, code, operator, stop_id):
        group = self.groups.setdefault(code, {"operators": {}})
        group["operators"][operator] = stop_id
        self.native_to_code[native_key(operator, stop_id)] = code
        self.reserved.discard(code)
        return code

    def _allocate(self, operator, stop_id):
        while True:
            length = BUS_CODE_LEN if self.next_index < self.capacity3 else GMB_CODE_LEN
            code = code_from_index(self.next_index, length)
            self.next_index += 1
            if code not in self.groups and code not in self.reserved and code not in self.collisions:
                return self._activate(code, operator, stop_id)

    def assign(self, key, preferred=None, allow_cross=False):
        if key in self.native_to_code:
            return self.native_to_code[key]
        operator, stop_id = split_native_key(key)
        if preferred and self._can_use(preferred, operator):
            other_company = (preferred in self.groups and
                             any(op != operator for op in self.groups[preferred]["operators"]))
            if allow_cross or not other_company:
                return self._activate(preferred, operator, stop_id)
        return self._allocate(operator, stop_id)

    def merge(self, left, right):
        a, b = self.native_to_code[left], self.native_to_code[right]
        if a == b:
            return False
        ga, gb = self.groups[a]["operators"], self.groups[b]["operators"]
        if set(ga) & set(gb):
            return False
        survivor, retired = sorted((a, b), key=lambda code: (index_from_code(code), code))
        keep, drop = survivor, retired
        moving = self.groups[drop]["operators"]
        self.groups[keep]["operators"].update(moving)
        for op, stop in moving.items():
            self.native_to_code[native_key(op, stop)] = keep
        del self.groups[drop]
        self.reserved.add(drop)
        return True

    def assign_td(self, td, preferred=None):
        if td in self.td_codes:
            code = self.td_codes[td]
            if code not in self.groups:
                self.reserved.discard(code)
                return code
            del self.td_codes[td]
        if preferred and preferred not in self.groups:
            code = preferred
            self.reserved.discard(code)
        else:
            code = self._allocate("TD", td)
            # _allocate created a placeholder native group; TD-only groups are
            # represented separately and must not become a fake company pole.
            del self.groups[code]["operators"]["TD"]
            if not self.groups[code]["operators"]:
                del self.groups[code]
            self.native_to_code.pop(native_key("TD", td), None)
        self.td_codes[td] = code
        return code

    def retire_unused_td(self, active_td):
        for td, code in list(self.td_codes.items()):
            if td not in active_td:
                self.reserved.add(code)
                del self.td_codes[td]

    def state(self):
        return {"version": 1,
                "groups": {code: self.groups[code] for code in sorted(self.groups)},
                "tdCodes": {td: self.td_codes[td] for td in sorted(self.td_codes)},
                "reserved": sorted(self.reserved)}


# --------------------------------------------------------------------------
# Dataset assembly
# --------------------------------------------------------------------------

def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def build_links(native_stops, index, overrides, alignments, variants):
    candidates = {op: {} for op in native_stops}
    links = {op: {} for op in native_stops}
    unmatched = {op: [] for op in native_stops}
    for operator, stops in native_stops.items():
        for stop_id, stop in sorted(stops.items()):
            rows = index.candidates(operator, stop, (overrides.get(operator) or {}).get(stop_id))
            candidates[operator][stop_id] = rows
            if rows:
                best = dict(rows[0])
                best["q"] = float(best.get("q", best["c"]))
                links[operator][stop_id] = {best["td"]: best}
            else:
                links[operator][stop_id] = {}
                unmatched[operator].append({"id": stop_id, "en": stop["en"],
                    "zh": stop["zh"], "loc": [round(stop["loc"][0], 5), round(stop["loc"][1], 5)]})

    td_records = {str(record.get("td")): record for record in index.records.values()}
    for variant_id, by_operator in alignments.items():
        td_ids = [key[len(TD_PREFIX):] for key in variants[variant_id]["stops"]]
        for operator, aligned in by_operator.items():
            native_ids = aligned["route"]["stops"]
            for td_index, native_index, quality in aligned["pairs"]:
                stop_id = native_ids[native_index]
                td = td_ids[td_index]
                row = {"td": td, "d": 0, "c": round(quality, 3),
                       "m": "route", "q": quality}
                native = native_stops[operator].get(stop_id)
                td_record = td_records.get(td)
                if native and td_record:
                    row["d"] = round(distance_m(native["loc"], td_record["loc"]), 1)
                old = links[operator][stop_id].get(td)
                if old is None or quality > old.get("q", 0):
                    links[operator][stop_id][td] = row
    return candidates, links, unmatched


def safe_cross_company_pairs(alignments, variants, links, native_stops, stops_payload):
    evidence = defaultdict(int)
    for variant_id, by_operator in alignments.items():
        td_ids = [key[len(TD_PREFIX):] for key in variants[variant_id]["stops"]]
        at_position = defaultdict(dict)
        for operator, aligned in by_operator.items():
            inferred = aligned.get("inferred") or set()
            for td_index, native_index, _ in aligned["pairs"]:
                if (td_index, native_index) in inferred:
                    continue  # positional completion is not co-location evidence
                stop_id = aligned["route"]["stops"][native_index]
                at_position[td_index][operator] = stop_id
        for operators in at_position.values():
            keys = sorted(native_key(op, stop) for op, stop in operators.items())
            for i, left in enumerate(keys):
                for right in keys[i + 1:]:
                    evidence[(left, right)] += 1

    # A unique, jointly tagged TD pole is additional evidence. One-to-many
    # locations (CTB 1 / KMB many, etc.) intentionally produce no pair.
    by_td = defaultdict(lambda: defaultdict(list))
    for operator, rows in links.items():
        for stop_id, td_rows in rows.items():
            for td in td_rows:
                by_td[td][operator].append(stop_id)
    td_records = {str(row.get("td")): row for row in stops_payload.values()
                  if row.get("mode") == "bus"}
    for td, by_operator in by_td.items():
        record = td_records.get(td) or {}
        if not ({"KMB+CTB", "LWB+CTB"} & set(stop_company_names(record))):
            continue
        if any(len(stop_ids) != 1 for stop_ids in by_operator.values()):
            continue
        keys = sorted(native_key(op, rows[0]) for op, rows in by_operator.items())
        for i, left in enumerate(keys):
            for right in keys[i + 1:]:
                op_a, id_a = split_native_key(left)
                op_b, id_b = split_native_key(right)
                a, b = native_stops[op_a][id_a], native_stops[op_b][id_b]
                if (distance_m(a["loc"], b["loc"]) <= 25
                        and name_score(a["en"], b["en"]) >= NAME_MIN):
                    evidence[(left, right)] += 1

    partners = defaultdict(lambda: defaultdict(set))
    for left, right in evidence:
        op_a, _ = split_native_key(left)
        op_b, _ = split_native_key(right)
        partners[left][op_b].add(right)
        partners[right][op_a].add(left)
    safe = []
    for pair, count in evidence.items():
        left, right = pair
        op_a, _ = split_native_key(left)
        op_b, _ = split_native_key(right)
        if partners[left][op_b] == {right} and partners[right][op_a] == {left}:
            safe.append((count, pair))
    return [pair for _, pair in sorted(safe, key=lambda row: (-row[0], row[1]))]


def primary_link(rows):
    values = rows.values() if isinstance(rows, dict) else rows
    if not values:
        return None
    return sorted(values, key=lambda row: (
        0 if row["m"] in ("route", "override") else 1,
        -row.get("q", row["c"]), row["d"], row["td"]))[0]


def normalized_route(value):
    return str(value or "").strip().upper()


def operator_rank(name):
    try:
        return OPERATOR_ORDER.index(name)
    except ValueError:
        return len(OPERATOR_ORDER)


def is_native_only_variant(variant):
    return (variant.get("tdRouteId") is None
            and str(variant.get("stableKey") or "").startswith(NATIVE_STABLE_PREFIX))


def covered_td_route_numbers(variants):
    covered = defaultdict(set)
    for variant in variants.values():
        number = normalized_route(variant.get("route"))
        if not number:
            continue
        for agency in variant.get("co") or []:
            operator = AGENCY_OPERATOR.get(agency)
            if operator:
                covered[operator].add(number)
    return covered


def build_native_only_variants(variants, index_payload, native_routes,
                               native_stops, registry, variant_mapper, started):
    """Emit operator-only routes that have no corresponding TD variant."""
    covered = covered_td_route_numbers(variants)
    groups = defaultdict(list)
    seen = set()
    stats = {"routes": 0, "variants": 0, "byOperator": defaultdict(int),
             "skipped": []}
    used_route_ids = {entry.get("id") for entry in
                      (index_payload.get("routes") or {}).values()}

    for operator in sorted(native_routes):
        for route in native_routes[operator]:
            number = str(route.get("route") or "").strip()
            norm = normalized_route(number)
            if not number:
                continue
            if norm in covered.get(operator, set()):
                continue
            stops = [str(stop) for stop in route.get("stops") or []]
            if len(stops) < 2:
                stats["skipped"].append("%s %s: fewer than 2 stops" % (operator, number))
                continue
            missing = [stop for stop in stops
                       if stop not in native_stops.get(operator, {})]
            if missing:
                stats["skipped"].append("%s %s: missing stops %s" % (
                    operator, number, ",".join(missing[:3])))
                continue
            sequence_key = (operator, norm, route.get("bound"), tuple(stops))
            if sequence_key in seen:
                continue
            seen.add(sequence_key)

            codes = [registry.native_to_code[native_key(operator, stop)]
                     for stop in stops]
            digest = hashlib.sha1(",".join(stops).encode("utf-8")).hexdigest()[:16]
            stable_key = "%s%s|%s|%s|%s|%s|%s" % (
                NATIVE_STABLE_PREFIX, operator, number, route.get("bound"),
                stops[0], stops[-1], digest)
            preferred = "%s_%s_%s1" % (operator, route_slug(number), route.get("bound"))
            variant_id, _ = variant_mapper.assign(stable_key, preferred)

            orig = dict(route.get("orig") or {"en": "", "zh": ""})
            dest = dict(route.get("dest") or {"en": "", "zh": ""})
            circular = len(codes) > 1 and codes[0] == codes[-1]
            variant = {
                "id": variant_id,
                "co": [operator],
                "route": number,
                "bound": route.get("bound"),
                "tdRouteId": None,
                "nativeRouteId": "%s:%s:%s:%s" % (
                    operator, number, route.get("bound"), route.get("serviceType") or ""),
                "stableKey": stable_key,
                "orig": orig,
                "dest": dest,
                "circular": circular,
                "stops": codes,
                "operatorStopCodes": {operator: codes},
                "freq": {},
                "fixedDepartures": {},
                "fares": {"currency": "HKD", "kind": "flat", "flat": None},
                "hasHolidayFare": False,
                "sources": {"static": operator + "_API",
                            "generatedAt": started.isoformat(timespec="seconds")},
            }
            variants[variant_id] = variant
            index_payload.setdefault("variants", {})[variant_id] = {
                "co": [operator], "route": number, "bound": route.get("bound"),
                "orig": orig, "dest": dest, "circular": circular,
                "stops": len(codes), "services": [], "freq": {},
            }
            groups[norm].append(variant_id)
            stats["variants"] += 1
            stats["byOperator"][operator] += 1

    route_keys = set(index_payload.get("routes") or {})
    for norm, ids in sorted(groups.items()):
        ids.sort(key=lambda vid: (
            operator_rank((variants[vid].get("co") or [""])[0]), vid))
        first = variants[ids[0]]
        route_key = NATIVE_ROUTE_PREFIX + norm
        suffix = 2
        base_key = route_key
        while route_key in route_keys:
            route_key = "%s_%d" % (base_key, suffix)
            suffix += 1
        route_keys.add(route_key)

        operators = sorted({name for vid in ids for name in
                            (variants[vid].get("co") or [])},
                           key=lambda name: (operator_rank(name), name))
        primary = operators[0] if operators else "KMB"
        route_id_base = "%s_%s" % (primary, route_slug(first["route"]))
        route_id = route_id_base
        suffix = 2
        while route_id in used_route_ids:
            route_id = "%s_%d" % (route_id_base, suffix)
            suffix += 1
        used_route_ids.add(route_id)

        index_payload.setdefault("routes", {})[route_key] = {
            "co": operators,
            "route": first["route"],
            "orig": first["orig"],
            "dest": first["dest"],
            "variants": ids,
            "id": route_id,
        }
        stats["routes"] += 1

    stats["byOperator"] = dict(sorted(stats["byOperator"].items()))
    return stats


def sequences_close(left, right, final_stops, radius=NATIVE_MERGE_RADIUS):
    left_stops = left.get("stops") or []
    right_stops = right.get("stops") or []
    if not left_stops or len(left_stops) != len(right_stops):
        return False
    for left_code, right_code in zip(left_stops, right_stops):
        left_record = final_stops.get(left_code)
        right_record = final_stops.get(right_code)
        if not left_record or not right_record:
            return False
        if distance_m(left_record["loc"], right_record["loc"]) > radius:
            return False
    return True


def merge_native_only_variants(variants, index_payload, final_stops):
    """Merge close operator-only variants; TD routes are never touched."""
    td_numbers = {normalized_route(variant.get("route"))
                  for variant in variants.values()
                  if not is_native_only_variant(variant)}
    groups = defaultdict(list)
    for variant_id, variant in variants.items():
        if is_native_only_variant(variant):
            groups[normalized_route(variant.get("route"))].append(variant_id)

    stats = {"groups": 0, "variants": 0, "skippedTd": []}
    for number, ids in sorted(groups.items()):
        if number in td_numbers:
            stats["skippedTd"].append(number)
            continue
        ids.sort(key=lambda vid: (
            operator_rank((variants[vid].get("co") or [""])[0]), vid))
        parent = {vid: vid for vid in ids}

        def find(value):
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        def union(left, right):
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for position, left in enumerate(ids):
            for right in ids[position + 1:]:
                left_variant = variants[left]
                right_variant = variants[right]
                if set(left_variant.get("co") or []) & set(right_variant.get("co") or []):
                    continue
                if sequences_close(left_variant, right_variant, final_stops):
                    union(left, right)

        components = defaultdict(list)
        for variant_id in ids:
            components[find(variant_id)].append(variant_id)

        for component in components.values():
            if len(component) < 2:
                continue
            component_operators = [operator for vid in component
                                   for operator in (variants[vid].get("co") or [])]
            if len(component_operators) != len(set(component_operators)):
                continue
            canonical = sorted(component, key=lambda vid: (
                -len(variants[vid].get("co") or []),
                operator_rank((variants[vid].get("co") or [""])[0]),
                vid))[0]
            others = sorted(vid for vid in component if vid != canonical)
            target = variants[canonical]

            target["co"] = sorted({
                name for vid in component for name in
                (variants[vid].get("co") or [])},
                key=lambda name: (operator_rank(name), name))
            target["mergedFrom"] = others

            bounds = {}
            arrays = {}
            for vid in component:
                variant = variants[vid]
                for operator in variant.get("co") or []:
                    bounds.setdefault(operator, variant.get("bound"))
                for operator, array in (variant.get("operatorStopCodes") or {}).items():
                    arrays.setdefault(operator, array)
            if arrays:
                target["operatorStopCodes"] = arrays
            if len(set(bounds.values())) > 1:
                target["bounds"] = bounds
            else:
                target.pop("bounds", None)

            for other in others:
                variants.pop(other, None)
                index_payload.get("variants", {}).pop(other, None)
            stats["groups"] += 1
            stats["variants"] += len(others)

    if stats["variants"]:
        for entry in index_payload.get("routes", {}).values():
            seen = []
            for variant_id in entry.get("variants") or []:
                if variant_id not in variants:
                    continue
                if variant_id not in seen:
                    seen.append(variant_id)
            entry["variants"] = seen
            operators = {name for vid in seen for name in
                         (variants[vid].get("co") or [])}
            if operators:
                entry["co"] = sorted(operators, key=lambda name: (
                    operator_rank(name), name))
            if not seen:
                entry["variants"] = []
    return stats


def annotate_variant_codes(variant):
    """Record the positions each operator's own array leaves empty.

    No array is padded or guessed. A position stays null when the operator's
    own route list has no stop for it, and that null is listed in "unserved"
    (0-based indices into "stops") so it can be fixed later. Co-op variants
    also carry "codeMode": "shared" when every operator publishes the same
    code at every position, "split" when their arrays differ.
    """
    arrays = variant.get("operatorStopCodes") or {}
    unserved = {}
    for operator, array in arrays.items():
        missing = [index for index, code in enumerate(array) if not code]
        if missing:
            unserved[operator] = missing
    if unserved:
        variant["unserved"] = unserved
    else:
        variant.pop("unserved", None)
    if len(arrays) < 2:
        variant.pop("codeMode", None)
        return
    values = list(arrays.values())
    shared = any(values[0]) and all(array == values[0] for array in values[1:])
    variant["codeMode"] = "shared" if shared else "split"


def assemble(args, stops_payload, variants, index_payload, native_stops,
             native_routes, previous_state, previous_stops, previous_map,
             previous_td_codes, overrides, started, previous_variants=None):
    candidates, links, unmatched = build_links(
        native_stops, StopIndex(stops_payload), overrides, alignments={}, variants=variants)
    # Route alignment is recomputed here with the complete candidate map.
    alignments, alignment_stats = align_routes(variants, native_routes, candidates)
    complete_alignments(alignments)
    propagate_coop_alignments(alignments, variants, alignment_stats)
    candidates, links, unmatched = build_links(
        native_stops, StopIndex(stops_payload), overrides, alignments, variants)

    historical_bus_codes = {code for code, row in previous_stops.items()
                            if row.get("mode") == "bus"}
    historical_bus_codes |= {code for code in previous_td_codes.values()
                             if len(code) == BUS_CODE_LEN}
    gmb_codes = {code for code, row in stops_payload.items() if row.get("mode") == "gmb"}
    gmb_codes |= {code for code in previous_td_codes.values() if len(code) == GMB_CODE_LEN}
    registry = NativeCodeRegistry(previous_state, historical_bus_codes, gmb_codes)

    route_td_op = defaultdict(lambda: defaultdict(set))
    for variant_id, by_operator in alignments.items():
        td_ids = [key[len(TD_PREFIX):] for key in variants[variant_id]["stops"]]
        for operator, aligned in by_operator.items():
            for td_index, native_index, _ in aligned["pairs"]:
                route_td_op[td_ids[td_index]][operator].add(
                    aligned["route"]["stops"][native_index])
    ambiguous_td = {td for td, rows in route_td_op.items()
                    if any(len(stop_ids) > 1 for stop_ids in rows.values())}

    # On migration, route evidence decides which pole inherits the old TD code.
    # Ambiguous locations do not share that code at all.
    for td in sorted(route_td_op):
        preferred = previous_td_codes.get(td)
        if not preferred or td in ambiguous_td:
            continue
        for operator in sorted(route_td_op[td]):
            stop_ids = route_td_op[td][operator]
            if len(stop_ids) == 1:
                registry.assign(native_key(operator, next(iter(stop_ids))),
                                preferred, allow_cross=True)

    previous_native_codes = {}
    for operator, result in (previous_map.get("operators") or {}).items():
        for stop_id, row in (result.get("stops") or {}).items():
            if row.get("code"):
                previous_native_codes[native_key(operator, stop_id)] = row["code"]
    for operator, stops in native_stops.items():
        for stop_id in sorted(stops):
            key = native_key(operator, stop_id)
            registry.assign(key, previous_native_codes.get(key), allow_cross=False)

    merged = 0
    for left, right in safe_cross_company_pairs(
            alignments, variants, links, native_stops, stops_payload):
        merged += int(registry.merge(left, right))

    # Every active native pole now has a code. Attach all current TD links.
    code_links = defaultdict(lambda: defaultdict(dict))
    for operator, rows in links.items():
        for stop_id, td_rows in rows.items():
            code = registry.native_to_code[native_key(operator, stop_id)]
            for td, row in td_rows.items():
                old = code_links[code][operator].get(td)
                if old is None or row.get("q", 0) > old.get("q", 0):
                    code_links[code][operator][td] = row

    linked_td = {td for by_operator in code_links.values() for rows in by_operator.values()
                 for td in rows}
    td_records = {str(row["td"]): row for row in stops_payload.values()
                  if row.get("mode") == "bus"}
    td_only_codes = {}
    for td in sorted(td_records):
        if td not in linked_td:
            td_only_codes[td] = registry.assign_td(td, previous_td_codes.get(td))
    registry.retire_unused_td(set(td_only_codes))
    registry.reserved -= set(registry.groups)

    # Build final native and TD-only stop records.
    final_stops = {code: dict(record) for code, record in stops_payload.items()
                   if record.get("mode") == "gmb"}
    code_tds = defaultdict(set)
    for code, by_operator in code_links.items():
        for rows in by_operator.values():
            code_tds[code].update(rows)
    for code, group in registry.groups.items():
        active = {op: stop for op, stop in group["operators"].items()
                  if stop in native_stops.get(op, {})}
        if not active:
            continue
        tds = sorted(code_tds.get(code, set()))
        # Prefer route-confirmed TD identities over a coordinate-only alias.
        link_rows = [row for rows in code_links.get(code, {}).values()
                     for row in rows.values()]
        best_link = primary_link(link_rows)
        primary_td = best_link["td"] if best_link else (tds[0] if tds else None)
        td_record = td_records.get(primary_td) if primary_td else None
        first_operator, first_stop = sorted(active.items())[0]
        native_record = native_stops[first_operator][first_stop]
        source = td_record or native_record
        en_names = nested_names(source.get("en"), native_record["en"])
        zh_names = nested_names(source.get("zh"),
                                native_record["zh"] or native_record["en"])
        for operator, stop_id in active.items():
            native = native_stops[operator][stop_id]
            tag = "LRTFeeder" if operator == "MTR" else operator
            if native.get("en"):
                en_names.setdefault(tag, native["en"])
            if native.get("zh") or native.get("en"):
                zh_names.setdefault(tag, native.get("zh") or native["en"])
        entry = {"td": primary_td or "", "tds": tds, "mode": "bus",
                 "en": en_names, "zh": zh_names,
                 "loc": [round(native_record["loc"][0], 5),
                         round(native_record["loc"][1], 5)]}
        final_stops[code] = entry
    for td, code in td_only_codes.items():
        entry = dict(td_records[td])
        entry["tds"] = [td]
        final_stops[code] = entry

    native_only_stats = {"routes": 0, "variants": 0, "byOperator": {},
                         "skipped": []}
    native_merge_stats = {"groups": 0, "variants": 0, "skippedTd": []}
    if getattr(args, "native_only_routes", True):
        variant_mapper = VariantIdMapper({
            variant["stableKey"]: variant_id
            for variant_id, variant in (previous_variants or {}).items()
            if variant.get("stableKey")
        })
        variant_mapper.used.update(variants)
        native_only_stats = build_native_only_variants(
            variants, index_payload, native_routes, native_stops, registry,
            variant_mapper, started)
        native_merge_stats = merge_native_only_variants(
            variants, index_payload, final_stops)
        for variant in variants.values():
            if is_native_only_variant(variant):
                annotate_variant_codes(variant)

    # Route-specific arrays. A code is published only for a position that the
    # operator's own sequence covers; everything else stays null and is listed
    # in "unserved". No display fallback invents a code.
    for variant_id, variant in variants.items():
        if not variant.get("stops") or not variant["stops"][0].startswith(TD_PREFIX):
            continue
        td_ids = [key[len(TD_PREFIX):] for key in variant["stops"]]
        by_operator = alignments.get(variant_id, {})
        agencies = [AGENCY_OPERATOR.get(name) for name in variant.get("co") or []]
        operators = list(dict.fromkeys(op for op in agencies if op in OPERATORS))
        operator_arrays = {}
        for operator in operators:
            array = [None] * len(td_ids)
            aligned = by_operator.get(operator)
            trusted = set()
            if aligned:
                inferred = aligned.get("inferred") or set()
                for td_index, native_index, _ in aligned["pairs"]:
                    stop_id = aligned["route"]["stops"][native_index]
                    array[td_index] = registry.native_to_code[native_key(operator, stop_id)]
                    if (td_index, native_index) not in inferred:
                        trusted.add(td_index)
                # A TD loop lists the same stop id at both ends, while the
                # company may list the pole once. The same company code is the
                # only 1-to-1 answer for the repeated position, so reuse it
                # when it came from a real match rather than a positional one.
                repeats = defaultdict(list)
                for position, td in enumerate(td_ids):
                    repeats[td].append(position)
                for positions in repeats.values():
                    if len(positions) < 2:
                        continue
                    codes = {array[p] for p in positions if array[p] and p in trusted}
                    if len(codes) == 1:
                        code = next(iter(codes))
                        for position in positions:
                            if not array[position]:
                                array[position] = code
            operator_arrays[operator] = array

        primary_operator = next((op for op in agencies if op in OPERATORS), None)
        display = []
        for position in range(len(td_ids)):
            code = None
            if primary_operator in operator_arrays:
                code = operator_arrays[primary_operator][position]
            if code is None:
                for operator in operators:
                    if operator in operator_arrays and operator_arrays[operator][position]:
                        code = operator_arrays[operator][position]
                        break
            display.append(code)
        variant["stops"] = display

        if operator_arrays:
            variant["operatorStopCodes"] = operator_arrays
        else:
            variant.pop("operatorStopCodes", None)
        annotate_variant_codes(variant)

        primary = by_operator.get(primary_operator)
        if primary:
            route = primary["route"]
            forward = direction_score(variant, route)
            reverse = (name_score((variant.get("orig") or {}).get("en", ""), route["dest"]["en"])
                       + name_score((variant.get("dest") or {}).get("en", ""), route["orig"]["en"]))
            if reverse > forward and reverse >= 0.5:
                variant["orig"], variant["dest"] = variant.get("dest"), variant.get("orig")
            if route["bound"] in ("O", "I"):
                variant["bound"] = route["bound"]
            variant.pop("directionGuessed", None)
            bounds = dict(variant.get("bounds") or {})
            for operator, aligned in by_operator.items():
                # "O+I" is a joined track, not a direction; it is described in
                # operatorRoutes instead of overwriting the bound map.
                if aligned["route"]["bound"] in ("O", "I"):
                    bounds[operator] = aligned["route"]["bound"]
            if len(set(bounds.values())) > 1 or any(
                    bounds.get(op) != variant["bound"] for op in bounds):
                variant["bounds"] = bounds
            elif "bounds" in variant:
                del variant["bounds"]

        routes_meta = {}
        for operator, aligned in by_operator.items():
            route = aligned["route"]
            meta = {"bound": route["bound"]}
            if route.get("serviceType"):
                meta["serviceType"] = route["serviceType"]
            if route.get("loop"):
                meta["loop"] = True
            if route.get("segments"):
                meta["segments"] = route["segments"]
            routes_meta[operator] = meta
        if routes_meta:
            variant["operatorRoutes"] = routes_meta
        else:
            variant.pop("operatorRoutes", None)

    # Refresh index summaries after native direction correction.
    for variant_id, summary in index_payload.get("variants", {}).items():
        variant = variants.get(variant_id)
        if variant:
            for field in ("bound", "orig", "dest", "co"):
                summary[field] = variant[field]
    for route_entry in index_payload.get("routes", {}).values():
        ids = route_entry.get("variants") or []
        if ids and ids[0] in variants:
            route_entry["orig"] = variants[ids[0]]["orig"]
            route_entry["dest"] = variants[ids[0]]["dest"]

    stop_routes = defaultdict(set)
    for variant_id, variant in variants.items():
        arrays = variant.get("operatorStopCodes") or {}
        if arrays:
            codes = {code for array in arrays.values() for code in array if code}
        else:
            # Native-only and GMB variants carry their own dense display codes.
            codes = {code for code in variant.get("stops") or [] if code}
        for code in codes:
            stop_routes[code].add(variant_id)
    stop_routes = {code: sorted(ids) for code, ids in stop_routes.items()}
    nearby = build_nearby({code: code for code in final_stops}, final_stops, stop_routes)

    # Strict identity invariants.
    seen_native = set()
    for code, group in registry.groups.items():
        active = [(op, stop) for op, stop in group["operators"].items()
                  if stop in native_stops.get(op, {})]
        if len({op for op, _ in active}) != len(active):
            raise RuntimeError("same-company collision in %s" % code)
        for op, stop in active:
            key = native_key(op, stop)
            if key in seen_native:
                raise RuntimeError("native pole has multiple codes: %s" % key)
            seen_native.add(key)
    expected = {native_key(op, stop) for op, stops in native_stops.items() for stop in stops}
    if seen_native != expected:
        raise RuntimeError("not every active native pole has a code")
    if any(len(code) == BUS_CODE_LEN for code, row in final_stops.items()
           if row.get("mode") == "gmb"):
        raise RuntimeError("GMB received a three-character code")

    # Arrays are authoritative and sparse: "unserved" must match their nulls
    # exactly, co-op variants must classify as shared or split, and every
    # published display code must come from an operator's own array.
    for variant_id, variant in variants.items():
        arrays = variant.get("operatorStopCodes") or {}
        if not arrays:
            continue
        display = variant.get("stops") or []
        unserved = variant.get("unserved") or {}
        if len(arrays) > 1:
            if variant.get("codeMode") not in ("shared", "split"):
                raise RuntimeError("co-op variant lacks codeMode: %s" % variant_id)
        elif variant.get("codeMode") in ("shared", "split"):
            raise RuntimeError("single-operator variant has codeMode: %s" % variant_id)
        for operator, array in arrays.items():
            if len(array) != len(display):
                raise RuntimeError("array length mismatch for %s in %s" % (
                    operator, variant_id))
            missing = [index for index, code in enumerate(array) if not code]
            if missing != list(unserved.get(operator) or []):
                raise RuntimeError("unserved mismatch for %s in %s" % (
                    operator, variant_id))
        if variant.get("codeMode") == "shared":
            values = list(arrays.values())
            if any(array != values[0] for array in values[1:]):
                raise RuntimeError("shared variant with differing arrays: %s" % variant_id)
        for position, code in enumerate(display):
            if code is None:
                continue
            if not any(array[position] == code for array in arrays.values()):
                raise RuntimeError("display code absent from operator arrays: %s" % variant_id)

    operators_output = {}
    for operator, stops in native_stops.items():
        rows = {}
        linked_count = 0
        for stop_id in sorted(stops):
            td_rows = links[operator][stop_id]
            primary = primary_link(td_rows)
            linked_count += int(bool(td_rows))
            row = {"code": registry.native_to_code[native_key(operator, stop_id)],
                   "td": primary["td"] if primary else None,
                   "tds": sorted(td_rows),
                   "d": primary["d"] if primary else None,
                   "c": primary["c"] if primary else 0,
                   "m": primary["m"] if primary else "unmatched"}
            rows[stop_id] = row
        operators_output[operator] = {
            "count": len(stops), "matched": len(stops), "ratio": 1.0 if stops else 0,
            "tdLinked": linked_count,
            "tdRatio": round(linked_count / len(stops), 4) if stops else 0,
            "stops": rows}

    output = {"version": args.version or "v" + started.strftime("%Y%m%d-%H%M"),
              "generatedAt": started.isoformat(timespec="seconds"),
              "params": {"identity": "native-pole", "radiusM": MATCH_RADIUS,
                         "autoAcceptM": AUTO_ACCEPT, "nameMin": NAME_MIN,
                         "nameMinFar": 0.5, "busCodeLengths": [3, 4],
                         "gmbCodes": "separate-4-char"},
              "operators": operators_output, "unmatched": unmatched,
              "alignment": {op: dict(row) for op, row in alignment_stats.items()}}
    return {"stops": final_stops, "routeStop": variants, "index": index_payload,
            "stop_routes": stop_routes, "nearby": nearby, "operator_map": output,
            "state": registry.state(), "unmatched": unmatched,
            "alignment": alignment_stats, "mergedGroups": merged,
            "ambiguousTd": len(ambiguous_td),
            "nativeOnlyRoutes": native_only_stats,
            "nativeMergedRoutes": native_merge_stats}


def company_ids(previous_map, code):
    rows = []
    for operator, result in (previous_map.get("operators") or {}).items():
        for operator_stop_id, entry in (result.get("stops") or {}).items():
            if entry.get("code") == code:
                rows.append("%s:%s" % (operator, operator_stop_id))
    return sorted(rows)


def merge_deprecated_report(previous, rows):
    merged = {}
    for line in (previous or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line.split("|", 1)[0].strip():
            merged[line.split("|", 1)[0].strip()] = line
    for code, row in rows.items():
        merged[code] = " | ".join([code, row["td"] or "-",
            (row["en"] or "-").replace("|", "/"), (row["zh"] or "-").replace("|", "/"),
            ",".join(row["companies"]) or "-"])
    return merged


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--static", default="static")
    parser.add_argument("--out", default="")
    parser.add_argument("--overrides", default="overrides/operator_stops.json")
    parser.add_argument("--operators", default=",".join(OPERATORS))
    parser.add_argument("--version", default="")
    parser.add_argument("--previous-dir", default="")
    parser.add_argument("--remote-base", default=REMOTE_BASE)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--no-native-only-routes", dest="native_only_routes",
                        action="store_false",
                        help="do not emit routes that are absent from TD")
    args = parser.parse_args(argv)
    started = datetime.now(timezone.utc)

    required = ("stops.json", "routeStop.json", "index.json")
    payloads = {}
    for name in required:
        path = os.path.join(args.static, name)
        if not os.path.isfile(path):
            raise SystemExit("missing %s: run build_static.py first" % path)
        payloads[name] = load_json(path)
    stops_payload, variants, index_payload = (payloads[name] for name in required)
    if not any(key.startswith(TD_PREFIX) for key in stops_payload):
        raise SystemExit("stops.json is already final; run build_static.py before this pass")

    remote_base = "" if args.offline else args.remote_base
    previous_state = previous_json("__stop_codes.json", args.previous_dir, remote_base) or {}
    previous_stops = previous_json("stops.json", args.previous_dir, remote_base) or {}
    previous_map = previous_json("operator_map.json", args.previous_dir, remote_base) or {}
    previous_td_map = previous_json("__stops.json", args.previous_dir, remote_base) or {}
    previous_variants = previous_json("routeStop.json", args.previous_dir, remote_base) or {}
    previous_td_codes = {str(td): code for td, code in previous_td_map.items()}
    if previous_stops:
        previous_td_codes.update({str(row["td"]): code for code, row in previous_stops.items()
                                  if row.get("mode") == "bus" and row.get("td")})

    overrides = load_json(args.overrides) if args.overrides and os.path.isfile(args.overrides) else {}
    wanted = [name.strip().upper() for name in args.operators.split(",") if name.strip()]
    native_stops, native_routes, issues = {}, {}, {}
    for operator in wanted:
        if operator not in FETCHERS:
            raise SystemExit("unknown operator: %s" % operator)
        log("fetching %s stops" % operator)
        native_stops[operator], problems = FETCHERS[operator]()
        if problems:
            issues[operator] = problems[:50]
        log("  %s native stops=%d" % (operator, len(native_stops[operator])))
        if operator in ROUTE_FETCHERS:
            log("fetching %s route sequences" % operator)
            native_routes[operator], problems = ROUTE_FETCHERS[operator]()
            if problems:
                issues[operator + "_routes"] = problems[:50]
            log("  %s route sequences=%d" % (operator, len(native_routes[operator])))

    result = assemble(args, stops_payload, variants, index_payload, native_stops,
                      native_routes, previous_state, previous_stops, previous_map,
                      previous_td_codes, overrides, started, previous_variants)
    out = args.static
    names = ("stops.json", "routeStop.json", "index.json", "stop_routes.json",
             "nearby.json", "operator_map.json")
    for name in names:
        key = "operator_map" if name == "operator_map.json" else name[:-5]
        write_json(os.path.join(out, name), result[key])
    write_json(os.path.join(out, "__stop_codes.json"), result["state"])
    if args.out:
        write_json(args.out, result["operator_map"])

    # Deprecated public codes remain reserved in the native state file.
    current_stops = result["stops"]
    deprecated = {}
    for code, record in previous_stops.items():
        if code not in current_stops:
            deprecated[code] = {"code": code, "td": str(record.get("td") or ""),
                "en": stop_name(record, "en"), "zh": stop_name(record, "zh"),
                "companies": company_ids(previous_map, code)}
    previous_report = previous_text("deprecated_stop.txt", args.previous_dir, remote_base)
    lines = merge_deprecated_report(previous_report, deprecated)
    report_path = os.path.join(out, "deprecated_stop.txt")
    with open(report_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("# eta6 deprecated stops -- codes stay reserved\n")
        handle.write("# code | td stop id | name_en | name_zh | operator stop ids\n")
        for code in sorted(lines):
            handle.write(lines[code] + "\n")

    native_only_active = sum(1 for variant in variants.values()
                             if is_native_only_variant(variant))
    manifest_path = os.path.join(out, "manifest.json")
    manifest = load_json(manifest_path) if os.path.isfile(manifest_path) else {}
    manifest.setdefault("counts", {}).update({
        "routes": len(result["index"].get("routes", {})),
        "stops": len(current_stops),
        "busStops": sum(row.get("mode") == "bus" for row in current_stops.values()),
        "gmbStops": sum(row.get("mode") == "gmb" for row in current_stops.values()),
        "nearbyPairs": sum(len(rows) for rows in result["nearby"].values()),
        "variants": len(variants),
        "nativeOnlyRoutes": result["nativeOnlyRoutes"]["routes"],
        "nativeOnlyVariants": native_only_active})
    manifest["routeStop"] = {"path": "routeStop.json", "count": len(variants)}
    manifest["nativeOnlyRoutes"] = dict(result["nativeOnlyRoutes"])
    manifest["nativeOnlyRoutes"]["variantsCreated"] = result["nativeOnlyRoutes"]["variants"]
    manifest["nativeOnlyRoutes"]["variantsActive"] = native_only_active
    manifest["nativeMergedRoutes"] = result["nativeMergedRoutes"]
    if result["nativeOnlyRoutes"]["skipped"]:
        manifest.setdefault("warnings", []).extend(
            "native-only route skipped: " + message
            for message in result["nativeOnlyRoutes"]["skipped"][:20])
    manifest["operatorMap"] = {
        "path": "operator_map.json", "params": result["operator_map"]["params"],
        "operators": {name: {key: value for key, value in row.items() if key != "stops"}
                      for name, row in result["operator_map"]["operators"].items()},
        "unmatchedCounts": {name: len(rows) for name, rows in result["unmatched"].items()},
        "alignment": result["operator_map"]["alignment"], "issues": issues}
    manifest["stopCodes"] = {"path": "__stop_codes.json", "model": "native-pole",
        "activeNativePoles": sum(row["count"] for row in result["operator_map"]["operators"].values()),
        "groups": len(result["state"]["groups"]), "tdOnly": len(result["state"]["tdCodes"]),
        "reserved": len(result["state"]["reserved"]),
        "crossCompanyMerges": result["mergedGroups"],
        "ambiguousTdLocations": result["ambiguousTd"],
        "threeCharacterCapacity": BUS_3_CAPACITY,
        "overflow": "automatic 4-character allocation; collisions skipped"}
    manifest["deprecatedStops"] = {"path": "deprecated_stop.txt", "count": len(lines)}
    for name in names + ("__stop_codes.json", "deprecated_stop.txt"):
        path = os.path.join(out, name)
        with open(path, "rb") as handle:
            body = handle.read()
        manifest.setdefault("files", {})[name] = {
            "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    write_json(manifest_path, manifest)

    for operator, row in result["operator_map"]["operators"].items():
        log("  %s coded=%d TD-linked=%d/%d (%.1f%%)" % (
            operator, row["matched"], row["tdLinked"], row["count"], row["tdRatio"] * 100))
    log("native groups=%d TD-only=%d merges=%d ambiguous TD=%d" % (
        len(result["state"]["groups"]), len(result["state"]["tdCodes"]),
        result["mergedGroups"], result["ambiguousTd"]))
    log("native-only routes=%d variants=%d proximity-merges=%d/%d" % (
        result["nativeOnlyRoutes"]["routes"],
        result["nativeOnlyRoutes"]["variants"],
        result["nativeMergedRoutes"]["groups"],
        result["nativeMergedRoutes"]["variants"]))
    log("wrote native-pole dataset -> %s" % os.path.abspath(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
