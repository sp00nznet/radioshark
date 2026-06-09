#!/usr/bin/env python3
"""shark_web.py - headless web interface for the radioSHARK.

A third front-end alongside the CLI and the Tkinter GUI (shark_gui.py), built for
running on a headless box (e.g. an LXC with the USB device passed through) where there
is no display and no local speakers. It reuses shark.py's command builders, so it stays
feature-identical to the GUI; the one real difference is audio delivery: instead of
playing to local speakers via ffplay, ONE ffmpeg capture is fanned out as an MP3 byte
stream to browser <audio> clients (plus transcription segments and an optional
recording). The visualizer is computed in the browser from that stream via Web Audio.

No third-party dependencies - pure stdlib http.server.

Run:  python shark.py web            (or:  python shark_web.py)
      python shark.py web --port 8080 --host 0.0.0.0
"""
import os, sys, json, time, glob, queue, threading, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import shark

WEB_DIR = os.path.join(shark.HERE, "web")
SEG_DIR = os.path.join(shark.runtime_dir(), "_seg")
TS_DIR = os.path.join(shark.runtime_dir(), "timeshift")


def _terminate(proc):
    """Stop a subprocess and WAIT for it to exit. The radioSHARK capture is a single
    consumer, so a respawn that races a still-dying ffmpeg hits 'Device or resource
    busy'. Waiting for exit guarantees the ALSA/dshow handle is released first."""
    if not proc or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try: proc.kill(); proc.wait(timeout=2)
        except Exception: pass


# ----------------------------------------------------------------- pub/sub bus
class Bus:
    """Minimal SSE fan-out: each subscriber gets its own queue of JSON events."""
    def __init__(self):
        self.subs = set()
        self.lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=200)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)

    def publish(self, event):
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


# ----------------------------------------------------------------- audio hub
class AudioHub:
    """Fan-out of the engine's MP3 byte stream to all connected /audio clients."""
    def __init__(self):
        self.clients = set()
        self.lock = threading.Lock()

    def add(self):
        q = queue.Queue(maxsize=512)
        with self.lock:
            self.clients.add(q)
        return q

    def remove(self, q):
        with self.lock:
            self.clients.discard(q)

    def feed(self, chunk):
        with self.lock:
            clients = list(self.clients)
        for q in clients:
            try:
                q.put_nowait(chunk)
            except queue.Full:
                pass            # slow client: drop audio rather than stall the reader

    def close_all(self):
        with self.lock:
            clients = list(self.clients)
        for q in clients:
            try:
                q.put_nowait(None)      # sentinel: end the response
            except queue.Full:
                pass


