"""Hard-reset the ESP32-S3 (native USB CDC) via RTS and capture serial output."""
import sys
import time
import serial

PORT = "/dev/ttyACM0"
SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0

s = serial.Serial()
s.port = PORT
s.baudrate = 115200
s.dtr = False   # both lines deasserted at open -> chip runs
s.rts = False
s.open()

# The USB-Serial-JTAG peripheral mimics the classic auto-reset circuit:
# RTS asserted alone resets the chip into run mode.
s.rts = True
time.sleep(0.1)
s.rts = False

end = time.monotonic() + SECONDS
buf = bytearray()
while time.monotonic() < end:
    buf += s.read(s.in_waiting or 1)
s.close()
sys.stdout.write(buf.decode(errors="replace"))
