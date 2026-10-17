"""A GTVH project: a folder with settings, images, optional metadata, codebook and runs.

Every image in the images folder is a cartoon, identified by its file name.
metadata.csv optionally adds date, cartoonist, publication and notes per image.

Typical usage example:

  project = Project.create("my-study", "My study")
  project.build_metadata()
"""

import csv
import datetime as dt
import json
import re
import shutil
from pathlib import Path

import yaml

from .framework import CODING_DIMENSIONS, STAGE_FILES, Framework, check_category_list, sha256

PROJECT_FILE = "project.yaml"
METADATA_FILE = "metadata.csv"
CODEBOOK_FILE = "codebook.yaml"
DRAFT_FILE = "codebook.draft.yaml"
METADATA_COLUMNS = ["cartoon_id", "image_file", "date", "cartoonist", "publication", "context_notes"]
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
ADJUDICATED = "adjudicated"
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
NAME_DATE_RE = re.compile(r"(\d{4})-?(\d{2})-?(\d{2})")

PROJECT_TEMPLATE = """\
name: {name}
description: {description}
material: editorial cartoons
send_context: false
corpus_context: ""
research_questions: []
images: images
extensions: {{}}
defaults:
  provider: anthropic
  model: null
  effort: null
  workers: 4
"""


class ProjectError(Exception):
    pass


def parse_file_name(stem):
    parts = stem.split("_")
    for i, part in enumerate(parts):
        m = NAME_DATE_RE.fullmatch(part)
        if m:
            try:
                date = dt.date(int(m[1]), int(m[2]), int(m[3])).isoformat()
            except ValueError:
                return None
            words = [x.replace("-", " ").strip().title() for x in parts[i + 1:i + 3]]
            return {"date": date, "cartoonist": words[0] if words else "",
                    "publication": words[1] if len(words) > 1 else ""}
    return None


