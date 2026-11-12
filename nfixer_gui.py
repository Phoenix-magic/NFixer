#!/usr/bin/env python3
"""NFixer window for nfixer.py: a list of files and folders, an action, progress and a log. The work itself is done by nfixer.py."""
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import nfixer

APP = "NFixer"
HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "logs", "gui.json")
ICON = os.path.join(HERE, "nfixer.ico")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LANGS = [("en", "English"), ("ru", "Русский")]
T = nfixer.L  # T(english, russian) -> текст на текущем языке (язык хранится в nfixer.LANG)


def actions():
    return [
        ("check", T("Check", "Проверить"), T("Find recordings with a damaged head. Changes nothing.", "Найти записи с испорченным началом. Ничего не изменяет.")),
        ("estimate", T("Estimate saving", "Оценить экономию"),
         T("Forecast for the list (12 random files if there are more). Changes nothing.", "Прогноз по списку (если файлов больше 12 - по 12 случайным). Ничего не изменяет.")),
        ("compress", T("Compress", "Сжать"), T("Re-encode to HEVC with quality control; audio is left untouched.", "Перекодировать в HEVC с контролем качества, звук без изменений.")),
        ("repair", T("Cut damaged head", "Обрезать испорченное начало"), T("No re-encoding; damaged recordings only.", "Без перекодирования, только у повреждённых записей.")),
    ]


def qualities():
    return [
        ("95", T("Visually lossless (VMAF 95) - recommended", "Без видимых потерь (VMAF 95) - рекомендуется")),
        ("97", T("Extra margin (VMAF 97) - larger files", "С запасом (VMAF 97) - файлы крупнее")),
        ("93", T("Smaller (VMAF 93) - fine detail slightly softer", "Компактнее (VMAF 93) - чуть мягче мелкие детали")),
    ]


