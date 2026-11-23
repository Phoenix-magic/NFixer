"""Opens the NFixer window on double-click (no black console window). Videos can be dropped onto this file or its shortcut."""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _lang():
    """Language for the error message: the one chosen in the window last time, English by default."""
    try:
        with open(os.path.join(HERE, "logs", "gui.json"), encoding="utf-8") as fh:
            return json.load(fh).get("lang", "en")
    except Exception:
        return "en"


try:
    import nfixer_gui

    nfixer_gui.main()
except Exception as ex:  # without a console the error would not be visible otherwise
    import tkinter
    from tkinter import messagebox

    tkinter.Tk().withdraw()
    title = "Не удалось запустить приложение" if _lang() == "ru" else "The application could not start"
    messagebox.showerror("NFixer", f"{title}:\n{ex!r}")
