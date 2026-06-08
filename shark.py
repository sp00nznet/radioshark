#!/usr/bin/env python3
"""shark.py - a software radio app for the Griffin radioSHARK v1 (USB 077d:627a).

Recreates the original Griffin app's features on Windows 11:
  tune (FM/AM), presets, live listen, record, scheduled recording, EQ, LED.

Audio flows over USB as the capture device "Analog Connector (RadioSHARK)"
(must be enabled in Sound > Recording first). Tuning + LEDs go over HID.

Usage examples:
  python shark.py tune 88.5                 # tune FM
  python shark.py tune 1010 --am            # tune AM (kHz)
  python shark.py 88.5                       # shorthand for tune
  python shark.py wnpr                       # tune a saved preset by name
  python shark.py listen --eq voice          # play live through speakers
  python shark.py rec 30 --out show.mp3      # record 30s to mp3 (red LED on)
  python shark.py preset add wnpr 88.5       # save a preset
  python shark.py presets                    # list presets
  python shark.py scan                       # find stations
  python shark.py schedule add atc --preset wnpr --at 17:00 --dur 3600 --repeat daily
  python shark.py led --red on               # LED control
"""
import sys, os, json, time, subprocess, argparse, re
import hid

try:                       # Windows consoles default to cp1252; emit UTF-8 so
    sys.stdout.reconfigure(encoding="utf-8")   # song titles/emoji don't crash
except Exception:
    pass

VID, PID = 0x077d, 0x627a
FMIF, AMIF = 10700, 450               # intermediate freqs (kHz)
BAND_MW = (1 << 20)                   # TEA575X_BIT_BAND_MW (AM)
BAND_FM_JAPAN_LO = 76.0               # chip also tunes the 76-90 MHz band
FM_LO, FM_HI, FM_STEP = 87.9, 107.9, 0.2          # US FM channel plan (MHz)
AM_LO, AM_HI, AM_STEP = 530, 1710, 10             # US AM/MW channel plan (kHz)
HERE = os.path.dirname(os.path.abspath(__file__))
PRESETS_FILE = os.path.join(HERE, "presets.json")

# ---- platform layer (the only OS-specific seam; Linux port flips these) ----
IS_WIN = sys.platform.startswith("win")

def default_device():
    """Name/handle of the radioSHARK audio capture device for this OS."""
    if IS_WIN:
        return "Analog Connector (RadioSHARK)"     # DirectShow friendly name
    # Linux: ALSA device. Override with RADIOSHARK_ALSA, else a sensible guess.
    return os.environ.get("RADIOSHARK_ALSA", "plughw:CARD=radioSHARK")

def audio_input(device=None):
    """ffmpeg/ffplay input args to capture from the radioSHARK, per platform."""
    device = device or default_device()
    if IS_WIN:
        return ["-f", "dshow", "-i", f"audio={device}"]
    return ["-f", "alsa", "-i", device]            # Linux: ALSA capture

DEFAULT_DEVICE = default_device()

EQ_PROFILES = {
    "flat":   None,
    "bass":   "bass=g=8",
    "treble": "treble=g=6",
    "voice":  "highpass=f=120,equalizer=f=2500:t=q:w=1.5:g=5,lowpass=f=6500",
    "music":  "bass=g=4,treble=g=3",
    "warm":   "bass=g=5,treble=g=-2",
}

# ---------------------------------------------------------------- HID layer
def _open():
    d = hid.device(); d.open(VID, PID); return d

def _write(d, *payload):
    buf = bytes([0x00, *payload]); d.write(buf + bytes(7 - len(buf)))

