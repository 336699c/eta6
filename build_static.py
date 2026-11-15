#!/usr/bin/env python3
"""Build the canonical static bus dataset from the official TD GTFS feed.

Input : https://static.data.gov.hk/td/pt-headway-en/  (+ -tc for Chinese names)
Output: static/manifest.json, index.json, routeStop.json, stops.json,
        stop_routes.json, nearby.json

routeStop.json holds every variant keyed by variant id (stops, fares, freq);
build diagnostics are folded into manifest.json so the published set stays
small enough to upload with a single GitHub Action.

The build is deterministic: stop codes and variant ids are allocated once and
persisted in state/, so a rebuild never renumbers existing identifiers.

Usage:
  python build_static.py --gtfs-en .cache/gtfs-en --gtfs-tc .cache/gtfs-tc
"""

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"  # A-Z without I/O, then 0-9
ALPHABET_SIZE = len(ALPHABET)  # 34

BUS_CODE_LEN = 3
GMB_CODE_LEN = 4
TD_PREFIX = "TD:"  # intermediate bus key; replaced by build_operator_map.py

INCLUDED_AGENCIES = {
    "KMB", "LWB", "CTB", "NLB", "LRTFeeder", "GMB", "KMB+CTB", "LWB+CTB",
}

FEED_BASE = "https://static.data.gov.hk/td/pt-headway-%s/"
EN_FILES = [
    "agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt",
    "calendar.txt", "calendar_dates.txt", "frequencies.txt",
    "fare_attributes.txt", "fare_rules.txt",
]
TC_FILES = ["stops.txt", "routes.txt"]

LAT_RANGE = (22.10, 22.60)
LON_RANGE = (113.80, 114.50)

NEARBY_RADIUS = 150.0
NEARBY_LIMIT = 10
WALK_SPEED = 1.2  # m/s


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def log(msg):
    print(msg, flush=True)


def fetch(url, retries=4, timeout=120):
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - surface after retries
            last = exc
            time.sleep(min(2 ** attempt, 10))
    raise RuntimeError("download failed: %s (%s)" % (url, last))


def ensure_feed(directory, lang, files):
    os.makedirs(directory, exist_ok=True)
    for name in files:
        path = os.path.join(directory, name)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            continue
        log("downloading %s%s" % (lang, name))
        data = fetch(FEED_BASE % lang + name)
        with open(path, "wb") as handle:
            handle.write(data)


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def write_json(path, payload, compact=True):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":") if compact else None,
    )
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)
    return text.encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def clean_text(value):
    value = value.replace("/<BR>", "\n").replace("<BR>", "\n")
    value = value.split("\n")[0]
    return re.sub(r"\s+", " ", value).strip()


def parse_tagged_name(raw):
    """Split a TD stop_name into (canonical, {operator: name})."""
    tagged = []
    for part in raw.split("|"):
        match = re.match(r"^\[([^\]]+)\]\s*(.*)$", part, re.S)
        if match:
            tagged.append((match.group(1), match.group(2)))
        else:
            tagged.append(("", part))
    operator_names = {}
    for tag, name in tagged:
        if tag:
            operator_names[tag] = clean_text(name)
    joint = [item for item in tagged if "+" in item[0]]
    chosen = joint[0][1] if joint else (tagged[0][1] if tagged else raw)
    return clean_text(chosen), operator_names


def stop_name(record, lang="en"):
    """Canonical display name, accepting old string and new nested maps."""
    value = record.get(lang)
    if isinstance(value, dict):
        if value.get("default"):
            return value["default"]
        for name in value.values():
            if name:
                return name
        return ""
    return value or ""


def stop_company_names(record):
    value = record.get("en")
    if isinstance(value, dict):
        return {key: name for key, name in value.items()
                if key != "default" and name}
    return dict(record.get("ops") or {})


def nested_names(value, fallback=""):
    if isinstance(value, dict):
        names = {key: name for key, name in value.items() if name}
    elif value:
        names = {"default": value}
    else:
        names = {}
    if not names.get("default"):
        names["default"] = fallback or next(iter(names.values()), "")
    return names


def stop_td_ids(record):
    values = record.get("tds")
    if values:
        return {str(value) for value in values}
    td = record.get("td")
    return {str(td)} if td else set()


def normalize_name(value):
    value = value.upper()
    value = re.sub(r"\(.*?\)", " ", value)
    value = re.sub(r"[^A-Z0-9 ]", " ", value)
    for word in ("BUS TERMINUS", "PUBLIC TRANSPORT INTERCHANGE", "PTI",
                 "TERMINUS", "BUS STOP", "BUS"):
        value = value.replace(word, " ")
    return " ".join(value.split())


