"""Working pages of the desktop app: cartoons, annotation, review, codebook, results.
"""

import json
import re

import yaml
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QRadioButton,
                               QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget)

from .. import review
from ..codebook import accept_draft, draft, induce
from ..compare import compare_runs
from ..export import check_item, export_run
from ..framework import CODING_DIMENSIONS, STAGE_FILES, STAGE_NAMES, STAGES
from ..pipeline import RunConfig, Runner, default_run_name, dry_run
from ..project import ADJUDICATED, DATE_RE, METADATA_COLUMNS, read_json
from ..providers import PROVIDERS, get_key, make_provider
from ..validate import Validator
from .app import (ImagePreview, KeysDialog, button, cell, error, hbox, info, note, open_path, run_in_thread,
                  table, table_rows)

STAGE_SHORT = {1: "SO", 2: "SI, TA, NS", 3: "LA, presuppositions, theme", 4: "corpus coding"}
STATUS_COLORS = {"ok": "#d7f0d7", "complete": "#d7f0d7", "needs_review": "#fff0c2", "warnings": "#fff0c2",
                 "invalid": "#f7d0d0", "errors": "#f7d0d0", "missing": "#eeeeee"}


class CartoonsPage(QWidget):
    title = "Cartoons"

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.dirty = False
        lay = QVBoxLayout(self)
        lay.addWidget(note("Every image in the project's images folder is a cartoon. Metadata is optional: fill it "
                           "in here, or name the files DATE_CARTOONIST_PUBLICATION (e.g. "
                           "2024-03-15_paresh-nath_cagle.png) and press 'Fill from file names'. Dates are YYYY-MM-DD. "
                           "The model sees metadata only if the project gives it context (Project tab)."))
        split = QSplitter()
        self.table = table([])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.itemChanged.connect(self._changed)
        self.table.currentCellChanged.connect(self._preview)
        split.addWidget(self.table)
        self.preview = ImagePreview()
        split.addWidget(self.preview)
        split.setSizes([800, 400])
        lay.addWidget(split, 1)
        self.count = QLabel()
        lay.addWidget(hbox(button("Add images…", self.add_files), button("Add folder…", self.add_folder),
                           button("Fill from file names", self.fill_from_names,
                                  "Read date, cartoonist and publication from file names; existing values are kept"),
                           button("Add column…", self.add_column), self.count,
                           button("Save metadata", self.save)))

    def load(self, p):
        self.refresh(force=True)

    def refresh(self, force=False):
        if self.dirty and not force:
            return
        p = self.win.project
        rows = p.items(check_dates=False)
        cols = p.columns(rows)
        self.table.blockSignals(True)
        self.table.clear()
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, k in enumerate(cols):
                self.table.setItem(r, c, cell(row.get(k), editable=k not in ("cartoon_id", "image_file")))
        self.table.resizeColumnsToContents()
        for c in range(len(cols)):
            self.table.setColumnWidth(c, min(self.table.columnWidth(c), 320))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.blockSignals(False)
        self.dirty = False
        self.count.setText(f"{len(rows)} cartoon(s)")

    def _changed(self, _item):
        self.dirty = True

    def _preview(self, row, *_):
        if row < 0:
            return
        cols = [self.table.horizontalHeaderItem(c).text() for c in range(self.table.columnCount())]
        it = self.table.item(row, cols.index("image_file"))
        self.preview.show_image(self.win.project.images_dir / it.text() if it else None)

    def rows(self):
        cols = [self.table.horizontalHeaderItem(c).text() for c in range(self.table.columnCount())]
        return [dict(zip(cols, r)) for r in table_rows(self.table)]

    def selected_ids(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, 0).text() for r in rows]

    def save(self):
        rows = self.rows()
        bad = [r["cartoon_id"] for r in rows if r.get("date") and not DATE_RE.fullmatch(r["date"])]
        if bad:
            return error(self, "Dates must be YYYY-MM-DD or empty. Check: " + ", ".join(bad[:10]))
        self.win.project.save_metadata(rows)
        self.dirty = False
        self.refresh(force=True)

    def _import(self, paths):
        if self.dirty:
            self.save()
        added = self.win.project.import_images(paths)
        self.refresh(force=True)
        info(self, f"Copied {len(added)} image(s) into the project.")

    def fill_from_names(self):
        if self.dirty:
            self.save()
        parsed, total = self.win.project.build_metadata()
        self.refresh(force=True)
        info(self, f"Read metadata from {parsed} of {total} file names. Files not named "
                   "DATE_CARTOONIST_PUBLICATION were left as they are.")

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Add cartoon images", "", "Images (*.png *.jpg *.jpeg *.gif *.webp)")
        if files:
            self._import(files)

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Add all images in a folder")
        if d:
            self._import([d])

    def add_column(self):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Add column", "Column name (e.g. country, series):")
        name = re.sub(r"\W+", "_", name.strip()).strip("_")
        if ok and name and name not in METADATA_COLUMNS:
            c = self.table.columnCount()
            self.table.insertColumn(c)
            self.table.setHorizontalHeaderItem(c, cell(name))
            for r in range(self.table.rowCount()):
                self.table.setItem(r, c, cell(""))
            self.dirty = True