def fm_pll(mhz):
    khz = int(round(mhz * 1000)); return (((khz + FMIF) * 10) // 125) & 0x7fff

def fm_pll_japan(mhz):                # low-side injection for the 76-90 MHz band
    khz = int(round(mhz * 1000)); return (((khz - FMIF) * 10) // 125) & 0x7fff

def am_pll(khz):
    return (int(round(khz)) + AMIF) & 0x7fff

def tune(freq, am=False, lowband=False, ack=True):
    if am:        val = BAND_MW | am_pll(freq)
    elif lowband: val = fm_pll_japan(freq)
    else:         val = fm_pll(freq)
    d = _open()
    try:
        _write(d, 0xc0 | ((val >> 24) & 0xff), (val >> 16) & 0xff,
               (val >> 8) & 0xff, val & 0xff)
        if ack:                       # brief red blink = "tuned"
            _write(d, 0xA9); time.sleep(0.15); _write(d, 0xA8)
    finally:
        d.close()
    unit = "kHz AM" if am else ("MHz FM (low band)" if lowband else "MHz FM")
    print(f"tuned to {freq} {unit}")

def set_led(red=None, blue=None, pulse=None):
    d = _open()
    try:
        if red is True:  _write(d, 0xA9)
        if red is False: _write(d, 0xA8)
        if blue is not None:  _write(d, 0xA0, max(0, min(127, blue)))
        if pulse is not None: _write(d, 0xA1, (256 - pulse) & 0xff)
    finally:
        d.close()

# ------------------------------------------------------------- presets
def load_presets():
    if os.path.exists(PRESETS_FILE):
        with open(PRESETS_FILE) as f: return json.load(f)
    return {}

def save_presets(p):
    with open(PRESETS_FILE, "w") as f: json.dump(p, f, indent=2)

# ------------------------------------------------------------- audio (ffmpeg)
def _eq_args(eq):
    chain = EQ_PROFILES.get(eq)
    return ["-af", chain] if chain else []

# ---- command builders (return arg lists; used by BOTH the CLI and the GUI) ----
# The CLI runs them with subprocess.run; the GUI launches them with Popen so it
# can start/stop. Keeping construction here is what makes the two front-ends
# feature-identical and the whole thing portable.

def listen_cmd(eq=None, device=None, seconds=None):
    cmd = ["ffplay", "-hide_banner", "-nodisp"] + audio_input(device)
    if seconds:
        cmd += ["-t", str(seconds), "-autoexit"]
    return cmd + _eq_args(eq)

def record_cmd(seconds, out=None, fmt=None, eq=None, device=None, outdir=None):
    if not out:
        out = os.path.join(outdir or HERE, f"radioSHARK-{time.strftime('%Y%m%d-%H%M%S')}.{fmt or 'mp3'}")
    elif os.path.isdir(out) or out.endswith(("\\", "/")):
        out = os.path.join(out, f"radioSHARK-{time.strftime('%Y%m%d-%H%M%S')}.{fmt or 'mp3'}")
    fmt = fmt or os.path.splitext(out)[1].lstrip(".") or "mp3"
    codec = {"mp3": ["-c:a", "libmp3lame", "-b:a", "192k"], "wav": ["-c:a", "pcm_s16le"],
             "aac": ["-c:a", "aac", "-b:a", "192k"], "m4a": ["-c:a", "aac", "-b:a", "192k"]
             }.get(fmt, ["-c:a", "libmp3lame", "-b:a", "192k"])
    cmd = ["ffmpeg", "-hide_banner", "-y"] + audio_input(device) + ["-t", str(seconds)] + _eq_args(eq) + codec + [out]
    return cmd, out

def stream_cmd(port=8000, fmt="mp3", bitrate="192k", icecast=None, device=None):
    codec = {"mp3": ["-c:a", "libmp3lame", "-b:a", bitrate, "-f", "mp3"],
             "aac": ["-c:a", "aac", "-b:a", bitrate, "-f", "adts"]}[fmt]
    base = ["ffmpeg", "-hide_banner", "-loglevel", "warning"] + audio_input(device) + codec
    if icecast:
        return base + ["-content_type", f"audio/{'mpeg' if fmt == 'mp3' else 'aac'}", icecast], f"pushing to Icecast: {icecast}"
    info = f"http://localhost:{port}/  (LAN: http://{_lan_ip()}:{port}/)"
    return base + ["-listen", "1", f"http://0.0.0.0:{port}/"], info

def log_cmd(segment=3600, fmt="mp3", out_dir=None, device=None):
    d = out_dir or os.path.join(HERE, "logs")
    os.makedirs(d, exist_ok=True)
    codec = {"mp3": ["-c:a", "libmp3lame", "-b:a", "128k"], "aac": ["-c:a", "aac", "-b:a", "128k"],
             "wav": ["-c:a", "pcm_s16le"]}.get(fmt, ["-c:a", "libmp3lame", "-b:a", "128k"])
    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "warning"] + audio_input(device) + codec +
           ["-f", "segment", "-segment_time", str(segment), "-strftime", "1", "-reset_timestamps", "1",
            os.path.join(d, f"station_%Y%m%d-%H%M%S.{fmt}")])
    return cmd, d