def name_score(a, b):
    left, right = set(normalize_name(a).split()), set(normalize_name(b).split())
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def code_from_index(index, length):
    chars = []
    for _ in range(length):
        chars.append(ALPHABET[index % ALPHABET_SIZE])
        index //= ALPHABET_SIZE
    return "".join(reversed(chars))


def index_from_code(code):
    index = 0
    for char in code:
        index = index * ALPHABET_SIZE + ALPHABET.index(char)
    return index


def distance_m(a, b):
    dy = (a[0] - b[0]) * 111320.0
    dx = (a[1] - b[1]) * 102000.0
    return math.hypot(dx, dy)


def hhmm(value):
    return value[:5]


def headway_minutes(seconds):
    minutes = round(float(seconds) / 60.0, 1)
    return int(minutes) if minutes == int(minutes) else minutes


def route_slug(value):
    slug = re.sub(r"[^A-Za-z0-9]", "", value)
    return slug or "X"


# --------------------------------------------------------------------------
# previous round (published on the data branch)
# --------------------------------------------------------------------------

REMOTE_BASE = (
    "https://raw.githubusercontent.com/336699c/eta6/refs/heads/data/static/"
)


def previous_bytes(name, previous_dir="", remote_base=REMOTE_BASE):
    """Read a file from the previous round: local dir first, then raw GitHub."""
    if previous_dir:
        path = os.path.join(previous_dir, name)
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                return handle.read()
        return None
    if not remote_base:
        return None
    url = remote_base.rstrip("/") + "/" + name
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "eta6/2.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except Exception:  # noqa: BLE001 - a missing file is normal on the first run
        return None


def previous_json(name, previous_dir="", remote_base=REMOTE_BASE):
    body = previous_bytes(name, previous_dir, remote_base)
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log("ignoring unreadable previous %s" % name)
        return None


def previous_text(name, previous_dir="", remote_base=REMOTE_BASE):
    body = previous_bytes(name, previous_dir, remote_base)
    return body.decode("utf-8") if body else ""


class StopMapper:
    """td stop id -> public code, round-tripped through static/__stops.json.

    Codes are never reused and never removed: a stop that disappears keeps its
    reservation so it gets the same code if it returns.
    """

    def __init__(self, mapping=None):
        self.mapping = {str(td): str(code)
                        for td, code in (mapping or {}).items() if code}
        self.used = set(self.mapping.values())
        self.next_index = {"bus": 0, "gmb": 0}
        for code in self.used:
            if len(code) == BUS_CODE_LEN:
                mode = "bus"
            elif len(code) == GMB_CODE_LEN:
                mode = "gmb"
            else:
                continue
            self.next_index[mode] = max(self.next_index[mode],
                                        index_from_code(code) + 1)

    def allocate(self, mode, td_stop_id):
        existing = self.mapping.get(td_stop_id)
        if existing:
            return existing, False
        length = BUS_CODE_LEN if mode == "bus" else GMB_CODE_LEN
        while True:
            code = code_from_index(self.next_index[mode], length)
            self.next_index[mode] += 1
            if code not in self.used:
                break
        self.mapping[td_stop_id] = code
        self.used.add(code)
        return code, True

    def reserved(self, active_ids):
        return {td: code for td, code in self.mapping.items()
                if td not in active_ids}


class VariantIdMapper:
    """stableKey -> variant id, seeded from the previous routeStop.json."""

    def __init__(self, mapping=None):
        self.mapping = dict(mapping or {})
        self.used = set(self.mapping.values())

    def assign(self, stable_key, preferred):
        if stable_key in self.mapping:
            return self.mapping[stable_key], False
        candidate = preferred
        suffix = 1
        while candidate in self.used:
            suffix += 1
            candidate = "%s_%d" % (preferred, suffix)
        self.mapping[stable_key] = candidate
        self.used.add(candidate)
        return candidate, True


# --------------------------------------------------------------------------
# ingestion
# --------------------------------------------------------------------------

def load_stops(en_dir, tc_dir):
    stops = {}
    for row in read_csv(os.path.join(en_dir, "stops.txt")):
        name, operator_names = parse_tagged_name(row["stop_name"])
        en_names = {"default": name}
        en_names.update({operator: value for operator, value in operator_names.items()
                         if value})
        stops[row["stop_id"]] = {
            "en": en_names,
            "zh": {"default": ""},
            "loc": [float(row["stop_lat"]), float(row["stop_lon"])],
        }
    for row in read_csv(os.path.join(tc_dir, "stops.txt")):
        record = stops.get(row["stop_id"])
        if record is None:
            continue
        name, operator_names = parse_tagged_name(row["stop_name"])
        zh_names = {"default": name}
        zh_names.update({operator: value for operator, value in operator_names.items()
                         if value})
        record["zh"] = zh_names
    return stops


