# examples

Minimal standalone scripts kept for reference — they show the raw radioSHARK HID
protocol in isolation. The real app (`shark.py` / `shark_gui.py` in the repo root)
supersedes them, but these are the smallest possible demonstrations:

* **`shark_led.py`** — opens the HID device and runs an LED sequence (blue off →
  red → blue → pulse). The "hello world" that proves you have HID control.
* **`shark_tune.py`** — the minimal FM tuner: convert MHz to the TEA575x PLL value
  and write the tune report. `python shark_tune.py 88.5`

See [`../docs/HARDWARE.md`](../docs/HARDWARE.md) for the full protocol.
