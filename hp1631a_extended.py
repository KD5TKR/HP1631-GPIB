"""
hp1631a_extended.py  --  Extended HP 1631A utilities
=====================================================
Builds on hp1631a_gpib.py.  Adds:
  - Configuration save / restore (writes a plain-text config file)
  - CSV export of state and timing listing data
  - ASCII waveform renderer (prints a timing diagram to the terminal)
  - SRQ / interrupt-driven capture (uses SRQ line rather than polling)
  - Batch capture loop (capture N traces and save sequentially)
  - Screen dump helper (invokes KE5FX ibplot if available)

Requires:  Python 3.x, pyserial   (pip install pyserial)
Optional:  KE5FX GPIB Toolkit in PATH for --screendump

Usage
-----
  python hp1631a_extended.py --port COM3 --addr 5 --save-config my_setup.cfg
  python hp1631a_extended.py --port COM3 --addr 5 --load-config my_setup.cfg
  python hp1631a_extended.py --port COM3 --addr 5 --capture-csv trace.csv
  python hp1631a_extended.py --port COM3 --addr 5 --waveform
  python hp1631a_extended.py --port COM3 --addr 5 --batch 10 --output-dir captures/
  python hp1631a_extended.py --port COM3 --addr 5 --screendump screen.hpgl
"""

import serial
import time
import sys
import argparse
import os
import csv
import subprocess
import shutil
from dataclasses import dataclass, field
from typing import List, Optional, Dict

# ---------------------------------------------------------------------------
# Prologix low-level driver  (same as hp1631a_gpib.py)
# ---------------------------------------------------------------------------

