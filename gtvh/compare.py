"""Agreement between runs, field by field.

Reports Krippendorff's alpha, pairwise Cohen's kappa and mean Jaccard overlap,
and writes the comparison as an Excel workbook.

Typical usage example:

  path, ids, summary = compare_runs(project, ["run-a", "adjudicated"])
"""

from collections import Counter
from itertools import combinations

from openpyxl import Workbook

from .export import _sheet, check_item
from .framework import so_list
from .project import slug
from .validate import Validator

NONE = "(none)"


def _real(o, so_id="so_1"):
    return next((r for r in o["realizations"] if r["so_id"] == so_id), None) or {}


def _prim(o):
    return o["primary_opposition"] or {}


def _theme4(o):
    th = o.get("theme") or {}
    return th.get("primary"), th.get("secondary")


FIELDS = [
    ("Status", "stage1_status", 1, "cat", lambda o: o["annotation_status"]),
    ("Status", "stage2_status", 2, "cat", lambda o: o["annotation_status"]),
    ("Status", "stage3_status", 3, "cat", lambda o: o["annotation_status"]),
    ("Shared reading", "text_status", 1, "cat", lambda o: o["shared_reading"]["text_status"]),
    ("Shared reading", "reference_kinds", 1, "set", lambda o: {r["kind"] for r in o["shared_reading"]["references"]}),
    ("SO", "n_so", 1, "cat", lambda o: len(so_list(o))),
    ("SO", "primary_abstract_code", 1, "cat", lambda o: _prim(o).get("abstract_code")),
    ("SO", "all_abstract_codes", 1, "set", lambda o: {s["abstract_code"] for s in so_list(o) if s["abstract_code"]}),
    ("SI", "situation_modality", 2, "cat", lambda o: o["situation"]["modality"]),
    ("TA", "orientations", 2, "set", lambda o: {x["orientation"] for x in o["orientations"]}),
    ("TA", "any_aggression", 2, "cat", lambda o: any(x["aggression"] == "yes" for x in o["orientations"])),
    ("TA", "n_targets", 2, "cat", lambda o: len(o["targets"])),
    ("TA", "target_types", 2, "set", lambda o: {t["target_type"] for t in o["targets"]}),
    ("TA", "primary_target_types", 2, "set",
     lambda o: {t["target_type"] for t in o["targets"] if t["role"] in ("primary", "co_primary")}),
    ("NS", "layout", 2, "cat", lambda o: o["narrative_strategy"]["layout"]),
    ("NS", "panel_count", 2, "cat", lambda o: o["narrative_strategy"]["panel_count"]),
    ("NS", "devices", 2, "set", lambda o: {d["code"] for d in o["narrative_strategy"]["devices"]}),
    ("NS", "humour_distribution", 2, "cat", lambda o: o["narrative_strategy"]["humour_distribution"]),
    ("LA", "so1_locus", 3, "cat", lambda o: _real(o).get("locus")),
    ("LA", "so1_interaction_dynamic", 3, "cat", lambda o: _real(o).get("interaction_dynamic")),
    ("LA", "so1_semiotic_mechanisms", 3, "set", lambda o: {m["code"] for m in _real(o).get("semiotic_mechanisms", [])}),
    ("LA", "so1_has_metaphor", 3, "cat", lambda o: bool(_real(o).get("metaphors"))),
    ("LA", "all_semiotic_mechanisms", 3, "set",
     lambda o: {m["code"] for r in o["realizations"] for m in r["semiotic_mechanisms"]}),
    ("Presupposition", "categories", 3, "set", lambda o: {p["category"] for p in o["presuppositions"]}),
    ("Presupposition", "necessary_categories", 3, "set",
     lambda o: {p["category"] for p in o["presuppositions"] if p["necessity"] == "necessary"}),
    ("Presupposition", "any_unresolved_central", 3, "cat",
     lambda o: any(u["central"] for u in o["unresolved_references"])),
    ("Coding", "primary_opposition_category", 4, "cat",
     lambda o: next((x["category"] for x in o.get("oppositions", []) if x["so_id"] == "so_1"), None)),
    ("Coding", "opposition_categories", 4, "set", lambda o: {x["category"] for x in o.get("oppositions", [])}),
    ("Coding", "target_categories", 4, "set", lambda o: {t["category"] for t in o.get("targets", [])}),
    ("Coding", "primary_target_categories", 4, "set", None),
    ("Coding", "theme_primary_category", 4, "cat", lambda o: (_theme4(o)[0] or {}).get("category")),
    ("Coding", "theme_categories", 4, "set", lambda o: {t["category"] for t in _theme4(o) if t}),
]
CODING_OPPOSITION_FIELDS = {"primary_opposition_category", "opposition_categories"}
CODING_TARGET_FIELDS = {"target_categories", "primary_target_categories"}
CODING_THEME_FIELDS = {"theme_primary_category", "theme_categories"}

FREE_TEXT = [
    ("SO", "primary_concrete_opposition", 1, lambda o: _prim(o).get("concrete_opposition")),
    ("SO", "additional_concrete_oppositions", 1,
     lambda o: " | ".join(s["concrete_opposition"] for s in o["additional_oppositions"]) or None),
    ("SO", "intermediate_labels", 1, lambda o: " | ".join(s["intermediate_label"] for s in so_list(o)) or None),
    ("SI", "situation_summary", 2, lambda o: o["situation"]["summary"]),
    ("TA", "target_entities", 2, lambda o: " | ".join(f"{t['role']}: {t['entity']}" for t in o["targets"]) or None),
    ("Theme", "theme_labels", 3,
     lambda o: " | ".join(x for x in (o["theme"]["primary"], o["theme"]["secondary"]) if x) or None),
]