class ModelPicker(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.provider = QComboBox()
        for name, spec in PROVIDERS.items():
            self.provider.addItem(spec["label"], name)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setMinimumWidth(240)
        self.effort = QComboBox()
        self.key = QLabel()
        lay.addWidget(hbox(QLabel("Provider"), self.provider, QLabel("Model"), self.model,
                           button("List models", self.list_models, "Ask the provider which models your key can use")))
        lay.addWidget(hbox(QLabel("Reasoning effort"), self.effort, self.key, button("API keys…", self.keys)))
        self.provider.currentIndexChanged.connect(self._provider_changed)

    @property
    def name(self):
        return self.provider.currentData()

    def load(self, p):
        d = p.defaults()
        i = self.provider.findData(d.get("provider") or "anthropic")
        self.provider.setCurrentIndex(max(i, 0))
        self._provider_changed()
        if d.get("model") and d.get("provider") == self.name:
            self.model.setCurrentText(d["model"])
        if d.get("effort"):
            self.effort.setCurrentText(d["effort"])

    def _provider_changed(self):
        spec = PROVIDERS[self.name]
        self.model.clear()
        if spec["default_model"]:
            self.model.addItem(spec["default_model"])
        self.effort.clear()
        self.effort.addItem("default")
        self.effort.addItems(spec["efforts"])
        self.effort.setEnabled(bool(spec["efforts"]))
        self.refresh_key()

    def refresh_key(self):
        _, source = get_key(self.name)
        self.key.setText("key: " + (source or "<b style='color:#b00'>missing</b>"))

    def keys(self):
        KeysDialog(self).exec()
        self.refresh_key()

    def values(self):
        e = self.effort.currentText()
        return self.name, self.model.currentText().strip() or None, (None if e in ("", "default") else e)

    def make(self, cancel=None):
        name, model, effort = self.values()
        return make_provider(name, model=model, effort=effort, cancel=cancel)

    def list_models(self):
        name, model, _ = self.values()

        def work(emit, cancel):
            return make_provider(name, model=model or "-").list_models()

        def done(models):
            current = self.model.currentText()
            self.model.clear()
            self.model.addItems(models)
            self.model.setCurrentText(current if current in models or not models else models[0])
            info(self, f"{len(models)} model(s) available. Fix one model for the whole study and report it.")
        run_in_thread(work, on_done=done, on_failed=lambda m: error(self, m))


class AnnotatePage(QWidget):
    title = "Annotate"

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.cancel = None
        lay = QVBoxLayout(self)
        self.picker = ModelPicker(win)
        lay.addWidget(self.picker)

        box = QGroupBox("What to run")
        form = QFormLayout(box)
        self.stage_boxes = {}
        sw = QWidget()
        sl = QHBoxLayout(sw)
        sl.setContentsMargins(0, 0, 0, 0)
        for st in STAGES:
            cb = QCheckBox(f"{st}: {STAGE_SHORT[st]}")
            cb.setToolTip(STAGE_NAMES[st])
            cb.setChecked(st != 4)
            self.stage_boxes[st] = cb
            sl.addWidget(cb)
        sl.addStretch(1)
        form.addRow("Stages", sw)
        self.scope_all = QRadioButton("All cartoons")
        self.scope_ids = QRadioButton("Only these IDs:")
        self.ids = QLineEdit()
        self.ids.setPlaceholderText("e.g. AI001 AI002")
        self.ids.setToolTip("Or select rows in the Cartoons tab and press 'Use selection'")
        grp = QButtonGroup(self)
        grp.addButton(self.scope_all)
        grp.addButton(self.scope_ids)
        self.scope_all.setChecked(True)
        form.addRow("Cartoons", hbox(self.scope_all, self.scope_ids, self.ids,
                                     button("Use selection", self.use_selection)))
        self.max_items = QSpinBox()
        self.max_items.setRange(0, 100000)
        self.max_items.setSpecialValueText("no limit")
        self.workers = QSpinBox()
        self.workers.setRange(1, 32)
        self.workers.setValue(4)
        self.retries = QSpinBox()
        self.retries.setRange(0, 3)
        self.retries.setValue(1)
        self.force = QCheckBox("Redo stages that already have output")
        self.batch = QCheckBox("Batch mode: about half the price; results arrive within 24 hours")
        form.addRow("Batch size", hbox(self.max_items, QLabel("Cartoons in parallel"), self.workers,
                                       QLabel("Retries after a failed check"), self.retries))
        form.addRow("", self.force)
        form.addRow("", self.batch)
        self.run_name = QLineEdit()
        self.run_name.setPlaceholderText("automatic: provider-model[-effort]")
        form.addRow("Run name", self.run_name)
        lay.addWidget(box)

        self.start_btn = button("Start", self.start)
        self.stop_btn = button("Stop", self.stop)
        self.stop_btn.setEnabled(False)
        lay.addWidget(hbox(button("Preview prompts", self.preview,
                                  "Write the exact prompts for the first cartoon to a folder; no model is called"),
                           self.start_btn, self.stop_btn))
        self.progress = QProgressBar()
        lay.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        lay.addWidget(self.log, 1)

    def load(self, p):
        self.picker.load(p)
        self.workers.setValue(int(p.defaults().get("workers") or 4))
        self.refresh()

    def refresh(self):
        has = self.win.project.framework().has_coding
        self.stage_boxes[4].setEnabled(has)
        self.stage_boxes[4].setToolTip("" if has else "Needs a codebook (Codebook tab)")
        if not has:
            self.stage_boxes[4].setChecked(False)
        self.picker.refresh_key()

    def use_selection(self):
        ids = self.win.page("CartoonsPage").selected_ids()
        if ids:
            self.ids.setText(" ".join(ids))
            self.scope_ids.setChecked(True)

    def stages(self):
        return tuple(st for st, cb in self.stage_boxes.items() if cb.isChecked() and cb.isEnabled())

    def preview(self):
        try:
            d = dry_run(self.win.project, self.stages() or (1, 2, 3))
        except Exception as e:
            return error(self, e)
        open_path(d)

    def config(self):
        name, model, effort = self.picker.values()
        ids = re.split(r"[\s,;]+", self.ids.text().strip()) if self.scope_ids.isChecked() and self.ids.text().strip() else None
        return RunConfig(provider=name, model=model, effort=effort, run=self.run_name.text().strip() or None,
                         stages=self.stages(), ids=ids, max_items=self.max_items.value() or None,
                         force=self.force.isChecked(), retries=self.retries.value(), workers=self.workers.value(),
                         batch=self.batch.isChecked())

    def start(self):
        cfg = self.config()
        if not cfg.stages:
            return error(self, "Choose at least one stage.")
        n = len(cfg.ids) if cfg.ids else len(self.win.project.items(check_dates=False))
        if cfg.max_items:
            n = min(n, cfg.max_items)
        run = cfg.run or default_run_name(cfg.provider, cfg.model or PROVIDERS[cfg.provider]["default_model"],
                                          cfg.effort)
        if QMessageBox.question(
                self, "Start annotation",
                f"Annotate up to {n} cartoon(s), stages {list(cfg.stages)}, with {cfg.provider} / {cfg.model}?\n\n"
                f"Results go to runs/{run}. Finished stages are skipped, so you can stop and continue later.\n"
                + ("Batch mode: the requests are submitted as batches and collected when ready; you can close the "
                   "app and press Start again later to collect them.\n" if cfg.batch else "") +
                "This uses your API credits.") != QMessageBox.Yes:
            return
        if self.win.page("CartoonsPage").dirty:
            self.win.page("CartoonsPage").save()
        self.log.clear()
        self.progress.setValue(0)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        project = self.win.project

        def work(emit, cancel):
            return Runner(project, cfg, progress=emit, cancel=cancel).run()

        self.cancel = run_in_thread(work, on_event=self._event, on_done=self._done, on_failed=self._failed)

    def stop(self):
        if self.cancel:
            self.cancel.set()
            self.log.appendPlainText("Stopping after the calls in progress finish ...")
            self.stop_btn.setEnabled(False)

    def _event(self, ev):
        if ev.get("msg"):
            self.log.appendPlainText(ev["msg"])
        if ev["type"] in ("start", "progress"):
            self.progress.setMaximum(max(ev["total"], 1))
            self.progress.setValue(ev.get("done", 0))

    def _done(self, summary):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.win.page("ReviewPage").refresh()

    def _failed(self, msg):
        self._done(None)
        self.log.appendPlainText("Error: " + msg)
        error(self, msg)


class JsonEditor(QDialog):
    def __init__(self, parent, project, cid, stage, obj, outs):
        super().__init__(parent)
        self.p, self.cid, self.stage, self.outs = project, cid, stage, outs
        self.result_obj = None
        self.setWindowTitle(f"Correct {cid}, stage {stage}")
        self.resize(900, 750)
        lay = QVBoxLayout(self)
        lay.addWidget(note(f"Edit the annotation and save. It is stored as the reviewed version "
                           f"(adjudicated/{cid}/{STAGE_FILES[stage]}); the model output is never changed."))
        self.edit = QPlainTextEdit(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2))
        lay.addWidget(self.edit, 1)
        self.note = QLineEdit()
        self.note.setPlaceholderText("Optional note on what was corrected and why")
        lay.addWidget(self.note)
        self.msg = QLabel()
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.addButton("Check", QDialogButtonBox.ActionRole).clicked.connect(self.check)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def check(self):
        try:
            obj = json.loads(self.edit.toPlainText())
        except json.JSONDecodeError as e:
            self.msg.setText(f"Not valid JSON: {e}")
            return None, ["json"]
        errs, warns = Validator(self.p.framework()).stage(self.stage, obj, self.cid, {**self.outs, self.stage: obj})
        self.msg.setText("\n".join(errs + ["warning: " + w for w in warns]) or "Valid.")
        return obj, errs

    def save(self):
        obj, errs = self.check()
        if obj is None:
            return
        if errs and QMessageBox.question(self, "Save anyway?", "The annotation has validation errors. Save anyway?") \
                != QMessageBox.Yes:
            return
        self.result_obj = obj
        self.accept()


