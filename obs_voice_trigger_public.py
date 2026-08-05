#!/usr/bin/env python3
"""
Voice Trigger for OBS (Web GUI)
-------------------------------------------------------------------
Runs a small local web server (like Bitfocus Companion's admin panel)
with a modern glass / rounded-corner interface. Open it in any browser
on this machine, or from another device on the same network.

SETUP (one-time, do this in Terminal):

1. Install dependencies:
   pip3 install vosk sounddevice obsws-python flask

2. Download a Vosk model (small English model, ~50MB):
   curl -L -o vosk-model-small-en-us-0.15.zip \
     https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
   unzip vosk-model-small-en-us-0.15.zip

   Put the extracted "vosk-model-small-en-us-0.15" folder in the SAME
   folder as this script.

3. In OBS: Tools > obs-websocket Settings — enable server, note port + password.

RUNNING IT:

   python3 obs_voice_trigger_public.py

This prints a URL (default http://localhost:8765) and opens it in your
default browser automatically. Leave the Terminal window running in
the background — closing it stops the server.

To reach it from another device on the same Wi-Fi (e.g. a tablet at
a tech booth), use this machine's local IP instead of localhost, e.g.
http://192.168.1.23:8765 (find your IP with: ipconfig getifaddr en0
on Mac, or `hostname -I` on Linux).
"""

import array
import json
import math
import os
import queue
import threading
import time
import webbrowser
from difflib import SequenceMatcher

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_trigger_config.json")
PORT = 8765

DEFAULT_CONFIG = {
    "obs_host": "",
    "obs_port": "",
    "obs_password": "",
    "model_path": "vosk-model-small-en-us-0.15",
    "cooldown": 0.0,
    "fuzzy_threshold": 0.78,
    "confirm": False,
    "device_index": None,
    "triggers": [],
}


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update(cfg)
            return merged
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"Could not save config: {e}")


def compute_audio_level(data_bytes, min_db=-50.0):
    """
    Converts a raw int16 PCM chunk into a 0..1 level for the UI meter, using
    dBFS (decibels relative to full scale) rather than a plain linear ratio.
    Linear scaling badly underrepresents normal speaking volume — it takes a
    very high amplitude to register as a large fraction of full scale, so a
    person talking at a normal-to-loud volume looks almost silent on the bar.
    dBFS is how real audio meters work and matches how loudness is actually
    perceived. min_db sets the "silence floor" — anything quieter than that
    reads as 0.
    """
    try:
        samples = array.array('h')
        samples.frombytes(data_bytes)
        if not samples:
            return 0.0
        s_sum = sum(s * s for s in samples)
        rms = (s_sum / len(samples)) ** 0.5
        if rms <= 1:
            return 0.0
        dbfs = 20 * math.log10(rms / 32768.0)
        level = (dbfs - min_db) / (0 - min_db)
        return max(0.0, min(1.0, level))
    except Exception:
        return 0.0


def fuzzy_best_ratio(phrase, text):
    """
    Slide a window the same rough length as `phrase` across `text` and return
    the best SequenceMatcher ratio found. This catches near-misses like
    "kid service" vs "kids service", or one word getting dropped/garbled,
    which a plain substring check would miss entirely.
    """
    phrase = phrase.strip()
    text = text.strip()
    if not phrase or not text:
        return 0.0
    if phrase in text:
        return 1.0

    phrase_words = phrase.split()
    text_words = text.split()
    n = len(phrase_words)
    best = 0.0

    for size in (max(1, n - 1), n, n + 1):
        if size > len(text_words):
            continue
        for i in range(0, len(text_words) - size + 1):
            window = " ".join(text_words[i:i + size])
            ratio = SequenceMatcher(None, phrase, window).ratio()
            if ratio > best:
                best = ratio
    return best


# =====================================================================
# Broker — pushes live events (log lines, status, confirm prompts) to
# any number of connected browser tabs via Server-Sent Events.
# =====================================================================
class Broker:
    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def publish(self, obj):
        with self.lock:
            for q in self.clients:
                q.put(obj)


broker = Broker()


