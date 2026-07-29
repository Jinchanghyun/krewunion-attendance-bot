@echo off
cd /d "%~dp0"

if not exist venv python -m venv venv
call venv\Scripts\activate.bat
python -m pip install -r requirements.txt >nul

echo.
echo Paste your Neon DATABASE_URL (from Vercel), then press Enter:
set /p DATABASE_URL=

echo.
echo [1/2] Seeding sample data into the cloud database...
python manage.py seed-demo

echo.
echo [2/2] Your admin login token (copy the whole line below):
python manage.py issue-token U3001

echo.
echo Done. Open your site and paste the token in the top box.
pause
