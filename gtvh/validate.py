"""Schema and rule validation of stage outputs.

Typical usage example:

  errors, warnings = Validator(fw).stage(1, output, cartoon_id)
"""

import re

from jsonschema import Draft202012Validator

from .framework import SNAKE, so_list

LAYOUT_PANELS = {"single_panel": 1, "split_screen": 1, "two_panel": 2, "three_panel": 3, "four_panel": 4}


class _Checker:
    def __init__(self, obj):
        self.obj, self.errors, self.warnings = obj, [], []
        self.nr = obj.get("annotation_status") == "needs_review"

    def err(self, msg):
        self.errors.append("rule: " + msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def need(self, cond, msg):
        if cond and not self.nr:
            self.err(f"{msg} -> annotation_status must be needs_review (with a review_reason)")

    def common(self, expected_id):
        o = self.obj
        if expected_id is not None and o.get("cartoon_id") != expected_id:
            self.err(f"cartoon_id must be '{expected_id}' (got '{o.get('cartoon_id')}')")
        if self.nr and not o.get("review_reasons"):
            self.err("needs_review requires at least one review_reason")
        if not self.nr and o.get("review_reasons"):
            self.warn("review_reasons present although status is complete")


def _stage1(c, fw, outs):
    o = c.obj
    sr = o["shared_reading"]
    ts, tt = sr["text_status"], sr["text_transcription"]
    if ts == "none" and tt != "":
        c.err('text_status none requires text_transcription ""')
    if ts == "unreadable" and tt is not None:
        c.err("text_status unreadable requires text_transcription null")
    if ts in ("readable", "partial") and not tt:
        c.err(f"text_status {ts} requires a non-empty text_transcription")
    if ts == "partial" and tt and "[illegible]" not in tt:
        c.warn("text_status partial but no [illegible] marker in transcription")
    for r in sr["references"]:
        c.need(r["status"] == "unresolved" and r["central"], f"central unresolved reference '{r['label']}'")
    p, adds = o["primary_opposition"], o["additional_oppositions"]
    if p is None:
        if adds:
            c.err("additional_oppositions must be [] when primary_opposition is null")
        c.need(True, "null primary_opposition")
    sos = so_list(o)
    expected = [f"so_{i}" for i in range(1, len(sos) + 1)]
    got = [s["id"] for s in sos]
    if got != expected:
        c.err(f"SO ids must be {expected} in order (got {got})")
    seen = set()
    for s in sos:
        sid = s["id"]
        sides = [x.strip() for x in s["intermediate_label"].split("/")]
        if any(len(x.split()) > 4 for x in sides):
            c.warn(f"{sid}: intermediate_label sides should be at most four words")
        if s["intermediate_label"].strip().lower() == s["concrete_opposition"].strip().lower():
            c.warn(f"{sid}: intermediate_label repeats the concrete opposition; generalize it by one step")
        c.need(s["abstract_code"] is None, f"{s['id']}: null abstract_code")
        if s["abstract_alternative"] is not None and s["abstract_alternative"] == s["abstract_code"]:
            c.err(f"{s['id']}: abstract_alternative must differ from abstract_code")
        key = s["concrete_opposition"].strip().lower()
        if key in seen:
            c.err(f"{s['id']}: duplicate concrete_opposition")
        seen.add(key)


def _stage2(c, fw, outs):
    o, stage1 = c.obj, outs.get(1)
    sit = o["situation"]
    if sit["summary"] is None:
        if sit["modality"] != "uncertain" or sit["confidence"] != "low":
            c.err("null situation.summary requires modality uncertain and confidence low")
        c.need(True, "null situation.summary")
    elif sit["modality"] == "uncertain":
        c.warn("situation.modality uncertain although a summary is given")
    ors = o["orientations"]
    expected = [f"orientation_{i}" for i in range(1, len(ors) + 1)]
    got = [x["id"] for x in ors]
    if got != expected:
        c.err(f"orientation ids must be {expected} in order (got {got})")
    aggr = {x["id"]: x["aggression"] for x in ors}
    for x in ors:
        c.need(x["aggression"] == "uncertain", f"{x['id']}: uncertain aggression")
        if x["orientation"] == "uncertain" and x["aggression"] == "yes":
            c.warn(f"{x['id']}: aggression yes on an uncertain orientation")
    ts = o["targets"]
    for t in ts:
        if t["orientation_id"] not in aggr:
            c.err(f"target '{t['entity']}' links to unknown orientation_id '{t['orientation_id']}'")
        elif aggr[t["orientation_id"]] != "yes":
            c.err(f"target '{t['entity']}' links to {t['orientation_id']} whose aggression is not yes")
    if ts:
        roles = [t["role"] for t in ts]
        n_p, n_co = roles.count("primary"), roles.count("co_primary")
        if n_p > 1:
            c.err("at most one primary target (use co_primary for similarly central targets)")
        if n_co == 1:
            c.err("co_primary requires at least two co_primary targets")
        if n_p and n_co:
            c.err("do not mix primary and co_primary")
        if n_p == 0 and n_co == 0:
            c.err("targets need a primary (or co_primary) target")
        ents = [t["entity"].strip().lower() for t in ts]
        if len(set(ents)) < len(ents):
            c.warn("same target entity listed more than once")
    for oid, a in aggr.items():
        if a == "yes" and not any(t["orientation_id"] == oid for t in ts):
            c.err(f"{oid} has aggression yes but no linked target")
    ns = o["narrative_strategy"]
    lay, pc = ns["layout"], ns["panel_count"]
    if lay in LAYOUT_PANELS and pc != LAYOUT_PANELS[lay]:
        c.err(f"layout {lay} requires panel_count {LAYOUT_PANELS[lay]} (got {pc})")
    if lay == "multi_panel" and (pc is None or pc < 5):
        c.err(f"layout multi_panel requires panel_count >= 5 (got {pc})")
    if lay == "uncertain" and pc is not None:
        c.warn("layout uncertain but panel_count given")
    if lay == "other" and not o["difficulty_notes"]:
        c.err("layout other must be explained in difficulty_notes")
    if stage1 is not None:
        n_so, dist = len(so_list(stage1)), ns["humour_distribution"]
        if n_so == 0 and dist != "uncertain":
            c.err("humour_distribution must be uncertain when no SO was supplied")
        if dist == "punch_only" and n_so != 1:
            c.err(f"humour_distribution punch_only requires exactly one SO (got {n_so})")
        if dist == "both" and n_so < 2:
            c.err(f"humour_distribution both requires at least two SOs (got {n_so})")
        if n_so > 1 and not all(s["id"] in ns["distribution_note"] for s in so_list(stage1)):
            c.warn("distribution_note should name each SO id when there are several")
    codes = [d["code"] for d in ns["devices"]]
    verbal = {it["code"] for it in fw.cb["device"] if it.get("verbal")}
    if len(set(codes)) < len(codes):
        c.err("each device code may appear only once")
    if "wordless" in codes and verbal & set(codes):
        c.err("wordless cannot be combined with verbal devices")
    if "dialogue" in codes and "monologue" in codes:
        c.warn("dialogue and monologue both selected; check they cover separate utterances")
    if not codes:
        c.warn("no narrative devices selected")
    if o["evidence_corrections"] and not c.nr:
        c.warn("evidence_corrections present with status complete; check whether they are material")


def _stage3(c, fw, outs):
    o, stage1 = c.obj, outs.get(1)
    reals = o["realizations"]
    if stage1 is not None:
        expected = [s["id"] for s in so_list(stage1)]
        got = [r["so_id"] for r in reals]
        if got != expected:
            c.err(f"realizations must cover SO ids {expected} in order (got {got})")
        c.need(not expected, "no SOs supplied")
    for r in reals:
        sid = r["so_id"]
        if r["locus"] != "interaction":
            if r["interaction_dynamic"] is not None or r["interaction_alternative"] is not None:
                c.err(f"{sid}: interaction_dynamic/alternative must be null when locus is {r['locus']}")
        else:
            if r["interaction_dynamic"] is None:
                c.err(f"{sid}: locus interaction requires interaction_dynamic")
            if r["interaction_alternative"] is not None and r["interaction_alternative"] == r["interaction_dynamic"]:
                c.err(f"{sid}: interaction_alternative must differ from interaction_dynamic")
            if r["interaction_dynamic"] == "uncertain" and r["interaction_alternative"] is not None:
                c.warn(f"{sid}: alternative given although dynamic is uncertain")
        c.need(r["locus"] == "uncertain", f"{sid}: uncertain locus")
        codes = [m["code"] for m in r["semiotic_mechanisms"]]
        if len(set(codes)) < len(codes):
            c.err(f"{sid}: each semiotic mechanism code may appear only once")
        if ("visual_metaphor" in codes) != bool(r["metaphors"]):
            c.err(f"{sid}: visual_metaphor must be selected if and only if metaphors is non-empty")
    ks = [p["specific_knowledge"].strip().lower() for p in o["presuppositions"]]
    if len(set(ks)) < len(ks):
        c.warn("duplicate presupposition knowledge")
    for u in o["unresolved_references"]:
        c.need(u["central"], f"central unresolved reference '{u['description']}'")
    th = o["theme"]
    c.need(th["primary"] is None, "null theme.primary")
    if th["primary"] is None and th["secondary"] is not None:
        c.err("theme.secondary requires a primary theme")
    for slot in ("primary", "secondary"):
        label = th[slot]
        if label is not None and len(label.split()) > 8:
            c.warn(f"theme.{slot} is long for a topic label ({len(label.split())} words)")
    if th["primary"] and th["secondary"] and th["primary"].strip().lower() == th["secondary"].strip().lower():
        c.err("theme.secondary must differ from theme.primary")
    c.need(bool(o["upstream_issues"]), "upstream_issues present")
    if o["evidence_corrections"] and not c.nr:
        c.warn("evidence_corrections present with status complete; check whether they are material")


def _proposed(c, where, slot):
    if slot["category" if "category" in slot else "code"] == "other":
        if not slot["proposed_category"]:
            c.err(f"{where}: category other requires proposed_category")
        elif not SNAKE.fullmatch(slot["proposed_category"]):
            c.err(f"{where}: proposed_category must be snake_case")
    elif slot["proposed_category"] is not None:
        c.err(f"{where}: proposed_category must be null unless category is other")


def _stage4(c, fw, outs):
    o = c.obj
    if fw.has_oppositions:
        s1 = outs.get(1)
        if s1 is not None:
            want = [s["id"] for s in so_list(s1)]
            got = [x["so_id"] for x in o["oppositions"]]
            if got != want:
                c.err(f"oppositions must code the Stage 1 SOs {want} in order (got {got})")
        for x in o["oppositions"]:
            _proposed(c, f"opposition {x['so_id']}", x)
    if fw.has_targets:
        s2 = outs.get(2)
        if s2 is not None:
            want = [t["entity"] for t in s2["targets"]]
            got = [t["entity"] for t in o["targets"]]
            if got != want:
                c.err(f"targets must code the Stage 2 targets {want} in order (got {got})")
        for t in o["targets"]:
            _proposed(c, f"target '{t['entity']}'", t)
    if fw.has_themes:
        th, s3 = o["theme"], outs.get(3)
        if s3 is not None:
            for slot in ("primary", "secondary"):
                if (s3["theme"][slot] is None) != (th[slot] is None):
                    c.err(f"theme.{slot} must be null exactly when the Stage 3 theme.{slot} is null")
        for slot in ("primary", "secondary"):
            if th[slot] is not None:
                _proposed(c, f"theme.{slot}", th[slot])
        p, s = th["primary"], th["secondary"]
        if p and s and p["category"] == s["category"] and (
                p["category"] != "other" or p["proposed_category"] == s["proposed_category"]):
            c.err("theme.secondary must differ from theme.primary")


_RULES = {1: _stage1, 2: _stage2, 3: _stage3, 4: _stage4}


class Validator:
    def __init__(self, fw):
        self.fw = fw
        self._schema_validators = {}

    def schema_errors(self, stage, obj):
        if stage not in self._schema_validators:
            self._schema_validators[stage] = Draft202012Validator(self.fw.schema(stage))
        out = []
        for e in sorted(self._schema_validators[stage].iter_errors(obj), key=lambda e: [str(p) for p in e.path]):
            loc = "/".join(str(p) for p in e.path) or "(root)"
            msg = e.message if len(e.message) < 300 else e.message[:300] + "..."
            out.append(f"schema: {loc}: {msg}")
        return out

    def stage(self, stage, obj, expected_id=None, outs=None):
        if not isinstance(obj, dict):
            return ["output is not a JSON object"], []
        errs = self.schema_errors(stage, obj)
        if errs:
            return errs, []
        c = _Checker(obj)
        c.common(expected_id)
        try:
            _RULES[stage](c, self.fw, outs or {})
        except (KeyError, TypeError) as e:
            c.err(f"unexpected structure: {e!r}")
        return c.errors, c.warnings

    def codebook_output(self, obj):
        errs = self.schema_errors("codebook", obj)
        if not errs:
            codes = [x["code"] for x in obj["categories"]]
            if "other" in codes:
                errs.append("rule: do not propose 'other'; it is added automatically")
            if len(set(codes)) < len(codes):
                errs.append("rule: category codes must be unique")
        return errs, []


def theme_label_key(label):
    return re.sub(r"\s+", " ", (label or "").strip().lower())