# =====================================================================
# Core service: same Vosk + obs-websocket logic as before, just
# publishing events to the broker instead of calling into a webview.
# =====================================================================
class VoiceTriggerService:
    def __init__(self):
        self.cfg = load_config()
        self.stop_event = threading.Event()
        self.worker_thread = None
        self.obs_client = None
        self.running = False
        self.pending_confirm = None
        self.generation = 0  # bumped on every start/stop so stale threads can't keep running
        self.revert_timer = None  # pending "switch back to DEFAULT" timer, if any
        self.preview_thread = None
        self.preview_stop_event = threading.Event()
        self.preview_generation = 0  # separate from the main listener's generation

    def get_config(self):
        return self.cfg

    def list_devices(self):
        try:
            import sounddevice as sd
        except Exception:
            return []
        devices = sd.query_devices()
        result = []
        for i, d in enumerate(devices):
            if d.get("max_input_channels", 0) > 0:
                result.append({"index": i, "label": f"[{i}] {d['name']} (in:{d['max_input_channels']})"})
        return result

    def save_settings(self, payload):
        if payload:
            self.cfg.update(payload)
            save_config(self.cfg)
        return True

    def list_obs_scenes(self, host=None, port=None, password=None):
        """
        Connects briefly to OBS to fetch the current list of scene names,
        used to validate trigger scene names before saving. Returns
        {"scenes": [...]} on success or {"error": "..."} if it can't connect
        (e.g. OBS isn't running yet) — callers should treat that as
        "couldn't verify" rather than "invalid", since OBS may just not be
        open right now.
        """
        try:
            import obsws_python as obs
        except Exception as e:
            return {"error": f"obsws_python not installed: {e}"}

        host = host or self.cfg.get("obs_host") or "localhost"
        port = port or self.cfg.get("obs_port") or 4455
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 4455
        password = password if password is not None else self.cfg.get("obs_password", "")

        try:
            client = obs.ReqClient(host=host, port=port, password=password, timeout=3)
            resp = client.get_scene_list()
            names = [s["sceneName"] for s in resp.scenes]
            try:
                client.disconnect()
            except Exception:
                pass
            return {"scenes": names}
        except Exception as e:
            return {"error": str(e)}

    def start_preview(self, device_index):
        """
        A lightweight mic-level-only stream, independent of the full
        listener — lets the meter work the moment a device is picked,
        without loading Vosk or connecting to OBS. Refuses to run
        alongside the real listener (would fight over the same device).
        """
        if self.running:
            return {"ok": False, "reason": "listener already running"}
        if device_index is None:
            return {"ok": False, "reason": "no device selected"}
        try:
            device_index = int(device_index)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid device"}

        self.stop_preview()
        self.preview_stop_event.clear()
        self.preview_generation += 1
        my_gen = self.preview_generation
        t = threading.Thread(target=self._preview_worker, args=(device_index, my_gen), daemon=True)
        self.preview_thread = t
        t.start()
        return {"ok": True}

    def stop_preview(self):
        self.preview_stop_event.set()
        self.preview_generation += 1
        self.preview_thread = None

    def _preview_worker(self, device_index, my_gen):
        try:
            import sounddevice as sd
        except Exception:
            return

        def cb(indata, frames, time_info, status):
            if self.preview_generation != my_gen or self.preview_stop_event.is_set():
                return
            level = compute_audio_level(bytes(indata))
            broker.publish({"type": "level", "value": round(level, 3)})

        try:
            with sd.RawInputStream(samplerate=16000, blocksize=1600, device=device_index,
                                    dtype="int16", channels=1, callback=cb):
                while self.preview_generation == my_gen and not self.preview_stop_event.is_set():
                    time.sleep(0.1)
        except Exception:
            pass  # device might be mid-switch or briefly unavailable — fail quietly, it's just a preview

    def start(self, payload):
        if self.running:
            return False
        self.stop_preview()  # release the mic before the real listener opens its own stream
        self.cfg.update(payload or {})
        save_config(self.cfg)

        triggers = [
            (t["phrase"].lower(), t["scene"], float(t.get("revert_seconds") or 0))
            for t in self.cfg.get("triggers", [])
        ]
        device_index = self.cfg.get("device_index")
        try:
            cooldown = float(self.cfg.get("cooldown", 0) or 0)
        except (TypeError, ValueError):
            cooldown = 0.0
        try:
            fuzzy_threshold = float(self.cfg.get("fuzzy_threshold", 0.78) or 0.78)
        except (TypeError, ValueError):
            fuzzy_threshold = 0.78
        confirm_mode = bool(self.cfg.get("confirm", False))

        if device_index is None or not triggers:
            self._log("Missing device or triggers — cannot start.")
            broker.publish({"type": "status", "text": "Stopped", "active": False})
            return False

        self.stop_event.clear()
        self.running = True
        self.generation += 1
        my_generation = self.generation
        self.worker_thread = threading.Thread(
            target=self._worker,
            args=(device_index, triggers, cooldown, confirm_mode, fuzzy_threshold, my_generation),
            daemon=True,
        )
        self.worker_thread.start()
        return True

    def stop(self):
        self.stop_event.set()
        self.running = False
        self.generation += 1  # invalidates any in-flight worker thread immediately, even a stale one
        self._cancel_revert()
        broker.publish({"type": "status", "text": "Stopped", "active": False})
        return True

    def _cancel_revert(self):
        if self.revert_timer:
            try:
                self.revert_timer.cancel()
            except Exception:
                pass
            self.revert_timer = None

    def _schedule_revert(self, seconds, my_generation):
        """
        After a scene switch, if that trigger has a revert time set, schedule
        switching back to a scene literally named "DEFAULT" once it elapses.
        Any previously pending revert is cancelled first — only the most
        recent switch's timer should ever be live.
        """
        self._cancel_revert()
        try:
            seconds = float(seconds or 0)
        except (TypeError, ValueError):
            seconds = 0
        if seconds <= 0:
            return

        def do_revert():
            self.revert_timer = None
            if self.generation != my_generation or self.stop_event.is_set():
                return  # stopped or a newer session started — don't act
            if self.obs_client:
                try:
                    self.obs_client.set_current_program_scene("DEFAULT")
                    self._log(f"-> Auto-reverted to scene 'DEFAULT' after {seconds:g}s.")
                except Exception as e:
                    self._log(f"-> Failed to auto-revert to 'DEFAULT': {e}")

        self.revert_timer = threading.Timer(seconds, do_revert)
        self.revert_timer.daemon = True
        self.revert_timer.start()
        self._log(f"-> Will auto-revert to 'DEFAULT' in {seconds:g}s.")

    def confirm_switch(self):
        if self.pending_confirm and self.obs_client:
            scene, revert_seconds = self.pending_confirm
            self.pending_confirm = None
            try:
                self.obs_client.set_current_program_scene(scene)
                self._log(f"-> Switched OBS to scene '{scene}'.")
                self._schedule_revert(revert_seconds, self.generation)
            except Exception as e:
                self._log(f"-> Failed to switch scene: {e}")
        broker.publish({"type": "hideConfirm"})
        return True

    def skip_confirm(self):
        self.pending_confirm = None
        broker.publish({"type": "hideConfirm"})
        return True

    def _log(self, msg):
        broker.publish({"type": "log", "msg": msg})

    def _worker(self, device_index, triggers, cooldown, confirm_mode, fuzzy_threshold=0.78, my_generation=0):
        try:
            import sounddevice as sd
            from vosk import Model, KaldiRecognizer
            import obsws_python as obs
        except ImportError as e:
            self._log(f"Missing dependency: {e}. Run: pip3 install vosk sounddevice obsws-python")
            self.running = False
            broker.publish({"type": "status", "text": "Stopped", "active": False})
            return

        self._log(f"Loading Vosk model from '{self.cfg['model_path']}'...")
        try:
            model = Model(self.cfg["model_path"])
        except Exception as e:
            self._log(f"Could not load Vosk model: {e}")
            self.running = False
            broker.publish({"type": "status", "text": "Stopped", "active": False})
            return

        samplerate = 16000
        rec = KaldiRecognizer(model, samplerate)

        obs_host = self.cfg.get("obs_host") or "localhost"
        try:
            obs_port = int(self.cfg.get("obs_port") or 4455)
        except (TypeError, ValueError):
            obs_port = 4455

        self._log(f"Connecting to OBS at {obs_host}:{obs_port}...")
        try:
            client = obs.ReqClient(host=obs_host, port=obs_port,
                                    password=self.cfg["obs_password"], timeout=5)
            self.obs_client = client
            self._log("Connected to OBS.")
        except Exception as e:
            self._log(f"Could not connect to OBS: {e}")
            self.running = False
            broker.publish({"type": "status", "text": "Stopped", "active": False})
            return

        compiled = [(p, s, r) for p, s, r in triggers]  # p is already lowercased phrase text
        last_trigger_time = 0.0
        last_matched_scene = None  # guards against re-firing every ~100ms while a phrase sits in a growing partial
        audio_q = queue.Queue()

        def audio_callback(indata, frames, time_info, status):
            data = bytes(indata)
            audio_q.put(data)
            # Live mic level for the UI meter (blocksize=1600 @ 16kHz means
            # this fires ~10x/sec on its own, so no extra throttling needed).
            level = compute_audio_level(data)
            broker.publish({"type": "level", "value": round(level, 3)})

        def try_match(text, is_final):
            nonlocal last_trigger_time, last_matched_scene
            text_l = text.lower()
            best_scene = None
            best_phrase = None
            best_revert = 0.0
            best_ratio = 0.0
            for phrase, scene, revert_seconds in compiled:
                ratio = fuzzy_best_ratio(phrase, text_l)
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_scene = scene
                    best_phrase = phrase
                    best_revert = revert_seconds
            if best_scene is None or best_ratio < fuzzy_threshold:
                return
            if best_scene == last_matched_scene:
                return  # already fired for this in-progress utterance
            now = time.time()
            if now - last_trigger_time < cooldown:
                return
            if self.generation != my_generation or self.stop_event.is_set():
                return  # stop was pressed (or a newer session started) — don't act on stale audio
            tag = "final" if is_final else "live"
            pct = round(best_ratio * 100)
            exactness = "exact" if best_ratio >= 0.999 else f"fuzzy {pct}%"
            self._log(f"-> MATCH ({tag}, {exactness}): '{best_phrase}' -> scene '{best_scene}'")
            if confirm_mode:
                self.pending_confirm = (best_scene, best_revert)
                broker.publish({"type": "confirm", "scene": best_scene})
            else:
                try:
                    client.set_current_program_scene(best_scene)
                    self._log(f"-> Switched OBS to scene '{best_scene}'.")
                    self._schedule_revert(best_revert, my_generation)
                except Exception as e:
                    self._log(f"-> Failed to switch scene: {e}")
            last_trigger_time = now
            last_matched_scene = best_scene

        broker.publish({"type": "status", "text": "Listening\u2026", "active": True})
        self._log("Listening started. Speak your trigger phrases.")

        # Small blocksize = audio reaches Vosk in ~90ms slices instead of 500ms,
        # and matching against the live partial (instead of waiting for Vosk to
        # finalize on a pause) is what actually kills most of the lag.
        try:
            with sd.RawInputStream(samplerate=samplerate, blocksize=1600, device=device_index,
                                    dtype="int16", channels=1, callback=audio_callback):
                while self.generation == my_generation and not self.stop_event.is_set():
                    try:
                        data = audio_q.get(timeout=0.15)
                    except queue.Empty:
                        continue
                    if self.generation != my_generation or self.stop_event.is_set():
                        break
                    if rec.AcceptWaveform(data):
                        result = json.loads(rec.Result())
                        text = result.get("text", "")
                        if text:
                            self._log(f"[heard] {text}")
                            try_match(text, is_final=True)
                        last_matched_scene = None  # utterance boundary reached, allow next phrase to fire again
                    else:
                        partial = json.loads(rec.PartialResult()).get("partial", "")
                        if not partial:
                            last_matched_scene = None  # silence gap, treat as a fresh utterance
                        else:
                            try_match(partial, is_final=False)
        except Exception as e:
            self._log(f"Audio stream error: {e}")

        if self.generation == my_generation:
            self.running = False
            broker.publish({"type": "status", "text": "Stopped", "active": False})