def load_routes(en_dir, tc_dir):
    routes = {}
    for row in read_csv(os.path.join(en_dir, "routes.txt")):
        agency = row["agency_id"]
        if agency not in INCLUDED_AGENCIES:
            continue
        routes[row["route_id"]] = {
            "agency": agency,
            "route": row["route_short_name"].strip(),
            "longEn": clean_text(row["route_long_name"]),
            "longZh": "",
        }
    for row in read_csv(os.path.join(tc_dir, "routes.txt")):
        record = routes.get(row["route_id"])
        if record is not None:
            record["longZh"] = clean_text(row["route_long_name"])
    return routes


def load_trips(en_dir, route_ids):
    trip_route, trip_service = {}, {}
    for row in read_csv(os.path.join(en_dir, "trips.txt")):
        if row["route_id"] not in route_ids:
            continue
        trip_route[row["trip_id"]] = row["route_id"]
        trip_service[row["trip_id"]] = row["service_id"]
    return trip_route, trip_service


def load_stop_times(en_dir, trip_ids):
    sequences = defaultdict(list)
    for row in read_csv(os.path.join(en_dir, "stop_times.txt")):
        trip_id = row["trip_id"]
        if trip_id not in trip_ids:
            continue
        sequences[trip_id].append((int(row["stop_sequence"]), row["stop_id"]))
    for trip_id in sequences:
        sequences[trip_id].sort()
    return {trip_id: tuple(stop for _, stop in rows)
            for trip_id, rows in sequences.items()}


def load_fares(en_dir, route_ids):
    prices = {}
    for row in read_csv(os.path.join(en_dir, "fare_attributes.txt")):
        prices[row["fare_id"]] = float(row["price"])
    fares = defaultdict(dict)
    for row in read_csv(os.path.join(en_dir, "fare_rules.txt")):
        route_id = row["route_id"]
        if route_id not in route_ids:
            continue
        price = prices.get(row["fare_id"])
        if price is None:
            continue
        fares[route_id][(row["origin_id"], row["destination_id"])] = price
    return fares


def load_frequencies(en_dir, trip_ids):
    rows = defaultdict(list)
    for row in read_csv(os.path.join(en_dir, "frequencies.txt")):
        trip_id = row["trip_id"]
        if trip_id not in trip_ids:
            continue
        rows[trip_id].append(
            (hhmm(row["start_time"]), hhmm(row["end_time"]), int(row["headway_secs"]))
        )
    return rows


def load_calendar(en_dir):
    services = {}
    for row in read_csv(os.path.join(en_dir, "calendar.txt")):
        services[row["service_id"]] = {
            "days": [int(row[day]) for day in
                     ("monday", "tuesday", "wednesday", "thursday",
                      "friday", "saturday", "sunday")],
            "start": row["start_date"],
            "end": row["end_date"],
            "exceptions": {},
        }
    holiday_votes = defaultdict(int)
    for row in read_csv(os.path.join(en_dir, "calendar_dates.txt")):
        service = services.get(row["service_id"])
        if service is None:
            continue
        service["exceptions"][row["date"]] = int(row["exception_type"])
        if int(row["exception_type"]) == 2 and any(service["days"][:5]):
            holiday_votes[row["date"]] += 1
    holidays = sorted(date for date, votes in holiday_votes.items() if votes >= 3)
    return services, holidays


def load_first_departures(en_dir, trip_ids):
    departures = {}
    for row in read_csv(os.path.join(en_dir, "stop_times.txt")):
        if row["stop_sequence"] != "1" or row["trip_id"] not in trip_ids:
            continue
        value = row["departure_time"] or row["arrival_time"]
        if value:
            departures[row["trip_id"]] = hhmm(value)
    return departures


# --------------------------------------------------------------------------
# patterns / variants
# --------------------------------------------------------------------------

def build_patterns(routes, trip_route, trip_service, trip_stops):
    patterns = defaultdict(dict)
    for trip_id, route_id in trip_route.items():
        sequence = trip_stops.get(trip_id)
        if not sequence:
            continue
        group = patterns[route_id].setdefault(
            sequence, {"stops": sequence, "trips": [], "services": set()}
        )
        group["trips"].append(trip_id)
        group["services"].add(trip_service[trip_id])
    return patterns


