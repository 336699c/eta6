#!/usr/bin/env python3
"""Merge variants that describe the same physical route.

Two variants are merged only when:

  * the route number is exactly the same -- different route numbers never
    merge, even when the stop sequence is identical; and
  * the ordered stop sequence is exactly the same -- the same route number
    alone is not enough, because one number can cover several services.

Direction letters are not part of the comparison: the same physical trip may
be labelled ``I`` by KMB and ``O`` by CTB. The canonical variant keeps its own
timetable and fares; ``co`` becomes the union of the operators, ``mergedFrom``
records the absorbed ids, and ``bounds`` records per-operator labels when they
disagree.

Usage:
  python merge_routes.py --static static
"""

import argparse
import hashlib
import json
import os
import sys

OPERATOR_ORDER = ["KMB", "LWB", "CTB", "NLB", "GMB", "MTR", "LRTFeeder"]


def log(message):
    print(message, flush=True)


def operator_rank(name):
    try:
        return OPERATOR_ORDER.index(name)
    except ValueError:
        return len(OPERATOR_ORDER)


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True,
                  separators=(",", ":"))
    os.replace(tmp, path)
    with open(path, "rb") as handle:
        return handle.read()


def refresh_manifest(manifest, out, names):
    for name in names:
        with open(os.path.join(out, name), "rb") as handle:
            body = handle.read()
        manifest.setdefault("files", {})[name] = {
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }


def pick_canonical(ids, variants):
    """Most operators first (joint route wins), then the lowest id."""
    return sorted(ids, key=lambda vid: (-len(variants[vid].get("co") or []), vid))[0]


def merge_variants(variants, index, stop_routes):
    groups = {}
    for variant_id, variant in variants.items():
        groups.setdefault((variant["route"], tuple(variant["stops"])), []).append(variant_id)

    merged_into = {}
    merged_groups = 0
    for (route, stops), ids in sorted(
            groups.items(), key=lambda item: (item[0][0], item[0][1])):
        if len(ids) < 2:
            continue
        canonical = pick_canonical(ids, variants)
        others = sorted(variant_id for variant_id in ids if variant_id != canonical)
        target = variants[canonical]

        target["co"] = sorted(
            {name for variant_id in ids for name in variants[variant_id].get("co") or []},
            key=lambda name: (operator_rank(name), name),
        )
        target["mergedFrom"] = others

        bounds = {}
        for variant_id in sorted(ids):
            variant = variants[variant_id]
            for operator in variant.get("co") or []:
                bounds.setdefault(operator, variant["bound"])
        if len(set(bounds.values())) > 1:
            target["bounds"] = bounds

        differing = []
        for variant_id in others:
            other = variants[variant_id]
            if other.get("freq") != target.get("freq"):
                differing.append("%s timetable" % variant_id)
            if other.get("fares") != target.get("fares"):
                differing.append("%s fare" % variant_id)
            merged_into[variant_id] = canonical
        merged_groups += 1
        log("merged %s <- %s (route %s, %d stops)" % (
            canonical, ", ".join(others), route, len(stops)))
        if differing:
            log("  canonical timetable/fare kept; differs: %s" % ", ".join(differing))

    if not merged_into:
        log("no identical routes found")
        return 0, 0

    for variant_id in merged_into:
        variants.pop(variant_id, None)
        index.get("variants", {}).pop(variant_id, None)

    for variant_id, canonical in merged_into.items():
        summary = index.get("variants", {}).get(canonical)
        if summary is not None:
            summary["co"] = variants[canonical]["co"]

    for entry in index.get("routes", {}).values():
        seen = []
        for variant_id in entry.get("variants", []):
            mapped = merged_into.get(variant_id, variant_id)
            if mapped not in seen:
                seen.append(mapped)
        entry["variants"] = seen
        operators = {name for variant_id in seen
                     for name in variants[variant_id].get("co") or []}
        if operators:
            entry["co"] = sorted(operators, key=lambda name: (operator_rank(name), name))

    for code, ids in stop_routes.items():
        stop_routes[code] = sorted({merged_into.get(variant_id, variant_id)
                                    for variant_id in ids})

    return merged_groups, len(merged_into)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--static", default="static")
    args = parser.parse_args(argv)

    variants_path = os.path.join(args.static, "routeStop.json")
    index_path = os.path.join(args.static, "index.json")
    stop_routes_path = os.path.join(args.static, "stop_routes.json")
    manifest_path = os.path.join(args.static, "manifest.json")
    for path in (variants_path, index_path, stop_routes_path):
        if not os.path.isfile(path):
            raise SystemExit("missing %s: run build_static.py first" % path)

    variants = load(variants_path)
    index = load(index_path)
    stop_routes = load(stop_routes_path)

    groups, removed = merge_variants(variants, index, stop_routes)
    if not removed:
        return 0

    write(variants_path, variants)
    write(index_path, index)
    write(stop_routes_path, stop_routes)

    if os.path.isfile(manifest_path):
        manifest = load(manifest_path)
        manifest.setdefault("counts", {})["variants"] = len(variants)
        manifest["routeStop"] = {"path": "routeStop.json", "count": len(variants)}
        manifest["mergedRoutes"] = {"groups": groups, "variants": removed}
        refresh_manifest(manifest, args.static,
                         ("routeStop.json", "index.json", "stop_routes.json"))
        write(manifest_path, manifest)

    log("merged %d groups, removed %d duplicate variants (%d left)" % (
        groups, removed, len(variants)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