def timeshift_recorder_cmd(buffer_min=60, tsdir=None, device=None):
    tsdir = tsdir or os.path.join(HERE, "timeshift")
    os.makedirs(tsdir, exist_ok=True)
    for f in os.listdir(tsdir):                  # clear stale buffer
        try: os.remove(os.path.join(tsdir, f))
        except OSError: pass
    m3u8 = os.path.join(tsdir, "ts.m3u8")
    seg = 4
    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error"] + audio_input(device) +
           ["-c:a", "aac", "-b:a", "160k", "-f", "hls", "-hls_time", str(seg),
            "-hls_list_size", str(max(4, (buffer_min * 60) // seg)),
            "-hls_flags", "delete_segments+append_list+omit_endlist", m3u8])
    return cmd, m3u8

def _lan_ip():
    import socket
    try: return socket.gethostbyname(socket.gethostname())
    except Exception: return "127.0.0.1"

# ---- run-functions (CLI front-end: blocking subprocess.run on the builders) ----
def listen(eq=None, device=None, seconds=None, freq=None, am=False):
    if freq is not None: tune(freq, am=am)
    print("listening (Ctrl+C to stop)...")
    subprocess.run(listen_cmd(eq, device, seconds))

def record(seconds, out=None, fmt=None, eq=None, device=None, outdir=None):
    cmd, out = record_cmd(seconds, out, fmt, eq, device, outdir)
    try:
        set_led(red=True)
        print(f"recording {seconds}s -> {out}")
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"saved {out}")
    finally:
        set_led(red=False, blue=64)
    return out

def scan(am=False, debug=False):
    band, unit = ("AM", "kHz") if am else ("FM", "MHz")
    print(f"scanning {band} band...")
    def prog(i, total, f, rms):
        print(f"\r  [{i:>3}/{total}] {f} {unit}    ", end="", flush=True)
    stations, floor = scan_band(am=am, progress=prog)
    print(f"\r{' ' * 40}\r=== stations found on {band} ===")
    if not stations:
        print("  (none clear - try the antenna)")
    for f, rms, lift in stations:
        bars = "█" * min(10, max(1, int(lift)))
        extra = f"   [rms {rms:.0f} dB, +{lift:.0f} over floor {floor:.0f}]" if debug else ""
        print(f"  {f:>7} {unit}   {bars}{extra}")

def timeshift(freq=None, am=False, buffer_min=60, device=None):
    """TiVo for radio: circular HLS buffer + ffplay's interactive pause/seek keys."""
    if freq is not None: tune(freq, am=am)
    cmd, m3u8 = timeshift_recorder_cmd(buffer_min, device=device)
    rec = subprocess.Popen(cmd)
    try:
        print(f"buffering {buffer_min}-min circular timeshift... (filling)")
        for _ in range(40):
            if os.path.exists(m3u8) and sum(1 for _ in open(m3u8)) > 6: break
            time.sleep(0.25)
        print("\n  TIMESHIFT CONTROLS (in the player window):")
        print("    SPACE = pause/resume     <- / -> = seek -/+10s")
        print("    DOWN / UP = seek -/+1min  q = quit\n")
        subprocess.run(["ffplay", "-hide_banner", "-nodisp", "-autoexit", m3u8])
    finally:
        rec.terminate()
        try: rec.wait(timeout=5)
        except Exception: rec.kill()
        print("timeshift stopped.")

