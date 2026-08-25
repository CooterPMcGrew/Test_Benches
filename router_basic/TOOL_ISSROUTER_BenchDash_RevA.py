"""Live bench dashboard: every ISS Router peripheral, one terminal.

Usage: uv run --with pyserial python3 TOOL_ISSROUTER_BenchDash_RevA.py \
           [--dmm 10.11.13.x]

Redraws ~1 Hz. Panels: ESP32-S3 (16-ch stream + firmware log), JDS6600
(decoded registers), both Siglent scopes (SCPI), optional DMM. Every panel
shows its comms channel and survives its peripheral vanishing/returning.

Note: connecting to the S3 sends serial bytes, which releases firmware
meter mode ('w') if it was armed. Run this after harness probing.
"""
import argparse
import glob
import socket
import time

import serial

S3_HINTS = ("/dev/serial/by-id/*Espressif*", "/dev/serial/by-id/*CP21*",
            "/dev/ttyACM*")
GEN_HINT = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
SCOPES = [("SDS1104X-E (new)", "10.11.13.220"),
          ("SDS1104X-U (old)", "10.11.13.221")]

CH_NAMES = ["ISNS_0/c1", "ISNS_1/c2", "ISNS_2/c3", "ISNS_3/c4",
            "ISNS_5/c6", "ISNS_4/c5",
            "ANALOG_0", "ANALOG_1", "ANALOG_2", "ANALOG_3",
            "ANALOG_5", "ANALOG_4", "ANALOG_6", "ANALOG_7",
            "ANALOG_8", "ANALOG_9"]
GEN_WAVES = {0: "sine", 1: "square", 2: "pulse", 3: "triangle", 4: "ramp"}


def now():
    return time.monotonic()


class S3:
    """Reads the firmware stream; keeps CSV mode alive; collects log lines."""

    def __init__(self):
        self.s = None
        self.port = "?"
        self.volts = [None] * 16
        self.t_data = 0.0
        self.t_poke = 0.0
        self.log = []

    def _open(self):
        for pat in S3_HINTS:
            hits = sorted(glob.glob(pat))
            if hits:
                self.port = hits[0]
                self.s = serial.Serial(self.port, 115200, timeout=0)
                return

    def poll(self):
        try:
            if not self.s:
                self._open()
            if not self.s:
                return
            for raw in self.s.read(65536).splitlines():
                line = raw.decode(errors="replace").strip()
                parts = line.split(",")
                if len(parts) in (17, 18):
                    try:
                        self.volts = [float(p) for p in parts[1:17]]
                        self.t_data = now()
                        continue
                    except ValueError:
                        pass
                if line:
                    self.log = (self.log + [line])[-6:]
            # no CSV for 4 s -> nudge the toggle (also releases meter mode)
            if now() - self.t_data > 4.0 and now() - self.t_poke > 4.0:
                self.s.write(b"c")
                self.t_poke = now()
        except (serial.SerialException, OSError):
            if self.s:
                self.s.close()
            self.s = None

    def panel(self):
        head = f"ESP32-S3  [{self.port}]"
        if not self.s:
            return [head + "  OFFLINE (no serial device)"]
        age = now() - self.t_data
        if age > 5.0 or self.volts[0] is None:
            lines = [head + "  no channel data (ADC down or wire diag pending)"]
        else:
            lines = [head + f"  streaming, last frame {age:0.1f}s ago"]
            for r in range(4):
                cells = []
                for c in range(4):
                    i = r * 4 + c
                    cells.append(f"{CH_NAMES[i]:>9s} {self.volts[i]:+7.3f}V")
                lines.append("   " + "  ".join(cells))
        lines += ["   fw| " + l for l in self.log[-3:]]
        return lines


class Gen:
    """JDS6600: decode on/off, wave, freq, amplitude, offset each cycle."""

    def __init__(self):
        self.s = None

    def _q(self, reg):
        self.s.reset_input_buffer()
        self.s.write(f":r{reg:02d}=0.\r\n".encode())
        r = self.s.read_until(b"\n", 64).decode(errors="replace")
        return r.split("=")[1].rstrip(".\r\n") if "=" in r else ""

    def panel(self):
        head = f"JDS6600 siggen  [{GEN_HINT.split('/')[-1]}]"
        try:
            if not self.s:
                self.s = serial.Serial(GEN_HINT, 115200, timeout=0.4)
            on = self._q(20)
            wave = self._q(21)
            freq = self._q(23)
            ampl = self._q(25)
            offs = self._q(27)
            ch1 = "ON " if on.startswith("1") else "off"
            w = GEN_WAVES.get(int(wave or -1), f"wave{wave}")
            f_hz = int(freq.split(",")[0]) / 100.0 if freq else 0.0
            vpp = int(ampl or 0) / 1000.0
            voff = (int(offs or 1000) - 1000) / 100.0
            return [head, f"   CH1 {ch1}  {w}  {f_hz:0.2f} Hz  {vpp:0.3f} Vpp"
                          f"  offset {voff:+0.2f} V"]
        except (serial.SerialException, OSError, ValueError, IndexError):
            if self.s:
                self.s.close()
            self.s = None
            return [head + "  OFFLINE"]


class Scpi:
    """One LAN SCPI box; reconnects; light per-cycle queries."""

    def __init__(self, name, ip, queries):
        self.name = name
        self.ip = ip
        self.queries = queries
        self.sk = None
        self.idn = ""

    def _q(self, q):
        self.sk.sendall((q + "\n").encode())
        return self.sk.recv(512).decode(errors="replace").strip()

    def panel(self):
        head = f"{self.name}  [{self.ip}:5025]"
        try:
            if not self.sk:
                self.sk = socket.create_connection((self.ip, 5025), timeout=1.5)
                self.sk.settimeout(1.5)
                self.idn = self._q("*IDN?")
            vals = "  ".join(f"{q}={self._q(q)}" for q in self.queries)
            return [head + "  " + self.idn.split(",")[1] if "," in self.idn
                    else head, "   " + vals]
        except OSError:
            if self.sk:
                self.sk.close()
            self.sk = None
            return [head + "  OFFLINE"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dmm", default=None, help="bench DMM IP when it lands")
    args = ap.parse_args()

    s3 = S3()
    gen = Gen()
    boxes = [Scpi(n, ip, ["TRMD?", "TDIV?"]) for n, ip in SCOPES]
    if args.dmm:
        boxes.append(Scpi("Bench DMM", args.dmm, ["READ?"]))

    print("\x1b[2J", end="")
    while True:
        s3.poll()
        out = ["\x1b[H\x1b[1mISS Router bench  " +
               time.strftime("%H:%M:%SZ", time.gmtime()) + "\x1b[0m", ""]
        out += s3.panel() + [""]
        out += gen.panel() + [""]
        for b in boxes:
            out += b.panel() + [""]
        if not args.dmm:
            out.append("Bench DMM  [--dmm <ip> to add]  not configured")
        print("\n".join(out) + "\x1b[J", flush=True)
        time.sleep(1.0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