def slug(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "run"


def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class Project:
    def __init__(self, root):
        self.root = Path(root).resolve()
        f = self.root / PROJECT_FILE
        if not f.is_file():
            raise ProjectError(f"No {PROJECT_FILE} in {self.root}. Create a project first (gtvh init).")
        self.settings = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        self._fw = None

    @classmethod
    def create(cls, root, name, description=""):
        root = Path(root)
        if (root / PROJECT_FILE).exists():
            raise ProjectError(f"{root} already contains a project")
        root.mkdir(parents=True, exist_ok=True)
        (root / "images").mkdir(exist_ok=True)
        (root / PROJECT_FILE).write_text(
            PROJECT_TEMPLATE.format(name=json.dumps(name), description=json.dumps(description)), encoding="utf-8")
        return cls(root)

    @property
    def name(self):
        return self.settings.get("name") or self.root.name

    @property
    def images_dir(self):
        return self.root / (self.settings.get("images") or "images")

    @property
    def metadata_path(self):
        return self.root / METADATA_FILE

    @property
    def codebook_path(self):
        return self.root / CODEBOOK_FILE

    @property
    def draft_path(self):
        return self.root / DRAFT_FILE

    @property
    def runs_dir(self):
        return self.root / "runs"

    @property
    def exports_dir(self):
        return self.root / "exports"

    def run_dir(self, run):
        return self.root / ADJUDICATED if run == ADJUDICATED else self.runs_dir / run

    def runs(self):
        names = sorted(p.name for p in self.runs_dir.iterdir() if p.is_dir() and not p.name.startswith("_")) \
            if self.runs_dir.is_dir() else []
        if (self.root / ADJUDICATED).is_dir():
            names.append(ADJUDICATED)
        return names

    def defaults(self):
        return dict(self.settings.get("defaults") or {})

    def save_settings(self, updates):
        f = self.root / PROJECT_FILE
        text = f.read_text(encoding="utf-8")
        for key, value in updates.items():
            dumped = yaml.safe_dump({key: value}, allow_unicode=True, sort_keys=False, width=1000).rstrip("\n")
            pat = re.compile(rf"^{re.escape(key)}:.*(?:\n(?:[ \t]+.*|-.*))*", re.M)
            text, n = pat.subn(lambda m: dumped, text, count=1)
            if not n:
                text = text.rstrip("\n") + "\n" + dumped + "\n"
        f.write_text(text, encoding="utf-8")
        self.settings = yaml.safe_load(text) or {}
        self._fw = None

    def load_codebook(self, path=None):
        path = path or self.codebook_path
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8")
        cb = yaml.safe_load(raw) or {}
        if not any(cb.get(d) for d in CODING_DIMENSIONS):
            return None
        cb["hash"] = sha256(json.dumps({d: cb.get(d) for d in (*CODING_DIMENSIONS, "guidance")},
                                       sort_keys=True, ensure_ascii=False))[:12]
        cb["version"] = str(cb.get("version") or cb["hash"])
        return cb

    def codebook_problems(self, path=None):
        path = path or self.codebook_path
        if not path.is_file():
            return [f"{path.name} not found"]
        try:
            cb = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            return [f"{path.name} is not valid YAML: {e}"]
        problems = []
        for dim in CODING_DIMENSIONS:
            if cb.get(dim):
                problems += check_category_list(cb[dim], dim)
        if not any(cb.get(d) for d in CODING_DIMENSIONS):
            problems.append("the codebook has no opposition, target or theme categories")
        return problems

    def framework(self):
        if self._fw is None:
            problems = self.codebook_problems() if self.codebook_path.is_file() else []
            if problems:
                raise ProjectError("codebook.yaml: " + "; ".join(problems))
            self._fw = Framework({**self.settings, "name": self.name}, self.load_codebook())
        return self._fw

    def reload(self):
        self.__init__(self.root)

    def image_files(self):
        d = self.images_dir
        return sorted(f for f in d.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS) if d.is_dir() else []

    def metadata(self):
        if not self.metadata_path.is_file():
            return {}
        rows = {}
        with open(self.metadata_path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
                if r.get("image_file"):
                    rows[r["image_file"]] = r
        return rows

    def items(self, check_dates=True):
        meta, rows, ids = self.metadata(), [], set()
        for f in self.image_files():
            cid, n = slug(f.stem), 2
            base = cid
            while cid in ids:
                cid, n = f"{base}-{n}", n + 1
            ids.add(cid)
            row = {c: "" for c in METADATA_COLUMNS}
            row.update(meta.get(f.name, {}))
            row.update(cartoon_id=cid, image_file=f.name)
            rows.append(row)
        if check_dates:
            bad = [r["cartoon_id"] for r in rows if r["date"] and not DATE_RE.fullmatch(r["date"])]
            if bad:
                raise ProjectError(
                    f"Dates in {METADATA_FILE} must be YYYY-MM-DD (or empty). Fix: {', '.join(bad[:10])}"
                    f"{' ...' if len(bad) > 10 else ''}. Excel rewrites dates in your local format when it saves a "
                    "CSV; edit the file in the app or a text editor instead.")
        return rows

    def columns(self, rows=None):
        cols = list(METADATA_COLUMNS)
        for r in rows if rows is not None else self.items(check_dates=False):
            cols += [k for k in r if k not in cols]
        return cols

    def save_metadata(self, rows):
        cols = self.columns(rows)
        with open(self.metadata_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k) or "" for k in cols})

    def build_metadata(self):
        rows, parsed = self.items(check_dates=False), 0
        for r in rows:
            found = parse_file_name(Path(r["image_file"]).stem)
            if found:
                parsed += 1
                for k, v in found.items():
                    if v and not r.get(k):
                        r[k] = v
        self.save_metadata(rows)
        return parsed, len(rows)

    def image_path(self, item):
        return self.images_dir / item["image_file"] if item.get("image_file") else None

    def import_images(self, paths):
        self.images_dir.mkdir(parents=True, exist_ok=True)
        files = []
        for p in map(Path, paths):
            files += sorted(x for x in p.iterdir() if x.is_file()) if p.is_dir() else [p]
        copied = []
        for src in files:
            dest = self.images_dir / src.name
            if src.suffix.lower() in IMAGE_EXTS and src.is_file() and not dest.exists():
                shutil.copy2(src, dest)
                copied.append(src.name)
        return copied

    def load_outputs(self, run, cartoon_id):
        d = self.run_dir(run) / cartoon_id
        return {st: read_json(d / f) for st, f in STAGE_FILES.items() if (d / f).is_file()}

    def item_dirs(self, run):
        d = self.run_dir(run)
        return sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith("_")) if d.is_dir() else []
