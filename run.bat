@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] Creating virtual environment (if needed)...
if not exist venv python -m venv venv
call venv\Scripts\activate.bat

echo [2/3] Installing packages...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt

echo [3/3] Starting demo server at http://127.0.0.1:8000 ...
python demo.py

pause
