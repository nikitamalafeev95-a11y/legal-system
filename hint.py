"""Подсказчик для внедренца 1С.

По медиа-клавише снимает активное окно, отправляет снимок в Claude
(headless-режим Claude Code, вход по подписке) и показывает короткую
подсказку в окне поверх всех окон. Окна программы исключены из захвата экрана.

Запуск: run.bat. Проверка установки: check.bat (python hint.py --check).
"""
import ctypes
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "config.json"
TASK_FILE = BASE / "task.txt"
PROMPT_FILE = BASE / "system_prompt.txt"
LOG_FILE = BASE / "hint.log"
IS_WIN = sys.platform == "win32"
SHOT_NAME = "shot.png"

DEFAULTS = {
    "key_capture": "media_stop",      # снимок активного окна
    "key_ask": "media_next",          # снимок + вопрос
    "shot_width": 1300,               # ширина снимка, px
    "model": "haiku",                 # самая быстрая и дешёвая
    "history_pairs": 5,               # сколько пар "экран - совет" помнить
    "font_size": 11,
    "window_width": 420,
    "window_x": 40,
    "window_y": 40,
    "timeout_sec": 90,                # сколько ждать ответ
    "same_shot_threshold": 0.05,      # допуск при сравнении снимков
    "hook_refresh_sec": 5,            # переустановка перехвата клавиш, 0 - выкл
    "claude_path": "",                # полный путь к claude, если не в PATH
}

TASK_TEMPLATE = (
    "Клиент: \n"
    "Программа и версия: \n"
    "Проблема: \n"
    "Что уже проверил: \n"
    "Цель: \n"
)

VK_CODES = {
    "media_next": 0xB0, "media_prev": 0xB1, "media_stop": 0xB2,
    "media_play_pause": 0xB3, "volume_mute": 0xAD, "volume_down": 0xAE,
    "volume_up": 0xAF, "pause": 0x13, "scroll_lock": 0x91, "insert": 0x2D,
}
VK_CODES.update({"f%d" % i: 0x70 + i - 1 for i in range(1, 25)})


class HintError(Exception):
    """Ошибка с текстом, который можно показать пользователю."""


# ---------------------------------------------------------------- настройки

def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        except ValueError as e:
            raise HintError("Ошибка в config.json: %s" % e)
        if not isinstance(data, dict):
            raise HintError("config.json должен быть объектом { ... }")
        cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    else:
        save_config(cfg)
    return cfg


def save_config(cfg):
    data = {k: cfg[k] for k in DEFAULTS}
    CONFIG_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_key(name):
    """Имя клавиши из config.json -> код виртуальной клавиши Windows."""
    key = str(name).strip().lower()
    if key in VK_CODES:
        return VK_CODES[key]
    try:
        code = int(key, 0)
    except ValueError:
        raise HintError(
            "Неизвестная клавиша в config.json: %r. Допустимо: %s или код вида 0xB2."
            % (name, ", ".join(sorted(VK_CODES))))
    if not 1 <= code <= 254:
        raise HintError("Код клавиши вне диапазона 1..254: %r" % name)
    return code


def read_task():
    if not TASK_FILE.exists():
        TASK_FILE.write_text(TASK_TEMPLATE, encoding="utf-8")
    return TASK_FILE.read_text(encoding="utf-8-sig").strip()


def write_task(text):
    TASK_FILE.write_text(text.strip() + "\n", encoding="utf-8")


# ------------------------------------------------------- запрос и история