# =====================================================================
# Front end (Apple-glass style): dark, blurred, rounded, minimal.
# Same look as before, wired up to fetch() + Server-Sent Events
# instead of a native webview bridge.
# =====================================================================
HTML_TEMPLATE = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Voice Trigger for OBS</title>
<style>
  :root {
    --bg-0: #060606;
    --glass: rgba(255,255,255,0.055);
    --glass-strong: rgba(255,255,255,0.09);
    --border: rgba(255,255,255,0.11);
    --border-soft: rgba(255,255,255,0.07);
    --fg: #f5f5f7;
    --sub: #9a9a9f;
    --muted: #6a6a6e;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", Helvetica, Arial, sans-serif;
    background:
      radial-gradient(1200px 600px at 15% -10%, rgba(255,255,255,0.05), transparent 60%),
      radial-gradient(1000px 500px at 110% 10%, rgba(255,255,255,0.035), transparent 55%),
      var(--bg-0);
    color: var(--fg);
    -webkit-font-smoothing: antialiased;
    overflow-x: hidden;
  }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 34px 30px 44px; }

  .header { display: flex; align-items: center; justify-content: space-between; gap: 24px; margin-bottom: 26px; }
  .titles h1 { margin: 0; font-size: 30px; font-weight: 700; letter-spacing: 0.3px; }
  .titles .sub { margin: 5px 0 0; font-size: 14.5px; color: var(--sub); font-weight: 500; }
  .titles .tag {
    margin: 9px 0 0; font-size: 11px; letter-spacing: 1.6px; color: var(--muted);
    font-weight: 700; text-transform: uppercase;
  }
  .logo-icon {
    width: 88px; height: 88px; border-radius: 50%; flex-shrink: 0;
    border: 1px solid var(--border); background: rgba(255,255,255,0.045);
    display: flex; align-items: center; justify-content: center;
  }
  .logo-icon svg { width: 40px; height: 40px; }
  .divider {
    height: 1px; background: linear-gradient(90deg, transparent, var(--border) 20%, var(--border) 80%, transparent);
    margin: 4px 0 22px;
  }

  .section { margin-bottom: 20px; }
  .section-label {
    font-size: 10.5px; font-weight: 700; letter-spacing: 1.3px; text-transform: uppercase;
    color: var(--muted); margin: 0 0 8px 4px;
  }

  /* ---- Dashboard grid: side-by-side on wide windows, stacks on narrow ones ---- */
  .dashboard {
    display: flex;
    flex-wrap: wrap;
    gap: 24px;
    align-items: flex-start;
  }
  .main-col { flex: 3 1 640px; min-width: 0; display: flex; flex-direction: column; gap: 20px; }
  .main-col > .row-2col, .main-col > .section { margin-bottom: 0; }
  .side-col { flex: 2 1 320px; min-width: 0; }
  .row-2col {
    display: flex;
    flex-wrap: wrap;
    align-items: stretch;
    gap: 20px;
  }
  .row-2col > .section {
    flex: 1 1 260px; min-width: 0; margin-bottom: 0;
    display: flex; flex-direction: column;
  }
  .row-2col > .section > .glass:last-of-type { flex: 1; }
  @media (min-width: 1020px) {
    .side-col { position: sticky; top: 24px; }
  }
  .glass {
    background: var(--glass);
    border: 1px solid var(--border);
    border-radius: 18px;
    backdrop-filter: blur(24px) saturate(160%);
    -webkit-backdrop-filter: blur(24px) saturate(160%);
    box-shadow: 0 1px 0 rgba(255,255,255,0.04) inset, 0 10px 30px rgba(0,0,0,0.35);
    padding: 16px 18px;
  }

  .field-label {
    font-size: 10.5px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
    color: var(--muted); margin-bottom: 6px; display: block;
  }
  input[type=text], input[type=password], input[type=number], select {
    width: 100%; background: rgba(255,255,255,0.045); color: var(--fg);
    border: 1px solid var(--border); border-radius: 12px;
    padding: 10px 13px; font-size: 14px; outline: none;
    transition: border-color .15s ease, box-shadow .15s ease, background .15s ease;
    font-family: inherit;
    appearance: none; -webkit-appearance: none;
  }
  input:focus, select:focus {
    border-color: rgba(255,255,255,0.4);
    box-shadow: 0 0 0 4px rgba(255,255,255,0.07);
    background: rgba(255,255,255,0.065);
  }
  .row { display: flex; gap: 14px; }
  .row > div { flex: 1; }
  .row .grow { flex: 2; }

  select {
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6'><path d='M0 0L5 6L10 0Z' fill='%239a9a9f'/></svg>");
    background-repeat: no-repeat; background-position: right 14px center; padding-right: 34px;
  }

  .btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    border-radius: 12px; border: 1px solid var(--border);
    background: rgba(255,255,255,0.055); color: var(--fg);
    font-size: 13.5px; font-weight: 600; padding: 9px 16px; cursor: pointer;
    transition: all .15s ease; user-select: none;
  }
  .btn:hover { background: rgba(255,255,255,0.1); transform: translateY(-1px); }
  .btn:active { transform: translateY(0); }
  .btn:disabled { opacity: 0.35; cursor: default; transform: none; }
  .btn-primary { background: #ffffff; color: #0a0a0a; border-color: #ffffff; font-weight: 700; padding: 11px 22px; font-size: 14.5px; }
  .btn-primary:hover { background: #e7e7e9; }
  .btn-ghost { background: transparent; border-color: var(--border-soft); }
  .btn-icon { padding: 7px 9px; border-radius: 10px; }
  .btn-row { display: flex; gap: 10px; margin-top: 12px; }

  #startBtn { background: #4ade80; color: #052e13; border-color: #4ade80; }
  #startBtn:hover:not(:disabled) { background: #3ecf72; border-color: #3ecf72; }
  #startBtn:disabled { background: rgba(74,222,128,0.3); color: rgba(5,46,19,0.55); border-color: rgba(74,222,128,0.3); }

  #stopBtn { border-color: rgba(255,107,107,0.45); }
  #stopBtn:hover:not(:disabled) { background: rgba(255,107,107,0.12); border-color: rgba(255,107,107,0.75); color: #ff6b6b; }

  .switch-row { display: flex; align-items: center; gap: 10px; cursor: pointer; user-select: none; }
  .switch { width: 42px; height: 25px; border-radius: 999px; background: rgba(255,255,255,0.13);
            position: relative; transition: background .2s ease; flex-shrink: 0; }
  .switch .knob { width: 21px; height: 21px; border-radius: 50%; background: #fff; position: absolute;
                  top: 2px; left: 2px; transition: transform .2s ease; box-shadow: 0 1px 3px rgba(0,0,0,.5); }
  .switch.active { background: #ffffff; }
  .switch.active .knob { transform: translateX(17px); background: #0a0a0a; }
  .switch-label { font-size: 13.5px; color: var(--sub); }

  input[type=range] {
    -webkit-appearance: none; appearance: none; width: 100%; height: 4px;
    border-radius: 999px; background: rgba(255,255,255,0.12); outline: none; margin: 6px 0 0;
  }
  input[type=range]::-webkit-slider-thumb {
    -webkit-appearance: none; width: 18px; height: 18px; border-radius: 50%;
    background: #ffffff; cursor: pointer; box-shadow: 0 1px 4px rgba(0,0,0,0.5);
    border: 2px solid #0a0a0a;
  }

  #triggerList { display: flex; flex-direction: column; gap: 8px; max-height: 260px; overflow-y: auto; }
  .trigger-row {
    display: flex; align-items: center; justify-content: space-between;
    background: rgba(255,255,255,0.035); border: 1px solid var(--border-soft);
    border-radius: 13px; padding: 11px 14px; transition: background .15s ease;
  }
  .trigger-row:hover { background: rgba(255,255,255,0.06); }
  .trigger-text { display: flex; align-items: center; gap: 10px; font-size: 14px; min-width: 0; }
  .trigger-phrase { font-weight: 600; }
  .arrow { color: var(--muted); }
  .trigger-scene {
    font-family: "SF Mono", Menlo, monospace; font-size: 12.5px; color: var(--fg);
    background: rgba(255,255,255,0.08); padding: 3px 9px; border-radius: 7px;
    letter-spacing: 0.3px;
  }
  .row-actions { display: flex; gap: 6px; flex-shrink: 0; }
  .icon-btn {
    width: 30px; height: 30px; border-radius: 9px; display: flex; align-items: center; justify-content: center;
    background: transparent; border: 1px solid transparent; cursor: pointer; transition: all .15s ease;
    color: var(--sub);
  }
  .icon-btn:hover { background: rgba(255,255,255,0.09); color: var(--fg); }
  .icon-btn.active { color: var(--fg); background: rgba(255,255,255,0.12); }
  .icon-btn-danger { border-color: rgba(255,107,107,0.4); }
  .icon-btn-danger:hover { background: rgba(255,107,107,0.12); border-color: rgba(255,107,107,0.7); color: #ff6b6b; }
  .empty-hint { color: var(--muted); font-size: 13px; text-align: center; padding: 18px 0; }

  .controls { display: flex; align-items: center; gap: 12px; margin: 4px 0 20px; flex-wrap: wrap; }
  .status-pill {
    display: flex; align-items: center; gap: 9px; padding: 8px 15px; border-radius: 999px;
    background: rgba(255,255,255,0.05); border: 1px solid var(--border-soft); font-size: 13px; font-weight: 600;
  }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); transition: all .2s ease; }
  .dot.active { background: #ffffff; box-shadow: 0 0 0 0 rgba(255,255,255,0.5); animation: pulse 1.6s infinite; }
  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0 rgba(255,255,255,0.35); }
    70%  { box-shadow: 0 0 0 8px rgba(255,255,255,0); }
    100% { box-shadow: 0 0 0 0 rgba(255,255,255,0); }
  }

  #connDot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); display: inline-block; margin-right: 6px; }
  #connDot.up { background: #4ade80; }

  #confirmBanner {
    display: none; align-items: center; justify-content: space-between; gap: 12px;
    background: rgba(255,255,255,0.09); border: 1px solid rgba(255,255,255,0.2);
    border-radius: 14px; padding: 12px 16px; margin-bottom: 16px;
  }
  #confirmBanner .msg { font-size: 14px; font-weight: 600; }

  #logGlass { display: flex; flex-direction: column; }
  #log {
    flex: 1; min-height: 0; overflow-y: auto; font-family: "SF Mono", Menlo, monospace; font-size: 12px;
    color: #cfcfd2; line-height: 1.6; white-space: pre-wrap; word-break: break-word;
    display: flex; flex-direction: column;
  }
  #log .match { color: #ffffff; font-weight: 700; }
  #log .heard { color: #8a8a90; }

  footer { text-align: center; font-size: 10.5px; letter-spacing: 1.2px; color: var(--muted);
           text-transform: uppercase; margin-top: 6px; font-weight: 700; }
  footer a { color: var(--muted); }

  #modalOverlay {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.55);
    backdrop-filter: blur(6px); align-items: center; justify-content: center; z-index: 50;
  }
  #modalCard {
    width: 360px; background: rgba(30,30,32,0.85); border: 1px solid rgba(255,255,255,0.14);
    border-radius: 20px; padding: 22px; backdrop-filter: blur(30px) saturate(180%);
    box-shadow: 0 20px 60px rgba(0,0,0,0.5);
    transform: scale(0.96); opacity: 0; transition: all .18s ease;
  }
  #modalOverlay.show #modalCard { transform: scale(1); opacity: 1; }
  #modalCard h3 { margin: 0 0 16px; font-size: 16px; font-weight: 700; }
  #modalCard .field { margin-bottom: 14px; }
  #modalCard .btn-row { justify-content: flex-end; }

  ::-webkit-scrollbar { width: 8px; }
  ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 8px; }
  ::-webkit-scrollbar-track { background: transparent; }