def stream(freq=None, am=False, port=8000, fmt="mp3", bitrate="192k", icecast=None, device=None):
    if freq is not None: tune(freq, am=am)
    cmd, info = stream_cmd(port, fmt, bitrate, icecast, device)
    print(f"serving stream at  {info}")
    if not icecast:
        print("open that URL in a browser or VLC on any device on your network.")
        print("(built-in server = one listener at a time; use --icecast for many)")
    subprocess.run(cmd)

def log_station(freq=None, am=False, segment=3600, fmt="mp3", out_dir=None, device=None):
    if freq is not None: tune(freq, am=am)
    cmd, d = log_cmd(segment, fmt, out_dir, device)
    print(f"logging to {d} in {segment}s segments (Ctrl+C to stop)...")
    subprocess.run(cmd)

def _capture_tmp(seconds, device=None):
    tmp = os.path.join(HERE, "_clip.wav")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + audio_input(device) +
                   ["-t", str(seconds), "-ar", "44100", "-ac", "2", tmp],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return tmp

def songid(seconds=10, device=DEFAULT_DEVICE):
    """Identify the currently-playing song via Shazam."""
    try:
        import asyncio
        from shazamio import Shazam
    except ImportError:
        print("song-ID needs shazamio:  python -m pip install shazamio"); return
    print(f"capturing {seconds}s to identify...")
    clip = _capture_tmp(seconds, device)
    async def go():
        out = await Shazam().recognize(clip)
        tr = out.get("track")
        if tr:
            print(f"\n  >> {tr.get('title')} - {tr.get('subtitle')}")
            for s in tr.get("sections", []):
                for m in s.get("metadata", []):
                    print(f"     {m.get('title')}: {m.get('text')}")
        else:
            print("  no match (try a longer clip or a clearer signal)")
    asyncio.run(go())

