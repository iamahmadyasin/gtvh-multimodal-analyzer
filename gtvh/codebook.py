"""Drafting, inducing and accepting the Stage 4 project codebook.

A draft is produced from the project description (deductive) or by grouping the
free-text labels of a run (inductive), written to codebook.draft.yaml, and made
the project codebook when accepted.

Typical usage example:

  path = induce(project, "anthropic-claude-opus-5-5", provider)
  accept_draft(project)
"""

import datetime as dt
from collections import Counter, defaultdict

import yaml

from .framework import CODING_DIMENSIONS, STAGE_FILES, correction_block, so_list
from .project import ProjectError, read_json
from .providers import extract_json
from .validate import Validator

BATCH = 250


def _key(label):
    text = " ".join((label or "").lower().split())
    sides = [s.strip() for s in text.split("/")]
    return "/".join(sorted(sides)) if len(sides) == 2 else text


def _from_run(project, run, dimension):
    for d in project.item_dirs(run):
        if dimension == "oppositions":
            f = d / STAGE_FILES[1]
            if f.is_file():
                for so in so_list(read_json(f)):
                    yield so["intermediate_label"], so["concrete_opposition"]
        elif dimension == "targets":
            f = d / STAGE_FILES[2]
            if f.is_file():
                for tg in read_json(f)["targets"]:
                    yield tg["entity"], tg["criticized_attribute"]
        else:
            f = d / STAGE_FILES[3]
            if f.is_file():
                th = read_json(f)["theme"]
                for label in (th["primary"], th["secondary"]):
                    if label:
                        yield label, None


def collect_labels(project, run, dimension):
    counts, forms, context = Counter(), defaultdict(Counter), defaultdict(list)
    for label, ctx in _from_run(project, run, dimension):
        k = _key(label)
        counts[k] += 1
        forms[k][label.strip()] += 1
        if ctx and len(context[k]) < 2:
            context[k].append(ctx)
    return [(forms[k].most_common(1)[0][0], n, context.get(k, [])) for k, n in counts.most_common()]


def _ask(fw, provider, prompt, log):
    validator = Validator(fw)
    schema = fw.schema("codebook")
    errors, previous = [], None
    for attempt in (1, 2):
        user = "Return the JSON object now." + (correction_block(errors, previous) if errors else "")
        resp = provider.generate(prompt, user, None, schema, "codebook")
        obj = extract_json(resp.text)
        errors = ["no parseable JSON object in the model output"] if obj is None else validator.codebook_output(obj)[0]
        if not errors:
            return obj
        previous = obj if obj is not None else resp.text
        log(f"   attempt {attempt}: {errors[0]}")
    raise ProjectError("The model did not return a valid codebook: " + "; ".join(errors[:3]))


def _format_labels(rows, dimension):
    lines = []
    for label, n, ctx in rows:
        extra = f" [{'; '.join(ctx)}]" if ctx else ""
        lines.append(f"- {label} ({n}){extra}")
    return "\n".join(lines)


def _categories(obj, rows=None):
    by_label = {_key(l): n for l, n, _ in rows} if rows else {}
    cats, assigned = [], set()
    for c in obj["categories"]:
        inc = [x for x in c["includes"] if not rows or _key(x) in by_label]
        cat = {"code": c["code"], "definition": c["definition"].strip(), "includes": inc}
        if rows:
            cat["n"] = sum(by_label[_key(x)] for x in {_key(x): x for x in inc}.values())
            assigned |= {_key(x) for x in inc}
        cats.append(cat)
    if rows:
        cats.sort(key=lambda c: -c["n"])
    unassigned = [l for l, n, _ in rows if _key(l) not in assigned] if rows else []
    return cats, unassigned


def draft(project, provider, dimensions=CODING_DIMENSIONS, target_size="10-20", log=print):
    fw = project.framework()
    out = {"version": f"draft-{dt.date.today().isoformat()}", "origin": "drafted",
           "model": f"{provider.name}/{provider.model}"}
    for dim in dimensions:
        log(f"Drafting {dim} categories ...")
        obj = _ask(fw, provider, fw.codebook_prompt("draft", dim, target_size=target_size), log)
        out[dim], _ = _categories(obj)
        if obj.get("notes"):
            out.setdefault("notes", {})[dim] = obj["notes"]
    return _write_draft(project, out)


def induce(project, run, provider, dimensions=CODING_DIMENSIONS, target_size="10-20", log=print):
    fw = project.framework()
    out = {"version": f"induced-{dt.date.today().isoformat()}", "origin": "induced", "from_run": run,
           "model": f"{provider.name}/{provider.model}"}
    for dim in dimensions:
        rows = collect_labels(project, run, dim)
        if not rows:
            log(f"No {dim} found in run '{run}'; skipped.")
            continue
        batches = [rows[i:i + BATCH] for i in range(0, len(rows), BATCH)]
        log(f"Inducing {dim} categories from {len(rows)} distinct labels in {len(batches)} batch(es) ...")
        parts = [_ask(fw, provider, fw.codebook_prompt("induce", dim, _format_labels(b, dim), target_size), log)
                 for b in batches]
        if len(parts) > 1:
            log("Consolidating the batches ...")
            text = "\n\n".join(f"### Batch {i}\n\n" + yaml.safe_dump(p["categories"], allow_unicode=True,
                                                                       sort_keys=False)
                               for i, p in enumerate(parts, 1))
            obj = _ask(fw, provider, fw.codebook_prompt("merge", dim, text, target_size), log)
        else:
            obj = parts[0]
        cats, unassigned = _categories(obj, rows)
        out[dim] = cats
        if unassigned:
            out.setdefault("unassigned", {})[dim] = unassigned
        if obj.get("notes"):
            out.setdefault("notes", {})[dim] = obj["notes"]
    if not any(out.get(d) for d in dimensions):
        raise ProjectError(f"Run '{run}' has no annotations to induce categories from.")
    return _write_draft(project, out)


def _write_draft(project, data):
    project.draft_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                                                                width=110), encoding="utf-8")
    return project.draft_path


def accept_draft(project):
    problems = project.codebook_problems(project.draft_path)
    if problems:
        raise ProjectError("The draft has problems: " + "; ".join(problems))
    data = yaml.safe_load(project.draft_path.read_text(encoding="utf-8"))
    keep = {k: data[k] for k in ("version", "origin", "from_run", "model", "guidance", *CODING_DIMENSIONS)
            if data.get(k)}
    for dim in CODING_DIMENSIONS:
        for c in keep.get(dim, []):
            c.pop("n", None)
    if keep.get("version", "").startswith(("draft-", "induced-")):
        keep["version"] = dt.datetime.now().strftime("%Y%m%d-%H%M")
    old = project.load_codebook()
    if old:
        project.codebook_path.rename(project.root / f"codebook.{old['version']}.yaml")
    project.codebook_path.write_text(
        yaml.safe_dump(keep, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
    project.draft_path.unlink()
    project.reload()
    return project.codebook_path
