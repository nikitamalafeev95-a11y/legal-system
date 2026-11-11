"""Проверка логики, не зависящей от Windows: python -m unittest discover tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hint  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402


def screen(text="", cursor=False):
    im = Image.new("RGB", (1300, 800), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 1300, 40), fill=(255, 221, 85))
    if text:
        d.rectangle((400, 300, 900, 420), outline="red", width=3)
        d.text((420, 340), text, fill="black")
    if cursor:
        d.rectangle((200, 200, 201, 216), fill="black")
    return im


class CoreTest(unittest.TestCase):
    def test_parse_answer_splits_service_line(self):
        advice, scr = hint.parse_answer(
            "Причина: период закрыт.\n\nНажмите OK.\nЭКРАН: ошибка проведения")
        self.assertEqual(advice, "Причина: период закрыт.\nНажмите OK.")
        self.assertEqual(scr, "ошибка проведения")

    def test_parse_answer_without_service_line(self):
        self.assertEqual(hint.parse_answer("Нажмите OK."), ("Нажмите OK.", ""))

    def test_prompt_has_task_history_question_and_one_shot(self):
        p = hint.build_prompt("Клиент: Ромашка", [("список", "Откройте Банк")],
                              "где выписки?")
        self.assertIn("Клиент: Ромашка", p)
        self.assertIn("1. Экран: список. Совет: Откройте Банк", p)
        self.assertIn("где выписки?", p)
        self.assertEqual(p.count(hint.SHOT_NAME), 1)

    def test_short_is_one_line(self):
        s = hint.short("а\nб   в " * 100, 50)
        self.assertLessEqual(len(s), 50)
        self.assertNotIn("\n", s)

    def test_same_shot(self):
        base = hint.signature(screen())
        self.assertTrue(hint.same_shot(base, hint.signature(screen()), 0.05))
        self.assertTrue(hint.same_shot(  # мигающий курсор - тот же экран
            base, hint.signature(screen(cursor=True)), 0.05))
        self.assertFalse(hint.same_shot(  # появилось окно ошибки - другой экран
            base, hint.signature(screen("Oshibka: period zakryt")), 0.05))
        self.assertFalse(hint.same_shot(None, base, 0.05))

    def test_shrink(self):
        self.assertEqual(hint.shrink(Image.new("RGB", (2600, 1000)), 1300).size,
                         (1300, 500))
        self.assertEqual(hint.shrink(Image.new("RGB", (800, 600)), 1300).size,
                         (800, 600))

    def test_keys(self):
        self.assertEqual(hint.parse_key("media_stop"), 0xB2)
        self.assertEqual(hint.parse_key("MEDIA_NEXT"), 0xB0)
        self.assertEqual(hint.parse_key("0x7B"), 0x7B)
        self.assertEqual(hint.parse_key("f9"), 0x78)
        with self.assertRaises(hint.HintError):
            hint.parse_key("нет такой")

    def test_command_allows_only_reading_the_shot(self):
        cmd = hint.build_command(dict(hint.DEFAULTS), "claude", "/tmp/hint_x",
                                 "/tmp/sys.txt")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read")
        i = cmd.index("--allowedTools")
        self.assertEqual(cmd[i + 1:i + 3],
                         ["Read(./shot.png)", "Read(//tmp/hint_x/shot.png)"])
        self.assertEqual(cmd[i + 3], "--permission-mode")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
        self.assertNotIn("--bare", cmd)

    def test_windows_rules_cover_only_the_shot(self):
        from pathlib import PureWindowsPath
        rules = hint.shot_rules(PureWindowsPath(r"C:\Users\Иван\Temp\hint_x"))
        self.assertEqual(rules, ["Read(./shot.png)",
                                 "Read(//C:/Users/Иван/Temp/hint_x/shot.png)",
                                 "Read(//c/Users/Иван/Temp/hint_x/shot.png)"])

    def test_system_part_carries_script(self):
        self.assertEqual(hint.build_system("Инструкция\n", ""), "Инструкция\n")
        text = hint.build_system("Инструкция", "1. Проверить банк")
        self.assertTrue(text.startswith("Инструкция\n\nСкрипт встречи"))
        self.assertIn("1. Проверить банк", text)

    def test_model_cycle(self):
        cfg = {"model": "haiku", "models": ["haiku", "sonnet", "opus"]}
        seen = []
        for _ in range(3):
            cfg["model"] = hint.next_model(cfg)
            seen.append(cfg["model"])
        self.assertEqual(seen, ["sonnet", "opus", "haiku"])
        self.assertEqual(hint.next_model({"model": "x", "models": ["a"]}), "a")
        self.assertEqual(hint.next_model({"model": "x", "models": []}), "x")

    def test_child_env_has_no_api_key(self):
        import os
        os.environ["ANTHROPIC_API_KEY"] = "x"
        try:
            self.assertNotIn("ANTHROPIC_API_KEY", hint.child_env())
        finally:
            del os.environ["ANTHROPIC_API_KEY"]


if __name__ == "__main__":
    unittest.main()