class ReviewPage(QWidget):
    title = "Review"
    SHOW = ["Needs attention", "Not yet reviewed", "Review sample", "All cartoons"]
    MARK = {"accepted": "ok", "corrected": "fixed"}

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.results, self.recs = {}, {}
        lay = QVBoxLayout(self)
        self.run = QComboBox()
        self.run.currentIndexChanged.connect(self.load_run)
        self.show_what = QComboBox()
        self.show_what.addItems(self.SHOW)
        self.show_what.currentIndexChanged.connect(self.load_run)
        self.reviewer = QLineEdit(self.win.settings.value("reviewer", "") or "")
        self.reviewer.setPlaceholderText("your name")
        self.reviewer.setMaximumWidth(160)
        self.reviewer.editingFinished.connect(lambda: self.win.settings.setValue("reviewer", self.reviewer.text()))
        lay.addWidget(hbox(QLabel("Run"), self.run, QLabel("Show"), self.show_what, QLabel("Reviewer"),
                           self.reviewer, button("Reload", self.load_run)))
        self.stats = note("")
        lay.addWidget(self.stats)
        split = QSplitter()
        self.table = table(["cartoon"] + [f"stage {s}" for s in STAGES] + ["reviewed"], stretch_last=False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.currentCellChanged.connect(self.show_item)
        split.addWidget(self.table)
        right = QSplitter(Qt.Vertical)
        self.preview = ImagePreview()
        right.addWidget(self.preview)
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        self.stage_tabs = QTabWidget()
        self.views = {}
        for st in STAGES:
            v = QPlainTextEdit()
            v.setReadOnly(True)
            self.views[st] = v
            self.stage_tabs.addTab(v, f"Stage {st}")
        self.stage_tabs.currentChanged.connect(self._update_review_label)
        bl.addWidget(self.stage_tabs, 1)
        self.review_label = note("")
        bl.addWidget(self.review_label)
        right.addWidget(box)
        right.setSizes([300, 500])
        split.addWidget(right)
        split.setSizes([430, 800])
        lay.addWidget(split, 1)
        lay.addWidget(hbox(button("Accept  (Ctrl+Enter)", self.accept_current,
                                  "The output is correct as it is; record it as reviewed and go to the next one"),
                           button("Correct…", self.correct_current, "Edit the output and save the corrected version"),
                           button("Undo review", self.undo_current),
                           button("Draw review sample…", self.draw_sample,
                                  "A random sample of the run for estimating model accuracy"),
                           button("Open cartoon folder", self.open_folder)))
        QShortcut(QKeySequence("Ctrl+Return"), self, self.accept_current)

    def load(self, p):
        self.refresh()

    def refresh(self):
        current = self.run.currentText()
        self.run.blockSignals(True)
        self.run.clear()
        self.run.addItems([r for r in self.win.project.runs() if r != ADJUDICATED])
        if current:
            self.run.setCurrentText(current)
        self.run.blockSignals(False)
        self.load_run()

    def _states(self, res):
        states = {}
        for st in STAGES:
            obj, errs, warns, invalid = res.get(st, (None, [], [], False))
            states[st] = ("invalid" if obj is None and invalid else "missing" if obj is None else
                          "errors" if errs else "needs_review" if obj["annotation_status"] == "needs_review"
                          else "warnings" if warns else "ok")
        return states

    def _reviewed(self, cid):
        run = self.run.currentText()
        return {st: r for st, r in self.recs.get(cid, {}).items() if r.get("run") == run}

    def load_run(self):
        p, run = self.win.project, self.run.currentText()
        self.table.setRowCount(0)
        self.results, self.recs = {}, {}
        self._update_stats()
        if not run:
            return
        v = Validator(p.framework())
        show = self.show_what.currentText()
        sample_ids = set((review.sample(p) or {}).get("ids", []))
        for d in p.item_dirs(run):
            cid = d.name
            res = check_item(p, v, d)
            self.recs[cid] = review.reviews(p, cid)
            states = self._states(res)
            done = self._reviewed(cid)
            pending = [st for st in STAGES if states[st] != "missing" and st not in done]
            if show == "Needs attention" and not any(
                    states[st] in ("errors", "needs_review", "warnings", "invalid") and st not in done for st in STAGES):
                continue
            if show == "Not yet reviewed" and not pending:
                continue
            if show == "Review sample" and cid not in sample_ids:
                continue
            self.results[cid] = res
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, cell(cid, False))
            for st in STAGES:
                it = cell(states[st], False)
                it.setBackground(QColor(STATUS_COLORS[states[st]]))
                self.table.setItem(r, st, it)
            self._set_reviewed_cell(r, cid)
        self.table.resizeColumnsToContents()
        self._update_stats()
        if self.table.rowCount():
            self.table.setCurrentCell(0, 0)

    def _set_reviewed_cell(self, row, cid):
        done = self._reviewed(cid)
        self.table.setItem(row, len(STAGES) + 1,
                           cell(", ".join(f"{st} {self.MARK[done[st]['status']]}" for st in sorted(done)), False))

    def _update_stats(self):
        p, run = self.win.project, self.run.currentText()
        if not run:
            self.stats.setText("No runs yet.")
            return
        rows = review.summary(p, run)
        parts = [f"stage {r['stage']}: {r['reviewed']}/{r['outputs']} reviewed ({r['accepted']} accepted, "
                 f"{r['corrected']} corrected)" for r in rows if r["outputs"]]
        s = review.sample(p)
        if s:
            srows = review.summary(p, run, s["ids"])
            parts.append(f"review sample ({s['n']} cartoons, seed {s['seed']}): " + ", ".join(
                f"stage {r['stage']} {r['reviewed']}/{r['outputs']}" for r in srows if r["outputs"]))
        self.stats.setText(" · ".join(parts))

    def _cid(self):
        r = self.table.currentRow()
        return self.table.item(r, 0).text() if r >= 0 and self.table.item(r, 0) else None

    def _stage(self):
        return self.stage_tabs.currentIndex() + 1

    def show_item(self, *_):
        cid = self._cid()
        if not cid:
            return
        p = self.win.project
        item = next((i for i in p.items(check_dates=False) if i["cartoon_id"] == cid), None)
        self.preview.show_image(p.image_path(item) if item else None)
        d = p.run_dir(self.run.currentText()) / cid
        for st in STAGES:
            obj, errs, warns, invalid = self.results[cid].get(st, (None, [], [], False))
            text = ""
            if errs or warns:
                text += "\n".join(["ERROR " + e for e in errs] + ["warning: " + w for w in warns]) + "\n\n"
            if obj is not None:
                text += json.dumps(obj, ensure_ascii=False, indent=2)
            elif invalid:
                f = d / STAGE_FILES[st].replace(".json", ".invalid.json")
                text += "Last failed attempt:\n" + f.read_text(encoding="utf-8")
            self.views[st].setPlainText(text or "(no output)")
        self._update_review_label()

    def _update_review_label(self, *_):
        cid = self._cid()
        if not cid:
            self.review_label.setText("")
            return
        st, done = self._stage(), self._reviewed(cid)
        rec = done.get(st)
        text = (f"Stage {st}: {rec['status']} by {rec['reviewer']} ({rec['time'][:10]})"
                + (f". Note: {rec['note']}" if rec.get("note") else "") if rec else f"Stage {st}: not reviewed yet.")
        if st > 1 and done.get(1, {}).get("status") == "corrected":
            text += " Stage 1 was corrected: check that this stage still fits the corrected oppositions."
        self.review_label.setText(text)

    def _reviewer_name(self):
        name = self.reviewer.text().strip()
        if not name:
            from PySide6.QtWidgets import QInputDialog
            name, ok = QInputDialog.getText(self, "Reviewer", "Your name (recorded with each review):")
            name = name.strip() if ok else ""
            if name:
                self.reviewer.setText(name)
                self.win.settings.setValue("reviewer", name)
        return name

    def _after_review(self, cid):
        self.recs[cid] = review.reviews(self.win.project, cid)
        self._set_reviewed_cell(self.table.currentRow(), cid)
        self._update_stats()
        nxt = next((s for s in STAGES if s > self._stage() and self.results[cid].get(s, (None,))[0] is not None
                    and s not in self._reviewed(cid)), None)
        if nxt:
            self.stage_tabs.setCurrentIndex(nxt - 1)
        elif self.table.currentRow() + 1 < self.table.rowCount():
            self.table.setCurrentCell(self.table.currentRow() + 1, 0)
            self.stage_tabs.setCurrentIndex(0)
        self._update_review_label()

    def accept_current(self):
        cid, st = self._cid(), self._stage()
        if not cid:
            return
        obj, errs, _, _ = self.results[cid].get(st, (None, [], [], False))
        if obj is None:
            return error(self, "This stage has no output to accept.")
        if errs:
            return error(self, "This output has validation errors; use Correct… instead.")
        name = self._reviewer_name()
        if not name:
            return
        review.accept(self.win.project, self.run.currentText(), cid, st, name)
        self._after_review(cid)

    def correct_current(self):
        cid, st = self._cid(), self._stage()
        if not cid:
            return
        name = self._reviewer_name()
        if not name:
            return
        p, run = self.win.project, self.run.currentText()
        adj = p.run_dir(ADJUDICATED) / cid / STAGE_FILES[st]
        start = read_json(adj) if adj.is_file() else self.results[cid].get(st, (None,))[0]
        if start is None:
            bad = p.run_dir(run) / cid / STAGE_FILES[st].replace(".json", ".invalid.json")
            start = bad.read_text(encoding="utf-8") if bad.is_file() else None
        if start is None:
            return error(self, "There is no output for this stage to start from.")
        outs = {**p.load_outputs(run, cid), **p.load_outputs(ADJUDICATED, cid)}
        dlg = JsonEditor(self, p, cid, st, start, outs)
        if dlg.exec() and dlg.result_obj is not None:
            review.correct(p, run, cid, st, dlg.result_obj, name, dlg.note.text().strip() or None)
            self._after_review(cid)

    def undo_current(self):
        cid, st = self._cid(), self._stage()
        if cid and st in self._reviewed(cid):
            review.undo(self.win.project, cid, st)
            self.recs[cid] = review.reviews(self.win.project, cid)
            self._set_reviewed_cell(self.table.currentRow(), cid)
            self._update_stats()
            self._update_review_label()

    def draw_sample(self):
        from PySide6.QtWidgets import QInputDialog
        run = self.run.currentText()
        if not run:
            return
        if review.sample(self.win.project) and QMessageBox.question(
                self, "Replace sample?", "A review sample already exists. Draw a new one?") != QMessageBox.Yes:
            return
        n, ok = QInputDialog.getInt(self, "Review sample", f"Number of cartoons to sample from run '{run}':",
                                    100, 1, 100000)
        if not ok:
            return
        try:
            s = review.draw_sample(self.win.project, run, n)
        except Exception as e:
            return error(self, e)
        info(self, f"Sampled {s['n']} of {s['population']} cartoons (seed {s['seed']}, recorded). "
                   "Review every stage of these cartoons; the Results tab then reports accuracy on the sample.")
        self.show_what.setCurrentText("Review sample")
        self.load_run()

    def open_folder(self):
        cid = self._cid()
        if cid:
            open_path(self.win.project.run_dir(self.run.currentText()) / cid)


