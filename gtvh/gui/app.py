"""Main window, shared widgets, project settings page and API key dialog.

The window is opened by the packaged app (app/GTVH Multimodal Analyzer.exe) through
packaging/launch.py, which calls main().
"""

import subprocess
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QStackedWidget,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from .. import __version__, update
from ..framework import Framework, FrameworkError
from ..project import Project, ProjectError
from ..providers import PROVIDERS, delete_key, get_key, set_key

HERE = Path(__file__).resolve().parent
MATERIALS = ["editorial cartoons", "comic strips", "internet memes", "humorous advertisements"]


class Bridge(QObject):
    event = Signal(object)
    done = Signal(object)
    failed = Signal(str)


_live = set()


def run_in_thread(fn, on_event=None, on_done=None, on_failed=None):
    bridge, cancel = Bridge(), threading.Event()
    _live.add(bridge)
    if on_event:
        bridge.event.connect(on_event)
    if on_done:
        bridge.done.connect(on_done)
    bridge.failed.connect(on_failed or (lambda msg: error(None, msg)))
    bridge.done.connect(lambda _: _live.discard(bridge))
    bridge.failed.connect(lambda _: _live.discard(bridge))

    def target():
        try:
            bridge.done.emit(fn(bridge.event.emit, cancel))
        except (ProjectError, FrameworkError, ValueError) as e:
            bridge.failed.emit(str(e))
        except Exception as e:
            bridge.failed.emit(f"{e}\n\n{traceback.format_exc(limit=4)}")

    threading.Thread(target=target, daemon=True).start()
    return cancel


def error(parent, msg):
    QMessageBox.critical(parent, "GTVH Multimodal Analyzer", str(msg))


def info(parent, msg):
    QMessageBox.information(parent, "GTVH Multimodal Analyzer", str(msg))


def open_path(path):
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def note(text):
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    return lbl


def button(text, slot, tip=None):
    b = QPushButton(text)
    b.clicked.connect(slot)
    if tip:
        b.setToolTip(tip)
    return b


def hbox(*widgets, stretch=True):
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    for x in widgets:
        lay.addWidget(x) if isinstance(x, QWidget) else lay.addLayout(x)
    if stretch:
        lay.addStretch(1)
    return w


def table(headers, stretch_last=True):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setStretchLastSection(stretch_last)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    t.verticalHeader().setVisible(False)
    t.setWordWrap(True)
    return t


def cell(text, editable=True):
    it = QTableWidgetItem("" if text is None else str(text))
    if not editable:
        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    return it


def table_rows(t):
    return [[(t.item(r, c).text().strip() if t.item(r, c) else "") for c in range(t.columnCount())]
            for r in range(t.rowCount())]


class ImagePreview(QLabel):
    def __init__(self):
        super().__init__("No image")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(280, 280)
        self._pix = None

    def show_image(self, path):
        self._pix = QPixmap(str(path)) if path and Path(path).is_file() else None
        self._rescale()
        if self._pix is None:
            self.setText("No image")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._rescale()

    def _rescale(self):
        if self._pix and not self._pix.isNull():
            self.setPixmap(self._pix.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))


class KeysDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("API keys")
        lay = QVBoxLayout(self)
        lay.addWidget(note("Keys are stored in your operating system's credential store "
                             "(Windows Credential Manager, macOS Keychain), not in project files.\n"
                             "Environment variables such as ANTHROPIC_API_KEY also work."))
        grid = QGridLayout()
        self.rows = {}
        for i, name in enumerate(PROVIDERS):
            status = QLabel()
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.Password)
            edit.setPlaceholderText("paste a new key")
            grid.addWidget(QLabel(PROVIDERS[name]["label"]), i, 0)
            grid.addWidget(status, i, 1)
            grid.addWidget(edit, i, 2)
            grid.addWidget(button("Save", lambda _=False, n=name: self.save(n)), i, 3)
            grid.addWidget(button("Delete", lambda _=False, n=name: self.delete(n)), i, 4)
            self.rows[name] = (status, edit)
        lay.addLayout(grid)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.refresh()

    def refresh(self):
        for name, (status, _) in self.rows.items():
            _, source = get_key(name)
            status.setText(f"key from {source}" if source else "no key")

    def save(self, name):
        key = self.rows[name][1].text().strip()
        if not key:
            return
        try:
            set_key(name, key)
        except Exception as e:
            return error(self, f"Could not store the key: {e}")
        self.rows[name][1].clear()
        self.refresh()

    def delete(self, name):
        delete_key(name)
        self.refresh()


