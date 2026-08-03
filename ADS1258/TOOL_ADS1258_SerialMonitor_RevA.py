"""Hard-reset an ESP32 DevKit via RTS and capture serial output."""
import sys
import time
import serial

PORT = "/dev/ttyUSB0"
SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0

s = serial.Serial()
s.port = PORT
s.baudrate = 115200
s.dtr = False   # both lines deasserted at open -> chip runs
s.rts = False
s.open()

# RTS alone asserted pulls EN low on the DevKit auto-reset circuit
s.rts = True
time.sleep(0.1)
s.rts = False

end = time.monotonic() + SECONDS
buf = bytearray()
while time.monotonic() < end:
    buf += s.read(s.in_waiting or 1)
s.close()
sys.stdout.write(buf.decode(errors="replace"))