# ----------------------------------------------------------------- controller
class WebEngine:
    """Headless port of shark_gui.SharkGUI's controller. Same single-consumer
    arbitration: the shared engine (listen/record/transcribe) is mutually exclusive
    with the standalone tools (timed-record/stream/log/timeshift)."""

    def __init__(self):
        self.bus = Bus()
        self.audio = AudioHub()
        self.lock = threading.RLock()

        self.engine = None              # ffmpeg fan-out (MP3 + segs + optional record)
        self.media = None               # exclusive tool process (record/stream/log)
        self.media_kind = None          # "record"|"stream"|"log" - which tool is running
        self.ts_proc = None             # timeshift HLS recorder
        self.listen_on = False
        self.recording = False
        self.record_file = None
        self.tx_on = False
        self.tx_stop = None
        self.model = None
        self._seeking = False
        self.seek_cancel = threading.Event()
        self.scanning = False
        self.scan_results = []
        self.stream_info = None

        self.am = False
        self.fm_freq, self.am_freq = 96.3, 1000
        self.freq = self.fm_freq
        self.eq_on = False
        self.eq = "flat"
        self.eq_b = self.eq_m = self.eq_t = 0
        self.now_playing = ""
        self.status = "ready"

    # ----- helpers
    def say(self, m):
        self.status = m
        self.push_state()

    def _engine_alive(self):
        return self.engine is not None and self.engine.poll() is None

    def _media_alive(self):
        return self.media is not None and self.media.poll() is None

    def state(self):
        return {
            "freq": self.freq, "am": self.am,
            "listen_on": self.listen_on and self._engine_alive(),
            "recording": self.recording, "tx_on": self.tx_on,
            "seeking": self._seeking, "scanning": self.scanning,
            "eq_on": self.eq_on, "eq": self.eq,
            "eq_b": self.eq_b, "eq_m": self.eq_m, "eq_t": self.eq_t,
            "now_playing": self.now_playing, "status": self.status,
            "timeshift_on": self.ts_proc is not None and self.ts_proc.poll() is None,
            "stream_on": self._media_alive() and self.media_kind == "stream",
            "log_on": self._media_alive() and self.media_kind == "log",
            "stream_info": self.stream_info,
            "media_on": self._media_alive(),
            "eq_profiles": list(shark.EQ_PROFILES) + ["custom"],
            "fm_step": shark.FM_STEP, "am_step": shark.AM_STEP,
        }

    def push_state(self):
        self.bus.publish({"type": "state", "state": self.state()})

    # ----- engine (shared capture: MP3 fan-out + segments + optional record)
    def current_eq(self):
        if not self.eq_on:
            return None
        if self.eq == "custom":
            shark.EQ_PROFILES["custom"] = (f"bass=g={self.eq_b},"
                                           f"equalizer=f=1000:t=q:w=1:g={self.eq_m},"
                                           f"treble=g={self.eq_t}")
        return self.eq

    def _spawn_engine(self):
        os.makedirs(SEG_DIR, exist_ok=True)
        for s in glob.glob(os.path.join(SEG_DIR, "*.wav")):
            try: os.remove(s)
            except OSError: pass
        rec = self.record_file if self.recording else None
        cmd = shark.engine_cmds_web(eq=self.current_eq(), segdir=SEG_DIR, record_file=rec)
        self.engine = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self._pump_audio, args=(self.engine,), daemon=True).start()

    def _pump_audio(self, proc):
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                self.audio.feed(chunk)
        except Exception:
            pass

    def _kill_engine(self):
        proc, self.engine = self.engine, None
        self.audio.close_all()
        _terminate(proc)        # wait for exit so ALSA frees before any respawn

    def ensure_engine(self):
        if not self._engine_alive():
            self._spawn_engine()

    def restart_engine(self):
        self._kill_engine(); self._spawn_engine()

    def stop_engine(self):
        self.stop_transcribe_worker()
        self._kill_engine()
        self.listen_on = False

    def stop_media(self):
        procs = (self.media, self.ts_proc)
        self.media = None; self.ts_proc = None; self.stream_info = None; self.media_kind = None
        for p in procs:
            _terminate(p)       # wait for exit so the single-consumer device frees

    def stop_all(self):
        self.tx_on = False; self.recording = False; self.record_file = None
        self.stop_engine(); self.stop_media()

    # ----- tuning
    def tune(self, freq=None, am=None):
        with self.lock:
            if am is not None: self.am = bool(am)
            if freq is not None: self.freq = float(freq)
            if self._seeking:
                self.seek_cancel.set()
            try:
                shark.tune(self.freq, am=self.am)
                if self.am: self.am_freq = self.freq
                else: self.fm_freq = self.freq
                self.now_playing = ""
                if not self._seeking and not self._media_alive() and self.ts_proc is None:
                    self.listen_on = True; self.ensure_engine()
                self.say(f"tuned {self.freq} {'kHz AM' if self.am else 'MHz FM'}")
            except Exception as e:
                self.say(f"tune error: {e}")

    def step(self, d):
        st = shark.AM_STEP if self.am else shark.FM_STEP
        self.tune(freq=round(self.freq + d * st, 1))

    def set_band(self, am):
        with self.lock:
            if am:
                self.fm_freq = self.freq; nf = self.am_freq
            else:
                self.am_freq = self.freq; nf = self.fm_freq
        self.tune(freq=nf, am=am)

    def seek(self, up):
        if self._seeking:
            self.seek_cancel.set(); self.say("seek cancelled"); return
        self.stop_all()
        self.seek_cancel = threading.Event(); self._seeking = True
        self.say("seeking…")
        def run():
            def prog(fr, r):
                self.freq = fr; self.push_state()
            found = shark.seek(self.freq, up=up, am=self.am,
                               progress=prog, cancel=self.seek_cancel.is_set)
            self._seeking = False
            if found:
                self.freq = found
                self.listen_on = True; self.ensure_engine()
                self.say(f"found {found}")
            else:
                self.say("seek stopped")
        threading.Thread(target=run, daemon=True).start()

    # ----- listen / record / transcribe
    def set_listen(self, on):
        if on:
            self.stop_media(); self.listen_on = True; self.ensure_engine(); self.say("listening")
        else:
            self.listen_on = False
            if not (self.tx_on or self.recording):
                self.stop_engine()
            self.say("stopped")
        self.push_state()

    def set_record(self, on):
        if on:
            if self._media_alive(): self.stop_media()
            self.record_file = os.path.join(shark.HERE,
                f"radioSHARK-{time.strftime('%Y%m%d-%H%M%S')}.mp3")
            self.recording = True; shark.set_led(red=True)
            self.restart_engine() if self._engine_alive() else self.ensure_engine()
            self.say(f"recording -> {os.path.basename(self.record_file)}")
        else:
            self.recording = False; shark.set_led(red=False, blue=64)
            saved = self.record_file; self.record_file = None
            if self.listen_on or self.tx_on:
                self.restart_engine()
            else:
                self.stop_engine()
            self.say(f"saved {os.path.basename(saved)}" if saved else "stopped")
        self.push_state()

    def set_eq(self, on=None, profile=None, b=None, m=None, t=None):
        if on is not None: self.eq_on = bool(on)
        if profile is not None: self.eq = profile
        if b is not None: self.eq_b = int(b)
        if m is not None: self.eq_m = int(m)
        if t is not None: self.eq_t = int(t)
        if self._engine_alive():
            self.restart_engine()
        self.say("EQ " + (f"on: {self.eq}" if self.eq_on else "off"))

    def set_transcribe(self, on):
        if on:
            if self._media_alive(): self.stop_media()
            self.tx_on = True; self.ensure_engine()
            self.tx_stop = threading.Event()
            threading.Thread(target=self._tx_worker, daemon=True).start()
            self.say("transcribing live…")
        else:
            self.tx_on = False; self.stop_transcribe_worker()
            if not (self.listen_on or self.recording):
                self.stop_engine()
            self.say("transcription stopped")
        self.push_state()

    def stop_transcribe_worker(self):
        if self.tx_stop:
            self.tx_stop.set()

    def _tx(self, text):
        self.bus.publish({"type": "transcript", "text": text})

    def _tx_worker(self):
        self._tx("[loading speech model…]\n")
        try:
            from faster_whisper import WhisperModel
            if self.model is None:
                self.model = WhisperModel("base", device="cpu", compute_type="int8")
        except Exception as e:
            self._tx(f"\n[transcription unavailable: {e}]\n"); return
        self._tx("[listening — reads along with the audio]\n\n")
        while not self.tx_stop.is_set():
            files = sorted(glob.glob(os.path.join(SEG_DIR, "seg_*.wav")))
            if len(files) >= 2:
                seg = files[0]
                try:
                    segs, _ = self.model.transcribe(seg, vad_filter=True, language="en")
                    text = " ".join(s.text.strip() for s in segs).strip()
                    if text: self._tx(text + " ")
                except Exception:
                    pass
                try: os.remove(seg)
                except OSError: pass
            else:
                time.sleep(0.3)

    # ----- LED
    def led(self, red=None, blue=None, pulse=None):
        shark.set_led(red=red, blue=blue, pulse=pulse)

    # ----- scan
    def scan(self, am):
        if self.scanning: return
        self.stop_all()
        self.scanning = True; self.scan_results = []
        unit = "kHz" if am else "MHz"
        self.bus.publish({"type": "scan_start", "am": am})
        def prog(i, total, fr, rms):
            self.bus.publish({"type": "scan_progress", "i": i, "total": total,
                              "freq": fr, "unit": unit})
        def run():
            stations, floor = shark.scan_band(am=am, progress=prog)
            self.scan_results = [{"freq": fr, "rms": rms, "lift": lift}
                                 for fr, rms, lift in stations]
            self.scanning = False
            self.bus.publish({"type": "scan_done", "am": am, "unit": unit,
                              "floor": floor, "stations": self.scan_results})
            self.say(f"scan done: {len(stations)} stations")
        threading.Thread(target=run, daemon=True).start()

    # ----- song id (graceful: shazamio segfaults on this Python)
    def songid(self):
        def run():
            try:
                import asyncio
                from shazamio import Shazam
                files = sorted(glob.glob(os.path.join(SEG_DIR, "seg_*.wav")))[:-1][-3:]
                clip = shark._capture_tmp(10) if not files else None
                if files:
                    clip = os.path.join(shark.runtime_dir(), "_songclip.wav")
                    inp = []
                    for fp in files: inp += ["-i", fp]
                    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
                                   + inp + ["-filter_complex",
                                            f"concat=n={len(files)}:v=0:a=1", clip])
                out = asyncio.run(Shazam().recognize(clip))
                tr = out.get("track")
                self.now_playing = f"{tr['title']} — {tr['subtitle']}" if tr else "no match"
            except Exception as e:
                self.now_playing = f"song ID unavailable: {e}"
            self.say("song id done")
        threading.Thread(target=run, daemon=True).start()
        self.now_playing = "identifying…"; self.push_state()

    # ----- tools: timed record / stream / log / timeshift
    def record_timed(self, seconds, fmt):
        self.stop_all()
        cmd, out = shark.record_cmd(seconds, fmt=fmt, eq=self.current_eq())
        shark.set_led(red=True); self.media = subprocess.Popen(cmd); self.media_kind = "record"
        self.say(f"recording {seconds}s -> {os.path.basename(out)}")
        def watch():
            self.media.wait(); shark.set_led(red=False, blue=64)
            self.say(f"saved {os.path.basename(out)}")
        threading.Thread(target=watch, daemon=True).start()
        self.push_state()

    def set_stream(self, on, port=8000, fmt="mp3", bitrate="192k"):
        if not on:
            self.stop_media(); self.say("stream stopped"); self.push_state(); return
        self.stop_all()
        cmd, info = shark.stream_cmd(port=port, fmt=fmt, bitrate=bitrate)
        self.media = subprocess.Popen(cmd); self.stream_info = info; self.media_kind = "stream"
        self.say(f"streaming: {info}"); self.push_state()

    def set_log(self, on, segment=3600, fmt="mp3"):
        if not on:
            self.stop_media(); self.say("logging stopped"); self.push_state(); return
        self.stop_all()
        cmd, d = shark.log_cmd(segment=segment, fmt=fmt)
        self.media = subprocess.Popen(cmd); self.media_kind = "log"
        self.say(f"logging to {d}"); self.push_state()

    def set_timeshift(self, on, buffer_min=60):
        if not on:
            self.stop_media(); self.say("timeshift stopped"); self.push_state(); return
        self.stop_all()
        cmd, m3u8 = shark.timeshift_recorder_cmd(buffer_min, tsdir=TS_DIR)
        self.ts_proc = subprocess.Popen(cmd)
        self.say(f"buffering {buffer_min}-min timeshift…")
        self.push_state()

    # ----- presets / schedule (thin pass-throughs to shark.py)
    def presets(self):
        return shark.load_presets()

    def preset(self, action, name=None, freq=None, am=False):
        pr = shark.load_presets()
        if action == "add":
            pr[name] = {"freq": float(freq), "am": bool(am), "label": ""}
            shark.save_presets(pr); self.say(f"saved preset '{name}'")
        elif action == "remove":
            pr.pop(name, None); shark.save_presets(pr); self.say(f"removed '{name}'")
        elif action == "tune":
            if name in pr:
                self.tune(freq=pr[name]["freq"], am=pr[name].get("am", False))
        return shark.load_presets()

    def schedule_add(self, name, at, dur, repeat):
        shark.schedule_add(name, self.freq, self.am, dur, at, repeat, None)
        self.say(f"recording '{name}' scheduled")

    def schedule_remove(self, name):
        shark.schedule_remove(name); self.say(f"removed schedule '{name}'")

    def schedule_list(self):
        out = subprocess.run([sys.executable, os.path.join(shark.HERE, "shark.py"),
                              "schedule", "list"], capture_output=True, text=True).stdout
        return out.strip()

    def recordings(self):
        out = []
        for d, tag in ((shark.HERE, ""), (os.path.join(shark.HERE, "logs"), "logs/")):
            if not os.path.isdir(d): continue
            for fn in sorted(os.listdir(d), reverse=True):
                if fn.startswith("_"):           # skip engine scratch (_clip.wav etc.)
                    continue
                if fn.lower().endswith((".mp3", ".wav", ".aac", ".m4a")):
                    p = os.path.join(d, fn)
                    out.append({"name": tag + fn, "size": os.path.getsize(p)})
        return out


