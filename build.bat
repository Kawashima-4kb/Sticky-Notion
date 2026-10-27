@echo off
rem Sticky-Notion.exe をビルドする（.venv に requirements-dev.txt を入れておくこと）
cd /d %~dp0
.venv\Scripts\python -m PyInstaller --noconfirm --onefile --windowed --name Sticky-Notion --collect-submodules pystray run.py
