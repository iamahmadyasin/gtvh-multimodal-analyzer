"""The GTVH framework: code lists, stage schemas, prompts and stage inputs.

Everything is generated from the files in gtvh/resources plus a project's
settings and codebook, so each code and rule is defined once.

Typical usage example:

  fw = Framework(project_settings, codebook)
  system_prompt = fw.system_prompt(1)
  schema = fw.schema(1)
"""

import copy
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

import jinja2
import yaml

RESOURCES = Path(__file__).resolve().parent / "resources"
ANNOTATION_STAGES = (1, 2, 3)
STAGES = (1, 2, 3, 4)
STAGE_FILES = {1: "01_so.json", 2: "02_si_ta_ns.json", 3: "03_la_presup_theme.json", 4: "04_coding.json"}
STAGE_NAMES = {1: "Script Opposition", 2: "Situation, Target, Narrative Strategy",
               3: "Language/semiotics, presuppositions, theme", 4: "Corpus coding"}
SNAKE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
CODING_DIMENSIONS = {"oppositions": "opposition_category", "targets": "target_category",
                     "themes": "theme_category"}


class FrameworkError(ValueError):
    pass


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load(name):
    return yaml.safe_load((RESOURCES / name).read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def core():
    return {"codebooks": _load("codebooks.yaml"), "schemas": _load("schemas.yaml"),
            "examples": _load("examples.yaml")}


def schema_version():
    return core()["codebooks"]["schema_version"]


def check_category_list(items, where, reserved=()):
    problems, seen = [], set(reserved)
    if not isinstance(items, list):
        return [f"{where}: must be a list"]
    for i, it in enumerate(items):
        code = (it or {}).get("code") if isinstance(it, dict) else None
        if not code or not SNAKE.fullmatch(str(code)):
            problems.append(f"{where}[{i}]: code {code!r} must be snake_case")
        elif code == "other":
            problems.append(f"{where}: 'other' is added automatically; remove it")
        elif code in seen:
            problems.append(f"{where}: duplicate code '{code}'")
        seen.add(code)
        if isinstance(it, dict) and not str(it.get("definition") or "").strip():
            problems.append(f"{where}: '{code}' needs a definition")
    return problems


class Framework:
    def __init__(self, settings=None, codebook=None):
        self.settings = settings or {}
        self.codebook = codebook
        self.cb = self._codebooks()
        self._schemas = {}
        self._prompts = {}

    def _codebooks(self):
        cbs = {name: [dict(it) for it in spec["items"]]
               for name, spec in core()["codebooks"]["codebooks"].items()}
        for name, extra in (self.settings.get("extensions") or {}).items():
            spec = core()["codebooks"]["codebooks"].get(name)
            if not spec or not spec.get("extensible"):
                raise FrameworkError(f"extensions: '{name}' is not an extensible code list")
            problems = check_category_list(extra, f"extensions.{name}", reserved=[i["code"] for i in cbs[name]])
            if problems:
                raise FrameworkError("; ".join(problems))
            items = cbs[name]
            pos = next((i for i, it in enumerate(items) if it["code"] == "other"), len(items))
            cbs[name] = items[:pos] + [dict(it, extension=True) for it in extra] + items[pos:]
        if self.codebook:
            for dim, name in CODING_DIMENSIONS.items():
                cats = self.codebook.get(dim) or []
                if cats:
                    cbs[name] = [dict(c, definition=c["definition"].strip() + (
                        " Examples: " + "; ".join(c["includes"][:4]) + "." if c.get("includes") else ""))
                        for c in cats] + [
                        {"code": "other", "definition": "A clear category outside this list; give `proposed_category`."}]
        return cbs

    def codes(self, name, exclude=()):
        if name not in self.cb:
            raise FrameworkError(f"unknown code list '{name}'")
        return [it["code"] for it in self.cb[name] if it["code"] not in exclude]

    def labels(self, name):
        return {it["code"]: it.get("label") for it in self.cb[name]}

    @property
    def has_oppositions(self):
        return "opposition_category" in self.cb

    @property
    def has_targets(self):
        return "target_category" in self.cb

    @property
    def has_themes(self):
        return "theme_category" in self.cb

    @property
    def has_coding(self):
        return self.has_oppositions or self.has_targets or self.has_themes

    def _expand(self, node):
        if isinstance(node, list):
            return [self._expand(x) for x in node]
        if not isinstance(node, dict):
            return node
        if "$object" in node:
            props = {k: self._expand(v) for k, v in node["$object"].items()}
            return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
        if "$array" in node:
            out = {"type": "array", "items": self._expand(node["$array"])}
            out.update({k: v for k, v in node.items() if k != "$array"})
            return out
        if "$codes" in node:
            return {"type": "string", "enum": self.codes(node["$codes"], node.get("$except", ()))}
        if "$nullable" in node:
            return {"anyOf": [self._expand(node["$nullable"]), {"type": "null"}]}
        if "$const" in node:
            return {"type": "string", "enum": [schema_version()]}
        return {k: self._expand(v) for k, v in node.items()}

    def schema(self, stage):
        if stage not in self._schemas:
            if stage == 4 and not self.has_coding:
                raise FrameworkError("Stage 4 needs a project codebook with target and/or theme categories")
            spec = copy.deepcopy(core()["schemas"]["stages"][stage])
            if stage == 4:
                for key, present in (("oppositions", self.has_oppositions), ("targets", self.has_targets),
                                     ("theme", self.has_themes)):
                    if not present:
                        del spec["$object"][key]
            s = self._expand(spec)
            s = {"$schema": "https://json-schema.org/draft/2020-12/schema", **s}
            self._schemas[stage] = s
        return self._schemas[stage]

    def _env(self):
        return jinja2.Environment(loader=jinja2.FileSystemLoader(RESOURCES / "prompts"),
                                  undefined=jinja2.StrictUndefined, trim_blocks=True, lstrip_blocks=True,
                                  keep_trailing_newline=True)

    def project_context(self):
        s = self.settings
        return {"name": s.get("name") or "", "description": s.get("description") or "",
                "material": s.get("material") or "editorial cartoons",
                "corpus_context": (s.get("corpus_context") or "") if s.get("send_context") else "",
                "send_context": bool(s.get("send_context")),
                "research_questions": s.get("research_questions") or []}

    def _examples(self):
        out = []
        for ex in core()["examples"]:
            outs = {st: {"schema_version": schema_version(), "cartoon_id": ex["cartoon_id"], **ex["stages"][st]}
                    for st in ANNOTATION_STAGES}
            out.append({
                "cartoon_id": ex["cartoon_id"], "title": ex["title"],
                "image_description": ex["image_description"].strip(),
                "input": {st: dumps(stage_input(st, ex["metadata"], outs)) for st in ANNOTATION_STAGES},
                "output": {st: dumps(outs[st]) for st in ANNOTATION_STAGES},
            })
        return out

    def system_prompt(self, stage):
        if stage not in self._prompts:
            if stage == 4:
                self.schema(4)
            cbk = self.codebook or {}
            ctx = {"cb": self.cb, "project": self.project_context(), "schema_version": schema_version(),
                   "examples": self._examples() if stage in ANNOTATION_STAGES else [],
                   "has_oppositions": self.has_oppositions, "has_targets": self.has_targets,
                   "has_themes": self.has_themes,
                   "codebook": {"version": cbk.get("version", ""),
                                "guidance": {d: (cbk.get("guidance") or {}).get(d) or [] for d in CODING_DIMENSIONS}}}
            text = self._env().get_template(f"stage{stage}.md").render(**ctx)
            self._prompts[stage] = re.sub(r"\n{3,}", "\n\n", text)
        return self._prompts[stage]

    def prompt_hash(self, stage):
        return sha256(self.system_prompt(stage))

    def codebook_prompt(self, mode, dimension, items="", target_size="10-20"):
        dims = {
            "oppositions": {
                "title": "intermediate script oppositions", "plural": "opposition categories",
                "examples": "intermediate opposition labels",
                "context_note": " and, in brackets, a concrete opposition it was derived from",
                "explanation": (
                    "In GTVH, a Script Opposition (SO) is a pair of overlapping, opposed scripts. Each annotated "
                    "cartoon records its SOs at a concrete level (specific to the cartoon), at an intermediate level "
                    "as a free-text pair such as \"few/many\", \"life/death\" or \"public service/private profit\", "
                    "and at an abstract level (actual/non-actual, normal/abnormal, possible/impossible). Intermediate "
                    "pairs may be semantic opposites or local antonyms, and may be general or domain-specific. "
                    "Opposition categories group intermediate pairs that express the same opposition, so that the "
                    "corpus can be analysed by kind of opposition. Treat a pair and its reverse order (a/b, b/a) as "
                    "the same opposition, and name each category as an a/b pair in its definition.")},
            "targets": {
                "title": "target categories", "plural": "target categories", "examples": "target referents",
                "context_note": " and, in brackets, what the cartoons criticize about it",
                "explanation": (
                    "In GTVH, the Target (TA) is the butt of the joke: the person, group, institution, or practice "
                    "or ideology that the humour attacks. Each annotated cartoon names its targets as specific "
                    "referents (e.g. \"the Greek Prime Minister\", \"the news media\"). Target categories group "
                    "such referents by the kind of social actor or practice they are, so that the corpus can be "
                    "analysed by who or what is ridiculed.")},
            "themes": {
                "title": "theme categories", "plural": "theme categories", "examples": "topic labels",
                "context_note": "",
                "explanation": (
                    "The theme is a cartoon's topic: what it is about, as distinct from its target or its script "
                    "opposition. Each annotated cartoon names its topic in a short free-text label (e.g. "
                    "\"electoral politics\", \"media coverage of disasters\"). Theme categories group these "
                    "labels so that the corpus can be analysed by topic.")},
        }
        return self._env().get_template("codebook.md").render(
            mode=mode, dimension=dims[dimension], items=items, target_size=target_size,
            project=self.project_context())


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)


