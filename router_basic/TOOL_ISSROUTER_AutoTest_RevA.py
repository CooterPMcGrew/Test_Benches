"""Automated bring-up test campaign: digipots + ADS1258 + siggen + scope.

Usage: uv run --with pyserial python3 TOOL_ISSROUTER_AutoTest_RevA.py

Phases (each skipped cleanly if its instrument is absent):
  1. Digipot scan / 5k set / readback          (S3 serial 'p')
  2. ADS1258 probe + internal self-test        (S3 serial 'a')
  3. Baseline stream, 20 s, all 16 channels    (CSV capture + stats)
  4. Sine fidelity: JDS6600 1 Hz 1 Vpp on ANALOG_0 -> pkpk + freq from ADC
  5. Amplitude linearity: 0.5 / 1.0 / 2.0 Vpp at 1 Hz
  6. 100 Hz cross-check: scope PKPK/FREQ vs ADC pkpk envelope
  7. NV wiper burn + readback                  (S3 serial 'n')

Everything lands in data/AUTOTEST_<UTC>/: raw CSVs per phase, boot/firmware
log, REPORT.txt summary. Close the bench dashboard first -- this tool owns
the S3 port and the generator for the duration.

ADC-side frequency truth only works well below the ~20 Hz CSV frame rate;
100 Hz points therefore use the scope for freq/pkpk truth and the ADC for
envelope only.
"""
import glob
import os
import socket
import time

import serial

GEN_PORT = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
SCOPE_IP = "10.11.13.220"
STIM_CH = 6          # ANALOG_0 = AIN6 = CSV column 6
CH_NAMES = ["ISNS_0/c1", "ISNS_1/c2", "ISNS_2/c3", "ISNS_3/c4",
            "ISNS_5/c6", "ISNS_4/c5",
            "ANALOG_0", "ANALOG_1", "ANALOG_2", "ANALOG_3",
            "ANALOG_5", "ANALOG_4", "ANALOG_6", "ANALOG_7",
            "ANALOG_8", "ANALOG_9"]

OUT = time.strftime("data/AUTOTEST_%Y%m%dT%H%M%SZ", time.gmtime())
os.makedirs(OUT, exist_ok=True)
report = []


def log(line):
    print(line, flush=True)
    report.append(line)


