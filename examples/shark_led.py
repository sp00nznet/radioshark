import hid, time

VID, PID = 0x077d, 0x627a

# radioSHARK v1 HID protocol: 6-byte reports, OUT endpoint 0x05.
# hidapi on Windows wants the report-ID byte first (0x00 = unnumbered), then payload.
def cmd(dev, *payload):
    buf = bytes([0x00, *payload])      # report id + payload
    buf = buf + bytes(7 - len(buf))    # pad to report id + 6 bytes
    dev.write(buf)

RED_ON  = (0xA9,)
RED_OFF = (0xA8,)
def BLUE(level):       return (0xA0, max(0, min(127, level)))   # solid blue 0..127
def BLUE_PULSE(level): return (0xA1, (256 - level) & 0xff)      # hardware pulse

dev = hid.device()
dev.open(VID, PID)
print("opened:", dev.get_product_string(), flush=True)

try:
    print("1) killing default solid blue ...", flush=True);        cmd(dev, *BLUE(0));        time.sleep(1.2)
    print("2) RED on ...", flush=True);                            cmd(dev, *RED_ON);         time.sleep(1.5)
    print("3) RED off ...", flush=True);                           cmd(dev, *RED_OFF);        time.sleep(0.8)
    print("4) blue full brightness ...", flush=True);              cmd(dev, *BLUE(127));      time.sleep(1.2)
    print("5) blue PULSE mode (watch it breathe) ...", flush=True);cmd(dev, *BLUE_PULSE(96)); time.sleep(4)
    print("6) restore: solid blue, red off ...", flush=True)
    cmd(dev, *RED_OFF); cmd(dev, *BLUE(64))
    print("DONE - if you saw red + a pulse, we have full HID control.", flush=True)
finally:
    dev.close()
