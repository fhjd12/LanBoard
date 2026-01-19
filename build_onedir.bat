@echo off
chcp 65001 >nul

REM 切换到 bat 所在目录（非常重要）
cd /d "%~dp0"

python -m PyInstaller ^
  --name LanBoard ^
  --onedir ^
  --noconsole ^
  --icon assets\lanboard.ico ^
  --add-data "assets;assets" ^
  src\lan_board.py

pause