class PrologixGPIB:
    """
    Low-level Prologix GPIB-USB driver.

    eos controls the terminator appended to commands sent to the instrument:
      0 = CR+LF   1 = CR   2 = LF (default)   3 = none
    Try eos=1 (CR) or eos=0 (CR+LF) if the 1631A returns "Unrecognized Command".

    ++read_tmo_ms is intentionally sent but harmlessly ignored on old Prologix
    firmware.  Any resulting "Unrecognized command" reply is drained from the
    buffer before the first instrument query, so it cannot corrupt responses.
    """

    def __init__(self, port: str, gpib_addr: int,
                 timeout: float = 5.0, eos: int = 2):
        self.port      = port
        self.gpib_addr = gpib_addr
        self.eos       = eos        # 0=CR+LF  1=CR  2=LF  3=none
        self.ser = serial.Serial(
            port=port, baudrate=115200,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=timeout,
            xonxoff=False, rtscts=False,
        )
        time.sleep(0.2)             # let USB-serial enumeration settle
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        self._init_controller()

    def _init_controller(self):
        for cmd in [
            "++mode 1",               # controller mode
            "++auto 0",               # manual read (we issue ++read explicitly)
            f"++eos {self.eos}",      # terminator appended to instrument commands
            "++eoi 1",                # assert EOI with last byte written
            "++read_tmo_ms 3000",     # silently accepted on new firmware; ignored on old
            f"++addr {self.gpib_addr}",
        ]:
            self._raw_write(cmd)
        # Drain any "Unrecognized command" replies from ++read_tmo_ms on old
        # Prologix firmware, plus any bytes left over from a previous session.
        self._drain()

    def _raw_write(self, cmd: str):
        """Write one line to the Prologix serial port."""
        self.ser.write((cmd + "\n").encode())
        time.sleep(0.05)

    def _drain(self, timeout_s: float = 0.3):
        """
        Read and discard everything in the serial input buffer.
        Waits up to timeout_s seconds for the last byte.
        Prevents stale Prologix error strings from being mistaken for
        instrument responses.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            waiting = self.ser.in_waiting
            if waiting:
                self.ser.read(waiting)
                deadline = time.time() + 0.1   # reset window after each burst
            else:
                time.sleep(0.02)

    def write(self, instrument_cmd: str):
        """Send a command to the addressed GPIB instrument."""
        self._raw_write(f"++addr {self.gpib_addr}")
        self._raw_write(instrument_cmd)

    def read(self, max_bytes: int = 262144) -> str:
        """
        Request a response via ++read eoi and return it as a string.
        Drains stale bytes first so only the instrument's actual reply is read.
        """
        self._drain(0.05)
        self._raw_write(f"++addr {self.gpib_addr}")
        self._raw_write("++read eoi")
        raw = self.ser.read(max_bytes)
        return raw.decode(errors="replace").strip()

    def query(self, cmd: str, max_bytes: int = 262144,
              delay: float = 0.3) -> str:
        """
        Write a query command and return the response.
        delay: seconds after sending before reading.  Increase for slow
               instruments or large responses.
        """
        self.write(cmd)
        time.sleep(delay)
        return self.read(max_bytes)

    def clear(self):
        self._raw_write(f"++addr {self.gpib_addr}")
        self._raw_write("++clr")
        time.sleep(0.5)

    def serial_poll(self) -> int:
        self._drain(0.05)
        self._raw_write(f"++addr {self.gpib_addr}")
        self._raw_write("++spoll")
        resp = self.ser.readline().decode(errors="replace").strip()
        try:
            return int(resp)
        except ValueError:
            return -1

    def srq(self) -> bool:
        self._raw_write("++srq")
        resp = self.ser.readline().decode(errors="replace").strip()
        return resp.strip() == "1"

    def ifc(self):
        """Send Interface Clear — resets the entire GPIB bus."""
        self._raw_write("++ifc")
        time.sleep(0.5)

    def close(self):
        self.ser.close()


# ---------------------------------------------------------------------------
# HP 1631A instrument class  (extended)
# ---------------------------------------------------------------------------

class HP1631A:
    SB_DATA_READY = 0x10
    SB_ERROR      = 0x20
    SB_RQS        = 0x40
    SB_POWER_ON   = 0x80

    def __init__(self, gpib: PrologixGPIB):
        self.gpib = gpib

    # ---- basic commands ----

    def identify(self) -> str:
        return self.gpib.query("ID?")

    def error_status(self) -> str:
        return self.gpib.query("ERR?")

    def clear(self):
        self.gpib.clear()

    def start(self):
        self.gpib.write("START")

    def stop(self):
        self.gpib.write("STOP")

    def set_menu(self, name: str):
        self.gpib.write(f"MENU {name}")
        time.sleep(0.2)

    def send_raw(self, cmd: str) -> str:
        if cmd.strip().endswith("?"):
            return self.gpib.query(cmd)
        self.gpib.write(cmd)
        return ""

    # ---- status / synchronisation ----

    def wait_for_data(self, poll_interval: float = 0.5,
                      timeout: float = 120.0) -> bool:
        """
        Poll via serial poll until DATA_READY or timeout.
        Returns True when data is available.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            sb = self.gpib.serial_poll()
            if sb < 0:
                pass  # transient read error, keep trying
            elif sb & self.SB_ERROR:
                print(f"  [ERROR] status byte 0x{sb:02X}")
                return False
            elif sb & self.SB_DATA_READY:
                return True
            time.sleep(poll_interval)
        return False

    def wait_for_srq(self, timeout: float = 120.0) -> bool:
        """
        Block until the SRQ line is asserted (instrument requests service).
        Faster than polling the status byte because it checks the bus signal
        directly.  After SRQ is seen, do a serial poll to clear the SRQ.
        Returns True if SRQ was seen, False on timeout.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.gpib.srq():
                _ = self.gpib.serial_poll()   # clear the SRQ
                return True
            time.sleep(0.1)
        return False

    # ---- data retrieval ----

    def get_state_listing(self) -> str:
        return self.gpib.query("SLIST?", max_bytes=524288)

    def get_timing_listing(self) -> str:
        return self.gpib.query("TLIST?", max_bytes=524288)

    def get_waveform_listing(self) -> str:
        return self.gpib.query("WLIST?", max_bytes=524288)

    def get_config(self) -> str:
        """
        Query the full instrument configuration block.
        On the 1631A this is the CONFIG? query from Chapter 9.
        The response is a multi-line ASCII block describing the current setup.
        """
        return self.gpib.query("CONFIG?", max_bytes=65536)


# ---------------------------------------------------------------------------
# Configuration save / restore
# ---------------------------------------------------------------------------

def save_config(analyzer: HP1631A, filepath: str):
    """
    Download the instrument configuration and write it to a plain-text file.

    The 1631A CONFIG? response contains all current setup information —
    channel labels, clock rates, trigger conditions, format settings, etc.
    This file can be examined and re-sent line-by-line to restore a setup.
    """
    print("  Querying instrument configuration...")
    cfg = analyzer.get_config()
    if not cfg:
        print("  [warn] No configuration data returned.  "
              "Verify the instrument supports CONFIG? on your firmware revision.")
        return

    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"# HP 1631A configuration dump\n")
        f.write(f"# Saved: {timestamp}\n")
        f.write(f"# Restore with: --load-config {filepath}\n\n")
        f.write(cfg)
        f.write("\n")

    print(f"  Configuration saved to: {filepath}")


def load_config(analyzer: HP1631A, filepath: str):
    """
    Read a saved configuration file and replay each command to the instrument.

    Lines beginning with '#' are comments and are skipped.
    Each non-blank line is sent as a command to the 1631A in sequence,
    with a short delay between commands to give the instrument time to process.
    """
    if not os.path.exists(filepath):
        print(f"  [error] File not found: {filepath}")
        return

    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    commands = [l.strip() for l in lines
                if l.strip() and not l.strip().startswith("#")]

    print(f"  Sending {len(commands)} configuration commands...")
    for i, cmd in enumerate(commands, 1):
        print(f"    [{i}/{len(commands)}] {cmd}")
        analyzer.gpib.write(cmd)
        time.sleep(0.15)

    print("  Configuration restore complete.")


# ---------------------------------------------------------------------------
# Listing data parser
# ---------------------------------------------------------------------------

@dataclass
class StateRow:
    """One row from the HP 1631A state (synchronous) listing."""
    line:   int
    labels: List[str]    # column headers
    values: List[str]    # one value per label

@dataclass
class TimingRow:
    """One row from the HP 1631A timing listing."""
    time_ns: Optional[float]
    labels:  List[str]
    values:  List[str]


def parse_listing(raw: str) -> List[Dict]:
    """
    Parse the ASCII listing data returned by SLIST? or TLIST?.

    The 1631A returns a header line followed by data rows.
    The exact format depends on firmware; this parser handles the common
    whitespace-delimited columnar format described in Chapter 9.

    Returns a list of dicts, one per data row, with column-name keys.
    """
    rows = []
    lines = [l for l in raw.splitlines() if l.strip()]
    if not lines:
        return rows

    # The first non-blank line is the column header
    header = lines[0].split()
    for data_line in lines[1:]:
        parts = data_line.split()
        if len(parts) < len(header):
            # pad short rows
            parts += [""] * (len(header) - len(parts))
        row = dict(zip(header, parts[:len(header)]))
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def capture_and_export_csv(analyzer: HP1631A, base_path: str,
                            use_srq: bool = False):
    """
    Run a capture, wait for completion, parse the listing data, and write
    three CSV files:
      <base>_state.csv    -- state (synchronous) listing
      <base>_timing.csv   -- timing (asynchronous) listing
      <base>_waveform.txt -- raw waveform listing (too irregular for CSV)
    """
    stem = base_path.rstrip(".csv")  # strip extension if user added it

    print("  Starting acquisition...")
    analyzer.start()

    if use_srq:
        print("  Waiting for SRQ (hardware trigger)...")
        ok = analyzer.wait_for_srq(timeout=120)
    else:
        print("  Waiting for DATA_READY (serial poll)...")
        ok = analyzer.wait_for_data(timeout=120)

    if not ok:
        print("  [error] Acquisition did not complete in time.")
        return

    # --- State listing ---
    print("  Downloading state listing...")
    sraw = analyzer.get_state_listing()
    srows = parse_listing(sraw)
    if srows:
        spath = stem + "_state.csv"
        with open(spath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(srows[0].keys()))
            writer.writeheader()
            writer.writerows(srows)
        print(f"    State listing: {len(srows)} rows -> {spath}")
    else:
        print("    State listing: no data (or instrument returned empty response)")

    # --- Timing listing ---
    print("  Downloading timing listing...")
    traw = analyzer.get_timing_listing()
    trows = parse_listing(traw)
    if trows:
        tpath = stem + "_timing.csv"
        with open(tpath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(trows[0].keys()))
            writer.writeheader()
            writer.writerows(trows)
        print(f"    Timing listing: {len(trows)} rows -> {tpath}")
    else:
        print("    Timing listing: no data")

    # --- Waveform (raw text, not CSV) ---
    print("  Downloading waveform listing...")
    wraw = analyzer.get_waveform_listing()
    wpath = stem + "_waveform.txt"
    with open(wpath, "w", encoding="utf-8") as f:
        f.write(wraw)
    print(f"    Waveform listing: {len(wraw)} bytes -> {wpath}")


# ---------------------------------------------------------------------------
# ASCII waveform renderer
# ---------------------------------------------------------------------------

def render_ascii_waveform(raw_waveform: str,
                          width: int = 80,
                          max_signals: int = 16):
    """
    Render a simple ASCII timing diagram from the 1631A WLIST? response.

    The waveform listing contains rows of 0/1 values for each signal at each
    sample point.  This function plots each signal as a row of H/L characters,
    producing a terminal-friendly timing diagram.

    Output example (8 signals, 40 time steps):

      CLK  ___HHHH____HHHH____HHHH____HHHH____
      D0   _H__________H_H_____H_______________
      D1   ____H_H_____________H_H_____________
      ...
    """
    lines = [l for l in raw_waveform.splitlines() if l.strip()]
    if not lines:
        print("  [warn] No waveform data to render.")
        return

    header = lines[0].split()
    signal_names = header  # column headers are signal names / labels
    n_signals = min(len(signal_names), max_signals)

    # Collect sample columns: each row is a time step, each column a signal
    samples: List[List[str]] = []
    for row_line in lines[1:]:
        vals = row_line.split()
        if len(vals) >= n_signals:
            samples.append(vals[:n_signals])

    if not samples:
        print("  [warn] Waveform listing contained no sample rows.")
        return

    # Down-sample to fit terminal width
    n_samples = len(samples)
    step = max(1, n_samples // (width - 12))

    print()
    print(f"  ASCII Waveform  ({n_samples} samples, "
          f"{n_signals} signals shown, 1 char per {step} sample(s))")
    print()

    for sig_idx in range(n_signals):
        name = signal_names[sig_idx]
        # Build the waveform string
        chars = []
        prev_val = None
        for t in range(0, n_samples, step):
            val = samples[t][sig_idx]
            if val == "1":
                ch = "H"
            elif val == "0":
                ch = "_"
            else:
                ch = "?"   # undefined / high-Z
            # Mark rising/falling edges
            if prev_val == "0" and val == "1":
                ch = "/"
            elif prev_val == "1" and val == "0":
                ch = "\\"
            chars.append(ch)
            prev_val = val

        waveform_str = "".join(chars)
        # Pad / truncate label to 8 chars
        label = (name[:8]).ljust(8)
        print(f"  {label}  {waveform_str}")

    print()


def cmd_waveform(analyzer: HP1631A, width: int = 80):
    """Capture and render waveform to terminal."""
    print("  Starting acquisition...")
    analyzer.start()
    print("  Waiting for data...")
    if not analyzer.wait_for_data(timeout=120):
        print("  [error] Acquisition did not complete.")
        return
    print("  Downloading waveform listing...")
    wraw = analyzer.get_waveform_listing()
    render_ascii_waveform(wraw, width=width)


# ---------------------------------------------------------------------------
# Batch capture
# ---------------------------------------------------------------------------

def batch_capture(analyzer: HP1631A, count: int, output_dir: str,
                  use_srq: bool = False, delay_between: float = 1.0):
    """
    Perform `count` sequential captures and save each to output_dir.
    Files are named trace_001.txt, trace_002.txt, etc.

    Useful for:
      - Capturing repetitive events over time
      - Statistical analysis of intermittent signals
      - Long-term monitoring (leave running overnight)

    The optional delay_between (seconds) is inserted between captures to
    allow the device under test to reset / stabilise.
    """
    os.makedirs(output_dir, exist_ok=True)
    print(f"  Batch capture: {count} traces -> {output_dir}/")

    for n in range(1, count + 1):
        print(f"\n  --- Trace {n}/{count} ---")
        analyzer.start()

        if use_srq:
            ok = analyzer.wait_for_srq(timeout=120)
        else:
            ok = analyzer.wait_for_data(timeout=120)

        if not ok:
            print(f"  [error] Trace {n} timed out, skipping.")
            continue

        slist = analyzer.get_state_listing()
        tlist = analyzer.get_timing_listing()
        wlist = analyzer.get_waveform_listing()

        filename = os.path.join(output_dir, f"trace_{n:03d}.txt")
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"# HP 1631A Batch Capture\n")
            f.write(f"# Trace {n} of {count}\n")
            f.write(f"# Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write("--- STATE LISTING ---\n")
            f.write(slist + "\n\n")
            f.write("--- TIMING LISTING ---\n")
            f.write(tlist + "\n\n")
            f.write("--- WAVEFORM LISTING ---\n")
            f.write(wlist + "\n")

        print(f"  Saved: {filename}")

        if n < count:
            time.sleep(delay_between)

    print(f"\n  Batch complete.  {count} traces saved to {output_dir}/")


# ---------------------------------------------------------------------------
# Screen dump via KE5FX ibplot
# ---------------------------------------------------------------------------

def screen_dump(port: str, gpib_addr: int, output_file: str):
    """
    Use the KE5FX GPIB Toolkit's ibplot utility to capture an HPGL screen dump
    from the HP 1631A.

    The HP 1631A can output its display as an HP-GL plot to a hardcopy device.
    ibplot intercepts this GPIB output and saves it as a file.

    Requires KE5FX GPIB Toolkit installed and 'ibplot' in PATH.
    The output file will be HPGL format (.hpgl or .plt), viewable with
    free tools such as ViewCompanion or convertible to PDF/SVG with hp2xx.
    """
    ibplot = shutil.which("ibplot")
    if ibplot is None:
        print("  [error] 'ibplot' not found in PATH.")
        print("  Install the KE5FX GPIB Toolkit and ensure its directory is in PATH.")
        print("  Download: http://www.ke5fx.com/gpib/readme.htm")
        return

    # ibplot command line:  ibplot -a <addr> -d <port> -o <outfile>
    cmd = [ibplot, "-a", str(gpib_addr), "-d", port, "-o", output_file]
    print(f"  Running: {' '.join(cmd)}")
    print("  On the HP 1631A front panel, press PRINT now...")

    try:
        result = subprocess.run(cmd, timeout=60, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  Screen dump saved to: {output_file}")
            print(f"  View with a HPGL viewer or convert with hp2xx.")
        else:
            print(f"  [error] ibplot returned code {result.returncode}")
            if result.stderr:
                print(f"  stderr: {result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        print("  [error] ibplot timed out waiting for the instrument to send data.")
    except FileNotFoundError:
        print("  [error] ibplot executable not found.")


# ---------------------------------------------------------------------------
# Quick self-test / connection check
# ---------------------------------------------------------------------------

def connection_check(gpib: PrologixGPIB, analyzer: HP1631A):
    """
    Run a sequence of quick checks to verify the Prologix and instrument
    are communicating correctly.  Useful to run first if you're having trouble.
    """
    print("=== HP 1631A Connection Check ===\n")

    print("1. Querying Prologix firmware version...")
    gpib._raw_write("++ver")
    resp = gpib.ser.readline().decode(errors="replace").strip()
    print(f"   Prologix: {resp if resp else '(no response -- check COM port)'}\n")

    print("2. Sending Interface Clear (IFC) to reset the bus...")
    gpib.ifc()
    print("   IFC sent.\n")

    print("3. Sending SDC (Selected Device Clear) to instrument...")
    analyzer.clear()
    print("   SDC sent.\n")

    print("4. Serial polling the instrument...")
    sb = gpib.serial_poll()
    if sb < 0:
        print("   [FAIL] No response to serial poll.  "
              "Check GPIB address and cable connection.")
    else:
        print(f"   Status byte: 0x{sb:02X}  ({sb})")
        print(f"     DATA_READY = {bool(sb & HP1631A.SB_DATA_READY)}")
        print(f"     ERROR      = {bool(sb & HP1631A.SB_ERROR)}")
        print(f"     RQS        = {bool(sb & HP1631A.SB_RQS)}")
        print(f"     POWER_ON   = {bool(sb & HP1631A.SB_POWER_ON)}")
    print()

    print("5. Sending ID query...")
    resp = analyzer.identify()
    if resp:
        print(f"   ID response: {resp!r}")
        print("   [PASS] Instrument is responding.\n")
    else:
        print("   [FAIL] No response to ID?.  "
              "Verify the instrument's HP-IB interface is enabled.\n")

    print("=== Check complete ===")


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="HP 1631A Logic Analyzer — extended GPIB utilities",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--port",    required=True,
                        help="Windows COM port (e.g. COM3)")
    parser.add_argument("--addr",    type=int, default=5,
                        help="GPIB address of the HP 1631A (default: 5)")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="Serial read timeout seconds (default: 5.0)")
    parser.add_argument("--srq",     action="store_true",
                        help="Use SRQ line to detect end-of-acquisition "
                             "(faster than serial poll; requires SRQ enabled on instrument)")

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check",        action="store_true",
                        help="Run connection / self-test check")
    action.add_argument("--save-config",  metavar="FILE",
                        help="Download instrument config and save to FILE")
    action.add_argument("--load-config",  metavar="FILE",
                        help="Restore instrument config from FILE")
    action.add_argument("--capture-csv",  metavar="STEM",
                        help="Capture and export CSV files (provide base name/path)")
    action.add_argument("--waveform",     action="store_true",
                        help="Capture and render ASCII timing diagram to terminal")
    action.add_argument("--batch",        type=int, metavar="N",
                        help="Batch-capture N traces to --output-dir")
    action.add_argument("--screendump",   metavar="FILE",
                        help="Capture HPGL screen dump via KE5FX ibplot")

    parser.add_argument("--output-dir",  default="captures",
                        help="Output directory for --batch (default: captures/)")
    parser.add_argument("--delay",       type=float, default=1.0,
                        help="Delay between batch captures in seconds (default: 1.0)")
    parser.add_argument("--width",       type=int, default=80,
                        help="Terminal width for ASCII waveform (default: 80)")

    args = parser.parse_args()

    print(f"Opening {args.port}, GPIB addr {args.addr} ...")
    try:
        gpib = PrologixGPIB(args.port, args.addr, timeout=args.timeout)
    except serial.SerialException as e:
        print(f"ERROR: Cannot open {args.port}: {e}")
        sys.exit(1)

    analyzer = HP1631A(gpib)

    try:
        if args.check:
            connection_check(gpib, analyzer)

        elif args.save_config:
            save_config(analyzer, args.save_config)

        elif args.load_config:
            load_config(analyzer, args.load_config)

        elif args.capture_csv:
            capture_and_export_csv(analyzer, args.capture_csv, use_srq=args.srq)

        elif args.waveform:
            cmd_waveform(analyzer, width=args.width)

        elif args.batch:
            batch_capture(analyzer, args.batch, args.output_dir,
                          use_srq=args.srq, delay_between=args.delay)

        elif args.screendump:
            screen_dump(args.port, args.addr, args.screendump)

    finally:
        gpib.close()
        print("Port closed.")


if __name__ == "__main__":
    main()
