@echo off
cd /d "%~dp0"

echo Uploading project to GitHub...
echo.

if exist ".git\index.lock" del /f /q ".git\index.lock"

git init
git add -A
git commit -m "krewunion attendance bot"
git branch -M main
git remote remove origin 2>nul
git remote add origin https://github.com/Jinchanghyun/krewunion-attendance-bot.git
git push -u origin main

echo.
echo Done. Refresh the GitHub repo page to check the files.
pause
