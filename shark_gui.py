#!/usr/bin/env python3
"""shark_gui.py - cross-platform Tkinter GUI for the radioSHARK.

Same engine as the CLI (imports shark.py); runs unchanged on Windows ("win32gui")
and Linux ("lingui"). Built on shark.py's fan-out audio engine: ONE capture feeds
speakers + recording + transcription + visualizer at once, so they all coexist on
the single-consumer USB capture device.

Run:  python shark_gui.py      (or:  python shark.py gui)
"""
import os, sys, glob, time, threading, subprocess, re
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
import shark

NO_WINDOW = subprocess.CREATE_NO_WINDOW if shark.IS_WIN else 0
PY = [sys.executable, os.path.join(shark.HERE, "shark.py")]
SEG_DIR = os.path.join(shark.HERE, "_seg")
VIZ_FILE = os.path.join(shark.HERE, "_viz.raw")


def _popen(cmd, **kw):
    return subprocess.Popen(cmd, creationflags=NO_WINDOW, **kw)


class SharkGUI:
    def __init__(self, root):
        self.root = root
        root.title("radioSHARK")
        root.minsize(620, 660)
        self.engine = None         # ffmpeg (shared capture)
        self.player = None         # ffplay (playback)
        self.media = None          # exclusive process (Tools record/stream/log)
        self.ts_procs = []         # timeshift
        self.listen_on = False
        self.tx_on = False
        self.recording = False
        self.record_file = None
        self.tx_stop = None
        self.model = None
        self.karaoke = None
        self.viz_running = False
        self._ref = 1.0
        self._seeking = False
        self.seek_cancel = threading.Event()

        self.am = tk.BooleanVar(value=False)
        self.fm_freq, self.am_freq = 96.3, 1000
        self.freq = tk.DoubleVar(value=self.fm_freq)
        self.eq_on = tk.BooleanVar(value=False)
        self.eq = tk.StringVar(value="flat")
        self.eq_b, self.eq_m, self.eq_t = tk.IntVar(value=0), tk.IntVar(value=0), tk.IntVar(value=0)

        nb = ttk.Notebook(root); nb.pack(fill="both", expand=True, padx=6, pady=6)
        self._tab_radio(nb)
        self._tab_tools(nb)

        self.status = tk.StringVar(value="ready")
        bar = ttk.Frame(root); bar.pack(fill="x", side="bottom")
        ttk.Separator(bar).pack(fill="x")
        ttk.Label(bar, textvariable=self.status, anchor="w", padding=4).pack(fill="x")
        self.refresh_presets()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ----------------------------------------------------------------- util
    def say(self, m): self.status.set(m)
    def _engine_alive(self): return self.engine and self.engine.poll() is None

    def sync_buttons(self):
        self.listen_btn.config(text="■ Stop" if (self.listen_on and self._engine_alive()) else "▶ Listen")
        self.rec_btn.config(text="■ Stop Rec" if self.recording else "● Record")
        self.tx_btn.config(text="Stop" if self.tx_on else "Transcribe")

    # ----------------------------------------------------------- Radio tab
    def _tab_radio(self, nb):
        f = ttk.Frame(nb, padding=10); nb.add(f, text="Radio")

        tun = ttk.LabelFrame(f, text="Tuner", padding=8); tun.pack(fill="x")
        ttk.Button(tun, text="◀◀ Seek", command=lambda: self.do_seek(False)).grid(row=0, column=0)
        ttk.Button(tun, text="◀", width=3, command=lambda: self.step(-1)).grid(row=0, column=1)
        ttk.Entry(tun, textvariable=self.freq, width=9, justify="center",
                  font=("Segoe UI", 16)).grid(row=0, column=2, padx=3)
        ttk.Button(tun, text="▶", width=3, command=lambda: self.step(1)).grid(row=0, column=3)
        ttk.Button(tun, text="Seek ▶▶", command=lambda: self.do_seek(True)).grid(row=0, column=4)
        ttk.Button(tun, text="Tune", command=self.do_tune).grid(row=0, column=5, padx=6)
        ttk.Checkbutton(tun, text="AM", variable=self.am, command=self.toggle_band).grid(row=0, column=6)

        ctl = ttk.Frame(f); ctl.pack(fill="x", pady=6)
        self.listen_btn = ttk.Button(ctl, text="▶ Listen", command=self.toggle_listen); self.listen_btn.pack(side="left")
        self.rec_btn = ttk.Button(ctl, text="● Record", command=self.toggle_record); self.rec_btn.pack(side="left", padx=6)
        self.tx_btn = ttk.Button(ctl, text="Transcribe", command=self.toggle_transcribe); self.tx_btn.pack(side="left")
        ttk.Button(ctl, text="Song ID", command=self.do_songid).pack(side="left", padx=6)

        eqf = ttk.LabelFrame(f, text="Equalizer", padding=8); eqf.pack(fill="x")
        ttk.Checkbutton(eqf, text="EQ on", variable=self.eq_on, command=self.apply_eq).grid(row=0, column=0)
        ttk.Combobox(eqf, textvariable=self.eq, width=8, state="readonly",
                     values=list(shark.EQ_PROFILES) + ["custom"]).grid(row=0, column=1, padx=6)
        for i, (lbl, var) in enumerate([("bass", self.eq_b), ("mid", self.eq_m), ("treble", self.eq_t)]):
            ttk.Label(eqf, text=lbl).grid(row=0, column=2 + i * 2)
            ttk.Scale(eqf, from_=-10, to=10, variable=var, length=66).grid(row=0, column=3 + i * 2)
        ttk.Button(eqf, text="Apply", command=self.apply_eq).grid(row=0, column=8, padx=6)

        vf = ttk.LabelFrame(f, text="Visualizer", padding=4); vf.pack(fill="x", pady=6)
        self.viz = tk.Canvas(vf, height=90, bg="#0a0a10", highlightthickness=0); self.viz.pack(fill="x")

        sf = ttk.Frame(f); sf.pack(fill="x")
        ttk.Label(sf, text="Now playing:").pack(side="left")
        self.song_lbl = ttk.Label(sf, text="—  (press Song ID)", wraplength=460); self.song_lbl.pack(side="left", padx=6)

        pf = ttk.LabelFrame(f, text="Presets", padding=8); pf.pack(fill="both", expand=True, pady=6)
        self.presets = tk.Listbox(pf, height=6); self.presets.pack(side="left", fill="both", expand=True)
        self.presets.bind("<Double-Button-1>", lambda e: self.load_preset())
        pb = ttk.Frame(pf); pb.pack(side="left", fill="y", padx=6)
        ttk.Button(pb, text="Tune", command=self.load_preset).pack(fill="x")
        ttk.Button(pb, text="Save current…", command=self.save_preset).pack(fill="x", pady=4)
        ttk.Button(pb, text="Delete", command=self.del_preset).pack(fill="x")

    # ------------------------------------------------------------- Tools tab
    def _tab_tools(self, nb):
        outer = ttk.Frame(nb); nb.add(outer, text="Tools")
        canvas = tk.Canvas(outer, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        f = ttk.Frame(canvas, padding=(10, 10, 16, 10))
        f.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=f, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))  # fit width
        wheel = lambda e: canvas.yview_scroll(int(-e.delta / 120), "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        # Scan
        sc = ttk.LabelFrame(f, text="Station scan", padding=8); sc.pack(fill="x")
        top = ttk.Frame(sc); top.pack(fill="x")
        ttk.Button(top, text="Scan FM", command=lambda: self.do_scan(False)).pack(side="left")
        ttk.Button(top, text="Scan AM", command=lambda: self.do_scan(True)).pack(side="left", padx=4)
        self.scan_debug = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="debug", variable=self.scan_debug).pack(side="left", padx=8)
        self.scan_prog = ttk.Progressbar(sc, mode="determinate"); self.scan_prog.pack(fill="x", pady=4)
        self.scan_lbl = ttk.Label(sc, text=""); self.scan_lbl.pack(anchor="w")
        self.scan_list = tk.Listbox(sc, height=7, font=("Consolas", 10)); self.scan_list.pack(fill="x", pady=4)
        self.scan_list.bind("<Double-Button-1>", self.tune_from_scan); self._scan_am = False

        # LED
        led = ttk.LabelFrame(f, text="LED", padding=8); led.pack(fill="x", pady=8)
        ttk.Button(led, text="Red", command=lambda: shark.set_led(red=True)).pack(side="left")
        ttk.Button(led, text="Blue", command=lambda: shark.set_led(red=False, blue=110)).pack(side="left", padx=4)
        ttk.Button(led, text="Purple", command=lambda: shark.set_led(red=True, blue=90)).pack(side="left")
        ttk.Button(led, text="Pulse", command=lambda: shark.set_led(pulse=96)).pack(side="left", padx=4)
        ttk.Button(led, text="Off", command=lambda: shark.set_led(red=False, blue=0)).pack(side="left")
        ttk.Scale(led, from_=0, to=127, command=lambda v: shark.set_led(blue=int(float(v)))
                  ).pack(side="left", fill="x", expand=True, padx=8)

        # Capture
        cap = ttk.LabelFrame(f, text="Record / Stream / Log", padding=8); cap.pack(fill="x")
        self.rec_dur = tk.IntVar(value=60); self.rec_fmt = tk.StringVar(value="mp3")
        ttk.Label(cap, text="record secs").grid(row=0, column=0); ttk.Entry(cap, textvariable=self.rec_dur, width=7).grid(row=0, column=1, padx=3)
        ttk.Combobox(cap, textvariable=self.rec_fmt, values=["mp3", "wav", "aac"], width=5, state="readonly").grid(row=0, column=2)
        ttk.Button(cap, text="Record (timed)", command=self.do_record).grid(row=0, column=3, padx=6)
        self.ts_min = tk.IntVar(value=60)
        ttk.Label(cap, text="timeshift min").grid(row=1, column=0, pady=4); ttk.Entry(cap, textvariable=self.ts_min, width=7).grid(row=1, column=1)
        self.ts_btn = ttk.Button(cap, text="Timeshift", command=self.toggle_timeshift); self.ts_btn.grid(row=1, column=3, padx=6)
        self.stream_port = tk.IntVar(value=8000)
        ttk.Label(cap, text="stream port").grid(row=2, column=0); ttk.Entry(cap, textvariable=self.stream_port, width=7).grid(row=2, column=1)
        self.stream_btn = ttk.Button(cap, text="Stream", command=self.toggle_stream); self.stream_btn.grid(row=2, column=3, padx=6)
        self.log_seg = tk.IntVar(value=3600)
        ttk.Label(cap, text="log seg secs").grid(row=3, column=0, pady=4); ttk.Entry(cap, textvariable=self.log_seg, width=7).grid(row=3, column=1)
        self.log_btn = ttk.Button(cap, text="24/7 Log", command=self.toggle_log); self.log_btn.grid(row=3, column=3, padx=6)

        # Schedule
        for kind in ("Recording", "Alarm"):
            lf = ttk.LabelFrame(f, text=f"Schedule {kind.lower()}", padding=8); lf.pack(fill="x", pady=4)
            name = tk.StringVar(); at = tk.StringVar(value="07:00")
            dur = tk.IntVar(value=1800 if kind == "Alarm" else 3600); rep = tk.StringVar(value="daily")
            ttk.Label(lf, text="name").grid(row=0, column=0); ttk.Entry(lf, textvariable=name, width=9).grid(row=0, column=1)
            ttk.Label(lf, text="at").grid(row=0, column=2); ttk.Entry(lf, textvariable=at, width=6).grid(row=0, column=3)
            ttk.Label(lf, text="dur").grid(row=0, column=4); ttk.Entry(lf, textvariable=dur, width=6).grid(row=0, column=5)
            ttk.Combobox(lf, textvariable=rep, values=["daily", "weekdays", "weekends", "weekly", "once"],
                         width=8, state="readonly").grid(row=0, column=6, padx=4)
            ttk.Button(lf, text="Add", command=lambda k=kind, n=name, a=at, d=dur, r=rep:
                       self.add_schedule(k, n.get(), a.get(), d.get(), r.get())).grid(row=0, column=7, padx=4)
        slf = ttk.LabelFrame(f, text="Scheduled jobs", padding=8); slf.pack(fill="x", pady=4)
        ttk.Button(slf, text="Refresh", command=self.list_schedule).pack(anchor="w")
        self.sched_text = tk.Text(slf, height=5); self.sched_text.pack(fill="x", pady=4)

    # ------------------------------------------------------------- tuning
    def step(self, d):
        self.freq.set(round(self.freq.get() + d * (shark.AM_STEP if self.am.get() else shark.FM_STEP), 1))
        self.do_tune()

    def toggle_band(self):
        if self.am.get():
            self.fm_freq = self.freq.get(); self.freq.set(self.am_freq)
        else:
            self.am_freq = self.freq.get(); self.freq.set(self.fm_freq)
        self.do_tune()

    def do_tune(self):
        if self._seeking:                 # a manual tune interrupts an in-progress seek
            self.seek_cancel.set()
        try:
            shark.tune(self.freq.get(), am=self.am.get())
            if self.am.get(): self.am_freq = self.freq.get()
            else: self.fm_freq = self.freq.get()
            # auto-play: tuning should produce sound (unless busy seeking or with an
            # exclusive capture like timed-record/stream/log running)
            if not self._seeking and not self.media and not self.ts_procs:
                self.listen_on = True; self.ensure_engine(); self.sync_buttons()
            self.say(f"tuned {self.freq.get()} {'kHz AM' if self.am.get() else 'MHz FM'}")
        except Exception as e:
            self.say(f"tune error: {e}")

    def do_seek(self, up):
        if self._seeking:                 # clicking seek again cancels the current one
            self.seek_cancel.set(); self.say("seek cancelled"); return
        self.stop_all()
        self.seek_cancel = threading.Event(); self._seeking = True
        self.say("seeking…  (click Seek again to stop)")
        def run():
            found = shark.seek(self.freq.get(), up=up, am=self.am.get(),
                               progress=lambda fr, r: self.root.after(0, lambda: self.freq.set(fr)),
                               cancel=self.seek_cancel.is_set)
            def done():
                self._seeking = False
                if found:
                    self.freq.set(found)
                    self.listen_on = True; self.ensure_engine()   # play the found station
                    self.say(f"found {found}")
                else:
                    self.say("seek stopped")
                self.sync_buttons()
            self.root.after(0, done)
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------- engine
    def current_eq(self):
        if not self.eq_on.get():
            return None
        prof = self.eq.get()
        if prof == "custom":
            shark.EQ_PROFILES["custom"] = (f"bass=g={self.eq_b.get()},"
                                           f"equalizer=f=1000:t=q:w=1:g={self.eq_m.get()},"
                                           f"treble=g={self.eq_t.get()}")
        return prof

    def _spawn_engine(self):
        os.makedirs(SEG_DIR, exist_ok=True)
        for s in glob.glob(os.path.join(SEG_DIR, "*.wav")):
            try: os.remove(s)
            except OSError: pass
        rec = self.record_file if self.recording else None
        ff, play = shark.engine_cmds(eq=self.current_eq(), vizfile=VIZ_FILE, segdir=SEG_DIR, record_file=rec)
        self.engine = _popen(ff, stdout=subprocess.PIPE)
        self.player = _popen(play, stdin=self.engine.stdout)
        self.engine.stdout.close()
        self.start_viz()

    def _kill_engine_procs(self):
        self.stop_viz()
        for p in (self.player, self.engine):
            if p and p.poll() is None:
                try: p.terminate()
                except Exception: pass
        self.engine = self.player = None

    def ensure_engine(self):
        if not self._engine_alive():
            self._spawn_engine()

    def _restart_engine(self):
        self._kill_engine_procs(); self._spawn_engine()

    def stop_engine(self):
        self.stop_transcribe_worker(); self._kill_engine_procs()
        self.listen_on = False

    def stop_media(self):
        for p in [self.media] + self.ts_procs:
            if p and p.poll() is None:
                try: p.terminate()
                except Exception: pass
        self.media = None; self.ts_procs = []
        if getattr(self, "ts_btn", None): self.ts_btn.config(text="Timeshift")
        if getattr(self, "stream_btn", None): self.stream_btn.config(text="Stream")
        if getattr(self, "log_btn", None): self.log_btn.config(text="24/7 Log")

    def stop_all(self):
        self.tx_on = False; self.recording = False; self.record_file = None
        self.stop_engine(); self.stop_media(); self.sync_buttons()

    def apply_eq(self):
        if self._engine_alive():
            self._restart_engine()
        self.say("EQ " + ("on: " + self.eq.get() if self.eq_on.get() else "off"))

    def toggle_listen(self):
        if self.listen_on:
            self.listen_on = False
            if not (self.tx_on or self.recording):
                self.stop_engine()
            self.say("stopped")
        else:
            self.stop_media(); self.listen_on = True; self.ensure_engine(); self.say("listening")
        self.sync_buttons()

    def toggle_record(self):
        if self.recording:
            self.recording = False; shark.set_led(red=False, blue=64)
            saved = self.record_file; self.record_file = None
            if self.listen_on or self.tx_on:
                self._restart_engine()          # drop the record output, keep playing
            else:
                self.stop_engine()
            self.say(f"saved {os.path.basename(saved)}" if saved else "stopped")
        else:
            self.stop_media()
            self.record_file = os.path.join(shark.HERE, f"radioSHARK-{time.strftime('%Y%m%d-%H%M%S')}.mp3")
            self.recording = True; shark.set_led(red=True)
            self._restart_engine() if self._engine_alive() else self.ensure_engine()
            self.say(f"recording -> {os.path.basename(self.record_file)}")
        self.sync_buttons()

    # ------------------------------------------------------------- viz
    def start_viz(self):
        if not self.viz_running:
            self.viz_running = True; self._viz_tick()

    def stop_viz(self):
        self.viz_running = False
        try: self.viz.delete("all")
        except Exception: pass

    def _viz_tick(self):
        if not self.viz_running:
            return
        try:
            import numpy as np
            with open(VIZ_FILE, "rb") as fh:
                fh.seek(0, 2); sz = fh.tell(); fh.seek(max(0, sz - 2048)); raw = fh.read()
            data = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            if len(data) >= 256:
                w = data[-512:] * np.hanning(len(data[-512:]))
                spec = np.abs(np.fft.rfft(w))[1:]
                bars = 36
                mags = np.sqrt(np.array([g.mean() for g in np.array_split(spec[:170], bars)]))
                ref = max(mags.max(), self._ref * 0.90, 1e-3)   # decaying auto-gain
                self._ref = ref
                vals = np.clip(mags / ref, 0, 1)
                W = self.viz.winfo_width() or 600; H = 90; bw = W / bars
                self.viz.delete("all")
                for i, v in enumerate(vals):
                    x = i * bw; h = v * H
                    self.viz.create_rectangle(x + 1, H - h, x + bw - 1, H, fill="#27d0a8", outline="")
        except Exception:
            pass
        self.root.after(33, self._viz_tick)

    # ------------------------------------------------------- transcription
    def open_karaoke(self):
        if self.karaoke and self.karaoke.winfo_exists():
            self.karaoke.lift(); return
        self.karaoke = tk.Toplevel(self.root)
        self.karaoke.title("radioSHARK — Transcript"); self.karaoke.geometry("540x320")
        self.k_text = tk.Text(self.karaoke, wrap="word", font=("Segoe UI", 17),
                              bg="#101014", fg="#e8e8ea", padx=14, pady=14, spacing3=4)
        self.k_text.pack(fill="both", expand=True)
        self.karaoke.protocol("WM_DELETE_WINDOW", self.close_karaoke)

    def _k_add(self, text):
        def add():
            if self.karaoke and self.karaoke.winfo_exists():
                self.k_text.insert("end", text); self.k_text.see("end")
        self.root.after(0, add)

    def close_karaoke(self):
        self.tx_on = False; self.stop_transcribe_worker()
        if self.karaoke:
            try: self.karaoke.destroy()
            except Exception: pass
            self.karaoke = None
        if not (self.listen_on or self.recording):
            self.stop_engine()
        self.sync_buttons(); self.say("transcription stopped")

    def toggle_transcribe(self):
        if self.tx_on:
            self.close_karaoke(); return
        self.stop_media(); self.tx_on = True
        self.ensure_engine(); self.open_karaoke()
        self.tx_stop = threading.Event()
        threading.Thread(target=self._tx_worker, daemon=True).start()
        self.sync_buttons(); self.say("transcribing live…")

    def stop_transcribe_worker(self):
        if self.tx_stop:
            self.tx_stop.set()

    def _tx_worker(self):
        self.root.after(0, lambda: self.k_text.delete("1.0", "end"))
        self._k_add("[loading speech model…]\n")
        try:
            from faster_whisper import WhisperModel
            if self.model is None:
                self.model = WhisperModel("base", device="cpu", compute_type="int8")
        except Exception as e:
            self._k_add(f"\n[transcription unavailable: {e}]\n"); return
        self._k_add("[listening — reads along with the audio]\n\n")
        while not self.tx_stop.is_set():
            files = sorted(glob.glob(os.path.join(SEG_DIR, "seg_*.wav")))
            if len(files) >= 2:
                seg = files[0]
                try:
                    segs, _ = self.model.transcribe(seg, vad_filter=True, language="en")
                    text = " ".join(s.text.strip() for s in segs).strip()
                    if text: self._k_add(text + " ")
                except Exception:
                    pass
                try: os.remove(seg)
                except OSError: pass
            else:
                time.sleep(0.3)

    # ------------------------------------------------- Tools: record/stream/log/timeshift
    def do_record(self):
        self.stop_all()
        cmd, out = shark.record_cmd(self.rec_dur.get(), fmt=self.rec_fmt.get(), eq=self.current_eq())
        shark.set_led(red=True); self.media = _popen(cmd)
        self.say(f"recording {self.rec_dur.get()}s -> {os.path.basename(out)}")
        def watch():
            self.media.wait(); shark.set_led(red=False, blue=64)
            self.root.after(0, lambda: self.say(f"saved {os.path.basename(out)}"))
        threading.Thread(target=watch, daemon=True).start()

    def toggle_timeshift(self):
        if self.ts_procs:
            self.stop_media(); self.say("timeshift stopped"); return
        self.stop_all()
        cmd, m3u8 = shark.timeshift_recorder_cmd(self.ts_min.get())
        self.ts_procs = [_popen(cmd)]; self.ts_btn.config(text="Stop TS"); self.say("buffering timeshift…")
        def launch():
            for _ in range(40):
                try:
                    if os.path.exists(m3u8) and sum(1 for _ in open(m3u8)) > 6: break
                except OSError: pass
                time.sleep(0.25)
            self.ts_procs.append(_popen(["ffplay", "-hide_banner", "-nodisp", "-autoexit", m3u8]))
            self.root.after(0, lambda: self.say("timeshift live — player window: SPACE pause, arrows seek"))
        threading.Thread(target=launch, daemon=True).start()

    def toggle_stream(self):
        if self.media and self.media.poll() is None:
            self.stop_media(); self.say("stream stopped"); return
        self.stop_all()
        cmd, info = shark.stream_cmd(port=self.stream_port.get())
        self.media = _popen(cmd); self.stream_btn.config(text="Stop"); self.say(f"streaming: {info}")

    def toggle_log(self):
        if self.media and self.media.poll() is None:
            self.stop_media(); self.say("logging stopped"); return
        self.stop_all()
        cmd, d = shark.log_cmd(segment=self.log_seg.get())
        self.media = _popen(cmd); self.log_btn.config(text="Stop"); self.say(f"logging to {d}")

    # ------------------------------------------------------------- scan
    def do_scan(self, am):
        self.stop_all(); self._scan_am = am
        self.scan_list.delete(0, "end"); self.scan_prog["value"] = 0
        unit = "kHz" if am else "MHz"
        def prog(i, total, fr, rms):
            self.root.after(0, lambda: (self.scan_prog.configure(maximum=total, value=i),
                                        self.scan_lbl.config(text=f"scanning {fr} {unit}  ({i}/{total})")))
        def run():
            stations, floor = shark.scan_band(am=am, progress=prog)
            self.root.after(0, lambda: self._scan_done(stations, floor, unit))
        threading.Thread(target=run, daemon=True).start()

    def _scan_done(self, stations, floor, unit):
        self.scan_lbl.config(text=f"done — {len(stations)} stations  (floor {floor:.0f} dB)")
        self.scan_list.delete(0, "end")
        if not stations:
            self.scan_list.insert("end", "no clear stations — try the antenna")
        for fr, rms, lift in stations:
            bars = "█" * min(12, max(1, int(lift)))
            dbg = f"   rms {rms:.0f} +{lift:.0f}" if self.scan_debug.get() else ""
            self.scan_list.insert("end", f"  {fr:>7} {unit}   {bars}{dbg}")
        self.say(f"scan done: {len(stations)} stations")

    def tune_from_scan(self, _e):
        sel = self.scan_list.get(self.scan_list.curselection() or 0)
        m = re.search(r"([\d.]+)\s*(kHz|MHz)", sel)
        if m:
            self.am.set(self._scan_am); self.freq.set(float(m.group(1))); self.do_tune()

    # ------------------------------------------------------------- song id
    def do_songid(self):
        self.song_lbl.config(text="identifying…"); self.say("identifying")
        def run():
            clip = self._songid_clip()
            if not clip:
                self.root.after(0, lambda: self.song_lbl.config(text="press Listen first, then Song ID")); return
            try:
                import asyncio
                from shazamio import Shazam
                out = asyncio.run(Shazam().recognize(clip))
                tr = out.get("track")
                res = f"{tr['title']} — {tr['subtitle']}" if tr else "no match"
            except Exception as e:
                res = f"song-ID error: {e}"
            self.root.after(0, lambda: (self.song_lbl.config(text=res), self.say("song id done")))
        threading.Thread(target=run, daemon=True).start()

    def _songid_clip(self):
        if self._engine_alive():
            files = sorted(glob.glob(os.path.join(SEG_DIR, "seg_*.wav")))[:-1][-3:]
            if not files:
                time.sleep(2); files = sorted(glob.glob(os.path.join(SEG_DIR, "seg_*.wav")))[:-1][-3:]
            if not files:
                return None
            clip = os.path.join(shark.HERE, "_songclip.wav")
            inputs = []
            for fp in files: inputs += ["-i", fp]
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + inputs +
                           ["-filter_complex", f"concat=n={len(files)}:v=0:a=1", clip], creationflags=NO_WINDOW)
            return clip
        return shark._capture_tmp(10)

    # ------------------------------------------------------------- presets
    def refresh_presets(self):
        self.presets.delete(0, "end"); self._pr = shark.load_presets()
        for n, v in self._pr.items():
            self.presets.insert("end", f"{n}  -  {v['freq']} {'AM' if v.get('am') else 'FM'}")

    def _sel_preset(self):
        cur = self.presets.curselection()
        return list(self._pr)[cur[0]] if cur else None

    def load_preset(self):
        n = self._sel_preset()
        if n:
            self.am.set(self._pr[n].get("am", False)); self.freq.set(self._pr[n]["freq"]); self.do_tune()

    def save_preset(self):
        name = simpledialog.askstring("Save preset", "Preset name:")
        if name:
            shark.save_presets({**shark.load_presets(),
                                name: {"freq": self.freq.get(), "am": self.am.get(), "label": ""}})
            self.refresh_presets(); self.say(f"saved '{name}'")

    def del_preset(self):
        n = self._sel_preset()
        if n:
            pr = shark.load_presets(); pr.pop(n, None); shark.save_presets(pr)
            self.refresh_presets(); self.say(f"removed '{n}'")

    # ------------------------------------------------------------- schedule
    def add_schedule(self, kind, name, at, dur, rep):
        if not name:
            messagebox.showwarning("radioSHARK", "enter a name"); return
        if kind == "Alarm":
            shark.alarm_add(name, self.freq.get(), self.am.get(), at, dur, rep, self.current_eq())
        else:
            shark.schedule_add(name, self.freq.get(), self.am.get(), dur, at, rep, None)
        self.say(f"{kind.lower()} '{name}' scheduled"); self.list_schedule()

    def list_schedule(self):
        out = subprocess.run(PY + ["schedule", "list"], capture_output=True, text=True,
                             creationflags=NO_WINDOW).stdout
        self.sched_text.delete("1.0", "end"); self.sched_text.insert("end", out or "(none)")

    def on_close(self):
        self.stop_all(); self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if shark.IS_WIN else "clam")
    except Exception:
        pass
    SharkGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
