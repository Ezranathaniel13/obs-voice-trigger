# Voice Trigger for OBS

Switch OBS scenes automatically by voice — say a phrase, and the matching
scene switches instantly. No hotkeys, no Stream Deck, no manual clicking.

Runs as a small local web app (similar to Bitfocus Companion's browser-based
admin panel) with a modern glass-style interface. Built for live production
teams (churches, events, streamers) who want hands-free scene switching
driven off a live audio feed (e.g. a FOH/mixer tap or a microphone).

![screenshot placeholder — add your own after first run]

## Features

- **Live speech recognition** using [Vosk](https://alphacephei.com/vosk/) —
  fully offline, no cloud API, no account needed.
- **Fuzzy matching** — catches near-miss transcriptions ("kid service" still
  matches "kids service") with an adjustable sensitivity slider.
- **Low latency** — matches against Vosk's live partial transcript instead of
  waiting for a pause, so it fires mid-sentence rather than after you finish
  speaking.
- **Web-based control panel** — add/edit/remove trigger phrases, pick your
  audio input, and start/stop listening, all from a browser tab. Reachable
  from other devices on the same network too (e.g. a tablet at a tech booth).
- **Optional confirm mode** — for testing, require a manual click before it
  actually switches scenes.

## Requirements

- Python 3.9+
- [OBS Studio](https://obsproject.com/) with obs-websocket enabled
  (built into OBS 28+: **Tools → obs-websocket Settings**)
- An audio input OBS can also see — a virtual audio cable, a USB interface
  tapped off your mixer, or just your microphone for basic testing.

## Setup

1. **Clone this repo:**
   ```bash
   git clone https://github.com/YOUR-USERNAME/voice-trigger-obs.git
   cd voice-trigger-obs
   ```

2. **Install dependencies:**
   ```bash
   pip3 install vosk sounddevice obsws-python flask
   ```

3. **Download a Vosk speech model** (small English model, ~50MB):
   ```bash
   curl -L -o vosk-model-small-en-us-0.15.zip \
     https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
   unzip vosk-model-small-en-us-0.15.zip
   ```
   This creates a `vosk-model-small-en-us-0.15/` folder — leave it in the
   same folder as the script. (Other languages/model sizes are available at
   the link above if you need them — update `model_path` in the app if so.)

4. **Enable obs-websocket in OBS:** Tools → obs-websocket Settings → enable
   the server, note the port (default `4455`) and password.

## Running it

```bash
python3 obs_voice_trigger_public.py
```

This starts a local server and opens `http://localhost:8765` in your default
browser. From there:

1. Enter your OBS host/port/password under **OBS Connection**
2. Pick your audio input under **Audio Input**
3. Add your phrase → scene pairs under **Phrase → Scene Triggers**
   (defaults are generic examples — replace them with your own scene names)
4. Click **Start Listening**

Settings are saved automatically to `voice_trigger_config.json` in the
same folder, so you won't need to re-enter them next time.

To reach the control panel from another device on the same network (e.g. a
tablet at a tech booth), use this machine's local IP instead of `localhost`:

```bash
# macOS
ipconfig getifaddr en0

# Linux
hostname -I
```

Then visit `http://<that-ip>:8765` from the other device.

## Tuning detection

- **Cooldown (sec):** minimum time between repeat triggers of the same
  phrase. `0` means it can fire again immediately.
- **Match Sensitivity:** how forgiving the fuzzy matcher is. Lower = catches
  more mis-transcriptions but risks false positives; higher = stricter,
  fewer false triggers. Watch the live log (it tags each match as `exact` or
  `fuzzy XX%`) to help you tune this against your own room/mic setup.
- **Confirm mode:** while testing, enable "Ask before switching" so you can
  verify detections before they actually control OBS live.

## Security note

This runs an unauthenticated local web server bound to all network
interfaces, so anyone on the same network can open the control panel and
control your OBS instance. Fine for a closed production/home network —
don't expose it to the public internet.

## License

MIT — do whatever you want with it, no warranty. See [LICENSE](LICENSE).