def kappa(pairs):
    n = len(pairs)
    if n == 0:
        return None
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return None if pe == 1 else (po - pe) / (1 - pe)


def krippendorff_alpha(units):
    coinc, n_c = Counter(), Counter()
    for vals in units:
        m = len(vals)
        if m < 2:
            continue
        for i, j in ((i, j) for i in range(m) for j in range(m) if i != j):
            coinc[(vals[i], vals[j])] += 1 / (m - 1)
    for (c, _), w in coinc.items():
        n_c[c] += w
    n = sum(n_c.values())
    if n <= 1:
        return None
    d_o = sum(w for (c, k), w in coinc.items() if c != k)
    d_e = sum(n_c[c] * n_c[k] for c in n_c for k in n_c if c != k) / (n - 1)
    return None if d_e == 0 else 1 - d_o / d_e


def jaccard(a, b):
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def _fmt(v):
    if isinstance(v, frozenset):
        return ";".join(sorted(map(str, v))) or NONE
    return v


def compare_runs(project, runs, path=None, only_ids=None):
    if len(runs) < 2:
        raise ValueError("Choose at least two runs to compare.")
    fw = project.framework()
    v = Validator(fw)
    skip = ((set() if fw.has_oppositions else CODING_OPPOSITION_FIELDS) |
            (set() if fw.has_targets else CODING_TARGET_FIELDS) | (set() if fw.has_themes else CODING_THEME_FIELDS))
    valid = {}
    for run in runs:
        valid[run] = {}
        for d in project.item_dirs(run):
            res = check_item(project, v, d)
            valid[run][d.name] = {st: obj for st, (obj, errs, _, _) in res.items() if obj is not None and not errs}
    ids = sorted(set.intersection(*(set(x) for x in valid.values())) & (set(only_ids) if only_ids else set.union(*(set(x) for x in valid.values()))))
    pairs = list(combinations(runs, 2))

    def get(field, stage, kind, fn, data):
        if fn is None:
            if 2 not in data:
                return None
            prim = {t["entity"] for t in data[2]["targets"] if t["role"] in ("primary", "co_primary")}
            val = {t["category"] for t in data[4].get("targets", []) if t["entity"] in prim}
        else:
            val = fn(data[stage])
        return frozenset(val) if kind == "set" else (NONE if val is None else str(val))

    summary, per_cartoon, free = [], [], []
    for kr, field, stage, kind, fn in FIELDS:
        if field in skip:
            continue
        units = []
        for cid in ids:
            data = [valid[r][cid] for r in runs]
            if not all(stage in x for x in data):
                continue
            vals = [get(field, stage, kind, fn, x) for x in data]
            if any(x is None for x in vals):
                continue
            units.append(vals)
            per_cartoon.append({"cartoon_id": cid, "kr": kr, "field": field,
                                **{r: _fmt(x) for r, x in zip(runs, vals)}, "all_match": len(set(vals)) == 1})
        row = {"kr": kr, "field": field, "kind": "category" if kind == "cat" else "set", "n": len(units)}
        if units:
            row["all_runs_agree"] = round(sum(len(set(u)) == 1 for u in units) / len(units), 3)
            if kind == "cat":
                a = krippendorff_alpha(units)
                row["krippendorff_alpha"] = None if a is None else round(a, 3)
                for i, j in combinations(range(len(runs)), 2):
                    k = kappa([(u[i], u[j]) for u in units])
                    row[f"kappa {runs[i]} | {runs[j]}"] = None if k is None else round(k, 3)
            else:
                pj = [jaccard(u[i], u[j]) for u in units for i, j in combinations(range(len(runs)), 2)]
                row["mean_pairwise_jaccard"] = round(sum(pj) / len(pj), 3)
        summary.append(row)

    for kr, field, stage, fn in FREE_TEXT:
        for cid in ids:
            data = [valid[r][cid] for r in runs]
            if all(stage in x for x in data):
                vals = [fn(x[stage]) for x in data]
                if any(vals):
                    free.append({"cartoon_id": cid, "kr": kr, "field": field, **dict(zip(runs, vals))})

    wb = Workbook()
    wb.remove(wb.active)
    pair_cols = [f"kappa {a} | {b}" for a, b in pairs]
    _sheet(wb, "agreement_summary", summary, ["kr", "field", "kind", "n", "all_runs_agree", "krippendorff_alpha",
                                              "mean_pairwise_jaccard", *pair_cols])
    _sheet(wb, "per_cartoon", per_cartoon, ["cartoon_id", "kr", "field", *runs, "all_match"])
    _sheet(wb, "free_text", free, ["cartoon_id", "kr", "field", *runs])
    project.exports_dir.mkdir(parents=True, exist_ok=True)
    path = path or project.exports_dir / f"comparison_{slug('_vs_'.join(runs))[:120]}{'_sample' if only_ids else ''}.xlsx"
    wb.save(path)
    return path, ids, summary
