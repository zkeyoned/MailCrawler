@echo off
rem Windows 打包脚本：生成图标并打出 dist\MailCrawler
rem 用法：build.bat   （先建好 .venv 并装 requirements.txt + pyinstaller + pillow）
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
  echo 先建虚拟环境：py -3 -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt pyinstaller pillow
  exit /b 1
)

.venv\Scripts\python scripts\make_icon.py
.venv\Scripts\pyinstaller packaging\mailcrawler.spec --noconfirm
echo 完成：dist\MailCrawler\MailCrawler.exe