def transcribe(seconds=None, model="base", live=False, file=None, device=DEFAULT_DEVICE):
    """Transcribe talk radio to text (local, via faster-whisper)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("transcription needs faster-whisper:  python -m pip install faster-whisper"); return
    print(f"loading whisper model '{model}' (first run downloads it)...")
    wm = WhisperModel(model, device="cpu", compute_type="int8")
    def do(path):
        segs, _ = wm.transcribe(path, vad_filter=True)
        for s in segs:
            print(f"  [{int(s.start//60):02d}:{int(s.start%60):02d}] {s.text.strip()}", flush=True)
    if file:
        do(file); return
    if live:
        chunk = seconds or 20
        print(f"live transcription in {chunk}s chunks (Ctrl+C to stop)...\n")
        try:
            while True:
                do(_capture_tmp(chunk, device))
        except KeyboardInterrupt:
            print("\nstopped.")
    else:
        do(_capture_tmp(seconds or 30, device))

# ---------------------------------------------------- scan / seek / engine
def channels(am=False):
    if am:
        return list(range(AM_LO, AM_HI + 1, AM_STEP))
    n = int(round((FM_HI - FM_LO) / FM_STEP)) + 1
    return [round(FM_LO + FM_STEP * i, 1) for i in range(n)]

def measure_rms(seconds=0.6, device=None):
    """Quick mean-volume (dBFS) of the current tuning. Used by scan/seek."""
    p = subprocess.run(["ffmpeg", "-hide_banner"] + audio_input(device) +
                       ["-t", str(seconds), "-af", "volumedetect", "-f", "null", os.devnull],
                       capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+)", p.stderr)
    return float(m.group(1)) if m else -99.0

def scan_band(am=False, progress=None, device=None):
    """Sweep a whole band. Returns (stations, floor) where stations is a list of
    (freq, rms, db_above_floor) sorted strongest-first. progress(i, total, freq, rms)."""
    chans = channels(am)
    results = []
    for i, f in enumerate(chans):
        tune(f, am=am, ack=False)
        rms = measure_rms(0.6, device)
        results.append((f, rms))
        if progress:
            progress(i + 1, len(chans), f, rms)
    floor = sorted(r[1] for r in results)[len(results) // 2]      # median = noise floor
    stations = sorted([(f, rms, rms - floor) for f, rms in results if rms >= floor + 3.0],
                      key=lambda r: -r[2])
    return stations, floor

def seek(from_freq, up=True, am=False, progress=None, device=None, cancel=None):
    """Car-radio seek: step from from_freq to the next station peak, tune it, return it.
    cancel() (optional) is polled each step so a caller can abort. Stops one channel
    past a peak and tunes back to it, so it reliably lands ON the station."""
    chans = channels(am)
    step = 1 if up else -1
    i = min(range(len(chans)), key=lambda k: abs(chans[k] - from_freq))
    floor, prev_f, prev_rms = None, None, -999.0
    for _ in range(len(chans)):
        if cancel and cancel():
            return None
        i = (i + step) % len(chans)
        f = chans[i]
        tune(f, am=am, ack=False)
        rms = measure_rms(0.4, device)
        if progress:
            progress(f, rms)
        floor = rms if floor is None else min(floor, rms)
        # the previous channel was a peak if it sat well above the floor and we've
        # now dropped off it -> that was a station.
        if prev_f is not None and prev_rms >= floor + 5 and prev_rms > -33 and rms < prev_rms - 1:
            tune(prev_f, am=am)
            return prev_f
        prev_f, prev_rms = f, rms
    return None

def engine_cmds(eq=None, vizfile=None, segdir=None, device=None, record_file=None):
    """One capture, fanned out: WAV->stdout (for ffplay), optional viz raw, optional
    transcription segments, optional recording. Returns (ffmpeg_cmd, ffplay_cmd).
    The caller pipes ffmpeg.stdout into ffplay.stdin so a single device read feeds
    playback, visualizer, transcription and recording at once."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"] + audio_input(device)
    cmd += ["-map", "0:a"] + _eq_args(eq) + ["-c:a", "pcm_s16le", "-f", "wav", "pipe:1"]
    if vizfile:                       # small, per-packet-flushed chunks => snappy meter
        cmd += ["-map", "0:a", "-af", "aresample=8000,asetnsamples=n=400:p=0", "-ac", "1",
                "-flush_packets", "1", "-f", "s16le", "-y", vizfile]
    if segdir:
        cmd += ["-map", "0:a", "-ar", "16000", "-ac", "1", "-f", "segment",
                "-segment_time", "4", "-reset_timestamps", "1",
                os.path.join(segdir, "seg_%05d.wav")]
    if record_file:                   # record the clean (pre-EQ) signal to a file
        ext = os.path.splitext(record_file)[1].lstrip(".").lower()
        rc = {"wav": ["-c:a", "pcm_s16le"], "aac": ["-c:a", "aac", "-b:a", "192k"]
              }.get(ext, ["-c:a", "libmp3lame", "-b:a", "192k"])
        cmd += ["-map", "0:a"] + rc + ["-y", record_file]
    ffplay = ["ffplay", "-hide_banner", "-loglevel", "error", "-nodisp",
              "-autoexit", "-fflags", "nobuffer", "-i", "pipe:0"]
    return cmd, ffplay

