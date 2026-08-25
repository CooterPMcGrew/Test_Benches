"""Reset the ESP32-S3 and capture bring-up serial output.

Usage: uv run --with pyserial TOOL_ISSROUTER_Capture_RevA.py [seconds] [port]

Port autodetects: prefers the S3 native USB (303a:1001, ttyACM), falls back
to a CP210x UART bridge (ttyUSB). Handles the native port's re-enumeration
after reset (the device node vanishes for ~1 s).
"""
import glob
import sys
import time

import serial

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0


def find_port():
    if len(sys.argv) > 2:
        return sys.argv[2]
    for pat in ("/dev/serial/by-id/*Espressif*", "/dev/serial/by-id/*CP21*",
                "/dev/ttyACM*", "/dev/ttyUSB*"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    sys.exit("no serial device found -- is the S3 plugged in?")


def open_port(port, deadline_s=8.0):
    end = time.monotonic() + deadline_s
    while True:
        try:
            s = serial.Serial()
            s.port = port
            s.baudrate = 115200
            s.timeout = 0.2
            s.dtr = False   # both deasserted at open -> chip runs
            s.rts = False
            s.open()
            return s
        except (serial.SerialException, OSError):
            if time.monotonic() > end:
                raise
            time.sleep(0.3)


port = find_port()
s = open_port(port)
s.rts = True    # RTS alone = EN low on the DevKit reset circuit (both ports)
time.sleep(0.1)
s.rts = False
try:            # native USB re-enumerates on reset; reopen if the node drops
    s.read(1)
except (serial.SerialException, OSError):
    s.close()
    time.sleep(1.0)
    s = open_port(port)

end = time.monotonic() + SECONDS
buf = bytearray()
while time.monotonic() < end:
    try:
        buf += s.read(s.in_waiting or 1)
    except (serial.SerialException, OSError):
        s.close()
        time.sleep(1.0)
        s = open_port(port)
s.close()
sys.stdout.write(buf.decode(errors="replace"))
