#!/bin/bash
# Double-click this file in your file manager (or run it from a terminal)
# to launch Voice Trigger for OBS.
# See the "One-click launcher" section in README.md for one-time setup
# (making it executable).

cd "$(dirname "$0")"

echo "Starting Voice Trigger for OBS..."
echo "(This window will stay open while the app is running — closing it stops the app.)"
echo ""

python3 obs_voice_trigger_public.py

echo ""
echo "App stopped. You can close this window now."