</style>
</head>
<body>
<div class="wrap">

  <div class="header">
    <div class="titles">
      <h1>Voice Trigger</h1>
      <p class="sub">Speak a phrase, switch your OBS scene &mdash; instantly.</p>
      <p class="tag"><span id="connDot"></span>Voice-Triggered Scene Switcher for OBS</p>
    </div>
    <div class="logo-icon">
      <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Z" stroke="#f5f5f7" stroke-width="1.6"/>
        <path d="M19 11v1a7 7 0 0 1-14 0v-1M12 19v3" stroke="#f5f5f7" stroke-width="1.6" stroke-linecap="round"/>
      </svg>
    </div>
  </div>
  <div class="divider"></div>

  <div class="controls">
    <button class="btn btn-primary" id="startBtn" onclick="startListening()">&#9654;&nbsp; Start Listening</button>
    <button class="btn" id="stopBtn" onclick="stopListening()" disabled>&#9632;&nbsp; Stop</button>
    <div class="status-pill"><div class="dot" id="statusDot"></div><span id="statusText">Stopped</span></div>
  </div>

  <div id="restartHint" style="display:none; align-items:center; gap:10px;
       background:rgba(250,204,21,0.08); border:1px solid rgba(250,204,21,0.45);
       border-radius:12px; padding:10px 16px; margin:-8px 0 16px; font-size:13px;">
    <span style="color:#facc15; font-size:16px; line-height:1;">&#9888;</span>
    <span>Settings changed while listening &mdash; click <strong>Stop</strong> then <strong>Start Listening</strong> again to apply them.</span>
  </div>

  <div id="confirmBanner">
    <span class="msg" id="confirmMsg"></span>
    <div style="display:flex; gap:8px;">
      <button class="btn btn-ghost" onclick="skipConfirm()">Skip</button>
      <button class="btn btn-primary" onclick="confirmSwitch()">Switch now</button>
    </div>
  </div>

  <div class="dashboard">
    <div class="main-col">

      <div class="row-2col">
        <div class="section" id="obsConnSection">
          <div class="section-label">OBS Connection</div>
          <div class="glass" id="obsConnGlass" style="display:flex; flex-direction:column;">
            <div class="row">
              <div>
                <label class="field-label">Host</label>
                <input type="text" id="obsHost" placeholder="localhost">
              </div>
              <div>
                <label class="field-label">Port</label>
                <input type="text" id="obsPort" placeholder="4455">
              </div>
            </div>
            <div style="margin-top:12px;">
              <label class="field-label">Password</label>
              <input type="password" id="obsPassword" placeholder="obs-websocket password">
            </div>
            <div style="margin-top:16px; font-size:11.5px; color:var(--muted); line-height:1.6;">
              Enable obs-websocket in OBS: <strong style="color:var(--sub);">Tools &rarr; obs-websocket
              Settings</strong>. Make sure the port and password there match what you've entered above.
            </div>
            <div style="margin-top:auto; padding-top:16px;">
              <button class="btn" onclick="testObsConnection()" style="width:100%;">Test Connection</button>
              <div id="obsTestStatus" style="font-size:12px; color:var(--muted); margin-top:8px; text-align:center;"></div>
            </div>
          </div>
        </div>

        <div class="section" id="audioInputSection">
          <div class="section-label">Audio Input</div>
          <div class="glass" style="display:flex; gap:10px; align-items:center;">
            <select id="deviceSelect" style="flex:1;" onchange="onDeviceChange()"></select>
            <button class="btn btn-icon" onclick="refreshDevices()" title="Refresh devices">&#8635;</button>
          </div>
          <div class="glass" style="margin-top:12px;">
            <div style="display:flex; justify-content:space-between; font-size:10.5px; letter-spacing:1px;
                 text-transform:uppercase; color:var(--muted); margin-bottom:6px;">
              <span>Mic Level</span>
              <span>Live</span>
            </div>
            <div style="height:8px; border-radius:999px; background:rgba(255,255,255,0.08); overflow:hidden;">
              <div id="levelBar" style="height:100%; width:0%; border-radius:999px; background:#4ade80;
                   transition: width 80ms linear, background 120ms linear;"></div>
            </div>
          </div>
          <div class="section-label" style="margin-top:20px;">Bulk Auto-Revert &#9201;</div>
          <div class="glass" id="bulkRevertGlass">
            <div style="display:flex; gap:8px; align-items:center;">
              <input type="text" id="bulkRevertSeconds" placeholder="e.g. 5" style="width:80px;">
              <span style="font-size:12px; color:var(--muted);">sec &rarr;</span>
              <button class="btn" onclick="applyBulkRevert()" style="flex:1;">Apply to all triggers</button>
            </div>
            <div style="font-size:11px; color:var(--muted); margin-top:8px;">
              Sets this auto-revert time on every trigger below at once. Leave blank or 0
              and apply to clear reverts from all of them.
            </div>
          </div>
        </div>
      </div>

      <div class="section">
        <div class="section-label">Phrase &rarr; Scene Triggers</div>
        <div class="glass">
          <input type="text" id="triggerSearch" placeholder="Search phrases or scenes&hellip;"
                 oninput="renderTriggers()" style="margin-bottom:12px;">
          <div id="triggerList"></div>
          <div class="btn-row">
            <button class="btn btn-primary" onclick="openTriggerModal()">+ Add Trigger</button>
          </div>
        </div>
      </div>

    </div>

    <div class="side-col">
      <div class="section">
        <div class="section-label">Live Log</div>
        <div class="glass" id="logGlass"><div id="log"></div></div>
      </div>
    </div>

  </div>

  <div class="section">
    <div class="section-label">Options</div>
    <div class="glass">
      <div class="row" style="align-items:center;">
        <div>
          <label class="field-label">Cooldown (sec)</label>
          <input type="text" id="cooldown" value="0" style="width:90px;">
        </div>
        <div class="grow switch-row" onclick="toggleConfirm()" style="margin-top:18px;">
          <div class="switch" id="confirmSwitch"><div class="knob"></div></div>
          <span class="switch-label">Ask before switching (safer for first tests)</span>
        </div>
      </div>
      <div style="margin-top:16px;">
        <label class="field-label">Match Sensitivity &middot; <span id="fuzzyPct">78%</span></label>
        <input type="range" id="fuzzyThreshold" min="60" max="100" value="78"
               oninput="document.getElementById('fuzzyPct').innerText = this.value + '%'; persistFuzzy();">
        <div style="display:flex; justify-content:space-between; font-size:11px; color:var(--muted); margin-top:2px;">
          <span>Looser &middot; catches more near-misses</span>
          <span>Stricter &middot; fewer false triggers</span>
        </div>
      </div>
    </div>
  </div>

  <footer>Voice Trigger for OBS &middot; Open Source</footer>