def so_list(stage1):
    if not stage1:
        return []
    p = stage1.get("primary_opposition")
    return ([p] if p else []) + list(stage1.get("additional_oppositions") or [])


CONTEXT_FIELDS = ("date", "cartoonist", "publication", "context_notes")


def stage_input(stage, meta, outs, fw=None, with_context=True):
    base = {"cartoon_id": meta.get("cartoon_id")}
    if with_context:
        base.update({k: meta.get(k) or None for k in CONTEXT_FIELDS})
    s1 = outs.get(1)
    if stage == 1:
        return base
    if stage == 2:
        return {**base, "shared_reading": s1["shared_reading"],
                "oppositions": [{k: so[k] for k in ("id", "concrete_opposition", "intermediate_label")}
                                for so in so_list(s1)]}
    if stage == 3:
        return {**base, "shared_reading": s1["shared_reading"],
                "primary_opposition": s1["primary_opposition"],
                "additional_oppositions": s1["additional_oppositions"]}
    s2, s3 = outs.get(2), outs.get(3)
    inp = {"cartoon_id": base["cartoon_id"],
           "scene_description": s1["shared_reading"]["scene_description"],
           "text_transcription": s1["shared_reading"]["text_transcription"],
           "situation": s2["situation"]["summary"] if s2 else None}
    if fw is None or fw.has_oppositions:
        inp["oppositions"] = [{"so_id": so["id"], **{k: so[k] for k in ("concrete_opposition", "intermediate_label")}}
                              for so in so_list(s1)]
    if fw is None or fw.has_targets:
        inp["targets"] = [{k: t[k] for k in ("entity", "target_type", "criticized_attribute")}
                          for t in (s2 or {}).get("targets", [])]
    if fw is None or fw.has_themes:
        th = (s3 or {}).get("theme") or {}
        inp["theme"] = {k: th.get(k) for k in ("primary", "secondary", "note")}
    return inp


def stage_requirements(stage, fw):
    if stage == 4:
        return ([1] + ([2] if fw.has_targets else []) + ([3] if fw.has_themes else []))
    return [1] if stage in (2, 3) else []


def user_message(inp, correction=None):
    parts = ["# INPUT FOR THIS RUN\n\n```json\n", dumps(inp), "\n```\n"]
    if correction:
        parts.append(correction)
    parts.append("\nReturn the JSON object now.\n")
    return "".join(parts)


def correction_block(errors, previous):
    lines = "\n".join(f"- {e}" for e in errors[:40])
    prev = previous if isinstance(previous, str) else dumps(previous)
    return ("\n# CORRECTION REQUIRED\n"
            "A previous attempt for this same cartoon failed validation. Re-inspect the evidence, fix every "
            "problem listed, and return a complete corrected JSON object. Do not change correct fields "
            "merely because a retry was requested.\n\nProblems:\n" + lines +
            "\n\nPrevious output:\n```\n" + prev[:30000] + "\n```\n")
