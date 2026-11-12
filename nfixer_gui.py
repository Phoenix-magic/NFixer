#!/usr/bin/env python3
"""NFixer - окно для nfixer.py: список файлов и папок, действие, прогресс, журнал. Сама работа выполняется nfixer.py."""
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import nfixer

APP = "NFixer"
HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "logs", "gui.json")
ICON = os.path.join(HERE, "nfixer.ico")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

ACTIONS = [
    ("check", "Проверить", "Найти записи с испорченным началом. Ничего не изменяет."),
    ("estimate", "Оценить экономию", "Прогноз на 12 случайных файлах. Ничего не изменяет."),
    ("compress", "Сжать", "Перекодировать в HEVC с контролем качества, звук без изменений."),
    ("repair", "Обрезать испорченное начало", "Без перекодирования, только у повреждённых записей."),
]
QUALITY = [
    ("Без видимых потерь (VMAF 95) - рекомендуется", "95"),
    ("С запасом (VMAF 97) - файлы крупнее", "97"),
    ("Компактнее (VMAF 93) - чуть мягче мелкие детали", "93"),
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
        self.title(f"{APP} - сжатие и починка игровых записей")
        self.k = self.winfo_fpixels("1i") / 96.0  # масштаб экрана (1.0 = 100%, 1.5 = 150% ...)
        self.minsize(int(780 * self.k), int(680 * self.k))
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
        self.items = []
        self.out = tk.StringVar(value=cfg.get("out", ""))
        self.action = tk.StringVar(value=cfg.get("action", "check"))
        self.mode = tk.StringVar(value="out")  # режим замены оригиналов намеренно не запоминается
        self.quality = tk.StringVar(value=cfg.get("quality", QUALITY[0][0]))
        self.days = tk.StringVar(value=cfg.get("days", ""))
        self.hours = tk.StringVar(value=cfg.get("hours", ""))
        self.state_text = tk.StringVar(value="Готов к работе")
        self._build()
        self.add_paths(list(initial) or cfg.get("items", []), quiet=True)
        self.action.trace_add("write", lambda *a: self._sync())
        self.mode.trace_add("write", lambda *a: self._sync())
        self._sync()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.drop = None
        if os.name == "nt":
            try:
                self.update_idletasks()
                self.drop = DropTarget(self.winfo_id(), self.dropq)
            except Exception:
                self.drop = None  # перетаскивание - удобство; без него остаются кнопки «Добавить»
        self.after(200, self._poll_drop)

    # ---------------------------------------------------------------- интерфейс
    def _build(self):
        p = int(10 * self.k)
        pad = dict(padx=p, pady=int(5 * self.k))
        self.columnconfigure(0, weight=1)

        f = ttk.LabelFrame(self, text="Что обработать (видео .mp4 и папки можно перетащить сюда из Проводника)")
        f.grid(row=0, column=0, sticky="ew", **pad)
        f.columnconfigure(0, weight=1)
        self.lb = tk.Listbox(f, height=5, selectmode="extended", activestyle="none")
        self.lb.grid(row=0, column=0, rowspan=4, sticky="nsew", padx=(8, 0), pady=6)
        sb = ttk.Scrollbar(f, command=self.lb.yview)
        sb.grid(row=0, column=1, rowspan=4, sticky="ns", pady=6)
        self.lb.configure(yscrollcommand=sb.set)
        self.src_buttons = []
        for i, (text, cmd) in enumerate((("Добавить файлы...", self._add_files), ("Добавить папку...", self._add_folder),
                                         ("Убрать выбранное", self._remove_selected), ("Очистить список", self._clear_items))):
            b = ttk.Button(f, text=text, command=cmd)
            b.grid(row=i, column=2, sticky="ew", padx=8, pady=1)
            self.src_buttons.append(b)

        f = ttk.LabelFrame(self, text="Что сделать")
        f.grid(row=1, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        for i, (key, title, hint) in enumerate(ACTIONS):
            ttk.Radiobutton(f, text=title, value=key, variable=self.action).grid(row=i, column=0, sticky="w", padx=8, pady=2)
            ttk.Label(f, text=hint, foreground="#555").grid(row=i, column=1, sticky="w", padx=8)

        f = ttk.LabelFrame(self, text="Куда сохранять результат")
        f.grid(row=2, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        self.rb_out = ttk.Radiobutton(f, text="В отдельную папку:", value="out", variable=self.mode)
        self.rb_out.grid(row=0, column=0, sticky="w", padx=8, pady=2)
        self.e_out = ttk.Entry(f, textvariable=self.out)
        self.e_out.grid(row=0, column=1, sticky="ew", padx=6)
        self.b_out = ttk.Button(f, text="Обзор...", command=self._browse_out)
        self.b_out.grid(row=0, column=2, padx=(0, 8))
        self.rb_rep = ttk.Radiobutton(f, text="Заменить оригиналы (каждый файл - только после проверки; отменить нельзя)", value="replace", variable=self.mode)
        self.rb_rep.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=2)

        f = ttk.LabelFrame(self, text="Параметры")
        f.grid(row=3, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Качество:").grid(row=0, column=0, sticky="w", padx=8, pady=2)
        self.cb_q = ttk.Combobox(f, textvariable=self.quality, values=[q[0] for q in QUALITY], state="readonly")
        self.cb_q.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        ttk.Label(f, text="Только файлы за последние").grid(row=1, column=0, sticky="w", padx=8, pady=2)
        g = ttk.Frame(f)
        g.grid(row=1, column=1, sticky="w")
        ttk.Entry(g, textvariable=self.days, width=6).pack(side="left")
        ttk.Label(g, text="дней (пусто - все)").pack(side="left", padx=6)
        ttk.Label(g, text="     Остановиться через").pack(side="left")
        self.e_hours = ttk.Entry(g, textvariable=self.hours, width=6)
        self.e_hours.pack(side="left", padx=6)
        ttk.Label(g, text="часов (пусто - до конца)").pack(side="left")

        f = ttk.Frame(self)
        f.grid(row=4, column=0, sticky="ew", **pad)
        self.b_start = ttk.Button(f, text="Старт", command=self.start)
        self.b_start.pack(side="left")
        self.b_stop = ttk.Button(f, text="Стоп", command=self.stop, state="disabled")
        self.b_stop.pack(side="left", padx=6)
        ttk.Button(f, text="Открыть папку результатов", command=self._open_out).pack(side="right")
        ttk.Button(f, text="Очистить журнал", command=self._clear_log).pack(side="right", padx=6)

        f = ttk.Frame(self)
        f.grid(row=5, column=0, sticky="ew", **pad)
        f.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(f, maximum=100)
        self.bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(f, textvariable=self.state_text, anchor="w").grid(row=1, column=0, sticky="ew", pady=(3, 0))

        f = ttk.Frame(self)
        f.grid(row=6, column=0, sticky="nsew", **pad)
        self.rowconfigure(6, weight=1)
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
        for b in self.src_buttons:
            b.configure(state="disabled" if busy else "normal")

    # ---------------------------------------------------------------- список файлов и папок
    def add_paths(self, paths, quiet=False):
        """Добавить в список папки и файлы .mp4; остальное пропустить. Возвращает (добавлено, пропущено)."""
        have = {os.path.normcase(x) for x in self.items}
        added = skipped = 0
        for raw in paths:
            p = os.path.normpath(str(raw).strip().strip('"'))
            if not p or p == ".":
                continue
            if os.path.isdir(p) or (os.path.isfile(p) and p.lower().endswith(".mp4")):
                if os.path.normcase(p) not in have:
                    have.add(os.path.normcase(p))
                    self.items.append(p)
                    self.lb.insert("end", p)
                    added += 1
            else:
                skipped += 1
        if not quiet:
            msg = f"Добавлено: {added}" + (f", пропущено (не .mp4 или не найдено): {skipped}" if skipped else "")
            self.state_text.set(msg + f". Всего в списке: {len(self.items)}")
        return added, skipped

    def _poll_drop(self):
        try:
            while True:
                paths = self.dropq.get_nowait()
                if self.proc is None:
                    self.add_paths(paths)
                else:
                    self.state_text.set("Идёт работа - добавить файлы можно после её окончания")
        except queue.Empty:
            pass
        self.after(200, self._poll_drop)

    def _add_files(self):
        r = filedialog.askopenfilenames(title="Выберите видео", filetypes=[("Видео MP4", "*.mp4"), ("Все файлы", "*.*")])
        if r:
            self.add_paths(r)

    def _add_folder(self):
        d = filedialog.askdirectory(title="Выберите папку с записями")
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
            messagebox.showinfo(APP, "Такой папки пока нет." if d else "Папка результатов не выбрана.")

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
                json.dump({"items": self.items, "out": self.out.get(), "action": self.action.get(), "quality": self.quality.get(),
                           "days": self.days.get(), "hours": self.hours.get()}, fh, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # ---------------------------------------------------------------- запуск
    def build_command(self):
        """Собрать аргументы nfixer.py по состоянию окна (без списка путей). Возвращает (аргументы, None) или (None, текст ошибки)."""
        act = self.action.get()
        if not self.items:
            return None, "Список пуст. Добавьте видео или папку: кнопками справа или перетаскиванием."
        missing = [p for p in self.items if not os.path.exists(p)]
        if missing:
            return None, "Не найдено:\n" + "\n".join(missing[:5])
        cmd = [act]
        for var, opt, name in ((self.days, "--days", "дней"), (self.hours, "--max-hours", "часов")):
            val = var.get().strip().replace(",", ".")
            if not val or (opt == "--max-hours" and act not in ("compress", "repair")):
                continue
            try:
                if float(val) <= 0:
                    raise ValueError
            except ValueError:
                return None, f"Число {name} указано неверно: {val}"
            cmd += [opt, val]
        if act in ("compress", "estimate"):
            cmd += ["--vmaf", dict(QUALITY).get(self.quality.get(), "95")]
        if act in ("compress", "repair"):
            if self.mode.get() == "replace":
                cmd.append("--replace")
            else:
                out = self.out.get().strip().strip('"')
                if not out:
                    return None, "Укажите папку для результатов."
                a = os.path.normcase(os.path.abspath(out))
                for p in self.items:
                    b = os.path.normcase(os.path.abspath(p if os.path.isdir(p) else os.path.dirname(p)))
                    if a == b or (os.path.isdir(p) and a.startswith(b + os.sep)):
                        return None, "Папка для результатов не должна совпадать с папкой исходных записей или лежать внутри неё."
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
                APP, "Оригиналы будут ЗАМЕНЕНЫ результатом.\n\nКаждый файл заменяется только после успешной проверки, "
                     "но вернуть исходный файл после этого будет нельзя.\n\nПродолжить?", icon="warning", default="no"):
            return
        self._save_cfg()
        # пути передаются списком в файле: при перетаскивании сотен видео командная строка была бы слишком длинной
        fd, self.listfile = tempfile.mkstemp(prefix="nfixer-list-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(self.items))
        py = os.path.join(os.path.dirname(sys.executable), "python.exe")
        if not os.path.isfile(py):
            py = sys.executable
        env = dict(os.environ, PYTHONIOENCODING="utf-8", NFIXER_GUI="1")
        self.stopped = False
        self.proc = subprocess.Popen([py, "-u", "-X", "utf8", os.path.join(HERE, "nfixer.py"), cmd[0], "--list", self.listfile] + cmd[1:],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, cwd=HERE, creationflags=NO_WINDOW)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.b_start.configure(state="disabled")
        self.b_stop.configure(state="normal")
        self.bar.configure(value=0)
        self.state_text.set("Работаю...")
        self._write(f"=== {dict((a[0], a[1]) for a in ACTIONS)[cmd[0]]}: объектов в списке - {len(self.items)} ===", "head")
        self._sync()
        self.after(100, self._poll)

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
            self.state_text.set(f"Файл {m.group(1)} из {m.group(2)}")
        tag = "bad" if ("ОШИБКА" in line or "ПОВРЕЖДЁН" in line or "Traceback" in line) else "good" if ("готово:" in line or line.startswith("Было ")) else None
        self._write(line, tag)

    def _finished(self):
        self.proc = None
        if self.listfile:
            try:
                os.remove(self.listfile)
            except OSError:
                pass
            self.listfile = None
        self.b_start.configure(state="normal")
        self.b_stop.configure(state="disabled")
        if self.stopped:
            removed = self._cleanup()
            self.state_text.set("Остановлено")
            self._write("Остановлено. Оригиналы целы" + (", недописанный файл удалён." if removed else ".") + " Повторный запуск продолжит с места остановки.", "head")
        else:
            self.bar.configure(value=100)
            self.state_text.set("Готово")
        self._sync()

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
        self.state_text.set("Останавливаю...")
        subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)

    def _close(self):
        if self.proc:
            if not messagebox.askyesno(APP, "Работа ещё идёт. Остановить и закрыть?\n\nОригиналы останутся целы, позже можно продолжить."):
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
