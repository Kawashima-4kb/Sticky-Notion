@echo off
rem NotionTask.exe をビルドする（.venv に requirements-dev.txt を入れておくこと）
cd /d %~dp0
.venv\Scripts\python -m PyInstaller --noconfirm --onefile --windowed --name NotionTask --collect-submodules pystray run.py
