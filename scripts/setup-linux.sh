#!/usr/bin/env bash
#
# One-time Linux setup for the Griffin radioSHARK.
#
# The audio side already works out of the box: the radioSHARK's USB-Audio
# interface is handled by snd-usb-audio and shows up as an ALSA capture card.
#
# Tuning + LEDs go over the HID interface. On Linux the in-kernel `radio-shark`
# driver normally claims that interface (exposing a V4L2 tuner + sysfs LEDs).
# This app instead drives it from userspace with hidapi -- exactly like the
# Windows build -- so this script frees the HID interface (blacklists the kernel
# driver so usbhid exposes it as hidraw) and grants non-root access to it.
#
# Run:  ./scripts/setup-linux.sh
#
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
echo "=== radioSHARK Linux setup ==="

# 1. ffmpeg ------------------------------------------------------------------
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "! ffmpeg not found. Install it, e.g.:"
  echo "    sudo apt install ffmpeg     # Debian/Ubuntu"
  echo "    sudo dnf install ffmpeg     # Fedora"
  echo "    sudo pacman -S ffmpeg       # Arch"
else
  echo "ok: ffmpeg present"
fi

# 2. python deps -------------------------------------------------------------
echo "installing python dependencies..."
pip install -r "$HERE/../requirements.txt" 2>/dev/null \
  || pip install --user -r "$HERE/../requirements.txt" \
  || echo "! pip install failed - install the packages in requirements.txt manually"

# 3. free the HID interface + grant access (needs sudo) ----------------------
echo "configuring HID access (needs sudo)..."
sudo tee /etc/modprobe.d/blacklist-radioshark.conf >/dev/null <<'EOF'
# Let usbhid expose the radioSHARK HID interface so userspace (hidapi) can tune
# it. Audio is unaffected (snd-usb-audio handles the separate audio interface).
blacklist radio-shark
blacklist radio-shark2
EOF

sudo tee /etc/udev/rules.d/70-radioshark.rules >/dev/null <<'EOF'
# Griffin radioSHARK (077d:627a): allow non-root access to tune + drive LEDs.
KERNEL=="hidraw*", ATTRS{idVendor}=="077d", ATTRS{idProduct}=="627a", MODE="0666"
SUBSYSTEM=="usb", ATTRS{idVendor}=="077d", ATTRS{idProduct}=="627a", MODE="0666"
EOF

sudo modprobe -r radio-shark 2>/dev/null || true
sudo modprobe -r radio-shark2 2>/dev/null || true
sudo udevadm control --reload-rules
sudo udevadm trigger

# 4. detect the ALSA capture card -------------------------------------------
echo ""
echo "ALSA capture devices:"
if arecord -l 2>/dev/null | grep -i shark; then
  CARD=$(awk -F'[][]' '/shark/{print $2; exit}' /proc/asound/cards | tr -d ' ')
  [ -n "$CARD" ] && echo "-> set RADIOSHARK_ALSA=plughw:CARD=$CARD  (auto-detected otherwise)"
else
  echo "  (radioSHARK ALSA card not found - is it plugged in?)"
fi

echo ""
echo "Done. UNPLUG and REPLUG the radioSHARK so the changes take effect, then:"
echo "    python shark_gui.py        # or: python shark.py 88.5"
