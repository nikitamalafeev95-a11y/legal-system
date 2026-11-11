@echo off
rem Проверка установки: Python, Pillow, Claude Code, вход по подписке, config.json.
chcp 65001 >nul
cd /d "%~dp0"
python "%~dp0hint.py" --check
pause
