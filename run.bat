@echo off
chcp 65001 >nul
rem Запуск подсказчика без окна консоли.
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Установите Python 3.10+ с python.org и отметьте "Add python.exe to PATH".
    pause
    exit /b 1
)
start "" pythonw "%~dp0hint.py"
