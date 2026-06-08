#!/usr/bin/env python3
"""Tune the Griffin radioSHARK (v1, 077d:627a) to an FM frequency.

Protocol (from Linux kernel radio-shark.c + tea575x.c):
  - HID OUT, 6-byte report, command byte 0xc0 = "write shift register".
  - The 25-bit TEA575x register value is packed big-endian into bytes 0-3.
  - FM PLL value: pll = (freq_kHz + TEA575X_FMIF) * 10 / 125,  FMIF = 10700 kHz.
  - val = BAND_FM(0) | (pll & 0x7fff); SEARCH/MONO bits left 0 (tuned, stereo).
"""
import sys, hid

VID, PID = 0x077d, 0x627a
FMIF = 10700  # kHz, 10.7 MHz intermediate frequency

def fm_pll(mhz):
    khz = int(round(mhz * 1000))
    pll = ((khz + FMIF) * 10) // 125
    return pll & 0x7fff

def tune(mhz):
    if not (76.0 <= mhz <= 108.0):
        print(f"warning: {mhz} MHz is outside the 76-108 FM band", file=sys.stderr)
    pll = fm_pll(mhz)
    val = pll            # BAND_FM=0, stereo, no search
    buf = bytearray(7)   # [report_id, 6 payload bytes]
    buf[1] = 0xc0 | ((val >> 24) & 0xff)
    buf[2] = (val >> 16) & 0xff
    buf[3] = (val >> 8) & 0xff
    buf[4] = val & 0xff
    dev = hid.device()
    dev.open(VID, PID)
    try:
        dev.write(bytes(buf))
        # confirm: turn red LED on briefly as a visual "tuned" ack
        dev.write(bytes([0x00, 0xA9, 0, 0, 0, 0, 0]))
    finally:
        dev.close()
    print(f"tuned to {mhz:.1f} MHz  (pll={pll}, report={buf.hex()})")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python shark_tune.py <freq_MHz>   e.g. 99.5")
        sys.exit(1)
    tune(float(sys.argv[1]))