def prepare(model="base"):
    """One-time: download/cache the Whisper model so live transcription starts fast."""
    print(f"preparing transcription model '{model}' (one-time download)...")
    try:
        from faster_whisper import WhisperModel
        WhisperModel(model, device="cpu", compute_type="int8")
        print("model ready - live transcription will now start quickly.")
    except ImportError:
        print("faster-whisper not installed:  python -m pip install faster-whisper")

# ------------------------------------------------------------- scheduling
# Portable: Windows Task Scheduler (schtasks) / Linux cron. A job is a shark.py
# subcommand string; both back-ends just register that command at a time.
def _pyw():
    exe = sys.executable
    cand = exe.replace("python.exe", "pythonw.exe")
    return cand if os.path.exists(cand) else exe

def _shark_invocation(subcmd):
    return f'"{_pyw()}" "{os.path.join(HERE, "shark.py")}" {subcmd}'

def _schedule_register(name, at, repeat, subcmd, interactive=False):
    if IS_WIN:
        sc = {"once": "ONCE", "hourly": "HOURLY", "daily": "DAILY", "weekly": "WEEKLY",
              "weekdays": "WEEKLY", "weekends": "WEEKLY"}[repeat]
        args = ["schtasks", "/Create", "/F"] + (["/IT"] if interactive else []) + \
               ["/TN", f"radioSHARK\\{name}", "/TR", _shark_invocation(subcmd), "/SC", sc, "/ST", at]
        if repeat == "weekdays": args += ["/D", "MON,TUE,WED,THU,FRI"]
        if repeat == "weekends": args += ["/D", "SAT,SUN"]
        subprocess.run(args)
    else:                                            # Linux: cron
        hh, mm = at.split(":")
        dow = {"daily": "*", "weekly": "0", "weekdays": "1-5", "weekends": "0,6", "hourly": "*"}[repeat]
        sched = f"{mm} * * * *" if repeat == "hourly" else f"{mm} {hh} * * {dow}"
        line = f"{sched} {_shark_invocation(subcmd)}  # radioSHARK:{name}"
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        cur = "\n".join(l for l in cur.splitlines() if f"# radioSHARK:{name}" not in l)
        subprocess.run(["crontab", "-"], input=cur + "\n" + line + "\n", text=True)
    print(f"scheduled '{name}': {repeat} at {at}")

def schedule_add(name, freq, am, seconds, at, repeat, out_dir):
    outdir = (out_dir or HERE)
    sub = f'rec {seconds} --freq {freq}{" --am" if am else ""} --outdir "{outdir}"'
    _schedule_register(name, at, repeat, sub)

def alarm_add(name, freq, am, at, dur, repeat, eq):
    sub = f'listen --freq {freq}{" --am" if am else ""} --seconds {dur}{f" --eq {eq}" if eq else ""}'
    _schedule_register(f"alarm_{name}", at, repeat, sub, interactive=True)
    if IS_WIN:
        print("note: the PC must be on/awake at that time for the alarm to sound.")

def schedule_list():
    if IS_WIN:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-ScheduledTask -TaskPath '\\radioSHARK\\' | Select-Object TaskName,State | Format-Table -AutoSize"])
    else:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        for l in cur.splitlines():
            if "# radioSHARK:" in l: print("  " + l)

def schedule_remove(name):
    if IS_WIN:
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", f"radioSHARK\\{name}"])
    else:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        kept = "\n".join(l for l in cur.splitlines() if f"# radioSHARK:{name}" not in l)
        subprocess.run(["crontab", "-"], input=kept + "\n", text=True)
        print(f"removed '{name}'")