class ProjectPage(QWidget):
    title = "Project"

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit()
        self.description = QPlainTextEdit()
        self.description.setFixedHeight(70)
        self.description.setPlaceholderText("What the corpus is: source, period, selection. Used to draft a codebook.")
        self.material = QComboBox()
        self.material.setEditable(True)
        self.material.addItems(MATERIALS)
        self.send_context = QCheckBox("Give the model context: each cartoon's metadata and the corpus context below")
        self.context = QPlainTextEdit()
        self.context.setFixedHeight(50)
        self.context.setPlaceholderText("Optional; used only when context is switched on, e.g. "
                                        "\"Cartoons published in Pakistani newspapers, 2020-2024.\" Keep it factual.")
        self.rqs = QPlainTextEdit()
        self.rqs.setFixedHeight(70)
        self.rqs.setPlaceholderText("One research question per line. Used to draft a codebook.")
        form.addRow("Name", self.name)
        form.addRow("Description", self.description)
        form.addRow("Material", self.material)
        form.addRow("", self.send_context)
        form.addRow("Corpus context", self.context)
        form.addRow("Research questions", self.rqs)
        lay.addLayout(form)

        box = QGroupBox("Extra presupposition categories (optional)")
        bl = QVBoxLayout(box)
        bl.addWidget(note("Background-knowledge categories your corpus needs beyond the framework's list "
                          "(Stage 3). Codes must be snake_case."))
        self.ext = table(["code", "definition"])
        self.ext.setColumnWidth(0, 240)
        bl.addWidget(self.ext)
        bl.addWidget(hbox(button("Add row", lambda: self._add_ext()),
                          button("Remove selected", lambda: self.ext.removeRow(self.ext.currentRow()))))
        lay.addWidget(box, 1)
        lay.addWidget(hbox(button("Save project settings", self.save),
                           button("Open project folder", lambda: open_path(self.win.project.root))))

    def _add_ext(self, values=("", "")):
        r = self.ext.rowCount()
        self.ext.insertRow(r)
        for c, v in enumerate(values):
            self.ext.setItem(r, c, cell(v))

    def load(self, p):
        s = p.settings
        self.name.setText(p.name)
        self.description.setPlainText(s.get("description") or "")
        self.material.setCurrentText(s.get("material") or "editorial cartoons")
        self.context.setPlainText(s.get("corpus_context") or "")
        self.send_context.setChecked(bool(s.get("send_context")))
        self.rqs.setPlainText("\n".join(s.get("research_questions") or []))
        self.ext.setRowCount(0)
        for it in (s.get("extensions") or {}).get("presupposition_category") or []:
            self._add_ext((it.get("code", ""), it.get("definition", "")))

    def save(self):
        p = self.win.project
        cats = [{"code": code, "definition": definition} for code, definition in table_rows(self.ext) if code]
        ext = {"presupposition_category": cats} if cats else {}
        updates = {"name": self.name.text().strip() or p.root.name,
                   "description": self.description.toPlainText().strip(),
                   "material": self.material.currentText().strip() or "editorial cartoons",
                   "send_context": self.send_context.isChecked(),
                   "corpus_context": self.context.toPlainText().strip(),
                   "research_questions": [x.strip() for x in self.rqs.toPlainText().splitlines() if x.strip()],
                   "extensions": ext}
        try:
            Framework({**p.settings, **updates})
        except FrameworkError as e:
            return error(self, e)
        p.save_settings(updates)
        self.win.project_changed()
        info(self, "Saved. Prompts now use these settings; outputs made earlier keep the prompt hash they "
                   "were made with (see meta.json in each cartoon folder).")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        from .pages import AnnotatePage, CartoonsPage, CodebookPage, ResultsPage, ReviewPage
        self.setWindowTitle("GTVH Multimodal Analyzer")
        self.setWindowIcon(QIcon(str(HERE / "icon.png")))
        self.resize(1250, 820)
        self.settings = QSettings("gtvh-multimodal-analyzer", "GTVH Multimodal Analyzer")
        self.project = None

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.stack.addWidget(self._welcome())
        self.tabs = QTabWidget()
        self.pages = [ProjectPage(self), CartoonsPage(self), AnnotatePage(self), ReviewPage(self),
                      CodebookPage(self), ResultsPage(self)]
        for pg in self.pages:
            self.tabs.addTab(pg, pg.title)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.stack.addWidget(self.tabs)
        self._menus()

    def page(self, cls_name):
        return next(p for p in self.pages if type(p).__name__ == cls_name)

    def _welcome(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addStretch(1)
        logo = QLabel()
        logo.setPixmap(QPixmap(str(HERE / "icon.png")).scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        logo.setAlignment(Qt.AlignCenter)
        lay.addWidget(logo)
        t = QLabel("<h2>GTVH Multimodal Analyzer</h2><p>Multimodal General Theory of Verbal Humour annotation of cartoons "
                   "with Claude, GPT or Gemini.</p><p>A project is a folder holding your cartoons, settings, "
                   "annotations and exports.</p>")
        t.setAlignment(Qt.AlignCenter)
        lay.addWidget(t)
        lay.addWidget(hbox(QLabel(), button("New project…", self.new_project),
                           button("Open project…", self.open_project), stretch=False), alignment=Qt.AlignCenter)
        self.recent_box = QVBoxLayout()
        lay.addLayout(self.recent_box)
        lay.addStretch(2)
        self._fill_recent()
        return w

    def _fill_recent(self):
        while self.recent_box.count():
            self.recent_box.takeAt(0).widget().deleteLater()
        for path in self._recent():
            b = QPushButton(path)
            b.setFlat(True)
            b.clicked.connect(lambda _=False, p=path: self.load_project(p))
            self.recent_box.addWidget(b, alignment=Qt.AlignCenter)

    def _recent(self):
        r = self.settings.value("recent", []) or []
        return [x for x in (r if isinstance(r, list) else [r]) if Path(x, "project.yaml").is_file()]

    def _menus(self):
        m = self.menuBar().addMenu("&File")
        for text, slot in (("New project…", self.new_project), ("Open project…", self.open_project),
                           ("Open project folder", lambda: self.project and open_path(self.project.root))):
            a = QAction(text, self)
            a.triggered.connect(slot)
            m.addAction(a)
        m.addSeparator()
        q = QAction("Quit", self)
        q.triggered.connect(self.close)
        m.addAction(q)
        s = self.menuBar().addMenu("&Settings")
        a = QAction("API keys…", self)
        a.triggered.connect(lambda: KeysDialog(self).exec())
        s.addAction(a)
        h = self.menuBar().addMenu("&Help")
        a = QAction("About", self)
        a.triggered.connect(lambda: info(self, f"GTVH Multimodal Analyzer {__version__}\n\nAnnotation stages:\n"
                                               "1 Script Opposition\n2 Situation, Target, Narrative Strategy\n"
                                               "3 Language/semiotics, presuppositions, theme\n"
                                               "4 Corpus coding of oppositions, targets and themes (needs a codebook)"))
        h.addAction(a)
        a = QAction("Check for updates…", self)
        a.triggered.connect(lambda: self.check_updates(quiet=False))
        h.addAction(a)

    def check_updates(self, quiet=True):
        if not update.enabled():
            if not quiet:
                info(self, "Automatic updates work in the packaged app (the app folder) once its update.json names "
                           "a GitHub repository. When running from the source code, update the code itself.")
            return

        def found(release):
            if release is None:
                if not quiet:
                    info(self, "GTVH Multimodal Analyzer is up to date.")
                return
            if QMessageBox.question(
                    self, "Update available",
                    f"A new version is available on GitHub ({update.repository()}):\n\n{release['message']}\n"
                    f"{release['date'][:10]}\n\nInstall it now? The app restarts afterwards.") == QMessageBox.Yes:
                run_in_thread(lambda emit, cancel: update.install(release), on_done=lambda _: self._restart(),
                              on_failed=lambda m: error(self, f"The update failed; nothing was changed.\n\n{m}"))

        run_in_thread(lambda emit, cancel: update.available(), on_done=found,
                      on_failed=(lambda m: None) if quiet else (lambda m: error(self, m)))

    def _restart(self):
        subprocess.Popen([sys.executable] + sys.argv[1:], cwd=str(update.app_dir()))
        QApplication.quit()

    def new_project(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose an empty folder for the new project")
        if not folder:
            return
        name, ok = QInputDialog.getText(self, "New project", "Project name:", text=Path(folder).name)
        if not ok:
            return
        try:
            Project.create(folder, name.strip() or Path(folder).name)
        except ProjectError as e:
            return error(self, e)
        self.load_project(folder)

    def open_project(self):
        folder = QFileDialog.getExistingDirectory(self, "Open a project folder (it contains project.yaml)")
        if folder:
            self.load_project(folder)

    def load_project(self, folder):
        try:
            p = Project(folder)
            p.framework()
        except (ProjectError, FrameworkError) as e:
            return error(self, e)
        self.project = p
        recent = [str(p.root)] + [x for x in self._recent() if x != str(p.root)]
        self.settings.setValue("recent", recent[:8])
        self.setWindowTitle(f"GTVH Multimodal Analyzer: {p.name}")
        for pg in self.pages:
            pg.load(p)
        self.stack.setCurrentIndex(1)
        self.statusBar().showMessage(str(p.root))

    def project_changed(self):
        self.project.reload()
        self.setWindowTitle(f"GTVH Multimodal Analyzer: {self.project.name}")
        for pg in self.pages:
            if hasattr(pg, "refresh"):
                pg.refresh()

    def _tab_changed(self, i):
        pg = self.tabs.widget(i)
        if self.project and hasattr(pg, "refresh"):
            pg.refresh()


def main(project=None):
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("GTVH Multimodal Analyzer")
    app.setWindowIcon(QIcon(str(HERE / "icon.png")))
    w = MainWindow()
    if project:
        w.load_project(project)
    w.show()
    w.check_updates()
    return app.exec()