def short(text, limit):
    """Одна строка не длиннее limit символов."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def build_prompt(task, history, question):
    parts = ["Задача клиента:\n" + (task or "(не заполнена)")]
    if history:
        lines = ["%d. Экран: %s. Совет: %s" % (i, screen, advice)
                 for i, (screen, advice) in enumerate(history, 1)]
        parts.append("Предыдущие шаги (от старых к новым):\n" + "\n".join(lines))
    if question:
        parts.append("Вопрос внедренца к снимку: " + question.strip())
    parts.append("Текущий экран - файл %s в текущей папке. Прочитай его и ответь."
                 % SHOT_NAME)
    return "\n\n".join(parts)


def parse_answer(text):
    """Ответ модели -> (подсказка для окна, описание экрана для истории)."""
    advice, screen = [], ""
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper().startswith("ЭКРАН:"):
            screen = line[6:].strip()
        else:
            advice.append(line)
    return "\n".join(advice), screen


def find_claude(cfg):
    path = str(cfg.get("claude_path") or "").strip()
    if path:
        if Path(path).exists():
            return path
        raise HintError("В config.json указан claude_path, но файла нет: " + path)
    found = shutil.which("claude")
    if found:
        return found
    for cand in (Path.home() / ".local" / "bin" / "claude.exe",
                 Path.home() / ".local" / "bin" / "claude"):
        if cand.exists():
            return str(cand)
    raise HintError("Claude Code не найден. Установите его и выполните вход "
                    "(см. пособие) или укажите claude_path в config.json.")


def make_workdir(prefix):
    """Временная папка для снимка. Путь приводится к полному виду: Windows
    может выдать короткое имя (NMALAF~1), и тогда Claude Code считает файл
    лежащим вне рабочей папки и отказывается его читать."""
    return Path(os.path.realpath(tempfile.mkdtemp(prefix=prefix)))


def shot_rules(workdir):
    """Правила доступа: только файл снимка, в относительной и полной записи."""
    rules = ["Read(./%s)" % SHOT_NAME]
    full = (Path(workdir) / SHOT_NAME).as_posix()
    if len(full) > 1 and full[1] == ":":            # C:/Users/... (Windows)
        rules.append("Read(//%s)" % full)
        rules.append("Read(//%s%s)" % (full[0].lower(), full[2:]))
    else:
        rules.append("Read(/%s)" % full)
    return rules


def build_command(cfg, exe, workdir):
    """Команда headless-запуска. Модели доступно только чтение файла снимка:
    единственный инструмент - Read, режим dontAsk запрещает читать что-либо
    вне рабочей папки, а в рабочей папке лежит только снимок."""
    return [
        exe, "-p",
        "--model", str(cfg["model"]),
        "--system-prompt-file", str(PROMPT_FILE),  # заменяет стандартную инструкцию
        "--tools", "Read",                        # других инструментов нет
        "--allowedTools", *shot_rules(workdir),
        "--permission-mode", "dontAsk",           # всё остальное запрещено
        "--strict-mcp-config",                    # без MCP-серверов
        "--no-session-persistence",               # сессию на диск не писать
        "--max-turns", "3",
        "--output-format", "json",
    ]


def child_env():
    """Окружение без API-ключей, чтобы запрос шёл через вход по подписке."""
    env = dict(os.environ)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(name, None)
    return env


def ask_claude(cfg, prompt, workdir):
    """Отправляет запрос, возвращает текст ответа. Снимок уже лежит в workdir."""
    exe = find_claude(cfg)
    try:
        proc = subprocess.run(
            build_command(cfg, exe, workdir), input=prompt, capture_output=True,
            encoding="utf-8", errors="replace", cwd=str(workdir),
            env=child_env(), timeout=float(cfg["timeout_sec"]),
            creationflags=0x08000000 if IS_WIN else 0)  # CREATE_NO_WINDOW
    except subprocess.TimeoutExpired:
        raise HintError("Нет ответа за %s с. Нажмите клавишу ещё раз."
                        % cfg["timeout_sec"])
    except OSError as e:
        raise HintError("Не удалось запустить Claude Code: %s" % e)

    out = (proc.stdout or "").strip()
    data = None
    for chunk in [out] + out.splitlines()[::-1]:
        try:
            parsed = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            data = parsed
            break
    if data is None:
        msg = short(out or proc.stderr or "пустой ответ", 300)
        raise HintError("Claude Code вернул ошибку: " + msg)
    if data.get("permission_denials"):
        raise HintError("Claude Code не смог прочитать снимок: доступ к файлу "
                        "отклонён. Сообщите разработчику.")
    result = str(data.get("result") or "").strip()
    if data.get("is_error") or proc.returncode != 0 or not result:
        low = result.lower()
        if "login" in low or "authenticat" in low or "api key" in low:
            raise HintError("Нет входа в Claude Code. Откройте терминал, "
                            "запустите claude и выполните /login.")
        if "limit" in low:
            raise HintError("Лимит подписки исчерпан: " + short(result, 200))
        raise HintError("Ошибка Claude Code: "
                        + short(result or data.get("subtype") or "нет ответа", 300))
    return result


# --------------------------------------------------------------- снимки

def shrink(im, width):
    from PIL import Image
    im = im.convert("RGB")
    if im.width > width:
        im = im.resize((width, max(1, round(im.height * width / im.width))),
                       Image.LANCZOS)
    return im


def signature(im):
    """Уменьшенная серая копия снимка, только для сравнения с предыдущим."""
    from PIL import Image
    return im.convert("L").resize((64, 64), Image.BOX)


def same_shot(sig_a, sig_b, threshold):
    """True, если снимки отличаются не больше порога (курсор, часы)."""
    if sig_a is None or sig_b is None:
        return False
    from PIL import ImageChops, ImageStat
    diff = ImageStat.Stat(ImageChops.difference(sig_a, sig_b)).mean[0]
    return diff <= float(threshold)


# ------------------------------------------------------------- Windows API

if IS_WIN:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM)

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                         wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = LRESULT
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
    user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t,
                                wintypes.UINT, ctypes.c_void_p]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                         wintypes.BOOL]
    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
    user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wintypes.UINT]
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                         ctypes.c_ssize_t]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.UINT]
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                      wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE

    WH_KEYBOARD_LL = 13
    WM_KEYDOWN, WM_SYSKEYDOWN, WM_TIMER, WM_QUIT = 0x0100, 0x0104, 0x0113, 0x0012
    WDA_MONITOR, WDA_EXCLUDEFROMCAPTURE = 0x01, 0x11
    GWL_EXSTYLE, WS_EX_NOACTIVATE = -20, 0x08000000

    class KeyHook(threading.Thread):
        """Глобальный перехват клавиш. Перехваченные нажатия дальше не идут:
        ни в активное окно (в том числе удалённого подключения), ни в плеер."""

        def __init__(self, keymap, on_key, refresh_sec):
            super().__init__(daemon=True)
            self.keymap = keymap          # код клавиши -> действие
            self.on_key = on_key          # вызывается из потока перехвата
            self.refresh_ms = int(max(0, float(refresh_sec)) * 1000)
            self.installed = threading.Event()
            self.ok = False
            self._hook = None
            self._down = set()
            self._tid = 0
            self._proc = HOOKPROC(self._callback)

        def _install(self):
            # Новый перехват ставится раньше снятия старого: он оказывается
            # первым в цепочке, даже если другая программа (например, клиент
            # удалённого доступа) поставила свой перехват позже нас.
            new = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
            if new:
                old, self._hook = self._hook, new
                if old:
                    user32.UnhookWindowsHookEx(old)
            return bool(new)

        def _callback(self, code, wparam, lparam):
            if code == 0:
                kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                action = self.keymap.get(kb.vkCode)
                if action:
                    if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        if kb.vkCode not in self._down:   # без автоповтора
                            self._down.add(kb.vkCode)
                            self.on_key(action, user32.GetForegroundWindow())
                    else:
                        self._down.discard(kb.vkCode)
                    return 1                              # нажатие "съедено"
            return user32.CallNextHookEx(None, code, wparam, lparam)

        def run(self):
            self._tid = kernel32.GetCurrentThreadId()
            self.ok = self._install()
            self.installed.set()
            if not self.ok:
                return
            if self.refresh_ms:
                user32.SetTimer(None, 0, self.refresh_ms, None)
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_TIMER:
                    self._install()
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)

        def stop(self):
            if self._tid:
                user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)

    def set_dpi_aware():
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass

    def top_hwnd(widget):
        return user32.GetAncestor(widget.winfo_id(), 2)  # GA_ROOT

    def protect_window(widget):
        """Исключает окно из захвата экрана. True - окно скрыто от захвата."""
        hwnd = top_hwnd(widget)
        cur = wintypes.DWORD(0)
        user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(cur))
        if cur.value == WDA_EXCLUDEFROMCAPTURE:
            return True
        if user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            return True
        # Windows старше 10 версии 2004: окно будет чёрным прямоугольником.
        user32.SetWindowDisplayAffinity(hwnd, WDA_MONITOR)
        return False

    def keep_on_top(widget, no_activate):
        hwnd = top_hwnd(widget)
        if no_activate:
            style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            if not style & WS_EX_NOACTIVATE:
                user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)
        # HWND_TOPMOST; SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

    def is_own_window(hwnd):
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == os.getpid()

    def force_foreground(hwnd):
        """Передаёт окну фокус ввода без мигания в панели задач."""
        if not hwnd or not user32.IsWindow(hwnd):
            return
        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            return
        me = kernel32.GetCurrentThreadId()
        other = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = bool(other and other != me
                        and user32.AttachThreadInput(me, other, True))
        user32.SetForegroundWindow(hwnd)
        if attached:
            user32.AttachThreadInput(me, other, False)

    def capture_window(hwnd, width):
        """Снимок одного окна (не всего экрана), уменьшенный до width px."""
        from PIL import ImageGrab
        if not hwnd or not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
            return None
        rect = wintypes.RECT()
        got = False
        try:  # границы без тени окна
            got = ctypes.windll.dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)) == 0
        except Exception:
            got = False
        if not got and not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        left, top = max(rect.left, vx), max(rect.top, vy)
        right, bottom = min(rect.right, vx + vw), min(rect.bottom, vy + vh)
        if right - left < 50 or bottom - top < 50:
            return None
        im = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        return shrink(im, width)

    def already_running():
        kernel32.CreateMutexW(None, False, "Local\\Hint1C_single_instance")
        return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS

    def fatal_box(text):
        user32.MessageBoxW(None, text, "Подсказчик", 0x10)


# ------------------------------------------------------------------- окно

BG, BAR, FG, DIM = "#1f2125", "#2b2e34", "#e9eaec", "#9aa0a8"


class App:
    def __init__(self, cfg):
        import tkinter as tk
        self.tk = tk
        self.cfg = cfg
        self.events = queue.Queue()
        self.history = deque(maxlen=max(0, int(cfg["history_pairs"])))
        self.last_sig = None
        self.last_answer = ""
        self.busy = False
        self.dialog = None
        self.target_hwnd = None
        self.windows = []                      # окна, скрываемые от захвата
        self.workdir = make_workdir("hint_")

        self.root = root = tk.Tk()
        root.title("Подсказчик")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.0)         # невидимо, пока не скрыто от захвата
        root.configure(bg=BG)

        bar = tk.Frame(root, bg=BAR)
        bar.pack(fill="x")
        self._button(bar, "Задача", self.edit_task).pack(side="left")
        self._button(bar, "Новая сессия", self.new_session).pack(side="left")
        self._button(bar, "×", self.quit).pack(side="right")
        self._button(bar, "A+", lambda: self.change_font(1)).pack(side="right")
        self._button(bar, "A−", lambda: self.change_font(-1)).pack(side="right")

        self.label = tk.Label(root, bg=BG, fg=FG, justify="left", anchor="nw",
                              padx=10, pady=8)
        self.label.pack(fill="both", expand=True)
        for w in (bar, self.label):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)

        self.windows.append((root, True))
        self.show("Готов.\n%s - снимок окна.\n%s - снимок с вопросом."
                  % (cfg["key_capture"], cfg["key_ask"]))
        self._reveal(root, no_activate=True)

        self.hook = None
        if IS_WIN:
            keymap = {parse_key(cfg["key_capture"]): "capture",
                      parse_key(cfg["key_ask"]): "ask"}
            self.hook = KeyHook(
                keymap, lambda a, h: self.events.put(("key", a, h)),
                cfg["hook_refresh_sec"])
            self.hook.start()
            self.hook.installed.wait(3)
            if not self.hook.ok:
                self.show("Не удалось включить перехват клавиш. "
                          "Перезапустите программу.")
        root.after(50, self._poll)
        root.after(1000, self._tick)

    # --- оформление

    def _button(self, parent, text, command):
        return self.tk.Button(
            parent, text=text, command=command, bg=BAR, fg=DIM, bd=0,
            activebackground="#3a3e46", activeforeground=FG, padx=8, pady=2,
            font=("Segoe UI", 9), takefocus=0, cursor="hand2")

    def _font(self):
        return ("Segoe UI", int(self.cfg["font_size"]))

    def _reveal(self, win, no_activate=False):
        """Показывает окно только после того, как оно скрыто от захвата."""
        win.update_idletasks()
        win.update()
        if IS_WIN:
            protect_window(win)
            keep_on_top(win, no_activate)
        win.attributes("-alpha", 1.0)

    def show(self, text):
        width = int(self.cfg["window_width"])
        self.label.configure(text=text, font=self._font(), wraplength=width - 20)
        self.root.update_idletasks()
        self.root.geometry("%dx%d+%d+%d" % (
            width, self.root.winfo_reqheight(),
            int(self.cfg["window_x"]), int(self.cfg["window_y"])))

    def change_font(self, step):
        size = min(28, max(7, int(self.cfg["font_size"]) + step))
        self.cfg["font_size"] = size
        save_config(self.cfg)
        self.show(self.label.cget("text"))

    # --- перетаскивание

    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        dx, dy = self._drag
        self.root.geometry("+%d+%d" % (e.x_root - dx, e.y_root - dy))

    def _drag_end(self, _e):
        self.cfg["window_x"] = self.root.winfo_x()
        self.cfg["window_y"] = self.root.winfo_y()
        save_config(self.cfg)

    # --- служебные циклы

    def _tick(self):
        """Раз в секунду подтверждает скрытие от захвата и положение поверх окон."""
        alive = []
        for win, no_activate in self.windows:
            try:
                if not win.winfo_exists():
                    continue
                if IS_WIN:
                    protect_window(win)
                    keep_on_top(win, no_activate)
                alive.append((win, no_activate))
            except Exception:
                pass
        self.windows = alive
        self.root.after(1000, self._tick)

    def _poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "key":
                    self.on_key(event[1], event[2])
                elif event[0] == "answer":
                    self.on_answer(*event[1:])
                elif event[0] == "error":
                    self.busy = False
                    self.show(event[1])
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    # --- клавиши и запрос

    def on_key(self, action, hwnd):
        if self.busy or self.dialog is not None:
            return
        if IS_WIN:
            if hwnd and not is_own_window(hwnd):
                self.target_hwnd = hwnd
            try:
                im = capture_window(self.target_hwnd, int(self.cfg["shot_width"]))
            except Exception:
                im = None
        else:  # режим проверки интерфейса вне Windows
            from PIL import Image
            path = os.environ.get("HINT_TEST_IMAGE")
            im = shrink(Image.open(path), int(self.cfg["shot_width"])) if path else None
        if im is None:
            self.show("Не удалось снять окно. Сделайте окно подключения активным.")
            return
        if action == "ask":
            self.ask_question(im)
        else:
            self.start_request(im, "")

    def start_request(self, im, question):
        sig = signature(im)
        if (not question and self.last_answer
                and same_shot(sig, self.last_sig, self.cfg["same_shot_threshold"])):
            logging.info("повтор снимка, запрос не отправлялся")
            self.show(self.last_answer)
            return
        self.busy = True
        prompt = build_prompt(read_task(), list(self.history), question)
        threading.Thread(target=self._worker, args=(im, prompt, sig),
                         daemon=True).start()

    def _worker(self, im, prompt, sig):
        shot = self.workdir / SHOT_NAME
        started = time.time()
        try:
            im.save(shot, "PNG")
            text = ask_claude(self.cfg, prompt, self.workdir)
            logging.info("запрос: ответ за %.1f с", time.time() - started)
            self.events.put(("answer", text, sig))
        except HintError as e:
            logging.info("запрос: ошибка за %.1f с", time.time() - started)
            self.events.put(("error", str(e)))
        except Exception as e:
            logging.info("запрос: сбой %s за %.1f с",
                         type(e).__name__, time.time() - started)
            self.events.put(("error", "Сбой программы: %s" % type(e).__name__))
        finally:
            try:
                shot.unlink()      # снимок удаляется сразу после ответа
            except OSError:
                pass

    def on_answer(self, text, sig):
        self.busy = False
        advice, screen = parse_answer(text)
        advice = advice or text.strip()
        if self.history.maxlen:
            self.history.append((short(screen or "не описан", 150),
                                 short(advice, 220)))
        self.last_sig, self.last_answer = sig, advice
        self.show(advice)

    def new_session(self):
        self.history.clear()
        self.last_sig, self.last_answer = None, ""
        self.show("Новая сессия. История очищена.")

    # --- всплывающие поля ввода

    def _popup(self):
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.0)
        win.configure(bg=BAR, padx=6, pady=6)
        self.dialog = win
        self.windows.append((win, False))
        return win

    def _place_popup(self, win, focus_widget):
        win.update_idletasks()
        x = self.root.winfo_x()
        y = self.root.winfo_y() + self.root.winfo_height() + 4
        if y + win.winfo_reqheight() > win.winfo_screenheight():
            y = max(0, self.root.winfo_y() - win.winfo_reqheight() - 4)
        win.geometry("+%d+%d" % (x, y))
        self._reveal(win)
        if IS_WIN:
            force_foreground(top_hwnd(win))
        win.focus_force()
        focus_widget.focus_set()

    def _close_popup(self):
        if self.dialog is not None:
            self.dialog.destroy()
            self.dialog = None
        if IS_WIN and self.target_hwnd:
            force_foreground(self.target_hwnd)  # вернуть фокус окну подключения

    @staticmethod
    def _ru_hotkeys(widget):
        """Ctrl+C/V/X/A при русской раскладке (Tk их сам не понимает)."""
        events = {67: "<<Copy>>", 86: "<<Paste>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}

        def handler(e):
            name = events.get(e.keycode)
            if name and e.keysym.lower() not in ("c", "v", "x", "a"):
                widget.event_generate(name)
                return "break"
        widget.bind("<Control-KeyPress>", handler)

    def ask_question(self, im):
        tk = self.tk
        win = self._popup()
        tk.Label(win, text="Вопрос к снимку (Enter - отправить, Esc - отмена)",
                 bg=BAR, fg=DIM, font=("Segoe UI", 9)).pack(anchor="w")
        entry = tk.Entry(win, width=52, font=self._font(), bg=BG, fg=FG,
                         insertbackground=FG, relief="flat")
        entry.pack(fill="x", pady=(4, 0), ipady=3)
        self._ru_hotkeys(entry)

        def submit(_e=None):
            question = entry.get().strip()
            self._close_popup()
            self.start_request(im, question)

        entry.bind("<Return>", submit)
        entry.bind("<Escape>", lambda _e: self._close_popup())
        self._place_popup(win, entry)

    def edit_task(self):
        if self.dialog is not None:
            return
        tk = self.tk
        win = self._popup()
        tk.Label(win, text="Задача клиента (подставляется в каждый запрос)",
                 bg=BAR, fg=DIM, font=("Segoe UI", 9)).pack(anchor="w")
        box = tk.Text(win, width=60, height=10, font=self._font(), bg=BG, fg=FG,
                      insertbackground=FG, relief="flat", wrap="word", undo=True)
        box.pack(pady=4)
        box.insert("1.0", read_task())
        self._ru_hotkeys(box)

        def save(_e=None):
            write_task(box.get("1.0", "end"))
            self._close_popup()
            self.show("Задача сохранена.")
            return "break"

        row = tk.Frame(win, bg=BAR)
        row.pack(fill="x")
        self._button(row, "Сохранить (Ctrl+Enter)", save).pack(side="left")
        self._button(row, "Отмена (Esc)", self._close_popup).pack(side="left")
        box.bind("<Control-Return>", save)
        box.bind("<Escape>", lambda _e: self._close_popup())
        self._place_popup(win, box)

    # --- выход

    def quit(self):
        if self.hook:
            self.hook.stop()
        shutil.rmtree(self.workdir, ignore_errors=True)
        self.root.destroy()

    def run(self):
        try:
            self.root.mainloop()
        finally:
            shutil.rmtree(self.workdir, ignore_errors=True)


# --------------------------------------------------------------- проверка

def check():
    """Проверка установки: python hint.py --check"""
    ok = True

    def line(good, text):
        nonlocal ok
        ok = ok and good
        print(("[ OK ] " if good else "[ !! ] ") + text)

    line(sys.version_info >= (3, 10), "Python %s (нужен 3.10+)" % sys.version.split()[0])
    try:
        import PIL
        line(True, "Pillow %s" % PIL.__version__)
    except ImportError:
        line(False, "Pillow не установлен: python -m pip install -r requirements.txt")
        return 1
    try:
        import tkinter  # noqa: F401
        line(True, "tkinter есть")
    except ImportError:
        line(False, "нет tkinter: переустановите Python с галочкой tcl/tk")
    if IS_WIN:
        build = sys.getwindowsversion().build
        line(build >= 19041, "Windows, сборка %d (скрытие от захвата: 19041+)" % build)
    else:
        line(False, "это не Windows: клавиши и снимки работать не будут")
    try:
        cfg = load_config()
        codes = [parse_key(cfg["key_capture"]), parse_key(cfg["key_ask"])]
        line(codes[0] != codes[1], "config.json: клавиши %s и %s, модель %s"
             % (cfg["key_capture"], cfg["key_ask"], cfg["model"]))
        exe = find_claude(cfg)
        line(True, "Claude Code: " + exe)
    except HintError as e:
        line(False, str(e))
        return 1
    line(PROMPT_FILE.exists(), "system_prompt.txt на месте")
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("[ i  ] задана ANTHROPIC_API_KEY - программа её не использует, "
              "запросы идут по подписке")

    print("Тестовый запрос (несколько секунд)...")
    from PIL import Image, ImageDraw
    workdir = make_workdir("hint_check_")
    try:
        im = Image.new("RGB", (600, 200), "white")
        draw = ImageDraw.Draw(im)
        draw.rectangle((0, 0, 600, 30), fill=(255, 221, 85))
        draw.text((10, 9), "1C:Enterprise - test window", fill="black")
        draw.text((20, 80), "Error: period is closed.   [ OK ]", fill="black")
        im.save(workdir / SHOT_NAME)
        started = time.time()
        text = ask_claude(cfg, build_prompt(read_task(), [], ""), workdir)
        advice, screen = parse_answer(text)
        line(bool(advice), "ответ получен за %.1f с" % (time.time() - started))
        print("       Подсказка: " + short(advice, 200))
        print("       Экран: " + (screen or "(модель не дала строку ЭКРАН)"))
    except HintError as e:
        line(False, str(e))
        print("       Папка снимка: %s" % workdir)
        print("       Правила: %s" % " ".join(shot_rules(workdir)))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print("\nВсё готово, запускайте run.bat." if ok
          else "\nЕсть проблемы - см. строки с [ !! ].")
    return 0 if ok else 1


def main():
    if "--check" in sys.argv:
        return check()
    logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO,
                        format="%(asctime)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", encoding="utf-8")
    try:
        if IS_WIN:
            if already_running():
                return 0
            set_dpi_aware()
        cfg = load_config()
        parse_key(cfg["key_capture"])
        parse_key(cfg["key_ask"])
        read_task()
        if not PROMPT_FILE.exists():
            raise HintError("Нет файла system_prompt.txt рядом с программой.")
        App(cfg).run()
    except Exception as e:
        logging.info("запуск: ошибка %s", type(e).__name__)
        text = str(e) if isinstance(e, HintError) else "%s: %s" % (type(e).__name__, e)
        if IS_WIN:
            fatal_box(text)
        else:
            print(text, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