ENGINE = WebEngine()


# ----------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass                    # quiet

    # -- response helpers
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n: return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # -- GET
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._file(os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8")
        if path == "/static/app.js":
            return self._file(os.path.join(WEB_DIR, "app.js"), "application/javascript")
        if path == "/static/style.css":
            return self._file(os.path.join(WEB_DIR, "style.css"), "text/css")
        if path == "/api/state":
            return self._json(ENGINE.state())
        if path == "/api/presets":
            return self._json(ENGINE.presets())
        if path == "/api/schedule":
            return self._json({"text": ENGINE.schedule_list()})
        if path == "/api/recordings":
            return self._json(ENGINE.recordings())
        if path == "/events":
            return self._sse()
        if path == "/audio":
            return self._audio()
        if path.startswith("/rec/"):
            return self._download(path[len("/rec/"):])
        if path.startswith("/ts/"):
            return self._ts(path[len("/ts/"):])
        return self._json({"error": "not found"}, 404)

    # -- POST
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        b = self._body()
        E = ENGINE
        try:
            if path == "/api/tune":
                E.tune(freq=b.get("freq"), am=b.get("am"))
            elif path == "/api/step":
                E.step(int(b.get("dir", 1)))
            elif path == "/api/band":
                E.set_band(bool(b.get("am")))
            elif path == "/api/seek":
                E.seek(bool(b.get("up", True)))
            elif path == "/api/listen":
                E.set_listen(bool(b.get("on")))
            elif path == "/api/record":
                E.set_record(bool(b.get("on")))
            elif path == "/api/eq":
                E.set_eq(on=b.get("on"), profile=b.get("profile"),
                         b=b.get("bass"), m=b.get("mid"), t=b.get("treble"))
            elif path == "/api/led":
                E.led(red=b.get("red"), blue=b.get("blue"), pulse=b.get("pulse"))
            elif path == "/api/scan":
                E.scan(bool(b.get("am")))
            elif path == "/api/transcribe":
                E.set_transcribe(bool(b.get("on")))
            elif path == "/api/songid":
                E.songid()
            elif path == "/api/preset":
                return self._json(E.preset(b.get("action"), b.get("name"),
                                           b.get("freq"), b.get("am", False)))
            elif path == "/api/record-timed":
                E.record_timed(int(b.get("seconds", 60)), b.get("fmt", "mp3"))
            elif path == "/api/stream":
                E.set_stream(bool(b.get("on")), int(b.get("port", 8000)),
                             b.get("fmt", "mp3"), b.get("bitrate", "192k"))
            elif path == "/api/log":
                E.set_log(bool(b.get("on")), int(b.get("segment", 3600)), b.get("fmt", "mp3"))
            elif path == "/api/timeshift":
                E.set_timeshift(bool(b.get("on")), int(b.get("buffer_min", 60)))
            elif path == "/api/schedule":
                if b.get("action") == "remove":
                    E.schedule_remove(b.get("name"))
                else:
                    E.schedule_add(b.get("name"), b.get("at"),
                                   int(b.get("dur", 3600)), b.get("repeat", "daily"))
                return self._json({"text": E.schedule_list()})
            else:
                return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        return self._json({"ok": True, "state": E.state()})

    # -- streaming: SSE
    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        q = ENGINE.bus.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            # prime with current state so a fresh page is immediately in sync
            self.wfile.write(b"data: " + json.dumps(
                {"type": "state", "state": ENGINE.state()}).encode() + b"\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")     # keepalive
                self.wfile.flush()
        except Exception:
            pass
        finally:
            ENGINE.bus.unsubscribe(q)

    # -- streaming: live MP3
    def _audio(self):
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        q = ENGINE.audio.add()
        try:
            while True:
                chunk = q.get()
                if chunk is None:
                    break
                self.wfile.write(chunk)
        except Exception:
            pass
        finally:
            ENGINE.audio.remove(q)

    def _download(self, name):
        name = os.path.basename(name) if "/" not in name else name
        base = shark.HERE
        if name.startswith("logs/"):
            base = os.path.join(shark.HERE, "logs"); name = name[len("logs/"):]
        p = os.path.join(base, os.path.basename(name))
        if not os.path.isfile(p):
            return self._json({"error": "not found"}, 404)
        try:
            with open(p, "rb") as f:
                body = f.read()
        except OSError:
            return self._json({"error": "read failed"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(name)}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ts(self, name):
        p = os.path.join(TS_DIR, os.path.basename(name))
        if not os.path.isfile(p):
            return self._json({"error": "not found"}, 404)
        ctype = "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp2t"
        self._file(p, ctype)


def main(host="0.0.0.0", port=8080):
    httpd = ThreadingHTTPServer((host, port), Handler)
    ip = shark._lan_ip()
    print(f"radioSHARK web UI on  http://{host}:{port}/   (LAN: http://{ip}:{port}/)")
    print("Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down…")
        ENGINE.stop_all()
        httpd.shutdown()


if __name__ == "__main__":
    main()