</div>

<div id="modalOverlay">
  <div id="modalCard">
    <h3 id="modalTitle">Add Trigger</h3>
    <div class="field">
      <label class="field-label">Trigger phrase</label>
      <input type="text" id="modalPhrase" placeholder="e.g. camera one">
    </div>
    <div class="field">
      <label class="field-label">OBS scene name</label>
      <input type="text" id="modalScene" placeholder="e.g. CAM1" list="obsSceneOptions"
             oninput="clearSceneWarning()">
      <datalist id="obsSceneOptions"></datalist>
      <div id="sceneWarning" style="display:none; color:#ff6b6b; font-size:12px; margin-top:6px;"></div>
      <div id="sceneHint" style="color:var(--muted); font-size:11px; margin-top:6px;"></div>
    </div>
    <div class="field">
      <label class="field-label">Auto-revert after (seconds)</label>
      <input type="text" id="modalRevert" placeholder="Off — stays on this scene until the next trigger">
      <div style="color:var(--muted); font-size:11px; margin-top:6px;">
        Leave blank or 0 to stay on this scene permanently. Otherwise it flashes to this
        scene, then automatically switches back to a scene named <strong>DEFAULT</strong>
        after this many seconds.
      </div>
    </div>
    <div class="btn-row">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="modalSaveBtn" onclick="saveModal()">Save</button>
    </div>
  </div>