def parse_long_name(long_en, long_zh):
    match = re.match(r"^(.*?)\s+-\s+(.*)$", long_en)
    if not match:
        return None, None, None, None, False
    origin_en, dest_en = match.group(1).strip(), match.group(2).strip()
    origin_zh = dest_zh = ""
    zh_match = re.match(r"^(.*?)\s+-\s+(.*)$", long_zh or "")
    if zh_match:
        origin_zh, dest_zh = zh_match.group(1).strip(), zh_match.group(2).strip()
    circular = "CIRCULAR" in dest_en.upper()
    dest_en = re.sub(r"\s*\(CIRCULAR\)\s*$", "", dest_en, flags=re.I)
    if dest_zh:
        dest_zh = re.sub(r"\s*[（(]循環線[）)]\s*$", "", dest_zh)
    return origin_en, dest_en, origin_zh, dest_zh, circular


def describe_route(route, groups, stop_names_en):
    """Return per-pattern orientation metadata."""
    long_en = route["longEn"]
    parsed = parse_long_name(long_en, route["longZh"])
    single = len(groups) == 1

    if parsed[0] is None:
        origin_en = dest_en = origin_zh = dest_zh = ""
        circular = False
    else:
        origin_en, dest_en, origin_zh, dest_zh, circular = parsed

    if not origin_en:
        first = groups[0]["stops"][0]
        last = groups[0]["stops"][-1]
        origin_en = stop_names_en.get(first, first)
        dest_en = stop_names_en.get(last, dest_en or last)
        circular = first == last

    result = []
    if single:
        group = groups[0]
        first_name = stop_names_en.get(group["stops"][0], "")
        forward = name_score(first_name, origin_en) >= name_score(first_name, dest_en)
        if circular:
            chosen_orig, chosen_dest = origin_en, origin_en
            chosen_orig_zh = chosen_dest_zh = origin_zh or origin_en
        elif forward:
            chosen_orig, chosen_dest = origin_en, dest_en
            chosen_orig_zh, chosen_dest_zh = origin_zh, dest_zh
        else:
            chosen_orig, chosen_dest = dest_en, origin_en
            chosen_orig_zh, chosen_dest_zh = dest_zh, origin_zh
        result.append({
            "group": group, "bound": "O",
            "orig": {"en": chosen_orig, "zh": chosen_orig_zh or chosen_orig},
            "dest": {"en": chosen_dest, "zh": chosen_dest_zh or chosen_dest},
            "circular": circular, "resolved": True,
        })
        return result

    scored = []
    for group in groups:
        first_name = stop_names_en.get(group["stops"][0], "")
        last_name = stop_names_en.get(group["stops"][-1], "")
        forward = name_score(first_name, origin_en) + name_score(last_name, dest_en)
        reverse = name_score(first_name, dest_en) + name_score(last_name, origin_en)
        scored.append((forward, reverse, group))
    forward_group = max(scored, key=lambda item: (item[0] - item[1], len(item[2]["trips"])))
    if forward_group[0] == forward_group[1]:
        forward_group = max(scored, key=lambda item: len(item[2]["trips"]))
    reverse_group = next(item for item in scored if item[2] is not forward_group[2])
    resolved = forward_group[0] != reverse_group[0]

    result.append({
        "group": forward_group[2], "bound": "O",
        "orig": {"en": origin_en, "zh": origin_zh or origin_en},
        "dest": {"en": dest_en, "zh": dest_zh or dest_en},
        "circular": False, "resolved": resolved,
    })
    result.append({
        "group": reverse_group[2], "bound": "I",
        "orig": {"en": dest_en, "zh": dest_zh or dest_en},
        "dest": {"en": origin_en, "zh": origin_zh or origin_en},
        "circular": False, "resolved": resolved,
    })
    return result


def compress_runs(values):
    """values: list of (index, price). Returns [[first, last, price], ...]."""
    runs = []
    for index, price in values:
        if runs and runs[-1][2] == price and runs[-1][1] + 1 == index:
            runs[-1][1] = index
        else:
            runs.append([index, index, price])
    return runs