def find_s3():
    for pat in ("/dev/serial/by-id/*Espressif*", "/dev/serial/by-id/*CP21*",
                "/dev/ttyACM*"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    raise SystemExit("no S3 serial port")


class Fw:
    """Firmware conversation: routes CSV frames and text lines separately."""

    def __init__(self):
        self.s = serial.Serial(find_s3(), 115200, timeout=0.2)
        self.frames = []
        self.text = []
        time.sleep(0.3)
        self.s.reset_input_buffer()

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            raw = self.s.readline()
            if not raw:
                continue
            line = raw.decode(errors="replace").strip()
            parts = line.split(",")
            if len(parts) in (17, 18):
                try:
                    self.frames.append([float(p) for p in parts[:17]])
                    continue
                except ValueError:
                    pass
            if line:
                self.text.append(line)

    def cmd(self, ch, wait):
        mark = len(self.text)
        self.s.write(ch.encode())
        self.pump(wait)
        return self.text[mark:]

    def csv_on(self):
        # 'c' toggles; verify frames actually flow, retry once if not
        for _ in range(2):
            n = len(self.frames)
            self.cmd("c", 3.0)
            if len(self.frames) > n:
                return True
        return False

    def csv_off(self):
        for _ in range(2):
            n = len(self.frames)
            self.cmd("c", 2.0)
            time.sleep(0.5)
            if len(self.frames) == n:
                return

    def capture(self, seconds, name):
        mark = len(self.frames)
        self.pump(seconds)
        rows = self.frames[mark:]
        with open(f"{OUT}/{name}.csv", "w") as f:
            f.write("millis," + ",".join(CH_NAMES) + "\n")
            for r in rows:
                f.write(",".join(f"{v:0.4f}" for v in r) + "\n")
        return rows


def stats(rows, ch):
    vals = [r[1 + ch] for r in rows]
    if not vals:
        return 0.0, 0.0, 0.0
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return mean, var ** 0.5, max(vals) - min(vals)


def freq_zero_cross(rows, ch):
    """Fundamental estimate from mean-crossings; OK well below frame rate."""
    if len(rows) < 8:
        return 0.0
    vals = [r[1 + ch] for r in rows]
    t = [r[0] / 1000.0 for r in rows]
    mean = sum(vals) / len(vals)
    crossings = [t[i] for i in range(1, len(vals))
                 if (vals[i - 1] - mean) < 0 <= (vals[i] - mean)]
    if len(crossings) < 2:
        return 0.0
    return (len(crossings) - 1) / (crossings[-1] - crossings[0])


class Gen:
    def __init__(self):
        self.s = serial.Serial(GEN_PORT, 115200, timeout=0.5)

    def _c(self, c):
        self.s.reset_input_buffer()
        self.s.write((c + "\r\n").encode())
        self.s.read_until(b"\n", 64)

    def sine(self, f_hz, vpp):
        self._c(":w21=0.")
        self._c(f":w23={int(round(f_hz * 100))},0.")
        self._c(f":w25={int(vpp * 1000)}.")
        self._c(":w27=1000.")

    def output(self, on):
        self._c(f":w20={1 if on else 0},0.")


class Scope:
    def __init__(self):
        self.sk = socket.create_connection((SCOPE_IP, 5025), timeout=2)
        self.sk.settimeout(2)
        self.idn = self.q("*IDN?")

    def q(self, s):
        self.sk.sendall((s + "\n").encode())
        return self.sk.recv(1024).decode(errors="replace").strip()

    def w(self, s):
        self.sk.sendall((s + "\n").encode())

    def pava(self, meas):
        r = self.q(f"C1:PAVA? {meas}")   # 'C1:PAVA PKPK,1.02E+00V'
        try:
            return float(r.split(",")[1].rstrip("VSHz%s"))
        except (IndexError, ValueError):
            return float("nan")


def main():
    t0 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    log(f"=== ISS Router autotest {t0} -> {OUT} ===")
    fw = Fw()
    fw.csv_off()

    # --- 1: digipots ---
    lines = fw.cmd("p", 4.0)
    pots_ok = sum("OK" in l for l in lines)
    for l in lines:
        log("  " + l)
    log(f"[1] digipots: {pots_ok}/6 wipers verified at 5k "
        f"{'PASS' if pots_ok == 6 else 'FAIL'}")

    # --- 2: ADC probe + self-test ---
    lines = fw.cmd("a", 10.0)
    for l in lines:
        log("  " + l)
    adc_ok = any("ID=0x8B" in l for l in lines)
    st_pass = sum("PASS" in l for l in lines)
    log(f"[2] ADS1258: {'alive' if adc_ok else 'NOT RESPONDING'}, "
        f"{st_pass} self-test lines PASS")

    if not adc_ok:
        log("ABORT: no ADC, skipping stream phases")
        finish()
        return

    # --- 3: baseline ---
    if not fw.csv_on():
        log("ABORT: CSV stream would not start")
        finish()
        return
    rows = fw.capture(20, "phase3_baseline")
    log(f"[3] baseline: {len(rows)} frames / 20 s")
    for ch in range(16):
        m, sd, pp = stats(rows, ch)
        log(f"    {CH_NAMES[ch]:>9s}  mean {m:+0.4f} V  std {sd * 1000:6.2f} mV"
            f"  pkpk {pp * 1000:6.1f} mV")

    # --- 4-6: stimulus (needs generator lead on J2.2) ---
    try:
        gen = Gen()
    except (serial.SerialException, OSError):
        gen = None
        log("[4-6] SKIP: generator not reachable")
    if gen:
        gen.sine(1.0, 1.0)
        gen.output(True)
        time.sleep(2)
        rows = fw.capture(15, "phase4_sine_1hz_1vpp")
        m, sd, pp = stats(rows, STIM_CH)
        f_est = freq_zero_cross(rows, STIM_CH)
        ok = 0.80 < pp < 1.20 and 0.8 < f_est < 1.2
        log(f"[4] 1 Hz 1 Vpp on ANALOG_0: pkpk {pp:0.3f} V, freq {f_est:0.2f} Hz"
            f"  {'PASS' if ok else 'FAIL (lead on J2.2? see report)'}")
        others = [ch for ch in range(6, 16) if ch != STIM_CH]
        worst = max(others, key=lambda ch: stats(rows, ch)[2])
        log(f"    crosstalk worst neighbor {CH_NAMES[worst]}: "
            f"pkpk {stats(rows, worst)[2] * 1000:0.1f} mV")

        log("[5] amplitude linearity at 1 Hz:")
        for vpp in (0.5, 1.0, 2.0):
            gen.sine(1.0, vpp)
            time.sleep(1.5)
            rows = fw.capture(10, f"phase5_{int(vpp * 1000)}mvpp")
            _, _, pp = stats(rows, STIM_CH)
            log(f"    set {vpp:0.1f} Vpp -> ADC pkpk {pp:0.3f} V"
                f"  ({100 * pp / vpp:0.1f} %)")

        log("[6] 100 Hz cross-check:")
        gen.sine(100.0, 1.0)
        time.sleep(1.5)
        try:
            sc = Scope()
            sc.w("C1:TRA ON")
            sc.w("TDIV 5MS")
            sc.w("C1:VDIV 500MV")
            time.sleep(2)
            pk = sc.pava("PKPK")
            fr = sc.pava("FREQ")
            log(f"    scope [{SCOPE_IP}] {sc.idn.split(',')[1]}: "
                f"PKPK {pk:0.3f} V, FREQ {fr:0.1f} Hz")
        except OSError:
            log(f"    scope {SCOPE_IP} unreachable -- skipped")
        rows = fw.capture(10, "phase6_100hz")
        _, _, pp = stats(rows, STIM_CH)
        log(f"    ADC envelope pkpk {pp:0.3f} V (aliased sampling, envelope only)")
        gen.output(False)
        log("    generator output OFF")

    # --- 7: NV burn ---
    fw.csv_off()
    lines = fw.cmd("n", 6.0)
    for l in lines:
        log("  " + l)
    burned = sum("BURNED" in l for l in lines)
    log(f"[7] NV burn: {burned}/6 wipers persisted "
        f"{'PASS' if burned == 6 else '(check WP jumpers)' if burned == 0 else 'PARTIAL'}")
    fw.csv_on()   # leave the board streaming for the dashboard

    finish()


def finish():
    with open(f"{OUT}/REPORT.txt", "w") as f:
        f.write("\n".join(report) + "\n")
    print(f"\nreport + raw CSVs in {OUT}/")


if __name__ == "__main__":
    main()