</div>

<script>
let triggers = [];
let editIndex = null;
let obsScenesCache = null;   // null = not fetched yet, [] or [...] = fetched successfully
let obsScenesError = null;

async function api(path, payload) {
  const opts = payload !== undefined
    ? { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) }
    : { method: 'GET' };
  const res = await fetch(path, opts);
  if (!res.ok) {
    console.error(`API call to ${path} failed: ${res.status} ${res.statusText}`);
  }
  return res.json();
}

function renderTriggers() {
  const list = document.getElementById('triggerList');
  const searchEl = document.getElementById('triggerSearch');
  const q = (searchEl ? searchEl.value : '').trim().toLowerCase();

  const indexed = triggers.map((t, i) => ({ t, i }));
  const filtered = q
    ? indexed.filter(({t}) => t.phrase.toLowerCase().includes(q) || t.scene.toLowerCase().includes(q))
    : indexed;

  if (!triggers.length) {
    list.innerHTML = '<div class="empty-hint">No triggers yet — add your first phrase &rarr; scene pair.</div>';
    return;
  }
  if (!filtered.length) {
    list.innerHTML = `<div class="empty-hint">No triggers match "${escapeHtml(q)}".</div>`;
    return;
  }
  list.innerHTML = filtered.map(({t, i}) => {
    const rs = parseFloat(t.revert_seconds) || 0;
    const timerActive = rs > 0;
    const timerTitle = timerActive ? `Auto-reverts to DEFAULT after ${rs}s` : 'No auto-revert set';
    return `
    <div class="trigger-row">
      <div class="trigger-text">
        <span class="trigger-phrase">"${escapeHtml(t.phrase)}"</span>
        <span class="arrow">&rarr;</span>
        <span class="trigger-scene">${escapeHtml(t.scene)}</span>
        ${timerActive ? `<span class="trigger-scene" style="opacity:0.7;">${rs}s&nbsp;&#9201;</span>` : ''}
      </div>
      <div class="row-actions">
        <div class="icon-btn${timerActive ? ' active' : ''}" onclick="openTriggerModal(${i})" title="${timerTitle}">&#9201;</div>
        <div class="icon-btn" onclick="openTriggerModal(${i})" title="Edit">&#9998;</div>
        <div class="icon-btn icon-btn-danger" onclick="removeTrigger(${i})" title="Remove">&#128465;</div>
      </div>
    </div>
  `;
  }).join('');
}

function escapeHtml(s) {
  return (s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function fetchObsScenes() {
  const host = document.getElementById('obsHost').value;
  const port = document.getElementById('obsPort').value;
  const password = document.getElementById('obsPassword').value;
  const params = new URLSearchParams({host, port, password});
  try {
    const result = await api('/api/obs_scenes?' + params.toString());
    if (result.scenes) {
      obsScenesCache = result.scenes;
      obsScenesError = null;
    } else {
      obsScenesCache = null;
      obsScenesError = result.error || 'Could not connect to OBS';
    }
  } catch (e) {
    obsScenesCache = null;
    obsScenesError = 'Could not reach the server';
  }
  const datalist = document.getElementById('obsSceneOptions');
  datalist.innerHTML = (obsScenesCache || []).map(s => `<option value="${escapeHtml(s)}">`).join('');
  const hint = document.getElementById('sceneHint');
  if (obsScenesCache) {
    hint.innerText = obsScenesCache.length
      ? `${obsScenesCache.length} scene(s) found in OBS — pick from the list or type to filter.`
      : 'Connected to OBS, but it has no scenes yet.';
  } else {
    hint.innerText = `Couldn't verify against OBS (${obsScenesError}). You can still save, but double-check the scene name.`;
  }
}

async function testObsConnection() {
  const host = document.getElementById('obsHost').value;
  const port = document.getElementById('obsPort').value;
  const password = document.getElementById('obsPassword').value;
  const params = new URLSearchParams({host, port, password});
  const statusEl = document.getElementById('obsTestStatus');
  statusEl.style.color = 'var(--muted)';
  statusEl.innerText = 'Testing\u2026';
  try {
    const result = await api('/api/obs_scenes?' + params.toString());
    if (result.scenes) {
      statusEl.style.color = '#4ade80';
      statusEl.innerText = `\u2713 Connected \u2014 ${result.scenes.length} scene(s) found.`;
    } else {
      statusEl.style.color = '#ff6b6b';
      statusEl.innerText = `\u2717 ${result.error || 'Could not connect.'}`;
    }
  } catch (e) {
    statusEl.style.color = '#ff6b6b';
    statusEl.innerText = '\u2717 Could not reach the server.';
  }
}

function clearSceneWarning() {
  const w = document.getElementById('sceneWarning');
  w.style.display = 'none';
  w.innerText = '';
}

function openTriggerModal(i) {
  editIndex = (typeof i === 'number') ? i : null;
  document.getElementById('modalTitle').innerText = editIndex === null ? 'Add Trigger' : 'Edit Trigger';
  document.getElementById('modalPhrase').value = editIndex === null ? '' : triggers[editIndex].phrase;
  document.getElementById('modalScene').value = editIndex === null ? '' : triggers[editIndex].scene;
  document.getElementById('modalRevert').value = editIndex === null ? '' : (triggers[editIndex].revert_seconds || '');
  clearSceneWarning();
  document.getElementById('sceneHint').innerText = 'Checking OBS for available scenes\u2026';
  const overlay = document.getElementById('modalOverlay');
  overlay.style.display = 'flex';
  requestAnimationFrame(() => overlay.classList.add('show'));
  document.getElementById('modalPhrase').focus();
  fetchObsScenes();
}

function closeModal() {
  const overlay = document.getElementById('modalOverlay');
  overlay.classList.remove('show');
  setTimeout(() => overlay.style.display = 'none', 150);
}

function saveModal() {
  const phrase = document.getElementById('modalPhrase').value.trim();
  const scene = document.getElementById('modalScene').value.trim();
  const revertRaw = document.getElementById('modalRevert').value.trim();
  const revert_seconds = revertRaw ? (parseFloat(revertRaw) || 0) : 0;
  if (!phrase || !scene) return;

  if (obsScenesCache && !obsScenesCache.includes(scene)) {
    const w = document.getElementById('sceneWarning');
    w.style.display = 'block';
    w.innerText = `"${scene}" isn't a scene in OBS right now. Check spelling/capitalization, or pick one from the suggestions.`;
    return;
  }

  if (editIndex === null) {
    triggers.push({phrase, scene, revert_seconds});
  } else {
    triggers[editIndex] = {phrase, scene, revert_seconds};
  }
  renderTriggers();
  persistTriggers();
  closeModal();
}

function removeTrigger(i) {
  triggers.splice(i, 1);
  renderTriggers();
  persistTriggers();
}

function applyBulkRevert() {
  const raw = document.getElementById('bulkRevertSeconds').value.trim();
  const revert_seconds = raw ? (parseFloat(raw) || 0) : 0;
  triggers = triggers.map(t => ({...t, revert_seconds}));
  renderTriggers();
  persistTriggers();
}

function persistTriggers() {
  api('/api/settings', {triggers: triggers});
  maybeShowRestartHint();
}

function toggleConfirm() {
  const el = document.getElementById('confirmSwitch');
  el.classList.toggle('active');
  api('/api/settings', {confirm: el.classList.contains('active')});
  maybeShowRestartHint();
}

function persistFuzzy() {
  const val = parseInt(document.getElementById('fuzzyThreshold').value, 10) / 100;
  api('/api/settings', {fuzzy_threshold: val});
  maybeShowRestartHint();
}

async function refreshDevices() {
  const devices = await api('/api/devices');
  const sel = document.getElementById('deviceSelect');
  const prev = sel.value;
  sel.innerHTML = '';
  devices.forEach(d => {
    const opt = document.createElement('option');
    opt.value = d.index; opt.innerText = d.label;
    sel.appendChild(opt);
  });
  if (prev) sel.value = prev;
  startPreview();
}

async function startPreview() {
  if (isRunning) return;  // real listener owns the device — don't compete for it
  const sel = document.getElementById('deviceSelect');
  const device_index = parseInt(sel.value);
  if (isNaN(device_index)) return;
  await api('/api/preview/start', {device_index});
}

function onDeviceChange() {
  startPreview();
}

function appendLog(msg) {
  const log = document.getElementById('log');
  const line = document.createElement('div');
  if (msg.includes('MATCH') || msg.includes('Switched') || msg.includes('Connected')) {
    line.className = 'match';
  } else if (msg.startsWith('[heard]')) {
    line.className = 'heard';
  }
  line.innerText = msg;
  log.appendChild(line);
  while (log.children.length > 500) {
    log.removeChild(log.firstChild);
  }
  log.scrollTop = log.scrollHeight;
}

let isRunning = false;

function setStatus(text, active) {
  document.getElementById('statusText').innerText = text;
  const dot = document.getElementById('statusDot');
  if (active) dot.classList.add('active'); else dot.classList.remove('active');
  const starting = text.includes('Starting');
  document.getElementById('startBtn').disabled = active || starting;
  document.getElementById('stopBtn').disabled = !(active || starting);
  isRunning = active;
  if (!active) {
    document.getElementById('restartHint').style.display = 'none';
    updateLevelMeter(0);
  }
}

function maybeShowRestartHint() {
  if (isRunning) {
    document.getElementById('restartHint').style.display = 'flex';
  }
}

function showConfirm(scene) {
  document.getElementById('confirmMsg').innerText = `Switch to scene "${scene}"?`;
  document.getElementById('confirmBanner').style.display = 'flex';
}
function hideConfirm() {
  document.getElementById('confirmBanner').style.display = 'none';
}
function confirmSwitch() { api('/api/confirm', {}); }
function skipConfirm() { api('/api/skip', {}); }

async function startListening() {
  const payload = {
    obs_host: document.getElementById('obsHost').value,
    obs_port: parseInt(document.getElementById('obsPort').value || '4455'),
    obs_password: document.getElementById('obsPassword').value,
    cooldown: parseFloat(document.getElementById('cooldown').value || '0'),
    fuzzy_threshold: parseInt(document.getElementById('fuzzyThreshold').value, 10) / 100,
    confirm: document.getElementById('confirmSwitch').classList.contains('active'),
    device_index: parseInt(document.getElementById('deviceSelect').value),
    triggers: triggers,
  };
  setStatus('Starting\u2026', false);
  await api('/api/start', payload);
}

function stopListening() {
  api('/api/stop', {});
  setStatus('Stopped', false);
  // Give the real listener's audio stream a moment to fully release the
  // device before the lightweight preview tries to reopen it.
  setTimeout(startPreview, 400);
}

function connectEvents() {
  const es = new EventSource('/events');
  es.onopen = () => document.getElementById('connDot').classList.add('up');
  es.onerror = () => document.getElementById('connDot').classList.remove('up');
  es.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.type === 'log') appendLog(data.msg);
    else if (data.type === 'status') setStatus(data.text, data.active);
    else if (data.type === 'confirm') showConfirm(data.scene);
    else if (data.type === 'hideConfirm') hideConfirm();
    else if (data.type === 'level') updateLevelMeter(data.value);
  };
}

