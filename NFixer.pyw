"""Запуск окна NFixer двойным щелчком (без чёрного окна консоли). На файл или ярлык можно бросать видео."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import nfixer_gui

    nfixer_gui.main()
except Exception as ex:  # без консоли ошибку иначе не увидеть
    import tkinter
    from tkinter import messagebox

    tkinter.Tk().withdraw()
    messagebox.showerror("NFixer", f"Не удалось запустить приложение:\n{ex!r}")
