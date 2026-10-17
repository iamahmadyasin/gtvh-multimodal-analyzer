"""Self-update of the packaged app from its GitHub repository.

Compares the installed commit with the latest commit on the configured branch and
replaces the app's gtvh folder, keeping the previous version for rollback.

Typical usage example:

  release = available()
  if release:
      install(release)
"""

import datetime as dt
import io
import json
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

CONFIG_FILE = "update.json"
VERSION_FILE = "version.json"
TIMEOUT = 15


class UpdateError(Exception):
    pass


def app_dir():
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None


def _read(name):
    d = app_dir()
    f = d / name if d else None
    return json.loads(f.read_text(encoding="utf-8-sig")) if f and f.is_file() else {}


def repository():
    return (_read(CONFIG_FILE).get("repository") or "").strip()


def branch():
    return (_read(CONFIG_FILE).get("branch") or "main").strip()


def enabled():
    return app_dir() is not None and "/" in repository()


def installed():
    return _read(VERSION_FILE)


def _get(url):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "gtvh-multimodal-analyzer"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except OSError as e:
        raise UpdateError(f"Could not reach GitHub: {e}") from e


def latest():
    data = json.loads(_get(f"https://api.github.com/repos/{repository()}/commits/{branch()}"))
    commit = data["commit"]
    return {"sha": data["sha"], "date": commit["committer"]["date"],
            "message": commit["message"].splitlines()[0]}


def available():
    if not enabled():
        return None
    new = latest()
    return new if new["sha"] != installed().get("sha") else None


def install(release):
    d = app_dir()
    archive = zipfile.ZipFile(io.BytesIO(_get(f"https://api.github.com/repos/{repository()}/zipball/{release['sha']}")))
    staging = d / "gtvh.new"
    shutil.rmtree(staging, ignore_errors=True)
    found = False
    for name in archive.namelist():
        parts = name.split("/", 1)
        if len(parts) < 2 or not parts[1].startswith("gtvh/") or name.endswith("/"):
            continue
        rel = Path(parts[1]).relative_to("gtvh")
        if "__pycache__" in rel.parts:
            continue
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(name))
        found = True
    if not found or not (staging / "__init__.py").is_file() or not (staging / "resources").is_dir():
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError("The repository does not contain the gtvh package; nothing was changed.")
    current, previous = d / "gtvh", d / "gtvh.previous"
    shutil.rmtree(previous, ignore_errors=True)
    if current.exists():
        current.rename(previous)
    staging.rename(current)
    (d / VERSION_FILE).write_text(json.dumps({**release, "installed": dt.datetime.now(dt.timezone.utc)
                                              .isoformat(timespec="seconds")}, indent=2), encoding="utf-8")