function updateLevelMeter(level) {
  const bar = document.getElementById('levelBar');
  if (!bar) return;
  const pct = Math.max(0, Math.min(1, level)) * 100;
  bar.style.width = pct + '%';
  bar.style.background = pct > 85 ? '#f87171' : pct > 60 ? '#facc15' : '#4ade80';
}

async function init() {
  const cfg = await api('/api/config');
  document.getElementById('obsHost').value = cfg.obs_host || '';
  document.getElementById('obsPort').value = cfg.obs_port || '';
  document.getElementById('obsPassword').value = cfg.obs_password || '';
  document.getElementById('cooldown').value = cfg.cooldown ?? 0;
  const fuzzyPct = Math.round((cfg.fuzzy_threshold ?? 0.78) * 100);
  document.getElementById('fuzzyThreshold').value = fuzzyPct;
  document.getElementById('fuzzyPct').innerText = fuzzyPct + '%';
  if (cfg.confirm) document.getElementById('confirmSwitch').classList.add('active');
  triggers = cfg.triggers || [];
  renderTriggers();

  await refreshDevices();
  if (cfg.device_index !== null && cfg.device_index !== undefined) {
    document.getElementById('deviceSelect').value = cfg.device_index;
    startPreview();  // refreshDevices() already started one, but possibly on the wrong (default) device
  }
  connectEvents();
}

