"""Entry point of the packaged app.

Loads the gtvh code from the app folder and restores the previous version if an
update fails to load.
"""

import ctypes
import os
import shutil
import sys
import traceback

APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)


def load():
    from gtvh.gui.app import main
    return main


def restore_previous():
    current, previous = os.path.join(APP_DIR, "gtvh"), os.path.join(APP_DIR, "gtvh.previous")
    if not os.path.isdir(previous):
        return False
    shutil.rmtree(current, ignore_errors=True)
    os.rename(previous, current)
    for name in [m for m in sys.modules if m == "gtvh" or m.startswith("gtvh.")]:
        del sys.modules[name]
    return True


def fail(message):
    ctypes.windll.user32.MessageBoxW(None, message, "GTVH Multimodal Analyzer", 0x10)
    sys.exit(1)


if __name__ == "__main__":
    try:
        main = load()
    except Exception:
        problem = traceback.format_exc(limit=3)
        if not restore_previous():
            fail(f"GTVH Multimodal Analyzer could not start.\n\n{problem}")
        try:
            main = load()
        except Exception:
            fail(f"GTVH Multimodal Analyzer could not start.\n\n{traceback.format_exc(limit=3)}")
        ctypes.windll.user32.MessageBoxW(
            None, "The last update could not be loaded, so the previous version was restored. The new version "
                  f"probably needs a rebuilt app.\n\n{problem}", "GTVH Multimodal Analyzer", 0x30)
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