def build_fares(pattern, fare_map):
    size = len(pattern)
    matrix = []
    for origin in range(size):
        row = []
        for destination in range(origin + 1, size):
            row.append(fare_map.get((pattern[origin], pattern[destination])))
        matrix.append(row)

    prices = sorted({price for row in matrix for price in row if price is not None})
    price_index = {price: index for index, price in enumerate(prices)}

    def encode(rows):
        encoded = []
        for row in rows:
            runs = compress_runs(row)
            encoded.append([[first, last, price_index[price] if price is not None else -1]
                            for first, last, price in runs])
        return encoded

    if len(prices) <= 1:
        return {"currency": "HKD", "kind": "flat",
                "flat": prices[0] if prices else None}

    rows = []
    for origin in range(size - 1):
        rows.append([(destination, matrix[origin][destination - origin - 1])
                     for destination in range(origin + 1, size)])

    ladder_ok = True
    for destination in range(1, size):
        column = {matrix[origin][destination - origin - 1] for origin in range(destination)}
        if len(column) > 1:
            ladder_ok = False
            break

    if ladder_ok:
        ladder = compress_runs([(destination, matrix[0][destination - 1])
                                for destination in range(1, size)])
        return {
            "currency": "HKD", "kind": "ladder",
            "prices": prices,
            "ladder": [[a, b, price_index[p] if p is not None else -1]
                       for a, b, p in ladder],
        }

    return {"currency": "HKD", "kind": "matrix", "prices": prices,
            "rows": encode(rows)}


def build_freq(group, trip_service, frequencies, first_departures):
    bands = defaultdict(dict)
    templated = set()
    for trip_id in group["trips"]:
        service = trip_service[trip_id]
        for start, end, headway in frequencies.get(trip_id, []):
            bands[service][start] = [end, headway_minutes(headway)]
            templated.add(service)
    freq = {
        service: [[start, end_headway[0], end_headway[1]]
                  for start, end_headway in sorted(row.items())]
        for service, row in bands.items()
    }
    departures = defaultdict(list)
    for trip_id in group["trips"]:
        service = trip_service[trip_id]
        if service in templated:
            continue
        departure = first_departures.get(trip_id)
        if departure:
            departures[service].append(departure)
    fixed = {service: sorted(set(values)) for service, values in departures.items()}
    return freq, fixed


# --------------------------------------------------------------------------
# nearby stops
# --------------------------------------------------------------------------