class CodebookPage(QWidget):
    title = "Codebook"

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.extra = {}
        lay = QVBoxLayout(self)
        lay.addWidget(note(
            "Stages 1-3 record intermediate oppositions, targets and themes as free text. For corpus analysis, Stage 4 codes them into the "
            "categories of this codebook. Write it yourself, draft it from the project description and research "
            "questions, or derive it from the annotations of a run. Review every draft before accepting it."))
        self.which_cb = QRadioButton("codebook.yaml (in use)")
        self.which_draft = QRadioButton("codebook.draft.yaml (draft)")
        grp = QButtonGroup(self)
        grp.addButton(self.which_cb)
        grp.addButton(self.which_draft)
        self.which_cb.setChecked(True)
        self.which_cb.toggled.connect(lambda: self.refresh())
        self.version = QLineEdit()
        lay.addWidget(hbox(QLabel("Editing:"), self.which_cb, self.which_draft, QLabel("Version"), self.version))
        self.tabs = QTabWidget()
        self.tables, self.guidance = {}, {}
        for dim in CODING_DIMENSIONS:
            w = QWidget()
            wl = QVBoxLayout(w)
            t = table(["code", "definition", "includes (examples; separated by ;)", "n"])
            t.setColumnWidth(0, 200)
            t.setColumnWidth(1, 420)
            t.setColumnWidth(2, 360)
            wl.addWidget(t, 1)
            wl.addWidget(hbox(button("Add category", lambda _=False, t=t: self._add(t)),
                              button("Remove selected", lambda _=False, t=t: t.removeRow(t.currentRow()))))
            g = QPlainTextEdit()
            g.setFixedHeight(70)
            g.setPlaceholderText("Optional coding rules, one per line.")
            wl.addWidget(QLabel("Coding rules"))
            wl.addWidget(g)
            self.tables[dim], self.guidance[dim] = t, g
            self.tabs.addTab(w, dim.capitalize())
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setFixedHeight(70)
        self.tabs.addTab(self.notes, "Notes from the model")
        lay.addWidget(self.tabs, 1)
        self.accept_btn = button("Accept draft as codebook", self.accept_draft)
        lay.addWidget(hbox(button("Save", self.save), self.accept_btn))

        gen = QGroupBox("Generate a draft (uses the provider and model below)")
        gl = QVBoxLayout(gen)
        self.picker = ModelPicker(win)
        gl.addWidget(self.picker)
        self.dim_boxes = {d: QCheckBox(d) for d in CODING_DIMENSIONS}
        for b in self.dim_boxes.values():
            b.setChecked(True)
        self.size = QLineEdit("10-20")
        self.size.setMaximumWidth(80)
        self.from_run = QComboBox()
        gl.addWidget(hbox(QLabel("For"), *self.dim_boxes.values(), QLabel("about"), self.size, QLabel("categories")))
        gl.addWidget(hbox(button("Draft from project description", self.gen_draft,
                                 "A priori: from the description and research questions (Project tab)"),
                          QLabel("or"), button("Derive from annotations of run", self.gen_induce,
                                               "A posteriori: groups the free-text targets/themes of a run"),
                          self.from_run))
        self.gen_log = QLabel()
        gl.addWidget(self.gen_log)
        lay.addWidget(gen)

    def _add(self, t, values=("", "", "", "")):
        r = t.rowCount()
        t.insertRow(r)
        for c, v in enumerate(values):
            t.setItem(r, c, cell(v, editable=c != 3))

    def _path(self):
        p = self.win.project
        return p.codebook_path if self.which_cb.isChecked() else p.draft_path

    def load(self, p):
        self.picker.load(p)
        self.refresh()

    def refresh(self):
        p = self.win.project
        self.which_draft.setEnabled(p.draft_path.is_file())
        if not p.draft_path.is_file():
            self.which_cb.setChecked(True)
        self.accept_btn.setEnabled(self.which_draft.isChecked())
        self.from_run.clear()
        self.from_run.addItems([r for r in p.runs()])
        path = self._path()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        data = data or {}
        self.version.setText(str(data.get("version") or ""))
        for dim in CODING_DIMENSIONS:
            t = self.tables[dim]
            t.setRowCount(0)
            for c in data.get(dim) or []:
                self._add(t, (c.get("code", ""), c.get("definition", ""), "; ".join(c.get("includes") or []),
                              c.get("n", "")))
            self.guidance[dim].setPlainText("\n".join((data.get("guidance") or {}).get(dim) or []))
        notes = data.get("notes") or {}
        unassigned = data.get("unassigned") or {}
        self.notes.setPlainText("\n\n".join(
            [f"{d}: {n}" for d, n in notes.items()] +
            [f"Labels left out ({d}): " + "; ".join(u) for d, u in unassigned.items()]))
        self.extra = {k: v for k, v in data.items()
                      if k not in ("version", "guidance", "notes", "unassigned", *CODING_DIMENSIONS)}

    def collect(self):
        data = {"version": self.version.text().strip() or None, **self.extra}
        guidance = {}
        for dim in CODING_DIMENSIONS:
            cats = []
            for code, definition, includes, n in table_rows(self.tables[dim]):
                if not code:
                    continue
                c = {"code": code, "definition": definition}
                inc = [x.strip() for x in includes.split(";") if x.strip()]
                if inc:
                    c["includes"] = inc
                if n and self.which_draft.isChecked():
                    c["n"] = int(n)
                cats.append(c)
            if cats:
                data[dim] = cats
            rules = [x.strip() for x in self.guidance[dim].toPlainText().splitlines() if x.strip()]
            if rules:
                guidance[dim] = rules
        if guidance:
            data["guidance"] = guidance
        return {k: v for k, v in data.items() if v is not None}

    def save(self, quiet=False):
        path = self._path()
        data = self.collect()
        old = path.read_text(encoding="utf-8") if path.is_file() else None
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
        problems = self.win.project.codebook_problems(path)
        if problems and path == self.win.project.codebook_path:
            if old is None:
                path.unlink()
            else:
                path.write_text(old, encoding="utf-8")
            return error(self, "Not saved:\n" + "\n".join(problems))
        self.win.project_changed()
        if problems:
            info(self, "Draft saved. Fix these before accepting it:\n" + "\n".join(problems))
        elif not quiet:
            info(self, f"Saved {path.name}.")
        return True

    def accept_draft(self):
        if not self.save(quiet=True):
            return
        try:
            accept_draft(self.win.project)
        except Exception as e:
            return error(self, e)
        self.which_cb.setChecked(True)
        self.win.project_changed()
        info(self, "The draft is now codebook.yaml. Run Stage 4 in the Annotate tab to code the cartoons.")

    def _generate(self, fn):
        dims = [d for d, b in self.dim_boxes.items() if b.isChecked()]
        if not dims:
            return
        if self.win.project.draft_path.is_file() and QMessageBox.question(
                self, "Replace draft?", "This replaces the current codebook.draft.yaml. Continue?") != QMessageBox.Yes:
            return
        project, size = self.win.project, self.size.text().strip() or "10-20"
        self.gen_log.setText("Working ...")

        def work(emit, cancel):
            prov = self.picker.make(cancel)
            return fn(project, prov, dims, size, lambda m: emit({"msg": m}))

        def done(_):
            self.gen_log.setText("Draft written. Review and edit it here, then accept it.")
            self.which_draft.setEnabled(True)
            self.which_draft.setChecked(True)
            self.refresh()

        run_in_thread(work, on_event=lambda ev: self.gen_log.setText(ev["msg"]), on_done=done,
                      on_failed=lambda m: (self.gen_log.setText("Failed."), error(self, m)))

    def gen_draft(self):
        self._generate(lambda p, prov, dims, size, log: draft(p, prov, dims, size, log))

    def gen_induce(self):
        run = self.from_run.currentText()
        if not run:
            return error(self, "Annotate some cartoons first (Stages 2 and 3), then derive categories from that run.")
        self._generate(lambda p, prov, dims, size, log: induce(p, run, prov, dims, size, log))


