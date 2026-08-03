#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyserial>=3.5"]
# ///
"""Capture the INA260 bench CSV stream to a timestamped file.

Reads the sketch's CSV output, prepends a UTC wall-clock timestamp to each
sample, and mirrors it to stdout so you can watch a voltage sweep live while
it is also being written to disk.

Lines beginning with '#' are the sketch's diagnostic channel. They are shown
on stdout and kept in the file as comments, but never parsed as data.

Result files are named DATA_[DUT]_[TestTitle]_YYYYMMDD_HHMM.csv with the
timestamp in UTC, and land in ./data/ unless --out overrides the whole path.

Usage:
    ./log_ina260.py --dut AMB1 --test ReverseCurrent
    ./log_ina260.py --dut AMB1 --test BiasSweep --duration 60
"""

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import serial

DEFAULT_PORT = "/dev/ttyUSB0"
DEFAULT_BAUD = 921600
DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
# Long enough that a stalled sketch is obvious, short enough that Ctrl-C
# still feels responsive.
READ_TIMEOUT_S = 2.0
# Long enough for the reset to take effect, short enough that the flush lands
# before the ESP32 ROM hands off to the sketch -- so stale bytes are dropped
# but the startup banner and I2C scan are still captured.
RESET_SETTLE_S = 0.05


def utc_now_iso() -> str:
    """UTC timestamp, second-truncated microseconds, explicit Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sanitize(field: str) -> str:
    """Strip characters that would put spaces or separators in a filename.

    The naming convention is parsed by splitting on '_', so an underscore or
    space inside DUT/test would silently corrupt the field boundaries.
    """
    cleaned = re.sub(r"[^A-Za-z0-9-]", "", field.replace("_", "").replace(" ", ""))
    if not cleaned:
        raise ValueError(f"{field!r} has no usable characters")
    return cleaned


def build_output_path(dut: str, test: str, data_dir: Path) -> Path:
    """DATA_[DUT]_[TestTitle]_YYYYMMDD_HHMM.csv, timestamped in UTC."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    return data_dir / f"DATA_{sanitize(dut)}_{sanitize(test)}_{stamp}.csv"


def main() -> int:
    parser = argparse.ArgumentParser(description="Log INA260 bench data.")
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--dut", default="INA260",
                        help="device under test, e.g. AMB1")
    parser.add_argument("--test", default="Run",
                        help="test title in UpperCamel, e.g. ReverseCurrent")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="directory for result files (default: ./data)")
    parser.add_argument("--out", type=Path, default=None,
                        help="explicit output path, bypasses the naming rule")
    parser.add_argument("--duration", type=float, default=None,
                        help="stop after N seconds (default: run until Ctrl-C)")
    args = parser.parse_args()

    if args.out is None:
        try:
            args.out = build_output_path(args.dut, args.test, args.data_dir)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)

    # Two runs inside the same minute would collide and the second would
    # silently truncate the first -- refuse rather than destroy bench data.
    if args.out.exists():
        print(f"error: {args.out} already exists; refusing to overwrite",
              file=sys.stderr)
        return 2

    try:
        port = serial.Serial(args.port, args.baud, timeout=READ_TIMEOUT_S)
    except serial.SerialException as exc:
        # By far the most common causes are a missing dialout group and a
        # serial monitor already holding the port, so name both.
        print(f"error: cannot open {args.port}: {exc}", file=sys.stderr)
        print("hint: run under 'sg dialout -c ...' and close any other "
              "serial monitor holding the port.", file=sys.stderr)
        return 1

    # Toggling DTR/RTS resets the DevKit v1, so the capture starts from the
    # sketch's banner rather than mid-stream.
    port.dtr = False
    port.rts = False

    # Let the board boot, then drop whatever the OS buffered before the reset.
    # Those stale lines carry the previous run's micros() values, which are
    # higher than the samples that follow and silently corrupt any rate or
    # time-span computed from the file.
    time.sleep(RESET_SETTLE_S)
    port.reset_input_buffer()

    samples = 0
    last_us = None
    echo = True
    start = datetime.now(timezone.utc)

    print(f"# logging {args.port} @ {args.baud} -> {args.out}", file=sys.stderr)
    print("# Ctrl-C to stop", file=sys.stderr)

    try:
        with port, args.out.open("w", buffering=1) as sink:
            sink.write(f"# capture started {utc_now_iso()} from {args.port}\n")
            sink.write(f"# dut={args.dut} test={args.test} baud={args.baud}\n")
            sink.write("utc,micros,volts,amps,watts\n")

            while True:
                if args.duration is not None:
                    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
                    if elapsed >= args.duration:
                        break

                raw = port.readline()
                if not raw:
                    continue

                # errors="replace" keeps a garbled byte from killing a long
                # unattended capture -- a corrupt line beats a dead logger.
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                if line.startswith("#"):
                    print(line, file=sys.stderr)
                    sink.write(line + "\n")
                    continue

                # Drop the sketch's own header; this file has its own.
                if line.startswith("micros,"):
                    continue

                fields = line.split(",")
                if len(fields) != 4:
                    print(f"# skipped malformed line: {line!r}", file=sys.stderr)
                    continue

                try:
                    micros = int(fields[0])
                except ValueError:
                    print(f"# skipped unparseable line: {line!r}", file=sys.stderr)
                    continue

                # A backward step means the board reset mid-capture, so the
                # micros column is no longer a single monotonic timebase.
                # Record it inline -- a silent discontinuity turns any rate or
                # duration derived from this file into a wrong number.
                if last_us is not None and micros < last_us:
                    note = (f"# WARN board reset mid-capture: micros "
                            f"{last_us} -> {micros}; timebase restarts here")
                    print(note, file=sys.stderr)
                    sink.write(note + "\n")
                last_us = micros

                stamped = f"{utc_now_iso()},{line}"
                sink.write(stamped + "\n")
                samples += 1

                # Piping into head/less closes stdout early. That must not end
                # the capture -- the file is the deliverable, the echo is only
                # for watching. Drop the echo and keep logging.
                if echo:
                    try:
                        print(stamped)
                    except BrokenPipeError:
                        echo = False

    except KeyboardInterrupt:
        print("\n# interrupted", file=sys.stderr)

    print(f"# {samples} samples written to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
