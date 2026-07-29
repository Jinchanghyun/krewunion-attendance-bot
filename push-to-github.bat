@echo off
cd /d "%~dp0"

echo Uploading project to GitHub...
echo.

git init
git add .
git commit -m "krewunion attendance bot"
git branch -M main
git remote remove origin 2>nul
git remote add origin https://github.com/Jinchanghyun/krewunion-attendance-bot.git
git push -u origin main

echo.
echo Done. Refresh the GitHub repo page to check the files.
pause
