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
# Two things are required to get a hidraw node for userspace (hidapi):
#   a) blacklist the in-kernel radio-shark driver so it doesn't claim the iface;
#   b) tell usbhid NOT to ignore the device -- the HID core ships 077d:627a on
#      its built-in ignore list (so radio-shark could own it), so blacklisting
#      alone just leaves the HID interface orphaned with no hidraw node. The
#      HID_QUIRK_NO_IGNORE (0x40000000) quirk overrides that and makes usbhid
#      bind it and expose /dev/hidraw*. Audio is unaffected (snd-usb-audio
#      handles the separate audio interface).
echo "configuring HID access (needs sudo)..."
sudo tee /etc/modprobe.d/blacklist-radioshark.conf >/dev/null <<'EOF'
blacklist radio-shark
blacklist radio-shark2
EOF

sudo tee /etc/modprobe.d/radioshark-quirk.conf >/dev/null <<'EOF'
# Force usbhid to expose the radioSHARK (077d:627a) as a hidraw node instead of
# ignoring it for the in-kernel radio-shark driver. HID_QUIRK_NO_IGNORE.
options usbhid quirks=0x077D:0x627A:0x40000000
EOF

sudo tee /etc/udev/rules.d/70-radioshark.rules >/dev/null <<'EOF'
# Griffin radioSHARK (077d:627a): allow non-root access to tune + drive LEDs.
KERNEL=="hidraw*", ATTRS{idVendor}=="077d", ATTRS{idProduct}=="627a", MODE="0666"
SUBSYSTEM=="usb", ATTRS{idVendor}=="077d", ATTRS{idProduct}=="627a", MODE="0666"
EOF

sudo modprobe -r radio-shark 2>/dev/null || true
sudo modprobe -r radio-shark2 2>/dev/null || true

# Apply the usbhid quirk now. usbhid reads `quirks` only at load time, so a
# device replug alone won't pick it up if usbhid is already loaded -- it has to
# be reloaded. This briefly drops USB keyboards/mice; they re-enumerate. If
# usbhid is busy (modprobe -r fails), a reboot applies the quirk file instead.
QUIRK_APPLIED=0
if sudo modprobe -r usbhid 2>/dev/null && sudo modprobe usbhid 2>/dev/null; then
  QUIRK_APPLIED=1
else
  echo "! couldn't reload usbhid now (in use) -- reboot to apply the HID quirk."
fi

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
if [ "$QUIRK_APPLIED" = "1" ]; then
  echo "Done. UNPLUG and REPLUG the radioSHARK so the changes take effect, then:"
else
  echo "Done -- but REBOOT to apply the HID quirk (usbhid couldn't be reloaded),"
  echo "then verify with 'python shark.py doctor'. After that:"
fi
echo "    python shark_gui.py        # or: python shark.py 88.5"
