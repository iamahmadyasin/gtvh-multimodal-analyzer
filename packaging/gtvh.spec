"""PyInstaller specification for the packaged app.

The gtvh package is left out of the frozen archive and copied next to the
executable by build.ps1, so that the app can update it.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

datas = []
for package in ("jsonschema_specifications", "rfc3987_syntax"):
    datas += collect_data_files(package)
hiddenimports = collect_submodules("keyring.backends") + collect_submodules("google.genai")

a = Analysis([str(ROOT / "packaging" / "launch.py")], pathex=[str(ROOT)], datas=datas, hiddenimports=hiddenimports,
             excludes=["tkinter", "matplotlib", "pandas", "pyarrow", "numpy.tests"])
a.pure = [entry for entry in a.pure if entry[0] != "gtvh" and not entry[0].startswith("gtvh.")]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="GTVH Multimodal Analyzer", console=False,
          icon=str(ROOT / "gtvh" / "gui" / "icon.ico"))
coll = COLLECT(exe, a.binaries, a.datas, name="app")