# ------------------------------------------------------------- CLI
def main():
    p = argparse.ArgumentParser(prog="shark", description="radioSHARK control")
    sub = p.add_subparsers(dest="cmd")

    t = sub.add_parser("tune"); t.add_argument("freq", type=float); t.add_argument("--am", action="store_true"); t.add_argument("--japan", action="store_true", help="76-90 MHz low band (likely muted on US units)")
    l = sub.add_parser("listen"); l.add_argument("--eq", choices=EQ_PROFILES); l.add_argument("--device", default=DEFAULT_DEVICE); l.add_argument("--freq", type=float); l.add_argument("--am", action="store_true"); l.add_argument("--seconds", type=int)
    r = sub.add_parser("rec"); r.add_argument("dur", type=int); r.add_argument("--out"); r.add_argument("--outdir"); r.add_argument("--format", dest="fmt")
    r.add_argument("--freq", type=float); r.add_argument("--am", action="store_true"); r.add_argument("--eq", choices=EQ_PROFILES); r.add_argument("--device", default=DEFAULT_DEVICE)
    s = sub.add_parser("scan"); s.add_argument("--am", action="store_true"); s.add_argument("--debug", action="store_true")
    sk = sub.add_parser("seek"); sk.add_argument("--down", action="store_true"); sk.add_argument("--am", action="store_true"); sk.add_argument("--from", dest="frm", type=float)
    sub.add_parser("prepare")
    sub.add_parser("presets")
    pa = sub.add_parser("preset"); pa.add_argument("action", choices=["add", "remove"]); pa.add_argument("name"); pa.add_argument("freq", nargs="?", type=float); pa.add_argument("--am", action="store_true"); pa.add_argument("--label", default="")
    le = sub.add_parser("led"); le.add_argument("--red", choices=["on", "off"]); le.add_argument("--blue", type=int); le.add_argument("--pulse", type=int)
    sub.add_parser("gui")
    ts = sub.add_parser("timeshift"); ts.add_argument("--freq", type=float); ts.add_argument("--am", action="store_true"); ts.add_argument("--buffer-min", type=int, default=60, dest="buffer_min")
    st = sub.add_parser("stream"); st.add_argument("--freq", type=float); st.add_argument("--am", action="store_true"); st.add_argument("--port", type=int, default=8000); st.add_argument("--format", dest="fmt", default="mp3", choices=["mp3","aac"]); st.add_argument("--bitrate", default="192k"); st.add_argument("--icecast")
    lg = sub.add_parser("log"); lg.add_argument("--freq", type=float); lg.add_argument("--am", action="store_true"); lg.add_argument("--segment", type=int, default=3600); lg.add_argument("--format", dest="fmt", default="mp3", choices=["mp3","aac","wav"]); lg.add_argument("--dir")
    si = sub.add_parser("songid"); si.add_argument("--seconds", type=int, default=10)
    tx = sub.add_parser("transcribe"); tx.add_argument("--seconds", type=int); tx.add_argument("--model", default="base"); tx.add_argument("--live", action="store_true"); tx.add_argument("--file")
    sch = sub.add_parser("schedule"); ssub = sch.add_subparsers(dest="saction")
    sad = ssub.add_parser("add"); sad.add_argument("name"); sad.add_argument("--freq", type=float); sad.add_argument("--preset"); sad.add_argument("--am", action="store_true"); sad.add_argument("--at", required=True); sad.add_argument("--dur", type=int, default=3600); sad.add_argument("--repeat", default="daily", choices=["once","hourly","daily","weekly","weekdays","weekends"]); sad.add_argument("--out-dir")
    ssub.add_parser("list"); srm = ssub.add_parser("remove"); srm.add_argument("name")
    alm = sub.add_parser("alarm"); asub = alm.add_subparsers(dest="aaction")
    aad = asub.add_parser("add"); aad.add_argument("name"); aad.add_argument("--freq", type=float); aad.add_argument("--preset"); aad.add_argument("--am", action="store_true"); aad.add_argument("--at", required=True); aad.add_argument("--dur", type=int, default=1800); aad.add_argument("--repeat", default="daily", choices=["once","daily","weekly","weekdays","weekends"]); aad.add_argument("--eq", choices=EQ_PROFILES)
    asub.add_parser("list"); arm = asub.add_parser("remove"); arm.add_argument("name")

    # shorthand: `shark.py 88.5` or `shark.py wnpr`
    if len(sys.argv) >= 2 and sys.argv[1] not in [a for a in sub.choices]:
        arg = sys.argv[1]
        try:
            tune(float(arg), am=("--am" in sys.argv)); return
        except ValueError:
            pr = load_presets()
            if arg in pr:
                tune(pr[arg]["freq"], am=pr[arg].get("am", False)); return
            print(f"unknown station/preset: {arg}"); return

    a = p.parse_args()
    if a.cmd == "gui":
        import shark_gui; shark_gui.main()
    elif a.cmd == "tune":
        tune(a.freq, am=a.am, lowband=a.japan)
    elif a.cmd == "listen":
        listen(eq=a.eq, device=a.device, seconds=a.seconds, freq=a.freq, am=a.am)
    elif a.cmd == "timeshift":
        timeshift(freq=a.freq, am=a.am, buffer_min=a.buffer_min)
    elif a.cmd == "stream":
        stream(freq=a.freq, am=a.am, port=a.port, fmt=a.fmt, bitrate=a.bitrate, icecast=a.icecast)
    elif a.cmd == "log":
        log_station(freq=a.freq, am=a.am, segment=a.segment, fmt=a.fmt, out_dir=a.dir)
    elif a.cmd == "songid":
        songid(seconds=a.seconds)
    elif a.cmd == "transcribe":
        transcribe(seconds=a.seconds, model=a.model, live=a.live, file=a.file)
    elif a.cmd == "alarm":
        if a.aaction == "add":
            freq, am = a.freq, a.am
            if a.preset:
                pr = load_presets(); freq = pr[a.preset]["freq"]; am = pr[a.preset].get("am", False)
            alarm_add(a.name, freq, am, a.at, a.dur, a.repeat, a.eq)
        elif a.aaction == "list":
            schedule_list()
        elif a.aaction == "remove":
            schedule_remove(f"alarm_{a.name}")
    elif a.cmd == "rec":
        if a.freq is not None: tune(a.freq, am=a.am)
        record(a.dur, out=a.out, fmt=a.fmt, eq=a.eq, device=a.device, outdir=a.outdir)
    elif a.cmd == "scan":
        scan(am=a.am, debug=a.debug)
    elif a.cmd == "seek":
        frm = a.frm if a.frm is not None else (1000 if a.am else 98.0)
        found = seek(frm, up=not a.down, am=a.am,
                     progress=lambda f, r: print(f"\r  seeking... {f}    ", end="", flush=True))
        print(f"\r{' ' * 30}\r" + (f"found station at {found}" if found else "no station found"))
    elif a.cmd == "prepare":
        prepare()
    elif a.cmd == "presets":
        pr = load_presets()
        if not pr: print("no presets yet. add one: shark.py preset add <name> <freq>")
        for n, v in pr.items():
            print(f"  {n:12} {v['freq']}{' AM' if v.get('am') else ' FM'}  {v.get('label','')}")
    elif a.cmd == "preset":
        pr = load_presets()
        if a.action == "add":
            pr[a.name] = {"freq": a.freq, "am": a.am, "label": a.label}; save_presets(pr); print(f"saved preset '{a.name}'")
        else:
            pr.pop(a.name, None); save_presets(pr); print(f"removed preset '{a.name}'")
    elif a.cmd == "led":
        set_led(red={"on": True, "off": False}.get(a.red), blue=a.blue, pulse=a.pulse)
    elif a.cmd == "schedule":
        if a.saction == "add":
            freq, am = a.freq, a.am
            if a.preset:
                pr = load_presets(); freq = pr[a.preset]["freq"]; am = pr[a.preset].get("am", False)
            schedule_add(a.name, freq, am, a.dur, a.at, a.repeat, a.out_dir)
        elif a.saction == "list":
            schedule_list()
        elif a.saction == "remove":
            schedule_remove(a.name)
    else:
        p.print_help()

if __name__ == "__main__":
    main()