init();

function setupLogHeightSync() {
  // Keeps the Live Log box's height matched exactly to the left column's
  // rendered height (OBS Connection + Audio Input + Triggers), so the two
  // boxes always end on the same line — recalculates automatically
  // whenever that content changes size (adding/removing triggers, window
  // resize, etc), not just once on page load.
  const mainCol = document.querySelector('.main-col');
  const logGlass = document.getElementById('logGlass');
  if (!mainCol || !logGlass) return;

  const apply = () => {
    if (window.innerWidth >= 1020) {
      // Measure real bounding boxes rather than assuming label heights match —
      // this is what actually guarantees the two boxes end on the same line.
      const mainBottom = mainCol.getBoundingClientRect().bottom;
      const glassTop = logGlass.getBoundingClientRect().top;
      const target = mainBottom - glassTop;
      logGlass.style.height = Math.max(200, target) + 'px';
    } else {
      logGlass.style.height = '400px';
    }
  };

  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(apply).observe(mainCol);
  }
  window.addEventListener('resize', apply);

  // Run once immediately, then again after layout/paint has fully settled —
  // a single rAF isn't always enough right after page load, so we chain two.
  apply();
  requestAnimationFrame(() => requestAnimationFrame(apply));
}

setupLogHeightSync();
</script>
</body>
</html>
"""


def create_app():
    from flask import Flask, request, jsonify, Response

    app = Flask(__name__)
    service = VoiceTriggerService()

    @app.route("/")
    def index():
        return HTML_TEMPLATE

    @app.route("/api/config")
    def api_config():
        return jsonify(service.get_config())

    @app.route("/api/devices")
    def api_devices():
        return jsonify(service.list_devices())

    @app.route("/api/preview/start", methods=["POST"])
    def api_preview_start():
        data = request.get_json(force=True, silent=True) or {}
        result = service.start_preview(data.get("device_index"))
        return jsonify(result)

    @app.route("/api/preview/stop", methods=["POST"])
    def api_preview_stop():
        service.stop_preview()
        return jsonify({"ok": True})

    @app.route("/api/obs_scenes")
    def api_obs_scenes():
        host = request.args.get("host") or None
        port = request.args.get("port")
        port = int(port) if port else None
        password = request.args.get("password")
        return jsonify(service.list_obs_scenes(host=host, port=port, password=password))

    @app.route("/api/settings", methods=["POST"])
    def api_settings():
        service.save_settings(request.get_json(force=True, silent=True) or {})
        return jsonify({"ok": True})

    @app.route("/api/start", methods=["POST"])
    def api_start():
        ok = service.start(request.get_json(force=True, silent=True) or {})
        return jsonify({"ok": ok})

    @app.route("/api/stop", methods=["POST"])
    def api_stop():
        service.stop()
        return jsonify({"ok": True})

    @app.route("/api/confirm", methods=["POST"])
    def api_confirm():
        service.confirm_switch()
        return jsonify({"ok": True})

    @app.route("/api/skip", methods=["POST"])
    def api_skip():
        service.skip_confirm()
        return jsonify({"ok": True})

    @app.route("/events")
    def sse_events():
        q = broker.subscribe()

        def gen():
            try:
                while True:
                    item = q.get()
                    yield f"data: {json.dumps(item)}\n\n"
            finally:
                broker.unsubscribe(q)

        return Response(gen(), mimetype="text/event-stream")

    return app


def main():
    app = create_app()
    url = f"http://localhost:{PORT}"
    print(f"\nVoice Trigger for OBS running at: {url}")
    print("Leave this Terminal window open — closing it stops the server.")
    print("Opening in your default browser...\n")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
