"""Runs annotation stages over a project's cartoons with one provider and model.

Each stage of each cartoon is one stateless request, sent directly or through the
provider's batch API. Outputs are checked against the schemas and rules; a failed
check is retried with the problems shown to the model. Runs are resumable.

Typical usage example:

  summary = Runner(project, RunConfig(provider="anthropic", batch=True)).run()
"""

import datetime as dt
import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import yaml

from . import __version__
from .framework import (ANNOTATION_STAGES, STAGE_FILES, correction_block, core, schema_version, stage_input,
                        stage_requirements, user_message)
from .project import IMAGE_EXTS, ProjectError, read_json, slug, write_json
from .providers import ProviderError, SchemaRejected, StopRun, extract_json, make_provider
from .validate import Validator

META_FILE = "meta.json"
LOG_FILE = "run_log.jsonl"
RUN_FILE = "run.yaml"
BATCH_FILE = "batch.json"
POLL_SECONDS = 60


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def default_run_name(provider, model, effort=None):
    return slug(f"{provider}-{model}" + (f"-{effort}" if effort else ""))


@dataclass
class RunConfig:
    provider: str
    model: str = None
    effort: str = None
    run: str = None
    stages: tuple = ANNOTATION_STAGES
    ids: list = None
    max_items: int = None
    force: bool = False
    retries: int = 1
    workers: int = 4
    use_schema: bool = True
    timeout: int = 600
    batch: bool = False


@dataclass
class Summary:
    ok: int = 0
    review: int = 0
    failed: int = 0
    skipped: int = 0
    items: int = 0
    tokens: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0})
    stopped: str = None

    def line(self):
        t = self.tokens
        return (f"ok={self.ok} review={self.review} failed={self.failed} skipped={self.skipped}; "
                f"tokens in={t['input_tokens']:,} (cached {t['cache_read_tokens']:,}) out={t['output_tokens']:,}")


