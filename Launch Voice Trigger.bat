@echo off
REM Double-click this file in File Explorer to launch Voice Trigger for OBS.
REM See the "One-click launcher" section in README.md for details.

cd /d "%~dp0"

echo Starting Voice Trigger for OBS...
echo (This window will stay open while the app is running — closing it stops the app.)
echo.

python obs_voice_trigger_public.py

echo.
echo App stopped. You can close this window now.
pause
