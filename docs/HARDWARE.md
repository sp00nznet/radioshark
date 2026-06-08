# How the radioSHARK works (and how this app drives it)

## The device

The Griffin radioSHARK (USB `077d:627a`, the v1 fin) is **two USB devices in one**:

1. A **USB HID** interface — receives tuning commands and drives the LEDs.
2. A **USB Audio** capture interface — the demodulated AM/FM audio arrives at the
   PC as a standard *recording* input (no vendor driver needed).

Everything this app does follows from that: **tune over HID → handle the audio
that shows up as a USB capture device.**

The 3.5 mm jack on the fin is a **combination headphone-out / antenna** — you
never feed audio *into* it. A wire or pair of earbuds plugged in acts as an FM
antenna and noticeably lifts weak stations out of the noise.

**LEDs:** the fins glow **blue** when powered and **red** when recording. There's
no dedicated purple LED — but red and blue can be driven together for a purple
glow (the GUI's "Purple" button), which is also why the idle blue reads as
"blue-purple" through the translucent housing.

## The HID protocol

Reconstructed from the Linux kernel drivers `drivers/media/radio/radio-shark.c`
and `tea575x.c` (Hans de Goede). Commands are 6-byte HID reports (hidapi wants a
leading `0x00` report-id byte on Windows):

* **Tune** — command byte `0xc0`, then the 25-bit TEA575x register value packed
  big-endian into bytes 0–3.
  * FM: `pll = (freq_kHz + 10700) * 10 / 125`  (10.7 MHz IF)
  * AM (MW band bit `1<<20`): `pll = freq_kHz + 450`
* **LEDs** — blue `0xA0` + level (0–127), blue-pulse `0xA1` + (256−level),
  red on `0xA9` / off `0xA8`.

It is **not** a true SDR — the TEA5757 chip only demodulates broadcast AM/FM to
audio (no raw IQ). For wideband SDR, get an RTL-SDR. The 76–90 MHz "Japan" band
tunes but this US unit's RF front-end mutes it.

## The fan-out audio engine

Only one process can open the USB capture device at a time, so a single ffmpeg
capture is teed to everything at once:

```
                       ┌─ WAV  → ffplay         (speakers)
USB capture → ffmpeg ──┼─ 8 kHz raw → viz.raw   (visualizer reads the tail live)
                       ├─ 16 kHz segments       (live transcription)
                       └─ mp3/aac               (recording, optional)
```

This is why you can listen, record, transcribe and watch the visualizer
simultaneously. Low-latency playback comes from `ffplay -analyzeduration 0
-probesize 4096 -flags low_delay -fflags nobuffer` plus per-packet flushing on
the pipe; without those, ffplay spends ~5 seconds analyzing the stream first.

## Portability / Linux

All OS-specific behavior lives behind a thin **platform seam** in `shark.py`:

* `IS_WIN`, `default_device()`, `audio_input()` — DirectShow on Windows, ALSA on
  Linux.
* scheduling branches between `schtasks` and `cron`.
* tuning and LEDs use HID via `hidapi`, which is cross-platform.

The CLI and GUI both build their processes from the same command builders
(`engine_cmds`, `listen_cmd`, `record_cmd`, `stream_cmd`, `log_cmd`,
`timeshift_recorder_cmd`), so the two front-ends stay feature-identical.

To run on Linux: the mainline kernel already includes the `radio-shark` driver
(V4L2 tuning + a standard ALSA USB-audio capture). Set `RADIOSHARK_ALSA` to the
shark's ALSA capture device (e.g. `plughw:CARD=radioSHARK`), install ffmpeg, and
use the same commands. Tuning may use `hidapi` directly or `v4l2-ctl --set-freq`
depending on whether the kernel module has claimed the HID interface.

## The 4 modes, one codebase

| Mode | Command | File |
|------|---------|------|
| Windows CLI | `python shark.py …` | `shark.py` |
| Linux terminal | `python shark.py …` | `shark.py` (same file) |
| Windows GUI | `python shark_gui.py` | `shark_gui.py` |
| Linux GUI | `python shark_gui.py` | `shark_gui.py` (same file) |

## Credits

HID/tuning protocol from the Linux kernel `radio-shark.c` / `tea575x.c`
(Hans de Goede). Built with ffmpeg, hidapi, shazamio, and faster-whisper.