class Runner:
    def __init__(self, project, cfg, progress=None, cancel=None):
        self.p = project
        self.cfg = cfg
        self.fw = project.framework()
        self.validator = Validator(self.fw)
        self.cancel = cancel or threading.Event()
        self.progress = progress or (lambda ev: print(ev["msg"]) if ev.get("msg") else None)
        self.summary = Summary()
        self._lock = threading.Lock()
        self.use_schema = cfg.use_schema
        self.with_context = bool(project.settings.get("send_context"))
        if 4 in cfg.stages and not self.fw.has_coding:
            raise ProjectError("Stage 4 needs a codebook.yaml with opposition, target or theme categories.")

    def log(self, msg, **kw):
        self.progress({"type": "log", "msg": msg, **kw})

    def _open_run(self):
        cfg = self.cfg
        self.provider = make_provider(cfg.provider, model=cfg.model, effort=cfg.effort, timeout=cfg.timeout,
                                      cancel=self.cancel)
        self.run_name = cfg.run or default_run_name(cfg.provider, self.provider.model, cfg.effort)
        self.dir = self.p.run_dir(self.run_name)
        self.dir.mkdir(parents=True, exist_ok=True)
        rf = self.dir / RUN_FILE
        spec = {"provider": cfg.provider, "model": self.provider.model, "effort": cfg.effort}
        if rf.is_file():
            old = yaml.safe_load(rf.read_text(encoding="utf-8")) or {}
            if any(old.get(k) != v for k, v in spec.items()):
                raise ProjectError(
                    f"Run '{self.run_name}' was made with {old.get('provider')}/{old.get('model')} "
                    f"(effort {old.get('effort')}); choose another run name for {cfg.provider}/{self.provider.model}.")
        else:
            rf.write_text(yaml.safe_dump({**spec, "created": now(), "app_version": __version__,
                                          "schema_version": schema_version()}, sort_keys=False), encoding="utf-8")

    @staticmethod
    def _meta(d):
        return read_json(d / META_FILE) if (d / META_FILE).is_file() else {}

    @staticmethod
    def _outs(d):
        return {s: read_json(d / f) for s, f in STAGE_FILES.items() if (d / f).is_file()}

    def _todo(self, item_dir, meta, stages=None):
        out = []
        for st in stages or self.cfg.stages:
            f = item_dir / STAGE_FILES[st]
            stale = (st == 4 and f.exists() and
                     (meta.get("4") or {}).get("codebook_hash") != self.fw.codebook["hash"])
            if self.cfg.force or not f.exists() or stale:
                out.append(st)
        return out

    def _ready(self, stage, item, d):
        img = self.p.image_path(item)
        if stage in ANNOTATION_STAGES and (not img or not img.is_file() or img.suffix.lower() not in IMAGE_EXTS):
            return None, f"image missing or unsupported ({item.get('image_file')!r})"
        outs = self._outs(d)
        missing = [s for s in stage_requirements(stage, self.fw) if s not in outs]
        if missing:
            return None, f"needs stage {missing} output first"
        return outs, None

    def _request(self, stage, item, outs, errors=None, previous=None):
        inp = stage_input(stage, item, outs, self.fw, with_context=self.with_context)
        return {"system": self.fw.system_prompt(stage),
                "user": user_message(inp, correction_block(errors, previous) if errors else None),
                "image": self.p.image_path(item) if stage in ANNOTATION_STAGES else None,
                "schema": self.fw.schema(stage) if self.use_schema else None, "name": f"stage{stage}"}

    def _entry(self, stage, item, attempt):
        entry = {"time": now(), "stage": stage, "attempt": attempt, "provider": self.cfg.provider,
                 "model": self.provider.model, "effort": self.cfg.effort, "structured_output": self.use_schema,
                 "batch": self.cfg.batch, "prompt_sha256": self.fw.prompt_hash(stage),
                 "image_file": item.get("image_file")}
        if stage == 4:
            entry["codebook_hash"] = self.fw.codebook["hash"]
        return entry

    def _evaluate(self, stage, item, d, outs, resp, entry):
        if isinstance(resp, Exception):
            errors, warnings, obj, previous = [f"provider error: {resp}"], [], None, None
        else:
            obj = extract_json(resp.text)
            with self._lock:
                for k in self.summary.tokens:
                    self.summary.tokens[k] += resp.usage.get(k) or 0
            entry.update(usage=resp.usage, **resp.meta)
            if obj is None:
                errors, warnings = ["no parseable JSON object in the model output"], []
            else:
                errors, warnings = self.validator.stage(stage, obj, item["cartoon_id"], outs)
            previous = obj if obj is not None else (resp.text or "")[:30000]
        self._log_entry(d, {**entry, "errors": errors, "warnings": warnings})
        return obj, errors, warnings, previous

    def _save(self, stage, item, d, obj, warnings):
        cid = item["cartoon_id"]
        meta = self._meta(d)
        downstream = {1: [2, 3, 4], 2: [4], 3: [4]}.get(stage, [])
        self._supersede(d, [STAGE_FILES[s] for s in downstream])
        for s in downstream:
            meta.pop(str(s), None)
        (d / STAGE_FILES[stage].replace(".json", ".invalid.json")).unlink(missing_ok=True)
        write_json(d / STAGE_FILES[stage], obj)
        meta[str(stage)] = {"time": now(), "provider": self.cfg.provider, "model": self.provider.model,
                            "effort": self.cfg.effort, "batch": self.cfg.batch,
                            "prompt_sha256": self.fw.prompt_hash(stage),
                            **({"codebook_hash": self.fw.codebook["hash"],
                                "codebook_version": self.fw.codebook["version"]} if stage == 4 else {})}
        write_json(d / META_FILE, meta)
        status = obj.get("annotation_status")
        flag = "needs_review" if status == "needs_review" or warnings else "ok"
        with self._lock:
            self.summary.ok += flag == "ok"
            self.summary.review += flag != "ok"
        self.log(f"   {cid} stage {stage}: saved ({status}{'; ' + str(len(warnings)) + ' warning(s)' if warnings else ''})",
                 cartoon_id=cid, stage=stage, status=flag)

    def _fail(self, stage, item, d, obj, previous, errors):
        bad = d / STAGE_FILES[stage].replace(".json", ".invalid.json")
        bad.write_text(json.dumps(obj, ensure_ascii=False, indent=2) if obj is not None else str(previous or ""),
                       encoding="utf-8")
        with self._lock:
            self.summary.failed += 1
        self.log(f"   {item['cartoon_id']} stage {stage}: FAILED ({errors[0] if errors else 'no output'}); saved {bad.name}")

    def _skip(self, item, stage, reason):
        with self._lock:
            self.summary.skipped += 1
        self.log(f"{item['cartoon_id']} stage {stage}: {reason}; skipped")

    def run(self):
        self._open_run()
        items = self.p.items(check_dates=self.with_context)
        if self.cfg.ids:
            want = set(self.cfg.ids)
            missing = want - {i["cartoon_id"] for i in items}
            if missing:
                self.log(f"Not in the images folder: {', '.join(sorted(missing))}")
            items = [i for i in items if i["cartoon_id"] in want]
        work = [it for it in items if self._todo(self.dir / it["cartoon_id"], self._meta(self.dir / it["cartoon_id"]))]
        if self.cfg.max_items is not None and len(work) > self.cfg.max_items:
            work = work[:self.cfg.max_items]
            self.summary.stopped = f"batch size reached (max {self.cfg.max_items} cartoons)"
        self.log(f"Run '{self.run_name}': {self.cfg.provider}/{self.provider.model}"
                 f"{' effort ' + self.cfg.effort if self.cfg.effort else ''}{' (batch mode)' if self.cfg.batch else ''}; "
                 f"stages {list(self.cfg.stages)}; {len(work)} of {len(items)} cartoon(s) need work.")
        try:
            if self.cfg.batch:
                self._run_batch(items, work)
            else:
                self._run_direct(work)
        except StopRun as e:
            self.summary.stopped = str(e)
        if self.cancel.is_set() and not self.summary.stopped:
            self.summary.stopped = "cancelled"
        self.log(("Stopped: " + self.summary.stopped + ". Run the same command again to continue.\n"
                  if self.summary.stopped else "") + "Done. " + self.summary.line())
        self.progress({"type": "end", "summary": self.summary})
        return self.summary

    def _run_direct(self, work):
        self.progress({"type": "start", "total": len(work), "run": self.run_name})
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, self.cfg.workers)) as pool:
            futures = {pool.submit(self._item, it): it for it in work}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except StopRun as e:
                    if not self.cancel.is_set():
                        self.summary.stopped = str(e)
                        self.cancel.set()
                except Exception as e:
                    self.log(f"{futures[fut]['cartoon_id']}: unexpected error: {e!r}")
                    with self._lock:
                        self.summary.failed += 1
                done += 1
                self.progress({"type": "progress", "done": done, "total": len(work)})
        self.summary.items = done

    def _item(self, item):
        d = self.dir / item["cartoon_id"]
        d.mkdir(parents=True, exist_ok=True)
        for st in self.cfg.stages:
            if self.cancel.is_set():
                return
            if st not in self._todo(d, self._meta(d)):
                continue
            outs, reason = self._ready(st, item, d)
            if outs is None:
                self._skip(item, st, reason)
                if st in ANNOTATION_STAGES and "image" in reason:
                    return
                continue
            self.log(f"{item['cartoon_id']} stage {st} ...", cartoon_id=item["cartoon_id"], stage=st)
            if not self._stage_direct(st, item, d, outs) and st == 1:
                return

    def _stage_direct(self, stage, item, d, outs):
        errors, previous, obj = [], None, None
        for attempt in range(1, self.cfg.retries + 2):
            req = self._request(stage, item, outs, errors, previous)
            entry = self._entry(stage, item, attempt)
            t0 = time.time()
            try:
                try:
                    resp = self.provider.generate(req["system"], req["user"], req["image"], req["schema"], req["name"])
                except SchemaRejected as e:
                    self.log(f"   the API rejected the output schema ({str(e)[:150]}); continuing without it")
                    self.use_schema = False
                    entry["structured_output"] = False
                    resp = self.provider.generate(req["system"], req["user"], req["image"], None, req["name"])
            except StopRun as e:
                self._log_entry(d, {**entry, "stopped": str(e)})
                raise
            except ProviderError as e:
                resp = e
            entry["seconds"] = round(time.time() - t0, 1)
            obj, errors, warnings, previous = self._evaluate(stage, item, d, outs, resp, entry)
            if not errors:
                self._save(stage, item, d, obj, warnings)
                return True
            self.log(f"   {item['cartoon_id']} stage {stage} attempt {attempt}: {len(errors)} problem(s): {errors[0]}")
        self._fail(stage, item, d, obj, previous, errors)
        return False

    def _run_batch(self, items, work):
        by_id = {it["cartoon_id"]: it for it in items}
        self._collect_pending_batch(by_id)
        for st in self.cfg.stages:
            pending = {}
            for it in work:
                d = self.dir / it["cartoon_id"]
                if st not in self._todo(d, self._meta(d), [st]):
                    continue
                outs, reason = self._ready(st, it, d)
                if outs is None:
                    self._skip(it, st, reason)
                    continue
                pending[it["cartoon_id"]] = {"errors": [], "previous": None}
            for attempt in range(1, self.cfg.retries + 2):
                if not pending or self.cancel.is_set():
                    break
                self.log(f"Stage {st}, round {attempt}: submitting {len(pending)} request(s) as a batch ...")
                results = self._batch_round(st, attempt, {cid: (by_id[cid], s) for cid, s in pending.items()})
                still = {}
                for cid, state in pending.items():
                    it, d = by_id[cid], self.dir / cid
                    outs = self._outs(d)
                    obj, errors, warnings, previous = self._evaluate(
                        st, it, d, outs, results.get(cid, ProviderError("no result returned")),
                        self._entry(st, it, attempt))
                    if not errors:
                        self._save(st, it, d, obj, warnings)
                    elif attempt == self.cfg.retries + 1:
                        self._fail(st, it, d, obj, previous, errors)
                    else:
                        still[cid] = {"errors": errors, "previous": previous}
                pending = still
        self.summary.items = len(work)

    def _batch_round(self, stage, attempt, todo):
        rid_of, size = {}, 0
        chunks = [[]]
        for i, (cid, (item, state)) in enumerate(todo.items()):
            d = self.dir / cid
            d.mkdir(parents=True, exist_ok=True)
            req = {"rid": f"r{i}", **self._request(stage, item, self._outs(d), state["errors"], state["previous"])}
            rid_of[req["rid"]] = cid
            est = len(req["system"]) + len(req["user"]) + (int(req["image"].stat().st_size * 1.4) if req["image"] else 0)
            if chunks[-1] and size + est > self.provider.batch_limit_bytes:
                chunks.append([])
                size = 0
            chunks[-1].append(req)
            size += est
        try:
            jobs = [self.provider.batch_submit(c) for c in chunks]
        except SchemaRejected as e:
            self.log(f"   the API rejected the output schema ({str(e)[:150]}); continuing without it")
            self.use_schema = False
            jobs = [self.provider.batch_submit([dict(q, schema=None) for q in c]) for c in chunks]
        state = {"stage": stage, "attempt": attempt, "jobs": jobs, "map": rid_of, "submitted": now()}
        write_json(self.dir / BATCH_FILE, state)
        self.log(f"   submitted {len(jobs)} batch job(s): {', '.join(jobs)}. Results usually arrive within hours "
                 "(at most 24); you can close the app and start the run again later to collect them.")
        results = self._wait_and_collect(state)
        (self.dir / BATCH_FILE).unlink(missing_ok=True)
        return results

    def _wait_and_collect(self, state):
        open_jobs, results = list(state["jobs"]), {}
        self.progress({"type": "start", "total": len(open_jobs), "run": self.run_name})
        while open_jobs:
            for job in list(open_jobs):
                status, detail = self.provider.batch_poll(job)
                if status == "running":
                    continue
                open_jobs.remove(job)
                if status == "failed":
                    self.log(f"   batch {job} failed: {detail}")
                    continue
                for rid, resp in self.provider.batch_results(job).items():
                    if rid in state["map"]:
                        results[state["map"][rid]] = resp
                self.log(f"   batch {job} finished ({detail})")
            self.progress({"type": "progress", "done": len(state["jobs"]) - len(open_jobs), "total": len(state["jobs"])})
            if open_jobs:
                self.log(f"   waiting for {len(open_jobs)} batch job(s) ...")
                if self.cancel.wait(POLL_SECONDS):
                    raise StopRun("stopped while the batch is processing; start the run again later to collect the results")
        return results

    def _collect_pending_batch(self, by_id):
        f = self.dir / BATCH_FILE
        if not f.is_file():
            return
        state = read_json(f)
        self.log(f"Collecting the batch submitted {state['submitted']} (stage {state['stage']}) ...")
        results = self._wait_and_collect(state)
        st = state["stage"]
        for cid, resp in results.items():
            it, d = by_id.get(cid), self.dir / cid
            if it is None:
                continue
            outs = self._outs(d)
            obj, errors, warnings, previous = self._evaluate(st, it, d, outs, resp, self._entry(st, it, state["attempt"]))
            if not errors:
                self._save(st, it, d, obj, warnings)
            else:
                self._fail(st, it, d, obj, previous, errors)
        f.unlink(missing_ok=True)

    @staticmethod
    def _log_entry(item_dir, entry):
        item_dir.mkdir(parents=True, exist_ok=True)
        with open(item_dir / LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    @staticmethod
    def _supersede(item_dir, names):
        moved = [n for n in names if (item_dir / n).exists()]
        if not moved:
            return
        dest = item_dir / "superseded" / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest.mkdir(parents=True, exist_ok=True)
        for n in moved:
            shutil.move(str(item_dir / n), str(dest / n))


def dry_run(project, stages, out_dir=None):
    fw = project.framework()
    items = project.items(check_dates=False)
    if not items:
        raise ProjectError("The images folder has no cartoons yet.")
    item = items[0]
    ex = core()["examples"][0]["stages"]
    filler = {st: {"schema_version": schema_version(), "cartoon_id": item["cartoon_id"], **ex[st]}
              for st in ANNOTATION_STAGES}
    d = out_dir or project.runs_dir / "_dryrun"
    d.mkdir(parents=True, exist_ok=True)
    with_context = bool(project.settings.get("send_context"))
    for st in stages:
        if st == 4 and not fw.has_coding:
            continue
        (d / f"stage{st}.system.md").write_text(fw.system_prompt(st), encoding="utf-8")
        (d / f"stage{st}.user.md").write_text(
            user_message(stage_input(st, item, filler, fw, with_context=with_context)), encoding="utf-8")
        (d / f"stage{st}.schema.json").write_text(json.dumps(fw.schema(st), indent=2), encoding="utf-8")
    return d
