@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" -m vault_v2.main %*