def enable_hidpi():
    """Сообщить Windows, что окно само масштабируется под плотность экрана - иначе на 4K его растягивают, и текст выглядит размытым."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP + ".App")  # своя иконка на панели задач
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class DropTarget:
    """Приём файлов, перетащенных из Проводника (Windows, без сторонних библиотек).

    Подменяет оконную процедуру окна и ловит WM_DROPFILES. Пути складываются в очередь;
    из обработчика сообщений Tk не вызывается.
    """
    WM_DROPFILES = 0x0233
    GWLP_WNDPROC = -4

    def __init__(self, hwnd, out_queue):
        from ctypes import wintypes
        self.q = out_queue
        self.hwnd = hwnd
        u, s = ctypes.windll.user32, ctypes.windll.shell32
        lresult = ctypes.c_ssize_t
        self._proto = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        u.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.CallWindowProcW.restype = lresult
        u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        u.SetWindowLongPtrW.restype = ctypes.c_void_p
        s.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        s.DragQueryFileW.restype = wintypes.UINT
        s.DragFinish.argtypes = [ctypes.c_void_p]
        s.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        self._u, self._s = u, s
        self._new = self._proto(self._wndproc)  # ссылку нужно хранить, иначе обратный вызов соберёт сборщик мусора
        s.DragAcceptFiles(hwnd, True)
        self._old = u.SetWindowLongPtrW(hwnd, self.GWLP_WNDPROC, ctypes.cast(self._new, ctypes.c_void_p))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == self.WM_DROPFILES:
            try:
                n = self._s.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
                paths = []
                for i in range(n):
                    size = self._s.DragQueryFileW(wparam, i, None, 0) + 1
                    buf = ctypes.create_unicode_buffer(size)
                    self._s.DragQueryFileW(wparam, i, buf, size)
                    paths.append(buf.value)
                self._s.DragFinish(wparam)
                self.q.put(paths)
            except Exception:
                pass
            return 0
        return self._u.CallWindowProcW(self._old, hwnd, msg, wparam, lparam)


class App(tk.Tk):
    def __init__(self, initial=()):
        enable_hidpi()
        super().__init__()
        self.k = self.winfo_fpixels("1i") / 96.0  # масштаб экрана (1.0 = 100%, 1.5 = 150% ...)
        self.minsize(int(780 * self.k), int(700 * self.k))
        if os.path.isfile(ICON):
            try:
                self.iconbitmap(default=ICON)
            except tk.TclError:
                pass
        self.proc = None
        self.stopped = False
        self.listfile = None
        self.q = queue.Queue()
        self.dropq = queue.Queue()
        cfg = self._load_cfg()
        nfixer.LANG = cfg.get("lang") if cfg.get("lang") in dict(LANGS) else "en"
        self.items = []
        self.out = tk.StringVar(value=cfg.get("out", ""))
        self.action = tk.StringVar(value=cfg.get("action", "check"))
        self.mode = tk.StringVar(value="out")  # режим замены оригиналов намеренно не запоминается
        self.vmaf = cfg.get("vmaf", "95") if cfg.get("vmaf") in ("93", "95", "97") else "95"
        self.quality = tk.StringVar()
        self.lang_name = tk.StringVar()
        self.days = tk.StringVar(value=cfg.get("days", ""))
        self.hours = tk.StringVar(value=cfg.get("hours", ""))
        self.state_text = tk.StringVar()
        self.eta_text = tk.StringVar()
        self.eta = None  # прогноз времени от nfixer.py: (когда получен, секунд на файл, секунд на весь остаток)
        self.retry_paths = []  # файлы, не прошедшие проверку качества в последнем запуске
        self.problem_count = 0
        self.last_cmd = None
        self.onlyfile = None
        self.body = None
        self.items = self._clean(list(initial) or cfg.get("items", []))[0]
        self._build()
        self.action.trace_add("write", lambda *a: self._sync())
        self.mode.trace_add("write", lambda *a: self._sync())
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.drop = None
        if os.name == "nt":
            try:
                self.update_idletasks()
                self.drop = DropTarget(self.winfo_id(), self.dropq)
            except Exception:
                self.drop = None  # перетаскивание - удобство; без него остаются кнопки добавления
        self.after(200, self._poll_drop)

    # ---------------------------------------------------------------- интерфейс
    def _build(self):
        """Построить (или перестроить после смены языка) всё содержимое окна."""
        old_log = self.log.get("1.0", "end-1c") if self.body else ""
        old_bar = self.bar["value"] if self.body else 0
        if self.body:
            self.body.destroy()
        self.title(T(f"{APP} - repair and compress game recordings", f"{APP} - починка и сжатие игровых записей"))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        body = self.body = ttk.Frame(self)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        p = int(10 * self.k)
        pad = dict(padx=p, pady=int(5 * self.k))

        f = ttk.Frame(body)
        f.grid(row=0, column=0, sticky="ew", padx=p, pady=(int(6 * self.k), 0))
        self.cb_lang = ttk.Combobox(f, textvariable=self.lang_name, values=[n for _, n in LANGS], state="readonly", width=10)
        self.cb_lang.pack(side="right")
        ttk.Label(f, text="Language / Язык:").pack(side="right", padx=6)
        self.lang_name.set(dict(LANGS)[nfixer.LANG])
        self.cb_lang.bind("<<ComboboxSelected>>", self._on_lang)

        f = ttk.LabelFrame(body, text=T("What to process (.mp4 videos and folders can be dragged here from Explorer)",
                                        "Что обработать (видео .mp4 и папки можно перетащить сюда из Проводника)"))
        f.grid(row=1, column=0, sticky="ew", **pad)
        f.columnconfigure(0, weight=1)
        self.lb = tk.Listbox(f, height=5, selectmode="extended", activestyle="none")
        self.lb.grid(row=0, column=0, rowspan=4, sticky="nsew", padx=(8, 0), pady=6)
        sb = ttk.Scrollbar(f, command=self.lb.yview)
        sb.grid(row=0, column=1, rowspan=4, sticky="ns", pady=6)
        self.lb.configure(yscrollcommand=sb.set)
        for it in self.items:
            self.lb.insert("end", it)
        self.src_buttons = []
        for i, (text, cmd) in enumerate(((T("Add files...", "Добавить файлы..."), self._add_files), (T("Add folder...", "Добавить папку..."), self._add_folder),
                                         (T("Remove selected", "Убрать выбранное"), self._remove_selected), (T("Clear list", "Очистить список"), self._clear_items))):
            b = ttk.Button(f, text=text, command=cmd)
            b.grid(row=i, column=2, sticky="ew", padx=8, pady=1)
            self.src_buttons.append(b)

        f = ttk.LabelFrame(body, text=T("What to do", "Что сделать"))
        f.grid(row=2, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        for i, (key, title, hint) in enumerate(actions()):
            ttk.Radiobutton(f, text=title, value=key, variable=self.action).grid(row=i, column=0, sticky="w", padx=8, pady=2)
            ttk.Label(f, text=hint, foreground="#555").grid(row=i, column=1, sticky="w", padx=8)

        f = ttk.LabelFrame(body, text=T("Where to save the result", "Куда сохранять результат"))
        f.grid(row=3, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        self.rb_out = ttk.Radiobutton(f, text=T("To a separate folder:", "В отдельную папку:"), value="out", variable=self.mode)
        self.rb_out.grid(row=0, column=0, sticky="w", padx=8, pady=2)
        self.e_out = ttk.Entry(f, textvariable=self.out)
        self.e_out.grid(row=0, column=1, sticky="ew", padx=6)
        self.b_out = ttk.Button(f, text=T("Browse...", "Обзор..."), command=self._browse_out)
        self.b_out.grid(row=0, column=2, padx=(0, 8))
        self.rb_rep = ttk.Radiobutton(f, value="replace", variable=self.mode,
                                      text=T("Replace the originals (each file only after it passes verification; cannot be undone)",
                                             "Заменить оригиналы (каждый файл - только после проверки; отменить нельзя)"))
        self.rb_rep.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=2)

        f = ttk.LabelFrame(body, text=T("Options", "Параметры"))
        f.grid(row=4, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text=T("Quality:", "Качество:")).grid(row=0, column=0, sticky="w", padx=8, pady=2)
        q = qualities()
        self.cb_q = ttk.Combobox(f, textvariable=self.quality, values=[name for _, name in q], state="readonly")
        self.cb_q.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.quality.set(dict(q)[self.vmaf])
        self.cb_q.bind("<<ComboboxSelected>>", lambda e: setattr(self, "vmaf", {name: v for v, name in qualities()}.get(self.quality.get(), "95")))
        ttk.Label(f, text=T("Only files from the last", "Только файлы за последние")).grid(row=1, column=0, sticky="w", padx=8, pady=2)
        g = ttk.Frame(f)
        g.grid(row=1, column=1, sticky="w")
        ttk.Entry(g, textvariable=self.days, width=6).pack(side="left")
        ttk.Label(g, text=T("days (empty - all)", "дней (пусто - все)")).pack(side="left", padx=6)
        ttk.Label(g, text=T("     Stop after", "     Остановиться через")).pack(side="left")
        self.e_hours = ttk.Entry(g, textvariable=self.hours, width=6)
        self.e_hours.pack(side="left", padx=6)
        ttk.Label(g, text=T("hours (empty - run to the end)", "часов (пусто - до конца)")).pack(side="left")

        f = ttk.Frame(body)
        f.grid(row=5, column=0, sticky="ew", **pad)
        self.b_start = ttk.Button(f, text=T("Start", "Старт"), command=self.start)
        self.b_start.pack(side="left")
        self.b_stop = ttk.Button(f, text=T("Stop", "Стоп"), command=self.stop, state="disabled")
        self.b_stop.pack(side="left", padx=6)
        ttk.Button(f, text=T("Open output folder", "Открыть папку результатов"), command=self._open_out).pack(side="right")
        ttk.Button(f, text=T("Clear log", "Очистить журнал"), command=self._clear_log).pack(side="right", padx=6)

        f = ttk.Frame(body)
        f.grid(row=6, column=0, sticky="ew", **pad)
        f.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(f, maximum=100, value=old_bar)
        self.bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(f, textvariable=self.state_text, anchor="w").grid(row=1, column=0, sticky="ew", pady=(3, 0))
        ttk.Label(f, textvariable=self.eta_text, anchor="w", foreground="#1a4fa0").grid(row=2, column=0, sticky="ew", pady=(2, 0))
        self.state_text.set(T("Ready", "Готов к работе"))

        f = ttk.Frame(body)
        f.grid(row=7, column=0, sticky="nsew", **pad)
        body.rowconfigure(7, weight=1)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)
        self.log = tk.Text(f, height=12, wrap="word", state="disabled", font=("Consolas", 10))
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(f, command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=sb.set)
        self.log.tag_configure("bad", foreground="#b00020")
        self.log.tag_configure("good", foreground="#0a7a2f")
        self.log.tag_configure("head", foreground="#1a4fa0")
        for line in old_log.splitlines():
            self._write(line, self._tag(line))
        self._sync()

    def _on_lang(self, _event=None):
        code = {n: c for c, n in LANGS}.get(self.lang_name.get(), "en")
        if code != nfixer.LANG:
            nfixer.LANG = code
            self._save_cfg()
            self._build()

    def _sync(self):
        """Включить только те поля, которые относятся к выбранному действию."""
        act = self.action.get()
        writes = act in ("compress", "repair")
        busy = self.proc is not None
        for w in (self.rb_out, self.rb_rep):
            w.configure(state="normal" if writes and not busy else "disabled")
        st = "normal" if writes and self.mode.get() == "out" and not busy else "disabled"
        self.e_out.configure(state=st)
        self.b_out.configure(state=st)
        self.cb_q.configure(state="readonly" if act in ("compress", "estimate") and not busy else "disabled")
        self.e_hours.configure(state="normal" if writes and not busy else "disabled")
        self.cb_lang.configure(state="disabled" if busy else "readonly")  # журнал идущей работы пишется на языке, выбранном при старте
        for b in self.src_buttons:
            b.configure(state="disabled" if busy else "normal")

    # ---------------------------------------------------------------- список файлов и папок
    def _clean(self, paths, have=()):
        """Оставить папки и файлы .mp4, убрать повторы. Возвращает (годные пути, отвергнутые пути)."""
        seen = {os.path.normcase(x) for x in have}
        good, skipped = [], []
        for raw in paths:
            p = os.path.normpath(str(raw).strip().strip('"'))
            if not p or p == ".":
                continue
            if os.path.isdir(p) or (os.path.isfile(p) and p.lower().endswith(".mp4")):
                if os.path.normcase(p) not in seen:
                    seen.add(os.path.normcase(p))
                    good.append(p)
            else:
                skipped.append(p)
        return good, skipped

    def add_paths(self, paths, quiet=False):
        """Добавить в список папки и файлы .mp4; остальное не брать и объяснить почему. Возвращает (добавлено, пропущено)."""
        good, skipped = self._clean(paths, self.items)
        for p in good:
            self.items.append(p)
            self.lb.insert("end", p)
        msg = T(f"Added: {len(good)}", f"Добавлено: {len(good)}")
        if skipped:
            msg += T(f", not accepted: {len(skipped)}", f", не принято: {len(skipped)}")
        self.state_text.set(msg + T(f". In the list: {len(self.items)}", f". Всего в списке: {len(self.items)}"))
        if skipped and not quiet:
            rows = [("- " + os.path.basename(p) + ("" if os.path.exists(p) else T("  (not found)", "  (не найден)"))) for p in skipped[:8]]
            if len(skipped) > 8:
                rows.append(T(f"... and {len(skipped) - 8} more", f"... и ещё {len(skipped) - 8}"))
            messagebox.showwarning(APP, T("These files were not added: NFixer works only with .mp4 recordings.\n\n",
                                          "Эти файлы не добавлены: NFixer работает только с записями .mp4.\n\n") + "\n".join(rows))
        return len(good), len(skipped)

    def _poll_drop(self):
        try:
            while True:
                paths = self.dropq.get_nowait()
                if self.proc is None:
                    self.add_paths(paths)
                else:
                    self.state_text.set(T("Busy - files can be added once the current job ends", "Идёт работа - добавить файлы можно после её окончания"))
        except queue.Empty:
            pass
        self.after(200, self._poll_drop)

    def _add_files(self):
        r = filedialog.askopenfilenames(title=T("Choose videos", "Выберите видео"),
                                        filetypes=[(T("MP4 video", "Видео MP4"), "*.mp4"), (T("All files", "Все файлы"), "*.*")])
        if r:
            self.add_paths(r)

    def _add_folder(self):
        d = filedialog.askdirectory(title=T("Choose a folder with recordings", "Выберите папку с записями"))
        if d:
            self.add_paths([d])

    def _remove_selected(self):
        for i in reversed(self.lb.curselection()):
            self.lb.delete(i)
            del self.items[i]

    def _clear_items(self):
        self.lb.delete(0, "end")
        self.items.clear()

    def _browse_out(self):
        d = filedialog.askdirectory(initialdir=self.out.get() if os.path.isdir(self.out.get()) else None)
        if d:
            self.out.set(os.path.normpath(d))

    def _open_out(self):
        if self.mode.get() == "out" and self.out.get().strip():
            d = self.out.get().strip()
        elif self.items:
            d = self.items[0] if os.path.isdir(self.items[0]) else os.path.dirname(self.items[0])
        else:
            d = ""
        if d and os.path.isdir(d):
            os.startfile(d)
        else:
            messagebox.showinfo(APP, T("That folder does not exist yet.", "Такой папки пока нет.") if d else T("No output folder chosen.", "Папка результатов не выбрана."))

    @staticmethod
    def _tag(line):
        """Цвет строки журнала. Признаки ищутся на обоих языках: журнал мог быть написан до смены языка."""
        if any(w in line for w in ("ERROR", "ОШИБКА", "DAMAGED", "ПОВРЕЖДЁН", "Traceback", "FAILED", "НЕ ПРОЙДЕНА", "PROBLEM FILES", "ПРОБЛЕМНЫЕ ФАЙЛЫ")) \
                or line.lstrip().startswith("! "):
            return "bad"
        if any(w in line for w in ("done:", "готово:")) or line.startswith(("Was ", "Было ")):
            return "good"
        if line.startswith("==="):
            return "head"
        return None

    def _write(self, line, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n", tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ---------------------------------------------------------------- настройки
    def _load_cfg(self):
        try:
            with open(CFG, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save_cfg(self):
        try:
            os.makedirs(os.path.dirname(CFG), exist_ok=True)
            with open(CFG, "w", encoding="utf-8") as fh:
                json.dump({"lang": nfixer.LANG, "items": self.items, "out": self.out.get(), "action": self.action.get(), "vmaf": self.vmaf,
                           "days": self.days.get(), "hours": self.hours.get()}, fh, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # ---------------------------------------------------------------- запуск
    def build_command(self):
        """Собрать аргументы nfixer.py по состоянию окна (без списка путей). Возвращает (аргументы, None) или (None, текст ошибки)."""
        act = self.action.get()
        if not self.items:
            return None, T("The list is empty. Add a video or a folder with the buttons on the right or by dragging it in.",
                           "Список пуст. Добавьте видео или папку: кнопками справа или перетаскиванием.")
        missing = [p for p in self.items if not os.path.exists(p)]
        if missing:
            return None, T("Not found:\n", "Не найдено:\n") + "\n".join(missing[:5])
        cmd = [act]
        for var, opt, name in ((self.days, "--days", T("days", "дней")), (self.hours, "--max-hours", T("hours", "часов"))):
            val = var.get().strip().replace(",", ".")
            if not val or (opt == "--max-hours" and act not in ("compress", "repair")):
                continue
            try:
                if float(val) <= 0:
                    raise ValueError
            except ValueError:
                return None, T(f"The number of {name} is not valid: {val}", f"Число {name} указано неверно: {val}")
            cmd += [opt, val]
        if act in ("compress", "estimate"):
            cmd += ["--vmaf", self.vmaf]
        if act in ("compress", "repair"):
            if self.mode.get() == "replace":
                cmd.append("--replace")
            else:
                out = self.out.get().strip().strip('"')
                if not out:
                    return None, T("Choose an output folder.", "Укажите папку для результатов.")
                a = os.path.normcase(os.path.abspath(out))
                for p in self.items:
                    b = os.path.normcase(os.path.abspath(p if os.path.isdir(p) else os.path.dirname(p)))
                    if a == b or (os.path.isdir(p) and a.startswith(b + os.sep)):
                        return None, T("The output folder must not be the folder of the source recordings or lie inside it.",
                                       "Папка для результатов не должна совпадать с папкой исходных записей или лежать внутри неё.")
                cmd += ["--out", out]
        return cmd, None

    def start(self):
        if self.proc:
            return
        cmd, err = self.build_command()
        if err:
            messagebox.showerror(APP, err)
            return
        if "--replace" in cmd and not messagebox.askyesno(
                APP, T("The originals will be REPLACED with the result.\n\nEach file is replaced only after it passes verification, "
                       "but the original cannot be brought back afterwards.\n\nContinue?",
                       "Оригиналы будут ЗАМЕНЕНЫ результатом.\n\nКаждый файл заменяется только после успешной проверки, "
                       "но вернуть исходный файл после этого будет нельзя.\n\nПродолжить?"), icon="warning", default="no"):
            return
        self._save_cfg()
        self._launch(cmd)

    def _launch(self, cmd, only=None):
        """Запустить nfixer.py. only - список файлов для повтора на ступень качественнее (остальное из списка не трогается)."""
        # пути передаются списком в файле: при перетаскивании сотен видео командная строка была бы слишком длинной
        fd, self.listfile = tempfile.mkstemp(prefix="nfixer-list-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(self.items))
        extra = []
        if only:
            fd, self.onlyfile = tempfile.mkstemp(prefix="nfixer-only-", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(only))
            extra = ["--better", "--only", self.onlyfile]
        py = os.path.join(os.path.dirname(sys.executable), "python.exe")
        if not os.path.isfile(py):
            py = sys.executable
        env = dict(os.environ, PYTHONIOENCODING="utf-8", NFIXER_GUI="1")
        self.stopped = False
        self.last_cmd = cmd
        self.retry_paths, self.problem_count, self.eta = [], 0, None
        self.eta_text.set("")
        self.proc = subprocess.Popen([py, "-u", "-X", "utf8", os.path.join(HERE, "nfixer.py"), "--lang", nfixer.LANG, cmd[0], "--list", self.listfile] + cmd[1:] + extra,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, cwd=HERE, creationflags=NO_WINDOW)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.b_start.configure(state="disabled")
        self.b_stop.configure(state="normal")
        self.bar.configure(value=0)
        self.state_text.set(T("Working...", "Работаю..."))
        title = dict((a[0], a[1]) for a in actions())[cmd[0]]
        if only:
            self._write(T(f"=== {title}, one step higher quality: {len(only)} file(s) ===", f"=== {title}, на ступень качественнее: файлов - {len(only)} ==="), "head")
        else:
            self._write(T(f"=== {title}: {len(self.items)} item(s) in the list ===", f"=== {title}: объектов в списке - {len(self.items)} ==="), "head")
        self._sync()
        self.after(100, self._poll)
        self.after(500, self._tick)

    def _tick(self):
        """Раз в полсекунды обновлять обратный отсчёт: сколько осталось на текущий файл и на всю работу."""
        if not self.proc:
            return
        if self.eta:
            got, one, rest = self.eta
            el = time.time() - got
            left = lambda s: nfixer.hms(s - el) if s - el >= 1 else T("almost done", "вот-вот")
            self.eta_text.set(T(f"This file: about {nfixer.hms(one)}, left {left(one)}      Whole job: left about {left(rest)}",
                                f"Этот файл: около {nfixer.hms(one)}, осталось {left(one)}      Вся работа: осталось около {left(rest)}"))
        self.after(500, self._tick)

    def _reader(self, proc):
        for raw in proc.stdout:
            self.q.put(raw.decode("utf-8", "replace").rstrip("\r\n"))
        proc.wait()
        self.q.put(None)

    def _poll(self):
        try:
            while True:
                line = self.q.get_nowait()
                if line is None:
                    self._finished()
                    return
                self._handle(line)
        except queue.Empty:
            pass
        if self.proc:
            self.after(100, self._poll)

    def _handle(self, line):
        if line.startswith(("@@#", "@@!")):  # служебные строки: прогноз времени и итоговый список проблемных файлов
            try:
                d = json.loads(line[3:])
            except ValueError:
                return
            if line[2] == "#":
                self.eta = (time.time() - d.get("spent", 0), d.get("file", 0), d.get("all", 0))
            else:
                self.retry_paths, self.problem_count = list(d.get("retry", [])), int(d.get("problems", 0))
            return
        if line.startswith("@@"):
            msg = line[2:]
            self.state_text.set(msg)
            m = re.match(r"(\d+)/(\d+) ", msg)
            if m:
                self.bar.configure(value=int(m.group(1)) * 100 / max(1, int(m.group(2))))
            return
        if not line.strip():
            return
        m = re.match(r"\[(\d+)/(\d+)\]", line)
        if m:
            self.bar.configure(value=(int(m.group(1)) - 1) * 100 / max(1, int(m.group(2))))
            self.state_text.set(T(f"File {m.group(1)} of {m.group(2)}", f"Файл {m.group(1)} из {m.group(2)}"))
        self._write(line, self._tag(line))

    def _finished(self):
        self.proc = None
        if self.listfile:
            try:
                os.remove(self.listfile)
            except OSError:
                pass
            self.listfile = None
        if self.onlyfile:
            try:
                os.remove(self.onlyfile)
            except OSError:
                pass
            self.onlyfile = None
        self.eta = None
        self.eta_text.set("")
        self.b_start.configure(state="normal")
        self.b_stop.configure(state="disabled")
        if self.stopped:
            removed = self._cleanup()
            self.state_text.set(T("Stopped", "Остановлено"))
            self._write(T("Stopped. The originals are intact" + (", the unfinished file was removed." if removed else ".") + " Starting again continues where it left off.",
                          "Остановлено. Оригиналы целы" + (", недописанный файл удалён." if removed else ".") + " Повторный запуск продолжит с места остановки."), "head")
        else:
            self.bar.configure(value=100)
            self.state_text.set(T("Finished", "Готово") + (T(f" - problem files: {self.problem_count} (listed at the end of the log)",
                                                              f" - проблемных файлов: {self.problem_count} (перечислены в конце журнала)") if self.problem_count else ""))
        self._sync()
        if not self.stopped and self.retry_paths and self.last_cmd:
            self.after(50, self._offer_retry)

    def _offer_retry(self):
        """Файлы, не прошедшие проверку качества, были пропущены. Решение о повторе - за пользователем."""
        paths = self.retry_paths
        rows = ["- " + os.path.basename(p) for p in paths[:10]]
        if len(paths) > 10:
            rows.append(T(f"... and {len(paths) - 10} more", f"... и ещё {len(paths) - 10}"))
        if messagebox.askyesno(APP, T(f"{len(paths)} file(s) did not pass the quality check after compression and were skipped (the originals are untouched):\n\n",
                                      f"Файлов, не прошедших проверку качества после сжатия: {len(paths)}. Они пропущены, оригиналы не тронуты:\n\n")
                               + "\n".join(rows)
                               + T("\n\nCompress them again one step higher quality?\nThe files will come out somewhat larger; the other settings stay the same.",
                                   "\n\nСжать их ещё раз на ступень качественнее?\nФайлы выйдут немного крупнее; остальные настройки те же.")):
            self._launch(self.last_cmd, only=paths)

    def _cleanup(self):
        """После принудительной остановки убрать недописанные временные файлы (только свои, *.nfixer-tmp.mp4)."""
        dirs = {p if os.path.isdir(p) else os.path.dirname(p) for p in self.items}
        if self.out.get().strip():
            dirs.add(self.out.get().strip())
        dirs = [d for d in dirs if os.path.isdir(d)]
        before = sum(1 for d in dirs for _, _, fn in os.walk(d) for x in fn if x.endswith(nfixer.TMP_SUFFIX))
        nfixer.cleanup_tmp(dirs)
        return before

    def stop(self):
        if not self.proc:
            return
        self.stopped = True
        self.b_stop.configure(state="disabled")
        self.state_text.set(T("Stopping...", "Останавливаю..."))
        subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)

    def _close(self):
        if self.proc:
            if not messagebox.askyesno(APP, T("A job is still running. Stop it and close?\n\nThe originals stay intact and the job can be continued later.",
                                              "Работа ещё идёт. Остановить и закрыть?\n\nОригиналы останутся целы, позже можно продолжить.")):
                return
            self.stop()
            try:
                self.proc.wait(timeout=20)
            except Exception:
                pass
            self._cleanup()
        self._save_cfg()
        self.destroy()


def main():
    App(initial=sys.argv[1:]).mainloop()  # файлы, брошенные на ярлык, приходят аргументами


if __name__ == "__main__":
    main()
