@echo off
chcp 65001 >nul
title LanBoard v1.1.0 Build (onedir)

echo ===============================
echo   LanBoard 1.1.0 打包开始
echo ===============================

REM 清理旧构建
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist LanBoard.spec del LanBoard.spec

echo [1/3] 调用 PyInstaller...

pyinstaller ^
  --onedir ^
  --noconsole ^
  --clean ^
  --name LanBoard ^
  --icon lanboard.ico ^
  --add-data "lanboard.ico;." ^
  --version-file version.txt ^
  lan_board.py

IF ERRORLEVEL 1 (
  echo.
  echo ❌ 打包失败
  pause
  exit /b 1
)

echo.
echo [2/3] 构建完成，生成目录：
echo dist\LanBoard\

echo.
echo [3/3] 启动测试 exe...
start dist\LanBoard\LanBoard.exe

echo.
echo ✅ LanBoard 1.1.0 打包成功
pause