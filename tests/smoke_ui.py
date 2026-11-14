"""Проверка окна и сценариев без Windows и без запросов в Claude.

Запуск (нужны tkinter, Pillow и виртуальный экран):
    xvfb-run -a python tests/smoke_ui.py

Программа копируется во временную папку, ответы Claude и распознавание речи
подменяются заглушками. Проверяются: снимок, повтор снимка, вопрос со снимком
и без, пункты скрипта, "Вне скрипта", листание подсказок, смена модели,
голосовая клавиша (удержание и короткое нажатие), "Новая сессия".
Перехват клавиш, снимок окна, запись звука и скрытие от захвата здесь НЕ
проверяются: это работает только на Windows.
"""
import os
import shutil
import sys
import tempfile
import wave
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(prefix="hint_smoke_"))
for name in ("hint.py", "config.json", "task.txt", "system_prompt.txt", "knowledge.txt"):
    shutil.copy(SRC / name, tmp / name)
(tmp / "script.txt").write_text("1. Проверить банк\n2. Настроить права\n3. Обновить\n",
                                encoding="utf-8")
from PIL import Image  # noqa: E402
Image.new("RGB", (1300, 700), "white").save(tmp / "shot.png")
with wave.open(str(tmp / "a.wav"), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(b"\x00\x10" * 16000)
os.environ["HINT_TEST_IMAGE"] = str(tmp / "shot.png")
os.environ["HINT_TEST_AUDIO"] = str(tmp / "a.wav")
sys.path.insert(0, str(tmp))
import hint  # noqa: E402

prompts = []


def fake_claude(cfg, prompt, workdir, sysfile, png=None, mode="direct"):
    prompts.append((prompt, cfg["model"], bool(png)))
    return "Совет %d.\nЭКРАН: тестовый экран" % len(prompts)


hint.ask_claude = fake_claude
app = hint.App(hint.load_config())
app.transcriber.load = lambda: None
app.transcriber.transcribe = lambda audio: "не проводится документ"
root, checks = app.root, []


def check(name, ok):
    checks.append((name, bool(ok)))


def idle(then):
    root.after(100, (lambda: idle(then)) if app.busy else then)


def key(action):
    app.events.put(("key", action, 0))


def step1():
    key("capture")
    root.after(200, lambda: idle(step2))


def step2():
    check("снимок: ответ показан", app.label.cget("text") == "Совет 1.")
    check("на диске снимка нет", not list(app.workdir.iterdir()))
    check("снимок передан в запросе", prompts[0][2])
    key("capture")
    root.after(300, step3)


def step3():
    check("повтор снимка не отправляется", len(prompts) == 1 and not app.busy)
    app.move_step(1)
    check("пункт 2 выбран", app.cfg["script_step"] == 2)
    key("capture")
    root.after(200, lambda: idle(step4))


def step4():
    check("после смены пункта запрос ушёл с пунктом",
          len(prompts) == 2 and "Текущий пункт скрипта: 2. Настроить права" in prompts[1][0])
    app.toggle_off_script()
    key("capture")
    root.after(200, lambda: idle(step5))


def step5():
    check("вне скрипта", "отклонился от скрипта" in prompts[2][0])
    app.model_btn.invoke()
    key("capture")
    root.after(200, lambda: idle(step6))


def step6():
    check("смена модели пересылает снимок", len(prompts) == 4 and prompts[3][1] == "sonnet")
    key("ask")
    root.after(300, step7)


def step7():
    entry = [w for w in app.dialog.winfo_children() if w.winfo_class() == "Entry"][0]
    entry.insert(0, "а дальше?")
    entry.event_generate("<Control-Return>")
    root.after(200, lambda: idle(step8))


def step8():
    check("вопрос без снимка", "снимка нет" in prompts[4][0] and not prompts[4][2])
    app.page_hint(-1)
    check("листание назад", app.hint_lbl.cget("text") == "4/5")
    key("voice")
    root.after(700, lambda: (check("идёт запись", app.voice is not None),
                             key("voice_up"), root.after(200, lambda: idle(step9))))


def step9():
    check("голос: расшифровка в запросе", "не проводится документ" in prompts[5][0])
    check("голос: строка Услышал", app.hints[-1].startswith("Услышал: не проводится"))
    key("voice")
    root.after(100, lambda: key("voice_up"))
    root.after(500, step10)


def step10():
    check("короткое нажатие: запись продолжается", app.voice and app.voice["toggle"])
    key("voice")
    root.after(200, lambda: idle(step11))


def step11():
    check("второе нажатие останавливает запись", app.voice is None and len(prompts) == 7)
    app.new_session()
    check("новая сессия", not app.hints and not app.history and app.cfg["script_step"] == 1)
    folder = app.tmpdir
    app.quit()
    check("временная папка удалена", not folder.exists())


root.after(500, step1)
app.run()
shutil.rmtree(tmp, ignore_errors=True)
for name, ok in checks:
    print(("[ OK ] " if ok else "[ !! ] ") + name)
failed = [name for name, ok in checks if not ok]
print("\nПроверок: %d, ошибок: %d" % (len(checks), len(failed)))
sys.exit(1 if failed or len(checks) < 17 else 0)
