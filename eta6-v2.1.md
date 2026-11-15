# eta6 v2.1 — per-company stop names and same-stop nearby aliases

Date: 2026-11-15

This release contains two data-model fixes:

1. `stops.json` now stores per-company display names for **both** English and
   Chinese.
2. `nearby.json` has a new flag `8` for public codes that are aliases of the
   same canonical TD stop.

---

## 1. Per-company names in `stops.json`

### Before

The canonical names were strings, and only English had a per-company map:

```json
{
  "en": "PO LAM BUS TERMINUS",
  "zh": "寶林總站",
  "ops": {
    "CTB": "PO LAM STATION",
    "KMB": "PO LAM BUS TERMINUS"
  }
}
```

The Chinese per-company names parsed from the TD `-tc` feed were discarded.

### After

Both `en` and `zh` are now maps. `default` is the canonical display name; the
other keys are the TD operator tags.

```json
{
  "en": {
    "default": "PO LAM BUS TERMINUS",
    "CTB": "PO LAM STATION",
    "KMB": "PO LAM BUS TERMINUS",
    "KMB+CTB": "PO LAM BUS TERMINUS"
  },
  "zh": {
    "default": "寶林總站",
    "CTB": "寶琳站",
    "KMB": "寶林總站",
    "KMB+CTB": "寶林總站"
  }
}
```

The old `ops` field has been removed. Company names now live inside `en` and
`zh`, so the two languages follow the same structure.

### Implementation

* `build_static.py`
  * `load_stops()` keeps the EN and TC per-company maps separate instead of
    merging TC names into the English map.
  * Added helpers `stop_name()`, `stop_company_names()`, `nested_names()` and
    `stop_td_ids()`. These accept both the old string form and the new nested
    form, so previous-round files remain readable.
  * Route descriptions, nearby name matching and stop output use the canonical
    `default` name.
* `build_operator_map.py`
  * Name matching and joint-route detection read company keys from the nested
    `en` map.
  * Final native-pole records start from the TD names and add native operator
    names when they are missing.
  * The deprecated-stop report reads canonical names through `stop_name()`.

---

## 2. Nearby flag `8` — same canonical TD stop

### Rule

Two public codes receive `flags |= 8` when their `tds` lists intersect. They are
aliases of the same canonical TD stop, even though the native-pole allocator
gave them different public codes.

The existing flags are unchanged:

| Flag | Meaning |
|---:|---|
| `1` | Same normalised name |
| `2` | Same name, within 80 m, and a shared route |
| `4` | Different mode |
| `8` | Same canonical TD stop (`tds` intersection) |

Flags combine. For example, flag `9` means the stops have both the same name
(`1`) and the same TD identity (`8`).

### Row retention

Same-stop aliases are always kept in `nearby[code]`, even when they are beyond
the normal 150 m radius or would exceed `NEARBY_LIMIT`. Non-alias candidates
then fill the remaining slots up to `max(NEARBY_LIMIT, number_of_aliases)`.
Rows are sorted by distance in the final output.

This is necessary because the allocator can intentionally leave several native
poles as separate codes when one company has multiple candidates for the same
TD stop. On the previous published dataset, 1,991 TD ids were shared by more
than one code and 1,053 same-TD pairs were missing from `nearby.json` because of
the old radius/row limits.

---

## 3. Build verification

Build performed with the current TD GTFS cache and the previous published
`data` branch as the previous round:

```bash
python build_static.py --out <out> --previous-dir <prev> --offline
python merge_routes.py --static <out>
python build_operator_map.py --static <out> --previous-dir <prev> --offline \
  --overrides overrides/operator_stops.json
```

Result:

* stops: 13,874 (9,102 bus, 4,772 GMB)
* routes: 2,454
* variants: 3,585
* nearby pairs: 101,525 (previous published count: 100,650)
* native groups: 9,033
* TD-only codes: 69
* cross-company merges: 0
* ambiguous TD locations: 1,519

Validation of the output:

* every stop has `en` and `zh` objects;
* every `en`/`zh` object has a non-empty `default`;
* no stop record contains `ops`;
* nearby flag distribution after the change:
  * `0`: 30,801
  * `1`: 7,153
  * `3`: 585
  * `4`: 36,387
  * `5`: 1,091
  * `8`: 956
  * `9`: 22,936
  * `11`: 1,616

---

## 4. Finding — Po Lam Station aliases

The four supplied codes all resolve to TD stop `8154` (Po Lam Station / Po Lam
Bus Terminus):

| Code | TD id | Operator id | Location |
|---|---:|---|---|
| `C9K` | 8154 | `0246D8407B61118D` | 22.32325, 114.25740 |
| `G7N` | 8154 | `001700` | 22.32284, 114.25822 |
| `E17` | 8154 | `796CAA794D4DEBE8` | 22.32345, 114.25761 |
| `GC1` | 8154 | `DCF1319E6136DDB1` | 22.32356, 114.25752 |

They all have the same English/Chinese canonical names, but the per-company
names differ:

* CTB: `PO LAM STATION` / `寶琳站`
* KMB: `PO LAM BUS TERMINUS` / `寶林總站`

Pairwise result after the v2.1 build:

| Pair | Distance | Old flag | v2.1 flag | Present in `nearby` |
|---|---:|---:|---:|---|
| `C9K` ↔ `G7N` | 95.9 m | 1 | 9 | yes |
| `C9K` ↔ `E17` | 31.0 m | 1 | 9 | yes |
| `C9K` ↔ `GC1` | 36.6 m | 1 | 9 | yes |
| `G7N` ↔ `E17` | 92.4 m | 1 | 9 | yes |
| `G7N` ↔ `GC1` | 107.7 m | missing | 9 | yes, restored by flag 8 |
| `E17` ↔ `GC1` | 15.3 m | 1 | 9 | yes |

Conclusion: all four codes are the same TD stop. `G7N` and `GC1` were the only
pair missing from the old proximity list; with flag 8 they are now explicitly
linked in both directions and carry flag `9` (same name + same TD identity).

---

## 5. Consumer impact

This is a breaking `stops.json` schema change:

* `s.en` / `s.zh` are now objects; use `s.en.default` / `s.zh.default` for the
  canonical name.
* Use `s.en[company]` / `s.zh[company]` for operator-specific names.
* `s.ops` no longer exists.
* Nearby consumers can test `flags & 8` for same-stop aliases.

`eta7-main/script/eta6-data.js` currently reads `s.en`, `s.zh` and `s.ops` as
strings/maps, so its `stopInfo()` mapping needs a corresponding update before
the next data publish is consumed there.

---

## 6. Files changed

* `build_static.py`
* `build_operator_map.py`
* `eta6-v2.1.md`