def build_nearby(codes_by_td, stops, stop_routes):
    cell = 0.002
    grid = defaultdict(list)
    points = []
    td_codes = defaultdict(set)
    for td_id in codes_by_td:
        record = stops.get(td_id)
        if record is None:
            continue
        code = codes_by_td[td_id]
        entry = (code, record["loc"], record)
        points.append(entry)
        grid[(int(record["loc"][0] / cell), int(record["loc"][1] / cell))].append(entry)
        for td in stop_td_ids(record):
            td_codes[td].add(code)

    by_code = {code: (loc, record) for code, loc, record in points}
    nearby = {}
    for code, loc, record in points:
        row_key, col_key = int(loc[0] / cell), int(loc[1] / cell)
        td_ids = stop_td_ids(record)
        alias_codes = set()
        for td in td_ids:
            alias_codes.update(td_codes.get(td, ()))
        alias_codes.discard(code)

        candidates = []
        seen = set(alias_codes)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                for other_code, other_loc, other in grid[(row_key + dr, col_key + dc)]:
                    if other_code == code or other_code in seen:
                        continue
                    dist = distance_m(loc, other_loc)
                    if dist <= NEARBY_RADIUS:
                        candidates.append((dist, other_code, other))
                        seen.add(other_code)
        candidates.sort(key=lambda item: item[0])

        selected = []
        for other_code in alias_codes:
            other_loc, other = by_code[other_code]
            selected.append((distance_m(loc, other_loc), other_code, other, True))
        limit = max(NEARBY_LIMIT, len(selected))
        for dist, other_code, other in candidates:
            if len(selected) >= limit:
                break
            selected.append((dist, other_code, other, False))
        selected.sort(key=lambda item: item[0])

        keys = set(stop_routes.get(code, []))
        same_name = stop_name(record, "en")
        rows = []
        for dist, other_code, other, same_stop in selected:
            same = normalize_name(same_name) == normalize_name(stop_name(other, "en")) != ""
            shared = bool(keys & set(stop_routes.get(other_code, [])))
            flags = 0
            if same:
                flags |= 1
            if same and dist <= 80 and shared:
                flags |= 2
            if (record.get("mode") != other.get("mode")):
                flags |= 4
            if same_stop:
                flags |= 8
            rows.append([other_code, int(round(dist)), int(round(dist / WALK_SPEED)), flags])
        if rows:
            nearby[code] = rows
    return nearby


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--gtfs-en", default=".cache/gtfs-en")
    parser.add_argument("--gtfs-tc", default=".cache/gtfs-tc")
    parser.add_argument("--out", default="static")
    parser.add_argument("--version", default="")
    parser.add_argument("--valid-from", default="")
    parser.add_argument("--limit-routes", type=int, default=0)
    parser.add_argument("--previous-dir", default="",
                        help="local directory with the previous round outputs")
    parser.add_argument("--remote-base", default=REMOTE_BASE)
    parser.add_argument("--offline", action="store_true",
                        help="do not read the previous round from GitHub")
    args = parser.parse_args(argv)

    started = datetime.now(timezone.utc)
    ensure_feed(args.gtfs_en, "en", EN_FILES)
    ensure_feed(args.gtfs_tc, "tc", TC_FILES)

    log("loading stops / routes")
    stops = load_stops(args.gtfs_en, args.gtfs_tc)
    routes = load_routes(args.gtfs_en, args.gtfs_tc)
    if args.limit_routes:
        routes = dict(list(routes.items())[:args.limit_routes])
    log("  stops=%d routes=%d" % (len(stops), len(routes)))

    trip_route, trip_service = load_trips(args.gtfs_en, set(routes))
    log("  trips=%d" % len(trip_route))
    trip_stops = load_stop_times(args.gtfs_en, set(trip_route))
    log("  stop-time trips=%d" % len(trip_stops))

    fares = load_fares(args.gtfs_en, set(routes))
    frequencies = load_frequencies(args.gtfs_en, set(trip_route))
    first_departures = load_first_departures(args.gtfs_en, set(trip_route))
    services, holidays = load_calendar(args.gtfs_en)
    log("  services=%d holidays=%d" % (len(services), len(holidays)))

    patterns = build_patterns(routes, trip_route, trip_service, trip_stops)
    log("  routes with trips=%d" % len(patterns))

    # Which physical stops are used, and by which mode.
    used_stops = defaultdict(set)
    for route_id, groups in patterns.items():
        agency = routes[route_id]["agency"]
        for group in groups.values():
            for stop_id in group["stops"]:
                used_stops[stop_id].add("gmb" if agency == "GMB" else "bus")

    remote_base = "" if args.offline else args.remote_base
    previous_map = previous_json("__stops.json", args.previous_dir, remote_base)
    previous_stops = previous_json("stops.json", args.previous_dir, remote_base)
    if previous_stops:
        # Bus codes now belong to native operator poles and are restored by
        # build_operator_map.py.  This mapper only reserves minibus codes.
        previous_gmb = {
            str(record["td"]): code
            for code, record in previous_stops.items()
            if record.get("mode") == "gmb" and record.get("td")
        }
    else:
        # Legacy fallback: before the native-pole model only GMB used 4 chars.
        previous_gmb = {
            str(td): code for td, code in (previous_map or {}).items()
            if len(code) == GMB_CODE_LEN
        }
    stop_mapper = StopMapper(previous_gmb)
    if previous_gmb:
        log("  GMB stop mapper loaded (%d codes)" % len(previous_gmb))
    else:
        log("  no previous GMB stop mapper: allocating fresh codes")

    previous_variants = previous_json(
        "routeStop.json", args.previous_dir, remote_base) or {}
    variant_mapper = VariantIdMapper({
        variant["stableKey"]: variant_id
        for variant_id, variant in previous_variants.items()
        if variant.get("stableKey")
    })
    if previous_variants:
        log("  variant mapper loaded (%d ids)" % len(variant_mapper.mapping))

    codes_by_td = {}
    modes = {}
    new_codes = 0
    for stop_id in sorted(used_stops):
        mode = "bus" if "bus" in used_stops[stop_id] else "gmb"
        if mode == "gmb":
            code, created = stop_mapper.allocate(mode, stop_id)
            new_codes += int(created)
        else:
            # Native poles, not TD location groups, receive public bus codes in
            # phase 2.  A prefixed TD key keeps this intermediate unambiguous.
            code = TD_PREFIX + stop_id
        codes_by_td[stop_id] = code
        modes[stop_id] = mode
    active_gmb = {td for td, mode in modes.items() if mode == "gmb"}
    reserved = stop_mapper.reserved(active_gmb)
    log("  TD stops=%d GMB codes new=%d reserved=%d" % (
        len(codes_by_td), new_codes, len(reserved)))

    # Route-number collisions (GMB districts, TD service-type splits) require
    # an ordinal so each TD route id keeps a distinct public variant id.
    per_label = defaultdict(set)
    for route_id, route in routes.items():
        per_label[(route["agency"].split("+")[0], route["route"])].add(route_id)
    group_ordinal = {}
    for ids in per_label.values():
        for position, route_id in enumerate(sorted(ids)):
            group_ordinal[route_id] = position + 1

    stop_names_en = {stop_id: stop_name(record, "en")
                     for stop_id, record in stops.items()}

    variants = {}
    route_index = {}
    stop_routes = defaultdict(set)
    warnings = []
    fare_kinds = defaultdict(int)
    unresolved = 0
    for route_id in sorted(patterns):
        route = routes[route_id]
        groups = list(patterns[route_id].values())
        groups.sort(key=lambda group: (-len(group["trips"]), group["stops"][0]))
        described = describe_route(route, groups, stop_names_en)
        primary = route["agency"].split("+")[0]
        label = route["route"]
        collides = len(per_label[(primary, label)]) > 1
        slug = route_slug(label)
        ordinal = group_ordinal[route_id]
        index_id = "%s_%s_%d" % (primary, slug, ordinal) if collides else "%s_%s" % (primary, slug)
        route_key = route_id

        variant_ids = []
        counters = defaultdict(int)
        for item in described:
            group = item["group"]
            counters[item["bound"]] += 1
            stable_key = "%s|%s|%s|%s|%s" % (
                primary, label, item["bound"], item["group"]["stops"][0],
                item["group"]["stops"][-1])
            if len(item["group"]["stops"]) > 2:
                stable_key += "|" + hashlib.sha1(
                    ",".join(item["group"]["stops"]).encode("utf-8")
                ).hexdigest()[:16]
            if primary == "GMB":
                preferred = "GMB_%s_%s%d" % (route_id, item["bound"], counters[item["bound"]])
            elif collides:
                preferred = "%s_%s_%s%d" % (primary, slug, item["bound"], ordinal)
            else:
                preferred = "%s_%s_%s%d" % (primary, slug, item["bound"],
                                            counters[item["bound"]])
            variant_id, _ = variant_mapper.assign(stable_key, preferred)

            fare_map = fares.get(route_id, {})
            codes = [codes_by_td[stop_id] for stop_id in group["stops"]]
            freq, fixed = build_freq(group, trip_service, frequencies, first_departures)
            for code in codes:
                stop_routes[code].add(variant_id)

            variant = {
                "id": variant_id,
                "co": route["agency"].split("+"),
                "route": label,
                "bound": item["bound"],
                "tdRouteId": route_id,
                "stableKey": stable_key,
                "orig": item["orig"],
                "dest": item["dest"],
                "circular": item["circular"],
                "stops": codes,
                "freq": freq,
                "fixedDepartures": fixed,
                "fares": build_fares(group["stops"], fare_map),
                "hasHolidayFare": False,
                "sources": {
                    "static": "TD_GTFS",
                    "validFrom": args.valid_from or None,
                    "generatedAt": started.isoformat(timespec="seconds"),
                },
            }
            if not item["resolved"]:
                unresolved += 1
                variant["directionGuessed"] = True
            fare_kinds[variant["fares"]["kind"]] += 1
            variants[variant_id] = variant
            variant_ids.append(variant_id)

        if not variant_ids:
            continue
        route_index[route_key] = {
            "co": route["agency"].split("+"),
            "route": label,
            "orig": described[0]["orig"],
            "dest": described[0]["dest"],
            "variants": variant_ids,
            "id": index_id,
        }

    # Build output payloads.
    log("building stop records")
    stop_payload = {}
    for stop_id, code in codes_by_td.items():
        record = stops.get(stop_id)
        if record is None:
            warnings.append("stop %s used but missing from stops.txt" % stop_id)
            continue
        lat, lon = record["loc"]
        if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1]
                and LON_RANGE[0] <= lon <= LON_RANGE[1]):
            warnings.append("stop %s outside HK bounds: %s" % (code, record["loc"]))
        en_names = nested_names(record.get("en"))
        zh_names = nested_names(record.get("zh"), en_names["default"])
        entry = {
            "td": stop_id,
            "mode": modes[stop_id],
            "en": en_names,
            "zh": zh_names,
            "loc": [round(lat, 5), round(lon, 5)],
        }
        stop_payload[code] = entry

    log("building nearby pairs")
    for stop_id, mode in modes.items():
        if stop_id in stops:
            stops[stop_id]["mode"] = mode
    nearby = build_nearby(codes_by_td, stops, stop_routes)
    stop_routes_payload = {code: sorted(ids) for code, ids in stop_routes.items()}

    # Validate before writing.
    duplicate_codes = len(codes_by_td) - len(set(codes_by_td.values()))
    if duplicate_codes:
        raise SystemExit("duplicate stop codes: %d" % duplicate_codes)
    short_variants = [vid for vid, variant in variants.items() if len(variant["stops"]) < 2]
    if short_variants:
        warnings.append("%d variants have fewer than 2 stops" % len(short_variants))
    ids = [variant["id"] for variant in variants.values()]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate variant ids")

    missing_fare = [
        vid for vid, variant in variants.items()
        if variant["fares"].get("kind") == "flat" and variant["fares"].get("flat") is None
    ]
    if missing_fare:
        warnings.append("%d variants have no fare data: %s" % (
            len(missing_fare), ", ".join(sorted(missing_fare)[:5])))

    version = args.version or "v" + started.strftime("%Y%m%d-%H%M")
    manifest = {
        "version": version,
        "builtAt": started.isoformat(timespec="seconds"),
        "source": {
            "dataset": "TD Headway information of public transport services (GTFS)",
            "feed": FEED_BASE % "en",
            "validFrom": args.valid_from or None,
            "languages": ["en", "tc"],
        },
        "counts": {
            "routes": len(route_index),
            "variants": len(variants),
            "stops": len(stop_payload),
            "busStops": sum(1 for record in stop_payload.values() if record["mode"] == "bus"),
            "gmbStops": sum(1 for record in stop_payload.values() if record["mode"] == "gmb"),
            "nearbyPairs": sum(len(rows) for rows in nearby.values()),
        },
        "files": {},
        "warnings": warnings[:50],
    }

    out = args.out
    os.makedirs(out, exist_ok=True)

    def emit(path, payload, compact=True, indexable=True):
        body = write_json(path, payload, compact=compact)
        name = os.path.relpath(path, out).replace(os.sep, "/")
        if indexable:
            manifest["files"][name] = {"bytes": len(body), "sha256": sha256(body)}
        return len(body)

    log("writing artifacts")
    index = {
        "version": version,
        "routes": route_index,
        "variants": {
            variant_id: {
                "co": variant["co"],
                "route": variant["route"],
                "bound": variant["bound"],
                "orig": variant["orig"],
                "dest": variant["dest"],
                "circular": variant["circular"],
                "stops": len(variant["stops"]),
                "services": sorted(variant["freq"].keys() | variant["fixedDepartures"].keys()),
                "freq": variant["freq"],
            }
            for variant_id, variant in variants.items()
        },
        "services": services,
        "holidays": holidays,
    }
    emit(os.path.join(out, "index.json"), index)
    emit(os.path.join(out, "stops.json"), stop_payload)
    emit(os.path.join(out, "stop_routes.json"), stop_routes_payload)
    emit(os.path.join(out, "nearby.json"), nearby)

    # One file for every variant keeps the publish list manageable.
    emit(os.path.join(out, "routeStop.json"), variants)

    # GMB reservations stay here; native bus-pole groups are published
    # separately as __stop_codes.json by build_operator_map.py.
    emit(os.path.join(out, "__stops.json"), stop_mapper.mapping)

    report = {
        "version": version,
        "builtAt": started.isoformat(timespec="seconds"),
        "durationSeconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        "counts": manifest["counts"],
        "fareKinds": dict(fare_kinds),
        "unresolvedDirections": unresolved,
        "newStopCodes": new_codes,
        "holidays": holidays,
        "warnings": warnings,
    }

    # Compare with the previous round to catch accidental regressions.
    previous_manifest = previous_json(
        "manifest.json", args.previous_dir, remote_base) or {}
    before = previous_manifest.get("counts") or {}
    if before:
        deltas = {}
        native_previous = (previous_manifest.get("stopCodes") or {}).get("model") == "native-pole"
        for key, value in manifest["counts"].items():
            if native_previous and key in ("stops", "busStops", "nearbyPairs"):
                continue  # phase 2 expands TD locations into native poles
            old = before.get(key, 0)
            if old and abs(value - old) > max(5, old * 0.1):
                deltas[key] = {"previous": old, "current": value}
        if deltas:
            report["largeDeltas"] = deltas
            warnings.append("large count deltas: %s" % deltas)

    manifest["routeStop"] = {
        "path": "routeStop.json",
        "count": len(variants),
    }
    manifest["build"] = {
        key: report[key]
        for key in ("durationSeconds", "fareKinds", "unresolvedDirections",
                    "newStopCodes", "largeDeltas")
        if key in report
    }
    manifest["build"]["reservedCodes"] = len(reserved)

    emit(os.path.join(out, "manifest.json"), manifest)

    log(json.dumps(report["counts"], indent=2))
    log("fare kinds: %s" % dict(fare_kinds))
    if warnings:
        log("warnings:")
        for warning in warnings[:20]:
            log("  - %s" % warning)
    log("done in %ss -> %s" % (report["durationSeconds"], os.path.abspath(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
