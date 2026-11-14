"""Подсказчик для внедренца 1С.

По медиа-клавише снимает активное окно, отправляет снимок в Claude
(headless-режим Claude Code, вход по подписке) и показывает короткую
подсказку в окне поверх всех окон. Окна программы исключены из захвата экрана.

Запуск: run.bat. Проверка установки: check.bat (python hint.py --check).
"""
import base64
import ctypes
import io
import json
import logging
import os
import queue
import re
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
SCRIPT_FILE = BASE / "script.txt"
KNOWLEDGE_FILE = BASE / "knowledge.txt"
LOG_FILE = BASE / "hint.log"
IS_WIN = sys.platform == "win32"
SHOT_NAME = "shot.png"

DEFAULTS = {
    "key_capture": "media_stop",      # снимок активного окна
    "key_ask": "media_next",          # снимок + вопрос
    "key_voice": "media_prev",        # держать: запись звука, отпустить: снимок
    "audio_source": "system",         # system - звук встречи, mic - микрофон, both
    "whisper_model": "small",         # модель распознавания речи
    "voice_language": "ru",
    "voice_max_sec": 90,              # предел одной записи
    "voice_hint": "Разговор о работе в программе 1С: документы, проведение, "
                  "закрытие месяца, контрагенты, номенклатура, отчёты.",
    "shot_width": 1300,               # ширина снимка, px
    "model": "haiku",                 # самая быстрая и дешёвая
    "models": ["haiku", "sonnet", "opus"],  # по кругу кнопкой в окне
    "history_pairs": 5,               # сколько пар "экран - совет" помнить
    "font_size": 11,
    "window_width": 420,
    "window_x": 40,
    "window_y": 40,
    "timeout_sec": 90,                # сколько ждать ответ
    "same_shot_threshold": 0.05,      # допуск при сравнении снимков
    "hook_refresh_sec": 5,            # переустановка перехвата клавиш, 0 - выкл
    "claude_path": "",                # полный путь к claude, если не в PATH
    "image_mode": "direct",           # direct - снимок прямо в запросе (дешевле),
                                      # file - через файл и инструмент Read
    "script_step": 1,                 # текущий пункт скрипта (меняется кнопками)
    "off_script": False,              # режим "вне скрипта"
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


class DirectUnsupported(HintError):
    """Claude Code не принял снимок прямо в запросе: нужен режим file."""


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


def read_script():
    """Скрипт встречи и прочие материалы по клиенту. Файла может не быть."""
    if not SCRIPT_FILE.exists():
        return ""
    return SCRIPT_FILE.read_text(encoding="utf-8-sig").strip()


def write_script(text):
    SCRIPT_FILE.write_text(text.strip() + "\n", encoding="utf-8")


STEP_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)[.)]\s+(\S.*)$")


def parse_steps(script):
    """Пункты скрипта: строки вида "1. текст" или "2) текст" (и 1.1. текст).
    Строки без номера относятся к предыдущему пункту."""
    steps = []
    for line in script.splitlines():
        m = STEP_RE.match(line)
        if m:
            steps.append([m.group(1), m.group(2).strip()])
        elif steps and line.strip():
            steps[-1][1] += " " + line.strip()
    return [(num, text) for num, text in steps]


def read_knowledge():
    """Справочник внедренца: проверенные пути и типовые ошибки. Может не быть."""
    if not KNOWLEDGE_FILE.exists():
        return ""
    return KNOWLEDGE_FILE.read_text(encoding="utf-8-sig").strip()


SHOT_RULES = {
    "direct": "- Снимок экрана приложен к сообщению.",
    "file": "- Снимок экрана лежит в файле %s в текущей папке. Сначала прочитай "
            "его инструментом Read, других действий не выполняй." % "shot.png",
}


def build_system(instructions, script, knowledge="", mode="direct"):
    """Системная часть запроса: инструкция, справочник, скрипт встречи.
    Всё это между нажатиями не меняется, поэтому Claude Code берёт эту часть
    из кэша, а не обрабатывает заново. Порядок - от самого постоянного
    (инструкция, справочник) к тому, что меняется от клиента к клиенту."""
    lines = [ln for ln in instructions.strip().splitlines()
             if "shot.png" not in ln]          # правило из старого файла
    text = "\n".join(lines) + "\n" + SHOT_RULES[mode]
    if knowledge:
        text += "\n\n" + knowledge
    if script:
        text += ("\n\nСкрипт встречи и материалы по клиенту (справочно, не "
                 "пересказывай; текущий пункт указан в запросе):\n" + script)
    return text + "\n"