class ResultsPage(QWidget):
    title = "Results"

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        box = QGroupBox("Export a run to Excel")
        bl = QVBoxLayout(box)
        self.run = QComboBox()
        bl.addWidget(hbox(QLabel("Run"), self.run, button("Export", self.export),
                          button("Review summary", self.review_summary,
                                 "How many outputs of this run were reviewed, accepted and corrected"),
                          button("Open exports folder", lambda: open_path(self.win.project.exports_dir))))
        bl.addWidget(note("Sheets: cartoons (with your item columns and review status), oppositions, "
                          "orientations, targets, realizations, presuppositions, review_queue, "
                          "and the codebook when present."))
        lay.addWidget(box)
        box = QGroupBox("Compare runs (inter-model agreement, or model vs your adjudicated outputs)")
        bl = QVBoxLayout(box)
        self.runs = QListWidget()
        self.runs.setMaximumHeight(140)
        bl.addWidget(self.runs)
        self.sample_only = QCheckBox("Only the review sample")
        self.sample_only.setToolTip("Compare a run with 'adjudicated' on the random review sample: an unbiased "
                                    "estimate of model accuracy per field")
        bl.addWidget(hbox(button("Compare checked runs", self.compare), self.sample_only))
        lay.addWidget(box)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        lay.addWidget(self.out, 1)

    def load(self, p):
        self.refresh()

    def refresh(self):
        runs = self.win.project.runs()
        cur = self.run.currentText()
        self.run.clear()
        self.run.addItems(runs)
        if cur in runs:
            self.run.setCurrentText(cur)
        checked = {self.runs.item(i).text() for i in range(self.runs.count())
                   if self.runs.item(i).checkState() == Qt.Checked}
        self.runs.clear()
        for r in runs:
            it = QListWidgetItem(r)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if r in checked else Qt.Unchecked)
            self.runs.addItem(it)

    def export(self):
        run = self.run.currentText()
        if not run:
            return
        try:
            path, n, n_review = export_run(self.win.project, run)
        except PermissionError:
            return error(self, "Close the workbook in Excel first, then export again.")
        self.out.setPlainText(f"Exported {n} cartoon(s) to {path}\n{n_review} stage result(s) in review_queue.")
        open_path(path)

    def review_summary(self):
        run = self.run.currentText()
        if not run or run == ADJUDICATED:
            return
        p = self.win.project
        lines = [f"Review of run '{run}'", "", f"{'stage':<6} {'outputs':>8} {'reviewed':>9} {'accepted':>9} "
                                                f"{'corrected':>10} {'accepted %':>11}"]

        def rows(ids=None):
            for r in review.summary(p, run, ids):
                share = "" if r["accepted_share"] is None else f"{100 * r['accepted_share']:.1f}"
                lines.append(f"{r['stage']:<6} {r['outputs']:>8} {r['reviewed']:>9} {r['accepted']:>9} "
                             f"{r['corrected']:>10} {share:>11}")
        rows()
        s = review.sample(p)
        if s:
            lines += ["", f"Review sample: {s['n']} cartoons drawn from run '{s['run']}' (seed {s['seed']})"]
            rows(s["ids"])
            lines += ["", "On the sample, 'accepted %' estimates how often the model's whole stage output was "
                          "correct; compare this run with 'adjudicated' (review sample only) for per-field accuracy."]
        self.out.setPlainText("\n".join(lines))
        self.out.setStyleSheet("font-family: Consolas, monospace;")

    def compare(self):
        runs = [self.runs.item(i).text() for i in range(self.runs.count())
                if self.runs.item(i).checkState() == Qt.Checked]
        if len(runs) < 2:
            return error(self, "Check at least two runs.")
        only = None
        if self.sample_only.isChecked():
            s = review.sample(self.win.project)
            if not s:
                return error(self, "No review sample yet: draw one in the Review tab.")
            only = s["ids"]
        try:
            path, ids, summary = compare_runs(self.win.project, runs, only_ids=only)
        except PermissionError:
            return error(self, "Close the comparison workbook in Excel first.")
        lines = [f"{len(ids)} cartoon(s) annotated in all of: {', '.join(runs)}", "",
                 f"{'KR':<15} {'field':<30} {'n':>4}  {'agree':>6}  {'alpha / Jaccard':>15}"]
        for r in summary:
            extra = r.get("krippendorff_alpha") if r["kind"] == "category" else r.get("mean_pairwise_jaccard")
            lines.append(f"{r['kr']:<15} {r['field']:<30} {r['n']:>4}  {r.get('all_runs_agree', ''):>6}  "
                         f"{'' if extra is None else extra:>15}")
        lines += ["", f"Details: {path}", "Free-text fields are listed side by side in the free_text sheet "
                                         "for manual review; they are not scored."]
        self.out.setPlainText("\n".join(lines))
        self.out.setStyleSheet("font-family: Consolas, monospace;")
