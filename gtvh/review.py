"""Human review of model annotations.

Accepted or corrected stage outputs are saved to adjudicated/, with who reviewed
them and when; a random review sample estimates model accuracy.

Typical usage example:

  accept(project, run, cartoon_id, stage=1, reviewer="A. Researcher")
  rows = summary(project, run)
"""

import datetime as dt
import random

from .framework import STAGE_FILES, STAGES
from .project import ADJUDICATED, ProjectError, read_json, write_json

REVIEW_FILE = "review.json"
SAMPLE_FILE = "review_sample.json"


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _dir(project, cid):
    return project.run_dir(ADJUDICATED) / cid


def reviews(project, cid):
    f = _dir(project, cid) / REVIEW_FILE
    return {int(k): v for k, v in read_json(f).items()} if f.is_file() else {}


def _record(project, cid, stage, record):
    d = _dir(project, cid)
    d.mkdir(parents=True, exist_ok=True)
    recs = {str(k): v for k, v in reviews(project, cid).items()}
    if record is None:
        recs.pop(str(stage), None)
    else:
        recs[str(stage)] = record
    if recs:
        write_json(d / REVIEW_FILE, recs)
    else:
        (d / REVIEW_FILE).unlink(missing_ok=True)


def accept(project, run, cid, stage, reviewer, note=None):
    src = project.run_dir(run) / cid / STAGE_FILES[stage]
    if not src.is_file():
        raise ProjectError(f"{run}/{cid} has no stage {stage} output to accept")
    _dir(project, cid).mkdir(parents=True, exist_ok=True)
    write_json(_dir(project, cid) / STAGE_FILES[stage], read_json(src))
    _record(project, cid, stage, {"status": "accepted", "reviewer": reviewer, "time": _now(), "run": run,
                                  "note": note})


def correct(project, run, cid, stage, obj, reviewer, note=None):
    _dir(project, cid).mkdir(parents=True, exist_ok=True)
    write_json(_dir(project, cid) / STAGE_FILES[stage], obj)
    _record(project, cid, stage, {"status": "corrected", "reviewer": reviewer, "time": _now(), "run": run,
                                  "note": note})


def undo(project, cid, stage):
    (_dir(project, cid) / STAGE_FILES[stage]).unlink(missing_ok=True)
    _record(project, cid, stage, None)


def draw_sample(project, run, n, seed=None):
    ids = sorted(d.name for d in project.item_dirs(run) if (d / STAGE_FILES[1]).is_file())
    if not ids:
        raise ProjectError(f"Run '{run}' has no annotated cartoons to sample.")
    seed = random.randrange(1, 10**6) if seed is None else seed
    chosen = sorted(random.Random(seed).sample(ids, min(n, len(ids))))
    rec = {"run": run, "seed": seed, "n": len(chosen), "population": len(ids), "created": _now(), "ids": chosen}
    project.run_dir(ADJUDICATED).mkdir(parents=True, exist_ok=True)
    write_json(project.run_dir(ADJUDICATED) / SAMPLE_FILE, rec)
    return rec


def sample(project):
    f = project.run_dir(ADJUDICATED) / SAMPLE_FILE
    return read_json(f) if f.is_file() else None


def summary(project, run, ids=None):
    rows = []
    dirs = [d for d in project.item_dirs(run) if ids is None or d.name in set(ids)]
    recs = {d.name: reviews(project, d.name) for d in dirs}
    for st in STAGES:
        have = [d.name for d in dirs if (d / STAGE_FILES[st]).is_file()]
        mine = [recs[c][st] for c in have if st in recs[c] and recs[c][st].get("run") == run]
        acc = sum(r["status"] == "accepted" for r in mine)
        rows.append({"stage": st, "outputs": len(have), "reviewed": len(mine), "accepted": acc,
                     "corrected": len(mine) - acc,
                     "accepted_share": round(acc / len(mine), 3) if mine else None})
    return rows