def write_system(path, mode="direct"):
    Path(path).write_text(
        build_system(PROMPT_FILE.read_text(encoding="utf-8-sig"), read_script(),
                     read_knowledge(), mode),
        encoding="utf-8")


# ------------------------------------------------------- запрос и история

def short(text, limit):
    """Одна строка не длиннее limit символов."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def build_prompt(task, history, question, step=None, off_script=False,
                 with_shot=True, speech="", direct=True):
    """Текст запроса. step - (номер, текст) текущего пункта скрипта."""
    parts = ["Задача клиента:\n" + (task or "(не заполнена)")]
    if off_script:
        parts.append("Сейчас внедренец отклонился от скрипта: ситуация не "
                     "связана с его пунктами. Помоги с тем, что на экране, "
                     "к скрипту не возвращай.")
    elif step:
        parts.append("Текущий пункт скрипта: %s. %s" % (step[0], short(step[1], 400)))
    if history:
        lines = ["%d. Экран: %s. Совет: %s" % (i, screen, advice)
                 for i, (screen, advice) in enumerate(history, 1)]
        parts.append("Предыдущие шаги (от старых к новым):\n" + "\n".join(lines))
    if question:
        parts.append("Вопрос внедренца%s: %s"
                     % (" к снимку" if with_shot else "", question.strip()))
    if speech:
        parts.append("Расшифровка записи со встречи, сделанной только что "
                     "(распознано автоматически, возможны ошибки в словах):\n"
                     + speech + "\nПо сказанному и по снимку подскажи решение.")
    if with_shot and direct:
        parts.append("Текущий экран - на приложенном снимке. Ответь.")
    elif with_shot:
        parts.append("Текущий экран - файл %s в текущей папке. Прочитай его и "
                     "ответь." % SHOT_NAME)
    else:
        parts.append("В этом запросе снимка нет%s. Ответь на вопрос по задаче и "
                     "предыдущим шагам. Строку ЭКРАН не добавляй."
                     % ("" if direct else ": файл не читай, инструмент Read не "
                        "вызывай"))
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


def next_model(cfg):
    """Следующая модель из списка models (кнопка в окне подсказок)."""
    models = [str(m) for m in cfg.get("models") or [] if str(m).strip()]
    if not models:
        return str(cfg["model"])
    if cfg["model"] in models:
        return models[(models.index(cfg["model"]) + 1) % len(models)]
    return models[0]


def build_command(cfg, exe, workdir, sysfile, mode="direct"):
    """Команда headless-запуска Claude Code.

    direct: снимок идёт прямо в сообщении (stream-json), инструментов у модели
    нет совсем, обращение к модели одно. Это дешевле и быстрее.
    file: запасной способ. Единственный инструмент - Read, режим dontAsk
    запрещает читать что-либо вне рабочей папки, а в ней лежит только снимок."""
    cmd = [
        exe, "-p",
        "--model", str(cfg["model"]),
        "--system-prompt-file", str(sysfile),     # заменяет стандартную инструкцию
        "--permission-mode", "dontAsk",           # всё, что не разрешено, запрещено
        "--strict-mcp-config",                    # без MCP-серверов
        "--no-session-persistence",               # сессию на диск не писать
    ]
    if mode == "direct":
        return cmd + ["--tools", "", "--max-turns", "1",
                      "--input-format", "stream-json",
                      "--output-format", "stream-json", "--verbose"]
    return cmd + ["--tools", "Read", "--allowedTools", *shot_rules(workdir),
                  "--max-turns", "3", "--output-format", "json"]


def direct_message(prompt, png):
    """Сообщение для режима direct: текст и, если есть, снимок в base64."""
    content = [{"type": "text", "text": prompt}]
    if png:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.b64encode(png).decode("ascii")}})
    return json.dumps({"type": "user",
                       "message": {"role": "user", "content": content}}) + "\n"


def child_env():
    """Окружение без API-ключей, чтобы запрос шёл через вход по подписке."""
    env = dict(os.environ)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(name, None)
    return env


def ask_claude(cfg, prompt, workdir, sysfile, png=None, mode="direct"):
    """Отправляет запрос, возвращает текст ответа. png - снимок (байты PNG) или
    None. Системная часть лежит в файле sysfile (вне workdir)."""
    exe = find_claude(cfg)
    shot = Path(workdir) / SHOT_NAME
    try:
        if mode == "direct":
            stdin = direct_message(prompt, png)
        else:
            stdin = prompt
            if png:
                shot.write_bytes(png)
        proc = subprocess.run(
            build_command(cfg, exe, workdir, sysfile, mode), input=stdin,
            capture_output=True, encoding="utf-8", errors="replace",
            cwd=str(workdir), env=child_env(), timeout=float(cfg["timeout_sec"]),
            creationflags=0x08000000 if IS_WIN else 0)  # CREATE_NO_WINDOW
    except subprocess.TimeoutExpired:
        raise HintError("Нет ответа за %s с. Нажмите клавишу ещё раз."
                        % cfg["timeout_sec"])
    except OSError as e:
        raise HintError("Не удалось запустить Claude Code: %s" % e)
    finally:
        try:
            shot.unlink()          # снимок удаляется сразу после ответа
        except OSError:
            pass

    out = (proc.stdout or "").strip()
    data = None
    for chunk in out.splitlines()[::-1] + [out]:
        try:
            parsed = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(parsed, dict) and (parsed.get("type") == "result"
                                         or "result" in parsed):
            data = parsed
            break
    if data is None:
        msg = short(proc.stderr or out or "пустой ответ", 300)
        if mode == "direct":
            raise DirectUnsupported("Claude Code вернул ошибку: " + msg)
        raise HintError("Claude Code вернул ошибку: " + msg)
    if mode == "file" and png and data.get("permission_denials"):
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


def ask(cfg, make_prompt, workdir, sysfile, png, mode):
    """Запрос с запасным способом. make_prompt(direct) строит текст запроса.
    Возвращает (ответ, режим, который сработал)."""
    if mode == "direct":
        try:
            write_system(sysfile, "direct")
            return ask_claude(cfg, make_prompt(True), workdir, sysfile, png,
                              "direct"), "direct"
        except DirectUnsupported:
            logging.info("снимок в запросе не принят, перехожу на чтение файла")
    write_system(sysfile, "file")
    return ask_claude(cfg, make_prompt(False), workdir, sysfile, png, "file"), "file"


def to_png(im):
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


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


# ---------------------------------------------------------------- голос

VOICE_INSTALL = ("Голосовая клавиша не установлена. Выполните в PowerShell в "
                 "папке программы: python -m pip install -r requirements-voice.txt")
SPEAKERS = (("client", "Клиент"), ("me", "Внедренец"))


def to_mono16k(raw, channels, rate):
    """Сырые 16-битные отсчёты -> моно 16 кГц (формат для распознавания)."""
    import numpy as np
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data[:len(data) - len(data) % channels].reshape(-1, channels).mean(axis=1)
    if rate != 16000 and len(data):
        n = int(len(data) * 16000 / rate)
        data = np.interp(np.linspace(0, len(data) - 1, n),
                         np.arange(len(data)), data).astype(np.float32)
    return data


def speech_text(parts):
    """{"client": "...", "me": "..."} -> текст с подписями говорящих."""
    lines = [(label, parts.get(key, "").strip()) for key, label in SPEAKERS]
    lines = [(label, text) for label, text in lines if text]
    if len(lines) == 1 and lines[0][0] == "Клиент":
        return lines[0][1]
    return "\n".join("%s: %s" % line for line in lines)


class Recorder:
    """Запись звука в память (на диск ничего не пишется). Звук встречи берётся
    с устройства вывода (WASAPI loopback), микрофон - с устройства ввода."""

    def __init__(self, source):
        try:
            import numpy  # noqa: F401
            import pyaudiowpatch as pa
        except ImportError:
            raise HintError(VOICE_INSTALL)
        self.pa_mod = pa
        self.pa = pa.PyAudio()
        self.tracks = {}
        try:
            if source in ("system", "both"):
                self._open("client", self._loopback_device(), None, None)
            if source in ("mic", "both"):
                info = self.pa.get_default_input_device_info()
                try:
                    self._open("me", info, 1, 16000)
                except Exception:
                    self._open("me", info, None, None)
            if not self.tracks:
                raise HintError("В config.json audio_source должен быть "
                                "system, mic или both.")
        except HintError:
            self.pa.terminate()
            raise
        except Exception as e:
            self.pa.terminate()
            raise HintError("Не удалось начать запись звука: %s" % short(e, 150))

    def _loopback_device(self):
        if hasattr(self.pa, "get_default_wasapi_loopback"):
            return self.pa.get_default_wasapi_loopback()
        api = self.pa.get_host_api_info_by_type(self.pa_mod.paWASAPI)
        out = self.pa.get_device_info_by_index(api["defaultOutputDevice"])
        for dev in self.pa.get_loopback_device_info_generator():
            if out["name"] in dev["name"]:
                return dev
        raise HintError("Не найдено устройство для записи звука встречи.")

    def _open(self, name, info, channels, rate):
        channels = channels or max(1, int(info["maxInputChannels"]))
        rate = rate or int(info["defaultSampleRate"])
        chunks = []

        def callback(data, _frames, _time, _status):
            chunks.append(data)
            return (None, self.pa_mod.paContinue)

        stream = self.pa.open(
            format=self.pa_mod.paInt16, channels=channels, rate=rate, input=True,
            input_device_index=int(info["index"]), frames_per_buffer=1024,
            stream_callback=callback)
        self.tracks[name] = (chunks, channels, rate, stream)

    def stop(self):
        """Останавливает запись, возвращает {"client": звук, "me": звук}."""
        result = {}
        for name, (chunks, channels, rate, stream) in self.tracks.items():
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            result[name] = to_mono16k(b"".join(chunks), channels, rate)
        self.pa.terminate()
        return result


class Transcriber:
    """Распознавание речи на этом компьютере (faster-whisper). Модель
    скачивается один раз при первом использовании и остаётся в памяти."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.model = None
        self.lock = threading.Lock()

    def load(self):
        with self.lock:
            if self.model is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError:
                    raise HintError(VOICE_INSTALL)
                try:
                    self.model = WhisperModel(str(self.cfg["whisper_model"]),
                                              device="cpu", compute_type="int8")
                except Exception as e:
                    raise HintError("Не удалось загрузить модель распознавания "
                                    "речи: %s" % short(e, 200))

    def transcribe(self, audio):
        if audio is None or len(audio) < 16000 * 0.3:    # короче 0,3 с
            return ""
        self.load()

        def run(vad):
            segments, _info = self.model.transcribe(
                audio, language=str(self.cfg["voice_language"]) or None,
                initial_prompt=str(self.cfg["voice_hint"]) or None,
                condition_on_previous_text=False, vad_filter=vad)
            return " ".join(seg.text.strip() for seg in segments).strip()

        try:
            return run(True)       # с отсечением тишины
        except Exception:
            return run(False)


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
                    elif kb.vkCode in self._down:
                        self._down.discard(kb.vkCode)
                        self.on_key(action + "_up", user32.GetForegroundWindow())
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
        self.voice = None                      # идущая запись звука
        self.transcriber = Transcriber(cfg)
        self.hints = []                        # показанные подсказки, для листания
        self.hint_pos = -1
        self.steps = parse_steps(read_script())
        self.busy = False
        self.dialog = None
        self.target_hwnd = None
        self.windows = []                      # окна, скрываемые от захвата
        self.tmpdir = make_workdir("hint_")
        self.workdir = self.tmpdir / "shot"    # рабочая папка Claude Code: пусто
                                               # или только снимок (режим file)
        self.workdir.mkdir()
        self.sysfile = self.tmpdir / "system.txt"
        self.image_mode = "file" if cfg["image_mode"] == "file" else "direct"

        self.root = root = tk.Tk()
        root.title("Подсказчик")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.0)         # невидимо, пока не скрыто от захвата
        root.configure(bg=BG)

        bar = tk.Frame(root, bg=BAR)
        bar.pack(fill="x")
        self._button(bar, "Задача", self.edit_task).pack(side="left")
        self._button(bar, "Скрипт", self.edit_script).pack(side="left")
        self._button(bar, "Новая сессия", self.new_session).pack(side="left")
        self._button(bar, "×", self.quit).pack(side="right")
        self._button(bar, "A+", lambda: self.change_font(1)).pack(side="right")
        self._button(bar, "A−", lambda: self.change_font(-1)).pack(side="right")
        self.model_btn = self._button(bar, str(cfg["model"]), self.switch_model)
        self.model_btn.pack(side="right")

        nav = tk.Frame(root, bg=BG)
        nav.pack(fill="x")
        self.cap_lbl = self._caption(nav, "Подсказка")
        self.cap_lbl.pack(side="left", padx=(8, 0))
        self._button(nav, "<", lambda: self.page_hint(-1), BG).pack(side="left")
        self.hint_lbl = self._caption(nav, "0/0")
        self.hint_lbl.pack(side="left")
        self._button(nav, ">", lambda: self.page_hint(1), BG).pack(side="left")
        self.step_box = tk.Frame(nav, bg=BG)   # виден, когда в скрипте есть пункты
        self.off_btn = self._button(self.step_box, "Вне скрипта",
                                    self.toggle_off_script, BG)
        self.off_btn.pack(side="right")
        self._button(self.step_box, ">", lambda: self.move_step(1), BG).pack(side="right")
        self.step_lbl = self._caption(self.step_box, "")
        self.step_lbl.pack(side="right")
        self._button(self.step_box, "<", lambda: self.move_step(-1), BG).pack(side="right")
        self._caption(self.step_box, "Пункт").pack(side="right")
        self.step_text = tk.Label(root, bg=BG, fg=DIM, anchor="w", justify="left",
                                  padx=10, font=("Segoe UI", 9))

        self.label = tk.Label(root, bg=BG, fg=FG, justify="left", anchor="nw",
                              padx=10, pady=8)
        self.label.pack(fill="both", expand=True)
        self.refresh_steps()
        for w in (bar, nav, self.step_text, self.label):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)

        self.windows.append((root, True))
        self.show("Готов.\n%s - снимок окна.\n%s - снимок с вопросом.\n"
                  "%s - держать: запись звука, отпустить: снимок и ответ."
                  % (cfg["key_capture"], cfg["key_ask"], cfg["key_voice"]))
        self._reveal(root, no_activate=True)

        self.hook = None
        if IS_WIN:
            keymap = {parse_key(cfg["key_capture"]): "capture",
                      parse_key(cfg["key_ask"]): "ask",
                      parse_key(cfg["key_voice"]): "voice"}
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

    def _caption(self, parent, text):
        return self.tk.Label(parent, text=text, bg=BG, fg=DIM,
                             font=("Segoe UI", 9))

    def _button(self, parent, text, command, bg=BAR):
        return self.tk.Button(
            parent, text=text, command=command, bg=bg, fg=DIM, bd=0,
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

    def switch_model(self):
        """Переключает модель по кругу. Подсказка на экране остаётся, а тот же
        снимок после переключения отправится заново - уже новой модели."""
        if self.busy:
            return
        self.cfg["model"] = next_model(self.cfg)
        save_config(self.cfg)
        self.model_btn.configure(text=self.cfg["model"])
        self.last_sig = None

    # --- листание подсказок

    def page_hint(self, step):
        if not self.hints:
            return
        self.hint_pos = min(len(self.hints) - 1, max(0, self.hint_pos + step))
        self._show_hint()

    def _show_hint(self):
        self.hint_lbl.configure(text="%d/%d" % (self.hint_pos + 1, len(self.hints)))
        self.show(self.hints[self.hint_pos])

    def add_hint(self, text):
        self.hints.append(text)
        del self.hints[:-50]                   # в памяти не больше 50 подсказок
        self.hint_pos = len(self.hints) - 1
        self._show_hint()

    # --- пункты скрипта

    def current_step(self):
        if not self.steps:
            return None
        pos = min(len(self.steps), max(1, int(self.cfg["script_step"])))
        return self.steps[pos - 1]

    def refresh_steps(self):
        """Обновляет строку пункта после смены пункта или правки скрипта."""
        self.steps = parse_steps(read_script())
        if not self.steps:
            self.step_box.pack_forget()
            self.step_text.pack_forget()
            return
        pos = min(len(self.steps), max(1, int(self.cfg["script_step"])))
        self.cfg["script_step"] = pos
        off = bool(self.cfg["off_script"])
        num, text = self.steps[pos - 1]
        self.step_lbl.configure(text="%d/%d" % (pos, len(self.steps)))
        self.off_btn.configure(fg="#ffb454" if off else DIM)
        self.step_text.configure(
            text="Вне скрипта: подсказки без привязки к пунктам" if off
            else short("%s. %s" % (num, text), 110),
            wraplength=int(self.cfg["window_width"]) - 20)
        self.step_box.pack(side="right", padx=(0, 4))
        self.step_text.pack(fill="x", before=self.label)

    def _steps_changed(self):
        save_config(self.cfg)
        self.last_sig = None           # контекст изменился - не повторять ответ
        self.refresh_steps()
        self.show(self.label.cget("text"))

    def move_step(self, delta):
        if not self.steps:
            return
        self.cfg["script_step"] = min(
            len(self.steps), max(1, int(self.cfg["script_step"]) + delta))
        self.cfg["off_script"] = False         # смена пункта возвращает к скрипту
        self._steps_changed()

    def toggle_off_script(self):
        self.cfg["off_script"] = not self.cfg["off_script"]
        self._steps_changed()

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
                elif event[0] == "voice":
                    self.busy = False
                    self.start_request(event[1], "", speech=event[2])
                elif event[0] == "status":
                    self.show(event[1])
                elif event[0] == "error":
                    self.busy = False
                    self.show(event[1])
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    # --- клавиши и запрос

    def grab_shot(self, hwnd):
        """Снимок активного окна или None."""
        if IS_WIN:
            if hwnd and not is_own_window(hwnd):
                self.target_hwnd = hwnd
            try:
                return capture_window(self.target_hwnd, int(self.cfg["shot_width"]))
            except Exception:
                return None
        from PIL import Image          # режим проверки интерфейса вне Windows
        path = os.environ.get("HINT_TEST_IMAGE")
        return shrink(Image.open(path), int(self.cfg["shot_width"])) if path else None

    def on_key(self, action, hwnd):
        if action == "voice":
            return self.voice_down(hwnd)
        if action == "voice_up":
            return self.voice_up(hwnd)
        if action.endswith("_up") or self.busy or self.dialog or self.voice:
            return
        im = self.grab_shot(hwnd)
        if im is None:
            self.show("Не удалось снять окно. Сделайте окно подключения активным.")
            return
        if action == "ask":
            self.ask_question(im)
        else:
            self.start_request(im, "")

    # --- голосовая клавиша

    def voice_down(self, hwnd):
        if self.voice:                 # режим "нажал - говорят - нажал ещё раз"
            if self.voice["toggle"]:
                self.voice_finish(hwnd)
            return
        if self.busy or self.dialog is not None:
            return
        try:
            if IS_WIN:
                recorder = Recorder(str(self.cfg["audio_source"]))
            else:                      # режим проверки интерфейса вне Windows
                recorder = _TestRecorder()
        except HintError as e:
            self.show(str(e))
            return
        self.voice = {"rec": recorder, "t0": time.time(), "toggle": False}
        self.cap_lbl.configure(text="● запись", fg="#ff6b6b")
        threading.Thread(target=self._preload_voice, daemon=True).start()
        voice = self.voice
        self.root.after(int(float(self.cfg["voice_max_sec"]) * 1000),
                        lambda: self.voice is voice and self.voice_finish(None))

    def _preload_voice(self):
        try:
            self.transcriber.load()    # пока идёт запись, модель грузится в память
        except Exception:
            pass                       # об ошибке сообщит расшифровка

    def voice_up(self, hwnd):
        if not self.voice or self.voice["toggle"]:
            return
        if time.time() - self.voice["t0"] < 0.4:
            # Клавиатура отпускает медиа-клавишу сама, сразу после нажатия:
            # запись идёт до следующего нажатия.
            self.voice["toggle"] = True
            return
        self.voice_finish(hwnd)

    def voice_finish(self, hwnd):
        voice, self.voice = self.voice, None
        self.cap_lbl.configure(text="Подсказка", fg=DIM)
        seconds = time.time() - voice["t0"]
        try:
            audio = voice["rec"].stop()
        except Exception as e:
            self.show("Сбой записи звука: %s" % type(e).__name__)
            return
        im = self.grab_shot(hwnd)
        if im is None:
            self.show("Не удалось снять окно. Сделайте окно подключения активным.")
            return
        self.busy = True
        threading.Thread(target=self._voice_worker, args=(im, audio, seconds),
                         daemon=True).start()

    def _voice_worker(self, im, audio, seconds):
        started = time.time()
        try:
            if self.transcriber.model is None:
                self.events.put(("status", "Готовлю распознавание речи. В первый "
                                 "раз скачивается модель, это несколько минут."))
            parts = {name: self.transcriber.transcribe(track)
                     for name, track in audio.items()}
            logging.info("голос: запись %.1f с, расшифровка за %.1f с",
                         seconds, time.time() - started)
            speech = speech_text(parts)
            if not speech:
                self.events.put(("error", "Речь не распознана: в записи тишина. "
                                 "Держите клавишу, пока говорят."))
            else:
                self.events.put(("voice", im, speech))
        except HintError as e:
            self.events.put(("error", str(e)))
        except Exception as e:
            logging.info("голос: сбой %s", type(e).__name__)
            self.events.put(("error", "Сбой распознавания речи: %s"
                             % type(e).__name__))

    def start_request(self, im, question, speech=""):
        """im=None - вопрос без снимка: по задаче и предыдущим шагам.
        speech - расшифровка записи звука."""
        sig = signature(im) if im is not None else None
        if (im is not None and not question and not speech and self.last_answer
                and same_shot(sig, self.last_sig, self.cfg["same_shot_threshold"])):
            logging.info("повтор снимка, запрос не отправлялся")
            if self.hints:
                self.hint_pos = len(self.hints) - 1
                self._show_hint()
            return
        self.busy = True
        task, history = read_task(), list(self.history)
        step, off = self.current_step(), bool(self.cfg["off_script"])

        def make_prompt(direct):
            return build_prompt(task, history, question, step=step,
                                off_script=off, with_shot=im is not None,
                                speech=speech, direct=direct)

        threading.Thread(target=self._worker,
                         args=(im, make_prompt, sig, question, speech),
                         daemon=True).start()

    def _worker(self, im, make_prompt, sig, question, speech):
        started = time.time()
        kind = "запрос" if im is not None else "вопрос без снимка"
        try:
            png = to_png(im) if im is not None else None
            text, self.image_mode = ask(self.cfg, make_prompt, self.workdir,
                                        self.sysfile, png, self.image_mode)
            logging.info("%s: ответ за %.1f с", kind, time.time() - started)
            self.events.put(("answer", text, sig, question, speech))
        except HintError as e:
            logging.info("%s: ошибка за %.1f с", kind, time.time() - started)
            self.events.put(("error", str(e)))
        except Exception as e:
            logging.info("%s: сбой %s за %.1f с",
                         kind, type(e).__name__, time.time() - started)
            self.events.put(("error", "Сбой программы: %s" % type(e).__name__))

    def on_answer(self, text, sig, question, speech=""):
        self.busy = False
        advice, screen = parse_answer(text)
        advice = advice or text.strip()
        if sig is None:                        # вопрос без снимка
            screen = "без снимка, вопрос: " + question
        else:
            self.last_sig, self.last_answer = sig, advice
        if self.history.maxlen:
            self.history.append((short(screen or "не описан", 150),
                                 short(advice, 220)))
        if speech:
            self.add_hint("Услышал: %s\n\n%s" % (short(speech, 400), advice))
        else:
            self.add_hint(advice)

    def new_session(self):
        self.history.clear()
        self.last_sig, self.last_answer = None, ""
        self.hints, self.hint_pos = [], -1
        self.cfg["script_step"], self.cfg["off_script"] = 1, False
        save_config(self.cfg)
        self.refresh_steps()
        self.hint_lbl.configure(text="0/0")
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
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = self.root.winfo_x()
        y = self.root.winfo_y() + self.root.winfo_height() + 4
        if y + h > sh:                         # не помещается под окном
            y = self.root.winfo_y() - h - 4
        x = max(0, min(x, sw - w))
        y = max(0, min(y, sh - h))
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
        tk.Label(win, text="Вопрос. Enter - со снимком, Ctrl+Enter - без снимка, "
                           "Esc - отмена",
                 bg=BAR, fg=DIM, font=("Segoe UI", 9)).pack(anchor="w")
        entry = tk.Entry(win, width=52, font=self._font(), bg=BG, fg=FG,
                         insertbackground=FG, relief="flat")
        entry.pack(fill="x", pady=(4, 0), ipady=3)
        self._ru_hotkeys(entry)

        def submit(_e=None):
            question = entry.get().strip()
            self._close_popup()
            self.start_request(im, question)

        def submit_text_only(_e=None):
            question = entry.get().strip()
            self._close_popup()
            if question:               # без снимка нужен хотя бы вопрос
                self.start_request(None, question)
            return "break"

        entry.bind("<Control-Return>", submit_text_only)
        entry.bind("<Return>", submit)
        entry.bind("<Escape>", lambda _e: self._close_popup())
        self._place_popup(win, entry)

    def edit_task(self):
        self._editor("Задача клиента (подставляется в каждый запрос)",
                     read_task, write_task, 60, 10, "Задача сохранена.")

    def edit_script(self):
        self._editor("Скрипт встречи и материалы по клиенту (можно вставить "
                     "целиком)", read_script, write_script, 90, 26,
                     "Скрипт сохранён.")

    def _editor(self, title, read, write, width, height, saved_text):
        if self.dialog is not None:
            return
        tk = self.tk
        win = self._popup()
        tk.Label(win, text=title, bg=BAR, fg=DIM,
                 font=("Segoe UI", 9)).pack(anchor="w")
        frame = tk.Frame(win, bg=BAR)
        frame.pack(pady=4)
        scroll = tk.Scrollbar(frame)
        box = tk.Text(frame, width=width, height=height, font=self._font(),
                      bg=BG, fg=FG, insertbackground=FG, relief="flat",
                      wrap="word", undo=True, yscrollcommand=scroll.set)
        scroll.configure(command=box.yview)
        box.pack(side="left")
        scroll.pack(side="right", fill="y")
        box.insert("1.0", read())
        self._ru_hotkeys(box)

        def save(_e=None):
            write(box.get("1.0", "end"))
            self._close_popup()
            self.last_sig = None       # контекст изменился - не повторять ответ
            self.refresh_steps()
            self.show(saved_text)
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
        if self.voice:
            try:
                self.voice["rec"].stop()
            except Exception:
                pass
        if self.hook:
            self.hook.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        self.root.destroy()

    def run(self):
        try:
            self.root.mainloop()
        finally:
            shutil.rmtree(self.tmpdir, ignore_errors=True)


class _TestRecorder:
    """Вне Windows: вместо записи читает WAV из HINT_TEST_AUDIO (для проверки)."""

    def stop(self):
        import wave
        path = os.environ.get("HINT_TEST_AUDIO")
        if not path:
            return {}
        with wave.open(path, "rb") as w:
            raw = w.readframes(w.getnframes())
            return {"client": to_mono16k(raw, w.getnchannels(), w.getframerate())}


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
        codes = [parse_key(cfg[k]) for k in ("key_capture", "key_ask", "key_voice")]
        line(len(set(codes)) == 3, "config.json: клавиши %s, %s, %s, модель %s"
             % (cfg["key_capture"], cfg["key_ask"], cfg["key_voice"], cfg["model"]))
        exe = find_claude(cfg)
        line(True, "Claude Code: " + exe)
    except HintError as e:
        line(False, str(e))
        return 1
    line(PROMPT_FILE.exists(), "system_prompt.txt на месте")
    print("[ i  ] справочник knowledge.txt: %d знаков, скрипт script.txt: %d знаков"
          % (len(read_knowledge()), len(read_script())))
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("[ i  ] задана ANTHROPIC_API_KEY - программа её не использует, "
              "запросы идут по подписке")

    print("Тестовый запрос (несколько секунд)...")
    from PIL import Image, ImageDraw
    tmpdir = make_workdir("hint_check_")
    workdir = tmpdir / "shot"
    try:
        workdir.mkdir()
        im = Image.new("RGB", (600, 200), "white")
        draw = ImageDraw.Draw(im)
        draw.rectangle((0, 0, 600, 30), fill=(255, 221, 85))
        draw.text((10, 9), "1C:Enterprise - test window", fill="black")
        draw.text((20, 80), "Error: period is closed.   [ OK ]", fill="black")
        started = time.time()
        text, mode = ask(
            cfg, lambda direct: build_prompt(read_task(), [], "", direct=direct),
            workdir, tmpdir / "system.txt", to_png(im), str(cfg["image_mode"]))
        print("[ i  ] снимок передаётся: %s" % (
            "прямо в запросе (дешёвый способ)" if mode == "direct"
            else "через файл (запасной способ, дороже)"))
        advice, screen = parse_answer(text)
        line(bool(advice), "ответ получен за %.1f с" % (time.time() - started))
        print("       Подсказка: " + short(advice, 200))
        print("       Экран: " + (screen or "(модель не дала строку ЭКРАН)"))
    except HintError as e:
        line(False, str(e))
        print("       Папка снимка: %s" % workdir)
        print("       Правила: %s" % " ".join(shot_rules(workdir)))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    check_voice(cfg, line)
    print("\nВсё готово, запускайте run.bat." if ok
          else "\nЕсть проблемы - см. строки с [ !! ].")
    return 0 if ok else 1


def check_voice(cfg, line):
    """Проверка голосовой клавиши. Она необязательна: без неё всё работает."""
    try:
        import numpy as np
        import faster_whisper  # noqa: F401
        if IS_WIN:
            import pyaudiowpatch  # noqa: F401
    except ImportError as e:
        print("[ i  ] голосовая клавиша не установлена (%s). Чтобы включить: "
              "python -m pip install -r requirements-voice.txt" % e.name)
        return
    print("Распознавание речи: загрузка модели %s (в первый раз скачивается, "
          "несколько минут)..." % cfg["whisper_model"])
    try:
        started = time.time()
        tr = Transcriber(cfg)
        tr.load()
        tr.transcribe(np.zeros(16000, dtype=np.float32))
        line(True, "модель распознавания готова за %.1f с" % (time.time() - started))
    except Exception as e:
        line(False, "распознавание речи: %s" % short(e, 200))
        return
    if not IS_WIN:
        return
    try:
        print("Запись звука 3 секунды (включите любое видео или музыку)...")
        rec = Recorder(str(cfg["audio_source"]))
        time.sleep(3)
        for name, track in rec.stop().items():
            peak = float(abs(track).max()) if len(track) else 0.0
            print("       %s: записано %.1f с, уровень %.2f%s" % (
                dict(SPEAKERS)[name], len(track) / 16000, peak,
                "" if peak > 0.01 else " - тишина; если звук играл, пришлите "
                "этот вывод разработчику"))
        line(True, "запись звука работает (источник: %s)" % cfg["audio_source"])
    except Exception as e:
        line(False, "запись звука: %s" % short(e, 200))


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
        for name in ("key_capture", "key_ask", "key_voice"):
            parse_key(cfg[name])
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
