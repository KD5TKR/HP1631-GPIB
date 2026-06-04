"""
hp1631a_gui.py  --  Graphical control panel for the HP 1631A Logic Analyzer
=============================================================================
Self-contained tkinter GUI wrapping hp1631a_extended.py and hp1631a_to_sr.py.

Features
--------
  • Connection panel: COM port selector, GPIB address, timeout, online lamp
  • CONTROL tab : START/STOP, diagnostics, menu nav, direct command entry
  • CAPTURE tab : single capture with optional .sr export, config save/load
  • EXPORT tab  : CSV export, standalone .sr conversion, listing probe
  • BATCH tab   : multi-trace capture loop with configurable count & delay
  • Output log  : colour-coded, timestamped, saveable
  • Waveform panel: canvas-based timing diagram with per-channel colour rows
  • Progress bar : shows activity during long GPIB operations
  • Status bar   : current operation description
  • Settings     : last-used port/addr/paths persisted to hp1631a_gui.json

Requires
--------
  Python 3.8+
  pyserial              (pip install pyserial)
  hp1631a_extended.py   in the same directory
  hp1631a_to_sr.py      in the same directory  (optional; enables .sr export)
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
import threading
import queue
import time
import os
import sys
import json

# ── pyserial ────────────────────────────────────────────────────────────────
try:
    import serial
    import serial.tools.list_ports
except ImportError:
    _r = tk.Tk(); _r.withdraw()
    messagebox.showerror("Missing dependency",
                         "pyserial is not installed.\n\nRun:  pip install pyserial")
    sys.exit(1)

# ── hp1631a_extended ────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

try:
    from hp1631a_extended import (
        GPIBAdapter, PrologixGPIB, NI488GPIB, USBTmcGPIB, USBGpibV2GPIB, PyVisaGPIB,
        open_gpib_adapter,
        HP1631A,
        save_config, load_config,
        capture_and_export,
        render_ascii_waveform,
        batch_capture,
        connection_check,
    )
    HAS_EXT = True
except ImportError as _e:
    HAS_EXT = False
    _ext_err = str(_e)

# ── hp1631a_to_sr ───────────────────────────────────────────────────────────
try:
    from hp1631a_to_sr import (
        convert as convert_to_sr,
        parse_listing_columns,
        identify_signal_columns,
        probe_listing,
        parse_capture_bundle,
    )
    HAS_SR = True
except ImportError:
    HAS_SR = False

# ═══════════════════════════════════════════════════════════════════════════
#  Palette  —  green-phosphor CRT
# ═══════════════════════════════════════════════════════════════════════════
BG         = "#0d1117"
BG2        = "#161b22"
BG3        = "#1c2128"
BORDER     = "#30363d"
GREEN      = "#39d353"
GREEN_DIM  = "#196127"
AMBER      = "#e6a817"
RED        = "#f85149"
BLUE       = "#58a6ff"
CYAN       = "#79c0ff"
TEXT       = "#e6edf3"
TEXT_DIM   = "#8b949e"
TEXT_DARK  = "#484f58"

# Per-channel signal colours (cycles for >8 channels)
CH_COLORS = ["#39d353","#58a6ff","#e6a817","#f85149",
             "#79c0ff","#bc8cff","#ffa657","#ff7b72",
             "#7ee787","#a5d6ff","#ffa657","#ff9492"]

FM  = ("Courier New", 10)
FMS = ("Courier New", 9)
FML = ("Courier New", 12, "bold")
FU  = ("Courier New", 10)
FUB = ("Courier New", 10, "bold")
FT  = ("Courier New", 13, "bold")
FSM = ("Courier New", 8)

SETTINGS_FILE = os.path.join(_SCRIPT_DIR, "hp1631a_gui.json")


# ═══════════════════════════════════════════════════════════════════════════
#  Platform-aware serial port enumeration
# ═══════════════════════════════════════════════════════════════════════════

def _list_serial_ports() -> list:
    """
    Return candidate serial ports for GPIB adapters.

    On Windows  : pyserial list_ports is sufficient (COM1, COM3, …).
    On Linux    : pyserial sometimes misses CDC ACM devices that appear
                  after the port scan starts, and never lists ports that
                  lack a USB serial number string.  We therefore union the
                  pyserial list with a direct glob of the common device
                  nodes used by GPIB adapters on Linux:

                    /dev/ttyUSB*   — Prologix (CP210x / FTDI)
                    /dev/ttyACM*   — xyphro USBGpib V2 (CDC ACM)
                    /dev/ttyS*     — native RS-232 (unlikely but included)

                  Results are sorted: ACM first (V2), then USB (Prologix),
                  then anything else, all in natural order.
    On macOS    : pyserial finds /dev/cu.usbserial-* and /dev/cu.usbmodem*
                  correctly, so no extra glob is needed.
    """
    found = {p.device for p in serial.tools.list_ports.comports()}

    if sys.platform.startswith("linux"):
        import glob
        for pattern in ("/dev/ttyACM*", "/dev/ttyUSB*", "/dev/ttyS[0-9]*"):
            found.update(glob.glob(pattern))

        def _sort_key(p):
            if "ACM" in p:  return (0, p)
            if "USB" in p:  return (1, p)
            return (2, p)

        return sorted(found, key=_sort_key)

    return sorted(found)


# ═══════════════════════════════════════════════════════════════════════════
#  Settings persistence
# ═══════════════════════════════════════════════════════════════════════════

def load_settings() -> dict:
    defaults = {
        "port": "", "addr": "5", "timeout": "5.0",
        "adapter": "Prologix",
        "cap_path": "capture.txt", "sr_rate": "10000000",
        "csv_stem": "trace", "batch_n": "10",
        "batch_delay": "1.0", "batch_dir": "captures",
        "use_srq": False, "also_sr": True,
    }
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
        defaults.update(saved)
    except Exception:
        pass
    return defaults


def save_settings(d: dict):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
#  Worker thread
# ═══════════════════════════════════════════════════════════════════════════

class Worker(threading.Thread):
    def __init__(self, log_q: queue.Queue):
        super().__init__(daemon=True)
        self.log_q = log_q
        self.task_q: queue.Queue = queue.Queue()
        self.gpib = None
        self.analyzer = None
        self._stop   = threading.Event()
        self._cancel = threading.Event()   # set by GUI to abort wait_for_data
        self._learn_output_dir = "."       # updated by GUI when capture path changes

    def cancel(self):
        """Signal any in-progress wait to abort."""
        self._cancel.set()

    # ── thread machinery ───────────────────────────────────────────────────
    def run(self):
        while not self._stop.is_set():
            try:
                fn, a, kw = self.task_q.get(timeout=0.2)
                try:
                    fn(*a, **kw)
                except Exception as e:
                    self._log(f"ERROR: {e}", "error")
                finally:
                    self.log_q.put(("progress_done", None))
            except queue.Empty:
                pass

    def submit(self, fn, *a, **kw):
        self.log_q.put(("progress_start", None))
        self.task_q.put((fn, a, kw))

    def submit_cmd(self, fn, *a, **kw):
        """
        Submit a single-shot instrument command with a minimum 200 ms gap
        between commands to avoid overwhelming the HP 1631A parser.
        """
        self.log_q.put(("progress_start", None))
        self.task_q.put((self._throttled, (fn, a, kw), {}))

    def _throttled(self, fn, a, kw):
        time.sleep(0.2)
        fn(*a, **kw)

    def stop(self):
        self._stop.set()

    # ── logging helpers ────────────────────────────────────────────────────
    def _log(self, msg, tag="info"):
        self.log_q.put((tag, msg))

    def _status(self, msg):
        self.log_q.put(("status", msg))

    # ── connection ─────────────────────────────────────────────────────────
    def do_connect(self, adapter_name, port, addr, timeout, eos=2,
                   resource=""):
        self._status(f"Opening {adapter_name}…")
        eos_names = {0:"CR+LF", 1:"CR", 2:"LF", 3:"None"}
        self._log(
            f"Connecting  adapter={adapter_name}  "
            f"{'port='+port+'  ' if port else ''}"
            f"{'resource='+resource+'  ' if resource else ''}"
            f"GPIB addr={addr}  "
            f"{'EOS='+eos_names.get(eos,str(eos)) if adapter_name=='Prologix' else ''}",
            "cmd")
        try:
            if self.gpib:
                try: self.gpib.close()
                except: pass
            a = adapter_name.lower()
            if a == "prologix":
                self.gpib = PrologixGPIB(port, addr, timeout=timeout, eos=eos)
            elif a in ("usbgpib v2 (xyphro)", "usbgpib v2", "usbgpibv2"):
                self.gpib = USBGpibV2GPIB(port, addr, timeout=timeout)
            elif a in ("ni-488 / linux-gpib", "ni488", "kusb-488a"):
                self.gpib = NI488GPIB(gpib_addr=addr, timeout=timeout)
            elif a in ("usbtmc (xyphro usbgpib v1)", "usbtmc (xyphro usbgpib)", "usbtmc"):
                self.gpib = USBTmcGPIB(resource=resource, gpib_addr=addr,
                                        timeout=timeout)
            elif a in ("pyvisa", "visa"):
                self.gpib = PyVisaGPIB(resource=resource or
                                        f"GPIB0::{addr}::INSTR",
                                        timeout=timeout)
            else:
                raise ValueError(f"Unknown adapter: {adapter_name}")
            self.analyzer = HP1631A(self.gpib)
            self._log("Adapter open.  Querying ID…", "info")
            resp = self.analyzer.identify()
            self._log(f"ID: {resp}", "good")
            # Configure SRQ mask so the instrument asserts SRQ (and sets
            # status byte bits) when data is ready or an error occurs.
            # MB sets the SRQ mask byte. bit1=Measurement Complete, bit5=Error → 34
            self.gpib.write("MB 34")
            time.sleep(0.2)
            self.gpib.drain()
            self.log_q.put(("connected", True))
        except Exception as e:
            self._log(f"Connection failed: {e}", "error")
            self.log_q.put(("connected", False))
        self._status("Ready")

    def do_disconnect(self):
        if self.gpib:
            try: self.gpib.close()
            except: pass
            self.gpib = None
            self.analyzer = None
        self._log("Disconnected.", "info")
        self.log_q.put(("connected", False))
        self._status("")

    # ── diagnostics ────────────────────────────────────────────────────────
    def do_check(self):
        if not self._ready(): return
        self._status("Running connection check…")
        self._log("── Connection Check ──────────────────────────", "section")
        # Adapter type / firmware version (works for all adapters)
        atype = self.gpib.adapter_type()
        self._log(f"Adapter type      : {atype}", "info")
        ver = self.gpib.firmware_version()
        if ver:
            self._log(f"Adapter firmware  : {ver}", "info")
        else:
            self._log("Adapter firmware  : (not available for this adapter)", "info")
        # IFC
        self.gpib.ifc()
        self._log("IFC sent (bus reset)", "info")
        # SDC
        self.analyzer.clear()
        self._log("SDC sent (device clear)", "info")
        # Serial poll
        sb = self.gpib.serial_poll()
        if sb < 0:
            self._log("Serial poll: no response — check address/cable", "error")
        else:
            self._log(
                f"Status byte: 0x{sb:02X}  "
                f"DATA_READY={bool(sb & HP1631A.SB_DATA_READY)}  "
                f"ERROR={bool(sb & HP1631A.SB_ERROR)}  "
                f"RQS={bool(sb & HP1631A.SB_RQS)}", "info")
        # ID
        r = self.analyzer.identify()
        if r:
            self._log(f"ID: {r}", "good")
            self.log_q.put(("instrument_id", r))
        else:
            self._log("No response to ID? — check HP-IB address", "error")
        self._log("── Check complete ────────────────────────────", "section")
        self._status("Ready")

    def do_poll(self):
        if not self._ready(): return
        sb = self.gpib.serial_poll()
        if sb < 0:
            self._log("Serial poll: no response", "error")
        else:
            self._log(
                f"Status 0x{sb:02X} │ "
                f"DATA_READY={bool(sb & HP1631A.SB_DATA_READY)} │ "
                f"ERROR={bool(sb & HP1631A.SB_ERROR)} │ "
                f"RQS={bool(sb & HP1631A.SB_RQS)} │ "
                f"POWER_ON={bool(sb & HP1631A.SB_POWER_ON)}", "info")

    def do_ifc(self):
        if not self._ready(): return
        self.gpib.ifc()
        self._log("IFC sent (Interface Clear — bus reset)", "cmd")

    def do_sdc(self):
        if not self._ready(): return
        self.analyzer.clear()
        self._log("SDC sent (Selected Device Clear — parser reset)", "cmd")

    def do_start(self):
        """Legacy entry point — delegates to do_run."""
        self.do_run("START")

    def _wait_cancellable(self, timeout: float = 120.0,
                          poll_interval: float = 0.5) -> bool:
        """
        Poll for DATA_READY with cancel support.
        Returns True if data ready, False on timeout or cancel.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._cancel.is_set():
                return False
            sb = self.gpib.serial_poll()
            if sb < 0:
                pass   # transient read error, keep trying
            elif sb & HP1631A.SB_ERROR:
                self._log(f"Instrument error flag set (status 0x{sb:02X}).", "error")
                return False
            elif sb & HP1631A.SB_MEASUREMENT_COMPLETE:
                return True
            time.sleep(poll_interval)
        return False

    def do_stop(self):
        if not self._ready(): return
        self.analyzer.stop()   # sends ST;
        self._log("ST; → (STOP)", "cmd")

    def do_clear_stuck(self):
        """
        Emergency recovery for "WARNING Awaiting HP-IB transfer".
        The instrument gets stuck when a configuration-download command
        (SFORMAT, TFORMAT, STRIGGER, TTRIGGER) is sent without the required
        data block.  IFC aborts the transfer at the bus level; SDC resets the
        instrument's HP-IB parser.  Always do this before retrying commands.
        """
        if not self._ready(): return
        self._log("── Clearing stuck HP-IB transfer ────────────", "section")
        self._status("Sending IFC to abort transfer…")
        self.gpib.ifc()
        self._log("IFC sent — bus reset, transfer aborted.", "cmd")
        time.sleep(0.5)
        self._status("Sending SDC to reset parser…")
        self.analyzer.clear()
        self._log("SDC sent — instrument parser reset.", "cmd")
        time.sleep(0.5)
        self.gpib.drain()
        self._log("Buffer drained.  Re-applying SRQ mask…", "info")
        self.gpib.write("MB 34")
        time.sleep(0.3)
        self.gpib.drain()
        # Verify instrument is alive again
        resp = self.analyzer.identify()
        if resp:
            self._log(f"Instrument responding: {resp}  ← ready for commands.", "good")
        else:
            self._log("No response to ID? after clear — may need power cycle.", "error")
        self._log("── Clear complete ────────────────────────────", "section")
        self._status("Ready")

    def do_run(self, cmd: str = "RN"):
        """
        Send RN (RUN) or RE (RESUME) and wait for Measurement Complete (bit 1).
        """
        if not self._ready(): return
        self._cancel.clear()
        self._status(f"Sending {cmd}…")
        label = {"RN":"RUN","RE":"RESUME","ST":"STOP"}.get(cmd, cmd)
        self._log(f"{cmd}; → ({label})", "cmd")
        self.log_q.put(("acquiring", True))
        self.gpib.write(cmd)
        self._log(f"Waiting for DATA_READY…  (CANCEL to abort)", "info")
        ok = self._wait_cancellable(timeout=120)
        self.log_q.put(("acquiring", False))
        if ok:
            self._log("Data ready.", "good")
            self.log_q.put(("data_ready", True))
        elif self._cancel.is_set():
            self._log("Cancelled.", "error")
        else:
            sb = self.gpib.serial_poll()
            self._log(
                f"Timeout — DATA_READY not set after 120 s.  "
                f"Final status byte: 0x{sb:02X}  "
                f"DATA_READY={bool(sb & 0x10)}  ERROR={bool(sb & 0x20)}",
                "error")
            self._log("  → If the instrument captured data manually, use", "info")
            self._log("    GET TIMING or GET WAVEFORM to download it directly.", "info")
            self._log("  → If DATA_READY never sets, the SRQ mask may need", "info")
            self._log("    adjustment — try POLL to check the status byte.", "info")
        self._status("Ready")

    def do_raw_cmd(self, cmd):
        if not self._ready(): return
        self._log(f"→ {cmd}", "cmd")
        resp = self.analyzer.send_raw(cmd)
        if resp:
            self._log(f"← {resp}", "resp")
            # Detect stuck-transfer state and warn prominently
            rl = resp.lower()
            if "awaiting" in rl and "transfer" in rl:
                self._log(
                    "⚠  Instrument is waiting for a data block that was never sent.",
                    "error")
                self._log(
                    "   Click  ⚠ CLEAR STUCK TRANSFER  to recover before sending",
                    "error")
                self._log(
                    "   any further commands.", "error")
            elif "???" in resp:
                self._log(
                    f"   Command not recognised by the HP 1631A.", "error")

    def do_menu(self, mnemonic: str):
        """Send a 2-char keyboard mnemonic (SM, FM, TM, LM, WM, CL, etc.)."""
        if not self._ready(): return
        self.gpib.write(mnemonic)
        self._log(f"{mnemonic}; → (keyboard mnemonic)", "cmd")

    # ── capture ─────────────────────────────────────────────────────────────
    def do_capture(self, path, use_srq, also_sr, sr_rate):
        if not self._ready(): return
        self._status("Capturing…")
        self._log("── Capture ───────────────────────────────────", "section")
        self._log("START →", "cmd")
        self.gpib.write("RN")
        self._cancel.clear()
        self.log_q.put(("acquiring", True))
        self._log("Waiting for trigger…  (click CANCEL to abort)", "info")
        ok = (self.analyzer.wait_for_srq(120) if use_srq
              else self._wait_cancellable(120))
        self.log_q.put(("acquiring", False))
        if not ok:
            msg = "Cancelled." if self._cancel.is_set() else "Timeout or error."
            self._log(msg, "error")
            self._status("Ready")
            return

        self._status("Downloading learn strings…")
        stem = path.rsplit(".", 1)[0] if "." in path else path
        files_saved = []

        self._log("TC; → (Configuration, ~5145 bytes)…", "info")
        tc = self.gpib.query_binary("TC", max_bytes=6000, delay=0.8)
        if len(tc) >= 4:
            p = stem + "_config.lrn"
            with open(p, "wb") as f: f.write(tc)
            files_saved.append(p)
            self._log(f"  Config: {len(tc)} B → {p}", "good")

        self._log("TT; → (Timing data)…", "info")
        tt = self.gpib.query_binary("TT", max_bytes=65536, delay=1.5)
        if len(tt) >= 4:
            p = stem + "_timing.lrn"
            with open(p, "wb") as f: f.write(tt)
            files_saved.append(p)
            header = tt[0:2].decode(errors="replace")
            count  = (tt[2] << 8) | tt[3]
            self._log(f"  Timing: {len(tt)} B  header={header!r}  → {p}", "good")
            from hp1631a_extended import LearnStringParser
            info = LearnStringParser.parse_timing_header(tt)
            self._log(f"  CH={info.get('timing_channels')}  "
                      f"States={info.get('valid_states')}  "
                      f"Runs={info.get('runs')}", "info")
            samples = LearnStringParser.extract_timing_data(tt)
            if samples and info.get("timing_channels"):
                n_ch = info["timing_channels"]
                chs = [(f"CH{c}", [(s[c] if c < len(s) else 0) for s in samples])
                       for c in range(n_ch)]
                self.log_q.put(("learn_channels", chs))

        self._log("TS; → (State data)…", "info")
        ts = self.gpib.query_binary("TS", max_bytes=65536, delay=1.5)
        if len(ts) >= 4:
            p = stem + "_state.lrn"
            with open(p, "wb") as f: f.write(ts)
            files_saved.append(p)
            self._log(f"  State: {len(ts)} B → {p}", "good")

        self._log("Navigating to LM (List) and reading screen (DR)…", "info")
        self.gpib.write("LM")
        time.sleep(0.5)
        rows = self.analyzer.read_full_screen_rows()
        screen_text = "\n".join(rows)
        p = stem + "_screen.txt"
        with open(p, "w", encoding="utf-8") as f: f.write(screen_text)
        files_saved.append(p)
        self.log_q.put(("listing_data", screen_text))
        non_empty = sum(1 for r in rows if r.strip())
        self._log(f"  Screen: {non_empty} non-empty rows → {p}", "good")

        self._log(f"Capture complete. {len(files_saved)} file(s) saved.", "good")

        if also_sr and HAS_SR:
            sr_path = os.path.splitext(path)[0] + ".sr"
            self._status("Converting to .sr…")
            self._log("Converting to sigrok .sr…", "info")
            with open(path) as f:
                txt = f.read()
            ok2 = convert_to_sr(txt, sr_path, "auto", str(sr_rate))
            self._log(f"Sigrok: {sr_path}" if ok2 else "SR conversion failed.", 
                      "good" if ok2 else "error")

        # Push listing to waveform panel (prefer timing, fall back to state)
        listing_for_display = tlist if tlist.strip() else slist
        self.log_q.put(("listing_data", listing_for_display))
        self._log("── Capture complete ──────────────────────────", "section")
        self._status("Ready")

    # ── config save / load ─────────────────────────────────────────────────
    def do_save_config(self, path):
        if not self._ready(): return
        self._status("Downloading CONFIG?…")
        self._log("Querying CONFIG?…", "cmd")
        cfg = self.analyzer.get_config()
        if not cfg:
            self._log("No CONFIG? response — check firmware revision.", "error")
            self._status("Ready")
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# HP 1631A config  {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write(cfg + "\n")
        self._log(f"Config saved: {path}", "good")
        self._status("Ready")

    def do_load_config(self, path):
        if not self._ready(): return
        self._status("Restoring config…")
        self._log(f"Loading config: {path}", "cmd")
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        cmds = [l.strip() for l in lines
                if l.strip() and not l.strip().startswith("#")]
        self._log(f"Sending {len(cmds)} commands…", "info")
        for i, cmd in enumerate(cmds, 1):
            self.gpib.write(cmd)
            time.sleep(0.15)
            if i % 10 == 0:
                self.log_q.put(("progress_pct", int(100 * i / len(cmds))))
        self._log("Config restored.", "good")
        self._status("Ready")

    # ── CSV export ─────────────────────────────────────────────────────────
    def do_csv_export(self, stem, use_srq):
        """
        Capture and export binary learn strings, then decode timing data to CSV.
        The HP 1631A has no text listing commands — data comes via TT/TS binary.
        """
        if not self._ready(): return
        self._status("CSV capture…")
        self._log("── CSV Export ────────────────────────────────", "section")
        self.analyzer.start()
        ok = (self.analyzer.wait_for_srq(120) if use_srq
              else self.analyzer.wait_for_measurement_complete(120))
        if not ok:
            self._log("Timeout.", "error")
            self._status("Ready")
            return

        import csv as _csv
        from hp1631a_extended import LearnStringParser

        # ── Timing CSV ────────────────────────────────────────────────────
        self._log("TT; → Timing learn string…", "info")
        tt = self.gpib.query_binary("TT", max_bytes=65536, delay=1.5)
        if len(tt) >= 52:
            info = LearnStringParser.parse_timing_header(tt)
            n_ch = info.get("timing_channels", 0)
            self._log(f"  {len(tt)} B  CH={n_ch}  "
                      f"States={info.get('valid_states')}  "
                      f"Runs={info.get('runs')}", "good")
            samples = LearnStringParser.extract_timing_data(tt)
            if samples:
                csv_path = stem + "_timing.csv"
                headers = [f"CH{i}" for i in range(n_ch or 8)]
                with open(csv_path, "w", newline="", encoding="utf-8") as f:
                    w = _csv.writer(f)
                    w.writerow(["sample"] + headers)
                    for idx, row in enumerate(samples):
                        w.writerow([idx] + row)
                self._log(f"  {len(samples)} rows → {csv_path}", "good")
                # Push to waveform viewer
                chs = [(f"CH{c}", [s[c] for s in samples if c < len(s)])
                       for c in range(n_ch or 8)]
                self.log_q.put(("learn_channels", chs))
        else:
            self._log(f"TT: only {len(tt)} bytes — may need valid clock/probes", "error")

        # ── State binary save ─────────────────────────────────────────────
        self._log("TS; → State learn string…", "info")
        ts = self.gpib.query_binary("TS", max_bytes=65536, delay=1.5)
        if len(ts) >= 4:
            lrn_path = stem + "_state.lrn"
            with open(lrn_path, "wb") as f: f.write(ts)
            self._log(f"  {len(ts)} B → {lrn_path} (binary)", "good")
        else:
            self._log(f"TS: only {len(ts)} bytes", "error")

        # ── Screen text ───────────────────────────────────────────────────
        self._log("LM + DR → screen text…", "info")
        self.gpib.write("LM")
        time.sleep(0.5)
        rows = self.analyzer.read_full_screen_rows()
        screen_text = "\n".join(rows)
        txt_path = stem + "_screen.txt"
        with open(txt_path, "w", encoding="utf-8") as f: f.write(screen_text)
        self.log_q.put(("listing_data", screen_text))
        self._log(f"  Screen: {sum(1 for r in rows if r.strip())} rows → {txt_path}", "good")

        self._log("── CSV export complete ───────────────────────", "section")
        self._status("Ready")

    # ── batch ──────────────────────────────────────────────────────────────
    def do_batch(self, count, out_dir, delay, use_srq):
        if not self._ready(): return
        os.makedirs(out_dir, exist_ok=True)
        self._log(f"── Batch: {count} traces → {out_dir} ──────────────", "section")
        for n in range(1, count + 1):
            self._status(f"Batch trace {n}/{count}…")
            self._log(f"Trace {n}/{count}", "info")
            self.analyzer.start()
            ok = (self.analyzer.wait_for_srq(120) if use_srq
                  else self.analyzer.wait_for_data(120))
            if not ok:
                self._log(f"Trace {n}: timeout, skipping.", "error")
                continue
            slist = self.analyzer.get_state_listing()
            tlist = self.analyzer.get_timing_listing()
            wlist = self.analyzer.get_waveform_listing()
            fn = os.path.join(out_dir, f"trace_{n:03d}.txt")
            with open(fn, "w", encoding="utf-8") as f:
                f.write(f"# Trace {n}/{count}  {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                f.write("--- STATE LISTING ---\n" + slist + "\n\n")
                f.write("--- TIMING LISTING ---\n" + tlist + "\n\n")
                f.write("--- WAVEFORM LISTING ---\n" + wlist + "\n")
            self._log(f"  Saved: {fn}", "good")
            self.log_q.put(("progress_pct", int(100 * n / count)))
            if n < count:
                time.sleep(delay)
        self._log("── Batch complete ────────────────────────────", "section")
        self._status("Ready")

    # ── waveform download ──────────────────────────────────────────────────
    def do_get_waveform(self):
        if not self._ready(): return
        self._status("Downloading WLIST?…")
        self._log("WLIST? →", "cmd")
        wraw = self.analyzer.get_waveform_listing()
        self.log_q.put(("listing_data", wraw))
        self._log(f"Waveform: {len(wraw)} bytes received.", "good")
        self._status("Ready")

    def do_get_timing(self):
        """Legacy stub — download timing via TT learn string instead."""
        if not self._ready(): return
        self.do_learn_string("TT")

    def do_get_waveform(self):
        """Legacy stub — read current screen display instead."""
        if not self._ready(): return
        self.do_display_read()

    def do_learn_string(self, cmd: str):
        """Download a binary learn string (TC, TS, TT, or TE)."""
        if not self._ready(): return
        labels = {"TC":"Config","TS":"State","TT":"Timing","TE":"Everything"}
        label = labels.get(cmd, cmd)
        self._status(f"Downloading {label} learn string…")
        self._log(f"{cmd}; → ({label} learn string, binary)", "cmd")
        self.gpib.write(cmd)
        time.sleep(0.5 if cmd != "TE" else 2.0)
        data = self.gpib.read_binary(max_bytes=65536)
        if len(data) < 4:
            self._log(f"  No data received ({len(data)} bytes).", "error")
            self._status("Ready")
            return
        header = data[0:2].decode(errors="replace")
        try:
            byte_count = (data[2] << 8) | data[3]
        except IndexError:
            byte_count = 0
        self._log(f"  {len(data)} bytes  header={header!r}  "
                  f"count={byte_count}", "good")
        # Parse timing learn string for useful info
        if cmd == "TT" and len(data) >= 52:
            from hp1631a_extended import LearnStringParser
            info = LearnStringParser.parse_timing_header(data)
            self._log(f"  Channels={info.get('timing_channels')}  "
                      f"States={info.get('valid_states')}  "
                      f"Runs={info.get('runs')}  "
                      f"Period={info.get('sample_period_str')}", "info")
            # Extract samples and push to waveform viewer
            samples = LearnStringParser.extract_timing_data(data)
            if samples and info.get("timing_channels"):
                n_ch = info["timing_channels"]
                channels = []
                for ch in range(n_ch):
                    ch_samples = [(s[ch] if ch < len(s) else 0) for s in samples]
                    channels.append((f"CH{ch}", ch_samples))
                self.log_q.put(("learn_channels", channels))
        # Save to file alongside current capture path
        import os
        out_dir = os.path.dirname(self._learn_output_dir)
        if not out_dir:
            out_dir = "."
        fname = os.path.join(out_dir, f"learn_{cmd.lower()}.lrn")
        with open(fname, "wb") as f:
            f.write(data)
        self._log(f"  Saved: {fname}", "good")
        self._status("Ready")

    def do_display_read(self):
        """DR 1 1 1472 — read the full instrument display as ASCII text."""
        if not self._ready(): return
        self._status("Reading display (DR 1 1 1472)…")
        self._log("DR 1 1 1472; → (full screen read)", "cmd")
        # Use analyzer.read_full_screen_rows() which handles
        # inverse-video stripping and CR+LF terminator removal
        try:
            rows = self.analyzer.read_full_screen_rows()
            self._log(f"  {len(rows)} display rows received.", "good")
            non_empty = [r for r in rows if r.strip()]
            for r in non_empty:
                self._log(f"  | {r}", "resp")
            display_text = "\n".join(rows)
            self.log_q.put(("listing_data", display_text))
        except Exception as e:
            self._log(f"  DR error: {e}", "error")
        self._status("Ready")

    def do_ke(self):
        """KE — read the key echo buffer (last front-panel key pressed)."""
        if not self._ready(): return
        self._log("KE; → (key echo buffer)", "cmd")
        resp = self.gpib.query("KE", delay=0.3)
        if resp:
            self._log(f"  KE response: {resp!r}", "resp")
        else:
            self._log("  KE: no response", "error")

    # ── helpers ────────────────────────────────────────────────────────────
    def _ready(self):
        if self.gpib is None:
            self._log("Not connected.", "error")
            return False
        return True


# ═══════════════════════════════════════════════════════════════════════════
#  Canvas waveform widget
# ═══════════════════════════════════════════════════════════════════════════

class WaveformCanvas(tk.Frame):
    """
    Renders a multi-channel timing diagram on a scrollable canvas.

    Each channel occupies a fixed-height row.  Transitions are drawn as
    vertical steps.  Channel labels are shown on the left.  A horizontal
    scrollbar allows panning over long captures.
    """

    ROW_H    = 28   # pixels per channel row
    LABEL_W  = 90   # pixels reserved for the channel name column
    SIG_H    = 18   # signal trace height within the row
    PAD_TOP  = 8    # top padding above first row
    MIN_PX_PER_SAMPLE = 1

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=BG, **kw)
        self._channels = []   # list of (name, [0/1 samples])
        self._zoom = 4        # pixels per sample

        # Scrollbars
        self._hbar = tk.Scrollbar(self, orient="horizontal")
        self._hbar.pack(side="bottom", fill="x")
        self._vbar = tk.Scrollbar(self, orient="vertical")
        self._vbar.pack(side="right", fill="y")

        self._canvas = tk.Canvas(
            self, bg="#0a0f0a",
            xscrollcommand=self._hbar.set,
            yscrollcommand=self._vbar.set,
            highlightthickness=0,
        )
        self._canvas.pack(fill="both", expand=True)

        self._hbar.config(command=self._canvas.xview)
        self._vbar.config(command=self._canvas.yview)

        # Zoom controls
        ctrl = tk.Frame(self, bg=BG)
        ctrl.pack(side="bottom", fill="x")
        tk.Label(ctrl, text="ZOOM", font=FSM, fg=TEXT_DIM, bg=BG
                 ).pack(side="left", padx=4)
        self._zoom_var = tk.StringVar(value="4")
        tk.Scale(ctrl, from_=1, to=20, orient="horizontal",
                 variable=self._zoom_var, command=self._on_zoom,
                 length=120, bg=BG, fg=TEXT, troughcolor=BG3,
                 highlightthickness=0, bd=0, sliderlength=12,
                 font=FSM
                 ).pack(side="left")
        tk.Label(ctrl, text="px/sample", font=FSM, fg=TEXT_DIM, bg=BG
                 ).pack(side="left", padx=(0,10))
        self._info_lbl = tk.Label(ctrl, text="", font=FSM, fg=TEXT_DIM, bg=BG)
        self._info_lbl.pack(side="left")

        self._canvas.bind("<ButtonPress-1>",   self._on_click)
        self._canvas.bind("<MouseWheel>",       self._on_wheel)
        self._canvas.bind("<Button-4>",         self._on_wheel)
        self._canvas.bind("<Button-5>",         self._on_wheel)
        self._cursor_x = None

    # ── public API ─────────────────────────────────────────────────────────

    def load_listing(self, raw_text: str):
        """Parse a listing string and render it."""
        self._channels = []
        if not raw_text.strip():
            self._draw()
            return

        # Try to use the parser from hp1631a_to_sr if available
        if HAS_SR:
            try:
                headers, rows = parse_listing_columns(raw_text)
                sig_cols = identify_signal_columns(headers, rows)
                for col_idx, col_name in sig_cols:
                    samples = []
                    for row in rows:
                        try:
                            v = int(row[col_idx], 0) if row[col_idx] else 0
                        except (ValueError, TypeError):
                            v = 1 if row[col_idx].upper() in ("H","1") else 0
                        samples.append(min(1, v))  # clamp; multi-bit → always show 1
                    self._channels.append((col_name, samples))
            except Exception:
                self._fallback_parse(raw_text)
        else:
            self._fallback_parse(raw_text)

        n = sum(len(s) for _, s in self._channels)
        ch = len(self._channels)
        self._info_lbl.configure(
            text=f"{ch} channel{'s' if ch!=1 else ''}  │  "
                 f"{len(self._channels[0][1]) if self._channels else 0} samples")
        self._draw()

    def _fallback_parse(self, text: str):
        """Minimal parser used when hp1631a_to_sr is not available."""
        lines = [l for l in text.splitlines() if l.strip()]
        if not lines:
            return
        headers = lines[0].split()
        for i, h in enumerate(headers):
            samples = []
            for row_line in lines[1:]:
                parts = row_line.split()
                if i < len(parts):
                    try:
                        v = int(parts[i], 0)
                    except ValueError:
                        v = 1 if parts[i].upper() in ("H","1") else 0
                    samples.append(min(1, v))
            if samples:
                self._channels.append((h, samples))

    # ── drawing ─────────────────────────────────────────────────────────────

    def _draw(self):
        c = self._canvas
        c.delete("all")

        if not self._channels:
            c.create_text(200, 60, text="No waveform data",
                          fill=TEXT_DARK, font=FM, anchor="nw")
            return

        zoom      = int(self._zoom_var.get())
        n_samples = len(self._channels[0][1])
        total_w   = self.LABEL_W + n_samples * zoom + 20
        total_h   = self.PAD_TOP + len(self._channels) * self.ROW_H + 20

        c.configure(scrollregion=(0, 0, total_w, total_h))

        # Grid lines (vertical, every 10 samples)
        grid_step = max(10, 50 // zoom) * zoom
        x = self.LABEL_W
        grid_x = 0
        while x < total_w:
            if grid_x % (grid_step * 5) == 0:
                c.create_line(x, 0, x, total_h, fill=BORDER, dash=(4,4))
            else:
                c.create_line(x, 0, x, total_h, fill="#1e2830", dash=(2,6))
            x += grid_step
            grid_x += grid_step

        # Channel rows
        for ch_idx, (name, samples) in enumerate(self._channels):
            colour = CH_COLORS[ch_idx % len(CH_COLORS)]
            y_top  = self.PAD_TOP + ch_idx * self.ROW_H
            y_base = y_top + self.ROW_H - 4
            y_high = y_top + 4

            # Row background (alternating)
            bg_col = "#0e1710" if ch_idx % 2 == 0 else "#0a0f0a"
            c.create_rectangle(0, y_top, total_w, y_top + self.ROW_H,
                                fill=bg_col, outline="")

            # Channel label
            c.create_text(4, y_top + self.ROW_H // 2,
                          text=name[:12], fill=colour,
                          font=("Courier New", 8, "bold"), anchor="w")

            # Separator line
            c.create_line(self.LABEL_W - 2, y_top,
                          self.LABEL_W - 2, y_top + self.ROW_H,
                          fill=BORDER)

            # Signal trace
            pts = []
            for i, bit in enumerate(samples):
                x  = self.LABEL_W + i * zoom
                y  = y_high if bit else y_base
                if pts:
                    prev_y = pts[-1]
                    if prev_y != y:
                        # Vertical edge at transition
                        pts.extend([x, prev_y, x, y])
                else:
                    pts.extend([x, y])

            # Extend to end
            if pts:
                last_y = pts[-1]
                pts.extend([self.LABEL_W + n_samples * zoom, last_y])

            if len(pts) >= 4:
                c.create_line(*pts, fill=colour, width=2)

        # Cursor line (if placed)
        if self._cursor_x is not None:
            c.create_line(self._cursor_x, 0, self._cursor_x, total_h,
                          fill=AMBER, dash=(3,3), tags="cursor")
            sample_n = max(0, (self._cursor_x - self.LABEL_W) // zoom)
            c.create_text(self._cursor_x + 3, 2,
                          text=f"#{sample_n}", fill=AMBER, font=FSM, anchor="nw")

    def _on_zoom(self, _=None):
        self._draw()

    def _on_click(self, event):
        cx = self._canvas.canvasx(event.x)
        self._cursor_x = cx
        self._draw()

    def _on_wheel(self, event):
        if event.num == 4 or event.delta > 0:
            self._canvas.xview_scroll(-3, "units")
        else:
            self._canvas.xview_scroll(3, "units")

    def load_channels(self, channels: list):
        """
        Load pre-parsed channel data directly (from learn string decoder).
        channels: list of (name, [0/1 sample, ...]) tuples.
        """
        self._channels = channels
        n = len(channels)
        s = len(channels[0][1]) if channels else 0
        self._info_lbl.configure(
            text=f"{n} channel{'s' if n!=1 else ''}  |  {s} samples")
        self._draw()

    def clear(self):
        self._channels = []
        self._cursor_x = None
        self._info_lbl.configure(text="")
        self._canvas.delete("all")



# ═══════════════════════════════════════════════════════════════════════════
#  Connection diagnostics dialog
# ═══════════════════════════════════════════════════════════════════════════

class DiagnosticsDialog(tk.Toplevel):
    """
    Step-through diagnostics window.  Opened from the CONTROL tab when the
    user is having trouble connecting.  Walks through:
      1. Adapter type / firmware version
      2. IFC + SDC bus reset
      3. Serial poll (is any device at this address?)
      4. EOS sweep — Prologix only; skipped for other adapters
      5. ID command variants — ID?, *IDN?, ID
    Each step can be run independently, and results are shown inline.
    """

    STEPS = [
        ("1  Adapter / firmware",       "step_adapter_info"),
        ("2  Bus reset (IFC+SDC)",      "step_bus_reset"),
        ("3  Serial poll",              "step_serial_poll"),
        ("4  EOS sweep (Prologix only)","step_eos_sweep"),
        ("5  ID command variants",      "step_id_variants"),
    ]

    def __init__(self, parent, worker):
        super().__init__(parent)
        self.title("Connection Diagnostics")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(560, 480)
        self.worker = worker
        self._build()
        self.grab_set()

    def _build(self):
        tk.Label(self, text="CONNECTION DIAGNOSTICS",
                 font=FT, fg=AMBER, bg=BG).pack(pady=(10,2))
        tk.Label(self,
                 text="Run each step in order.  Results appear in the log below.",
                 font=FSM, fg=TEXT_DIM, bg=BG).pack(pady=(0,8))

        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.pack(fill="x", padx=12, pady=4)
        for label, method in self.STEPS:
            b = tk.Button(btn_frame, text=label,
                          command=lambda m=method: self._run(m),
                          font=FUB, fg=BG, bg=TEXT_DIM,
                          activebackground=AMBER, activeforeground=BG,
                          relief="flat", cursor="hand2",
                          pady=4, anchor="w", padx=8)
            b.pack(fill="x", pady=2)
            b.bind("<Enter>", lambda e, btn=b: btn.configure(bg=AMBER))
            b.bind("<Leave>", lambda e, btn=b: btn.configure(bg=TEXT_DIM))

        tk.Button(btn_frame, text="\u25b6  Run All Steps",
                  command=self._run_all,
                  font=FUB, fg=BG, bg=GREEN,
                  activebackground=GREEN, activeforeground=BG,
                  relief="flat", cursor="hand2", pady=5
                  ).pack(fill="x", pady=(8,0))

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", padx=8, pady=8)

        self._log = scrolledtext.ScrolledText(
            self, font=FMS, bg=BG3, fg=TEXT,
            insertbackground=GREEN, relief="flat",
            selectbackground=BORDER, wrap="word",
            state="disabled", height=16)
        self._log.pack(fill="both", expand=True, padx=8, pady=(0,8))
        for tag, col in [("good",GREEN),("error",RED),("cmd",AMBER),
                          ("info",TEXT),("section",TEXT_DIM)]:
            self._log.tag_configure(tag, foreground=col)

        tk.Button(self, text="Close", command=self.destroy,
                  font=FU, fg=BG, bg=TEXT_DIM,
                  relief="flat", cursor="hand2", pady=3
                  ).pack(pady=(0,8))

    def _append(self, msg, tag="info"):
        self._log.configure(state="normal")
        self._log.insert("end", msg + "\n", tag)
        self._log.configure(state="disabled")
        self._log.see("end")
        self.update_idletasks()

    def _run(self, method_name):
        method = getattr(self, method_name)
        threading.Thread(target=method, daemon=True).start()

    def _run_all(self):
        def _all():
            for _, m in self.STEPS:
                getattr(self, m)()
                time.sleep(0.3)
        threading.Thread(target=_all, daemon=True).start()

    def _need_connection(self) -> bool:
        if self.worker.gpib is None:
            self._append("Not connected — open a connection first.", "error")
            return False
        return True

    def _is_prologix(self) -> bool:
        """Return True when the active adapter is a PrologixGPIB instance."""
        return HAS_EXT and isinstance(self.worker.gpib, PrologixGPIB)

    def _is_v2(self) -> bool:
        """Return True when the active adapter is a USBGpibV2GPIB instance."""
        return HAS_EXT and isinstance(self.worker.gpib, USBGpibV2GPIB)

    # \u2500\u2500 Step 1: Adapter / firmware \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    def step_adapter_info(self):
        self._append("\u2500\u2500 Step 1: Adapter / firmware \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500", "section")
        if not self._need_connection(): return
        gpib = self.worker.gpib
        self._append(f"Adapter type: {gpib.adapter_type()}", "good")
        ver = gpib.firmware_version()
        if ver:
            self._append(f"Firmware / version: {ver}", "good")
            if self._is_prologix():
                import re
                m = re.search(r"(\d+\.\d+)", ver)
                if m:
                    v = float(m.group(1))
                    if v < 6.107:
                        self._append(
                            f"  Version {v} is older than 6.107.  "
                            "++read_tmo_ms is not supported.  "
                            "The driver drains the resulting error automatically.\n"
                            "  Update firmware from http://prologix.biz/ if problems persist.",
                            "error")
                    else:
                        self._append(f"  Version {v} supports ++read_tmo_ms \u2014 OK.", "good")
            elif self._is_v2():
                self._append("  USBGpib V2: no minimum firmware version requirement.", "good")
        else:
            self._append("(No firmware version query available for this adapter.)", "info")

    # \u2500\u2500 Step 2: Bus reset \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    def step_bus_reset(self):
        self._append("\u2500\u2500 Step 2: Bus reset (IFC + SDC) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500", "section")
        if not self._need_connection(): return
        self.worker.gpib.ifc()
        self._append("IFC sent (Interface Clear \u2014 resets all devices on the bus).", "good")
        time.sleep(0.3)
        self.worker.analyzer.clear()
        self._append("SDC sent (Selected Device Clear \u2014 resets instrument parser).", "good")
        self._append("Wait 1 second for the instrument to finish resetting\u2026", "info")
        time.sleep(1.0)
        self._append("Reset complete.", "good")
        self.worker.gpib.write("MB 34")
        time.sleep(0.2)
        self.worker.gpib.drain()
        self._append("MB 34; sent (Measurement Complete + Error mask).", "good")

    # \u2500\u2500 Step 3: Serial poll \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    def step_serial_poll(self):
        self._append("\u2500\u2500 Step 3: Serial poll \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500", "section")
        if not self._need_connection(): return
        sb = self.worker.gpib.serial_poll()
        if sb < 0:
            self._append(
                "Serial poll returned no response.\n"
                "  \u2022 Check GPIB cable is seated at both ends.\n"
                "  \u2022 Verify the GPIB address matches the instrument front panel:\n"
                "    SYSTEM \u2192 CONFIG \u2192 HP-IB ADDRESS (factory default = 5).\n"
                "  \u2022 Try a different GPIB address in the connection bar.", "error")
        else:
            self._append(f"Serial poll: 0x{sb:02X} ({sb})", "good")
            self._append(
                f"  DATA_READY = {bool(sb & 0x10)}  "
                f"ERROR = {bool(sb & 0x20)}  "
                f"RQS = {bool(sb & 0x40)}  "
                f"POWER_ON = {bool(sb & 0x80)}", "info")
            self._append("A valid status byte confirms GPIB address and cabling are correct.", "good")

    # \u2500\u2500 Step 4: EOS sweep (Prologix only) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    def step_eos_sweep(self):
        self._append("\u2500\u2500 Step 4: EOS terminator sweep \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500", "section")
        if not self._need_connection(): return
        if not self._is_prologix():
            self._append(
                "EOS sweep is only applicable to the Prologix adapter.\n"
                "Other adapters (USBGpib V2, NI-488, USBTMC, PyVISA) handle "
                "termination internally \u2014 skip this step.", "info")
            return
        gpib = self.worker.gpib
        eos_names = {0:"CR+LF", 1:"CR", 2:"LF", 3:"None"}
        found_eos = None
        for eos_val in [1, 0, 2, 3]:
            name = eos_names[eos_val]
            self._append(f"  Trying EOS={eos_val} ({name})\u2026", "cmd")
            gpib.set_eos(eos_val)
            gpib.drain()
            resp = gpib.query("ID?", delay=0.5)
            if resp and "unrecognized" not in resp.lower() and len(resp) > 1:
                self._append(f"  \u2713 EOS={eos_val} ({name}) \u2192 response: {resp!r}", "good")
                found_eos = eos_val
                break
            else:
                self._append(f"  \u2717 EOS={eos_val} ({name}) \u2192 {resp!r}", "error")
            gpib.drain()
            time.sleep(0.3)

        if found_eos is not None:
            self._append(
                f"\nWorking EOS found: {found_eos} ({eos_names[found_eos]}).\n"
                f"Set the EOS dropdown to  \"{found_eos}-{eos_names[found_eos]}\"  before reconnecting.",
                "good")
        else:
            self._append(
                "\nNo EOS setting produced a valid ID? response.\n"
                "Proceed to Step 5 \u2014 the issue may be the command name.", "error")

    # \u2500\u2500 Step 5: ID command variants \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    def step_id_variants(self):
        self._append("\u2500\u2500 Step 5: ID command variants \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500", "section")
        if not self._need_connection(): return
        gpib = self.worker.gpib
        candidates = [
            ("ID?",        "Standard HP 1631A identification query"),
            ("*IDN?",      "SCPI identification (unlikely on 1631A, worth trying)"),
            ("ID",         "ID without query suffix"),
            ("SYSTEM?",    "System status query"),
            ("CONFIG?",    "Configuration query"),
        ]
        found = False
        for cmd, desc in candidates:
            self._append(f"  Trying {cmd!r}  ({desc})\u2026", "cmd")
            gpib.drain()
            resp = gpib.query(cmd, delay=0.5)
            if resp and "unrecognized" not in resp.lower() and len(resp) > 1:
                self._append(f"  \u2713 {cmd!r} \u2192 {resp!r}", "good")
                found = True
            else:
                self._append(f"  \u2717 {cmd!r} \u2192 {resp!r}", "error")
            gpib.drain()
            time.sleep(0.3)

        if not found:
            self._append(
                "\nNo ID command produced a response.\n"
                "Suggestions:\n"
                "  \u2022 Click  \u26a0 CLEAR STUCK TRANSFER  in the main window first, then retry.\n"
                "  \u2022 Re-run Step 2 (bus reset) then retry.\n"
                "  \u2022 Increase the Timeout value in the connection bar to 10 s.\n"
                "  \u2022 Verify the HP 1631A HP-IB interface is enabled in SYSTEM \u2192 CONFIG.\n"
                "  \u2022 Check the GPIB cable \u2014 try a different cable if available.", "error")


# ═══════════════════════════════════════════════════════════════════════════
#  Main application
# ═══════════════════════════════════════════════════════════════════════════

class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("HP 1631A Logic Analyzer Controller")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(960, 700)

        self._settings = load_settings()
        self._log_q: queue.Queue = queue.Queue()
        self._worker = Worker(self._log_q)
        self._worker.start()
        self._connected = False

        self._apply_styles()
        self._build_ui()
        self._load_settings_to_ui()
        self._refresh_ports()
        self._poll_queue()

    # ── styles ─────────────────────────────────────────────────────────────

    def _apply_styles(self):
        s = ttk.Style(self)
        s.theme_use("default")
        s.configure("TNotebook",      background=BG,  borderwidth=0)
        s.configure("TNotebook.Tab",  background=BG3, foreground=TEXT_DIM,
                    font=FUB, padding=[10, 4])
        s.map("TNotebook.Tab",
              background=[("selected", BG2)],
              foreground=[("selected", AMBER)])
        s.configure("TCombobox",      fieldbackground=BG3,
                    background=BG3, foreground=TEXT,
                    selectbackground=BORDER, font=FM)
        s.configure("green.Horizontal.TProgressbar",
                    troughcolor=BG3, background=GREEN, thickness=4)

    # ── UI build ────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_topbar()

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        self._build_connection_bar()

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # Progress bar (thin, under connection bar)
        self._progress = ttk.Progressbar(
            self, mode="indeterminate", length=400,
            style="green.Horizontal.TProgressbar")
        self._progress.pack(fill="x", padx=0, pady=0)

        # Main split
        main = tk.PanedWindow(self, orient="horizontal", bg=BG,
                              sashwidth=5, sashrelief="flat")
        main.pack(fill="both", expand=True)

        left  = tk.Frame(main, bg=BG, width=310)
        right = tk.Frame(main, bg=BG)
        main.add(left,  minsize=270)
        main.add(right, minsize=550)

        self._build_left(left)
        self._build_right(right)

        self._build_statusbar()

    def _build_topbar(self):
        bar = tk.Frame(self, bg=BG, padx=12, pady=6)
        bar.pack(fill="x")

        tk.Label(bar, text="HP 1631A", font=("Courier New", 17, "bold"),
                 fg=GREEN, bg=BG).pack(side="left")
        tk.Label(bar, text=" LOGIC ANALYZER CONTROLLER",
                 font=("Courier New", 10), fg=TEXT_DIM, bg=BG
                 ).pack(side="left", pady=4)

        # Online lamp + label
        right = tk.Frame(bar, bg=BG)
        right.pack(side="right")
        self._lamp = tk.Label(right, text="⬤", font=("Courier New", 16, "bold"),
                              fg=GREEN_DIM, bg=BG)
        self._lamp.pack(side="right", padx=(4, 0))
        self._status_conn = tk.Label(right, text="OFFLINE",
                                     font=("Courier New", 9, "bold"),
                                     fg=TEXT_DIM, bg=BG)
        self._status_conn.pack(side="right")

        # Instrument ID (populated after successful connect+check)
        self._id_lbl = tk.Label(bar, text="", font=("Courier New", 9),
                                fg=TEXT_DIM, bg=BG)
        self._id_lbl.pack(side="right", padx=12)

    def _build_connection_bar(self):
        bar = tk.Frame(self, bg=BG2, padx=8, pady=5)
        bar.pack(fill="x")

        def lbl(parent, text):
            tk.Label(parent, text=text, font=FSM, fg=TEXT_DIM,
                     bg=BG2).pack(side="left", padx=(0,3))

        # ── Row 1: adapter + port/resource + addr + timeout + connect ────
        row1 = tk.Frame(bar, bg=BG2)
        row1.pack(fill="x")

        lbl(row1, "ADAPTER")
        self._adapter_var = tk.StringVar(value="Prologix")
        _adapter_names = [
            "Prologix",
            "USBGpib V2 (xyphro)",
            "NI-488 / linux-gpib",
            "USBTMC (xyphro UsbGpib V1)",
            "PyVISA",
        ]
        adapter_cb = ttk.Combobox(row1, textvariable=self._adapter_var,
                                  width=20, font=FM, state="readonly",
                                  values=_adapter_names)
        adapter_cb.pack(side="left", padx=(0,8))
        adapter_cb.bind("<<ComboboxSelected>>", lambda _: self._on_adapter_change())

        lbl(row1, "PORT / RESOURCE")
        self._port_var = tk.StringVar()
        self._port_cb = ttk.Combobox(row1, textvariable=self._port_var,
                                     width=14, font=FM, state="readonly")
        self._port_cb.pack(side="left", padx=(0,2))
        # Free-text resource entry (shown for USBTMC / PyVISA)
        self._resource_var = tk.StringVar()
        self._resource_entry = tk.Entry(row1, textvariable=self._resource_var,
                                        width=22, font=FM, bg=BG3, fg=TEXT,
                                        insertbackground=GREEN, relief="flat",
                                        highlightbackground=BORDER,
                                        highlightthickness=1)
        # (packed/hidden by _on_adapter_change)

        lbl(row1, "ADDR")
        self._addr_var = tk.StringVar(value="5")
        self._entry(row1, self._addr_var, 4).pack(side="left", padx=(0,8))

        lbl(row1, "TIMEOUT")
        self._timeout_var = tk.StringVar(value="5.0")
        self._entry(row1, self._timeout_var, 5).pack(side="left")
        tk.Label(row1, text="s", font=FSM, fg=TEXT_DIM, bg=BG2
                 ).pack(side="left", padx=(1,10))

        self._btn_conn = self._btn(row1, "CONNECT",    self._do_connect,    GREEN)
        self._btn_conn.pack(side="left", padx=(0,4))
        self._btn_disc = self._btn(row1, "DISCONNECT", self._do_disconnect, RED)
        self._btn_disc.pack(side="left", padx=(0,6))
        self._btn_disc.configure(state="disabled")

        lbl(row1, "EOS")
        self._eos_var = tk.StringVar(value="2-LF")
        self._eos_cb = ttk.Combobox(row1, textvariable=self._eos_var, width=9,
                              font=FM, state="readonly",
                              values=["0-CR+LF", "1-CR", "2-LF", "3-None"])
        self._eos_cb.pack(side="left", padx=(0,8))

        self._btn(row1, "⟳", self._refresh_ports, AMBER, w=2).pack(side="left")

        # Trigger initial layout
        self._on_adapter_change()

    def _on_adapter_change(self):
        """Show/hide PORT combobox vs RESOURCE entry depending on adapter."""
        a = self._adapter_var.get()
        prologix_mode = (a == "Prologix")
        v2_mode       = a.startswith("USBGpib V2")
        ni_mode       = a.startswith("NI-488")
        serial_mode   = prologix_mode or v2_mode   # adapters that use a COM/tty port

        # EOS terminator is only meaningful for Prologix
        self._eos_cb.configure(state="readonly" if prologix_mode else "disabled")

        # Port combobox vs resource entry vs neither
        if serial_mode:
            self._resource_entry.pack_forget()
            self._port_cb.pack(side="left", padx=(0, 2))
        elif ni_mode:
            # NI-488: no port or resource needed — board index only (addr field)
            self._port_cb.pack_forget()
            self._resource_entry.pack_forget()
        else:
            # USBTMC V1 / PyVISA: free-text VISA resource string
            self._port_cb.pack_forget()
            self._resource_entry.pack(side="left", padx=(0, 8))

    def _build_left(self, parent):
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True, padx=4, pady=4)
        self._build_tab_control(nb)
        self._build_tab_capture(nb)
        self._build_tab_export(nb)
        self._build_tab_batch(nb)

    # ── CONTROL tab ─────────────────────────────────────────────────────────

    def _build_tab_control(self, nb):
        f = self._tab(nb, "CONTROL")

        self._section(f, "ACQUISITION")
        row = tk.Frame(f, bg=BG2); row.pack(fill="x", padx=6, pady=2)
        start_btn = self._btn(row, "▶  RN",
            lambda: self._worker.submit(self._worker.do_run, "RN"), GREEN, w=7)
        start_btn.pack(side="left", padx=(0,3))
        self._tooltip(start_btn, "RN; = RUN (Table 10-1) — starts acquisition")
        run_btn = self._btn(row, "▶  RE",
            lambda: self._worker.submit(self._worker.do_run, "RE"), GREEN, w=7)
        run_btn.pack(side="left", padx=(0,3))
        self._tooltip(run_btn, "RE; = RESUME — continue after single trigger")
        self._btn_cancel = self._btn(row, "⊗ CANCEL", self._do_cancel, AMBER, w=8)
        self._btn_cancel.pack(side="left", padx=(0,3))
        self._btn_cancel.configure(state="disabled")
        self._btn(row, "■  ST", lambda: self._worker.submit(
            self._worker.do_stop), RED, w=7).pack(side="left")
        tk.Label(f, text="  RN=RUN  RE=RESUME  ST=STOP  (Chapter 10 Table 10-1)",
                 font=FSM, fg=TEXT_DIM, bg=BG2).pack(anchor="w", padx=8, pady=(0,4))

        self._section(f, "DIAGNOSTICS")
        # Emergency clear — use when instrument shows "Awaiting HP-IB transfer"
        self._btn(f, "⚠  CLEAR STUCK TRANSFER  (IFC + SDC)",
                  lambda: self._worker.submit(self._worker.do_clear_stuck),
                  RED, w=30).pack(fill="x", padx=6, pady=(0,4))
        row2 = tk.Frame(f, bg=BG2); row2.pack(fill="x", padx=6, pady=2)
        for label, fn, col in [
            ("CHECK", lambda: self._worker.submit(self._worker.do_check), TEXT_DIM),
            ("POLL",  lambda: self._worker.submit(self._worker.do_poll),  TEXT_DIM),
            ("IFC",   lambda: self._worker.submit_cmd(self._worker.do_ifc),   AMBER),
            ("SDC",   lambda: self._worker.submit_cmd(self._worker.do_sdc),   AMBER),
        ]:
            self._btn(row2, label, fn, col, w=7).pack(side="left", padx=(0,3))

        self._section(f, "MENU NAVIGATION  (Table 10-1 mnemonics)")
        # Correct 2-char keyboard mnemonics from Chapter 10
        menu_rows = [
            [("SM","System Spec"),    ("FM","Format Spec")],
            [("TM","Trace/Acquire"),  ("LM","List display")],
            [("WM","Waveform"),       ("CH","Cursor Home")],
        ]
        for pair in menu_rows:
            row = tk.Frame(f, bg=BG2); row.pack(fill="x", padx=6, pady=2)
            for mnemonic, tip in pair:
                b = self._btn(row, mnemonic,
                    lambda m=mnemonic: self._worker.submit_cmd(
                        self._worker.do_menu, m), w=6)
                b.pack(side="left", padx=(0,3))
                self._tooltip(b, tip)

        self._section(f, "CURSOR / SCROLL  (Table 10-1)")
        nav_rows = [
            [("CU","Cursor Up"),   ("CD","Cursor Down"),
             ("CL","Cursor Left"), ("CR","Cursor Right")],
            [("RU","Roll Up"),     ("RD","Roll Down"),
             ("NX","NEXT[]"),      ("PV","PREV[]")],
        ]
        for pair in nav_rows:
            row = tk.Frame(f, bg=BG2); row.pack(fill="x", padx=6, pady=2)
            for mnemonic, tip in pair:
                b = self._btn(row, mnemonic,
                    lambda m=mnemonic: self._worker.submit_cmd(
                        self._worker.do_menu, m), w=5)
                b.pack(side="left", padx=(0,3))
                self._tooltip(b, tip)

        self._section(f, "DATA DOWNLOAD  (Learn Strings)")
        # TC/TS/TT/TE are the correct binary data commands (Chapter 10).
        # SLIST?/TLIST?/WLIST? and CONFIG? do not exist on this instrument.
        dl_row1 = tk.Frame(f, bg=BG2); dl_row1.pack(fill="x", padx=6, pady=2)
        for mnemonic, tip in [("TC","Config (~5145 B)"), ("TS","State data")]:
            b = self._btn(dl_row1, mnemonic,
                lambda m=mnemonic: self._worker.submit(
                    self._worker.do_learn_string, m), w=10)
            b.pack(side="left", padx=(0,3))
            self._tooltip(b, tip)
        dl_row2 = tk.Frame(f, bg=BG2); dl_row2.pack(fill="x", padx=6, pady=2)
        for mnemonic, tip in [("TT","Timing data"), ("TE","Everything")]:
            b = self._btn(dl_row2, mnemonic,
                lambda m=mnemonic: self._worker.submit(
                    self._worker.do_learn_string, m), w=10)
            b.pack(side="left", padx=(0,3))
            self._tooltip(b, tip)

        self._section(f, "DISPLAY READ  (DR command)")
        dr_row = tk.Frame(f, bg=BG2); dr_row.pack(fill="x", padx=6, pady=2)
        self._btn(dr_row, "READ SCREEN",
                  lambda: self._worker.submit(self._worker.do_display_read),
                  BLUE, w=12).pack(side="left", padx=(0,3))
        self._btn(dr_row, "KE ECHO",
                  lambda: self._worker.submit(self._worker.do_ke),
                  w=8).pack(side="left", padx=(0,3))
        self._btn(dr_row, "BP BEEP",
                  lambda: self._worker.submit_cmd(
                      self._worker.do_raw_cmd, "BP"), w=8).pack(side="left")
        tk.Label(f,
                 text="  DR reads the current display as ASCII text (23×64 chars).",
                 font=FSM, fg=TEXT_DIM, bg=BG2
                 ).pack(anchor="w", padx=8, pady=(0,4))

        self._section(f, "TROUBLESHOOTING")
        self._btn(f, "⚑  CONNECTION DIAGNOSTICS",
                  self._open_diagnostics, BLUE, w=28
                  ).pack(padx=6, pady=4, fill="x")

        self._section(f, "DIRECT COMMAND")
        cf = tk.Frame(f, bg=BG2); cf.pack(fill="x", padx=6, pady=4)
        self._raw_var = tk.StringVar()
        e = tk.Entry(cf, textvariable=self._raw_var, font=FM,
                     bg=BG3, fg=GREEN, insertbackground=GREEN,
                     relief="flat", highlightbackground=BORDER,
                     highlightthickness=1)
        e.pack(fill="x", pady=(0,4))
        e.bind("<Return>", lambda _: self._send_raw())
        self._btn(cf, "SEND  →", self._send_raw, BLUE, w=24
                  ).pack(fill="x")

    # ── CAPTURE tab ──────────────────────────────────────────────────────────

    def _build_tab_capture(self, nb):
        f = self._tab(nb, "CAPTURE")

        self._section(f, "OUTPUT FILE")
        row = tk.Frame(f, bg=BG2); row.pack(fill="x", padx=6, pady=3)
        self._cap_path_var = tk.StringVar(value="capture.txt")
        tk.Entry(row, textvariable=self._cap_path_var, font=("Courier New",9),
                 bg=BG3, fg=TEXT, insertbackground=GREEN, relief="flat",
                 highlightbackground=BORDER, highlightthickness=1, width=22
                 ).pack(side="left", padx=(0,4))
        self._btn(row, "…", self._browse_cap_out, w=2).pack(side="left")

        self._section(f, "OPTIONS")
        opt = tk.Frame(f, bg=BG2); opt.pack(fill="x", padx=6, pady=2)
        self._srq_var = tk.BooleanVar(value=False)
        self._chk(opt, "Use SRQ (faster end-of-acquisition detect)",
                  self._srq_var).pack(anchor="w")
        self._sr_var = tk.BooleanVar(value=HAS_SR)
        cb = self._chk(opt, "Also export .sr for PulseView", self._sr_var,
                       state="normal" if HAS_SR else "disabled")
        cb.pack(anchor="w")
        if not HAS_SR:
            tk.Label(opt, text="  hp1631a_to_sr.py not found",
                     font=FSM, fg=RED, bg=BG2).pack(anchor="w")

        row2 = tk.Frame(f, bg=BG2); row2.pack(fill="x", padx=6, pady=2)
        tk.Label(row2, text="Sample rate (Hz)", font=FU, fg=TEXT_DIM,
                 bg=BG2).pack(side="left", padx=(0,4))
        self._sr_rate_var = tk.StringVar(value="10000000")
        self._entry(row2, self._sr_rate_var, 13).pack(side="left")

        self._section(f, "RUN")
        self._btn(f, "▶  CAPTURE", self._do_capture, GREEN, w=24
                  ).pack(padx=6, pady=4, fill="x")
        row3 = tk.Frame(f, bg=BG2); row3.pack(fill="x", padx=6, pady=2)
        self._btn(row3, "GET TIMING",  lambda: self._worker.submit(
            self._worker.do_get_timing),   w=12).pack(side="left", padx=(0,4))
        self._btn(row3, "GET WAVEFORM",lambda: self._worker.submit(
            self._worker.do_get_waveform), w=12).pack(side="left")

        self._section(f, "INSTRUMENT CONFIGURATION")
        row4 = tk.Frame(f, bg=BG2); row4.pack(fill="x", padx=6, pady=3)
        self._btn(row4, "SAVE CONFIG", self._do_save_config, w=13
                  ).pack(side="left", padx=(0,4))
        self._btn(row4, "LOAD CONFIG", self._do_load_config, w=13
                  ).pack(side="left")

    # ── EXPORT tab ───────────────────────────────────────────────────────────

    def _build_tab_export(self, nb):
        f = self._tab(nb, "EXPORT")

        self._section(f, "CAPTURE + CSV EXPORT")
        tk.Label(f, text="Base filename:", font=FU, fg=TEXT_DIM,
                 bg=BG2).pack(anchor="w", padx=8, pady=(4,0))
        row = tk.Frame(f, bg=BG2); row.pack(fill="x", padx=6, pady=2)
        self._csv_stem_var = tk.StringVar(value="trace")
        tk.Entry(row, textvariable=self._csv_stem_var, font=("Courier New",9),
                 bg=BG3, fg=TEXT, insertbackground=GREEN, relief="flat",
                 highlightbackground=BORDER, highlightthickness=1, width=22
                 ).pack(side="left", padx=(0,4))
        self._btn(row, "…", self._browse_csv_stem, w=2).pack(side="left")
        tk.Label(f, text="  → <stem>_state.csv\n  → <stem>_timing.csv\n  → <stem>_waveform.txt",
                 font=FSM, fg=TEXT_DIM, bg=BG2, justify="left"
                 ).pack(anchor="w", padx=8)
        self._csv_srq_var = tk.BooleanVar(value=False)
        self._chk(f, "Use SRQ", self._csv_srq_var).pack(anchor="w", padx=8)
        self._btn(f, "▶  CAPTURE & EXPORT CSV",
                  self._do_csv_export, GREEN, w=26
                  ).pack(padx=6, pady=6, fill="x")

        tk.Frame(f, bg=BORDER, height=1).pack(fill="x", padx=6, pady=4)
        self._section(f, "CONVERT FILE → .sr (PulseView)")
        tk.Label(f, text="Input listing / bundle:", font=FU, fg=TEXT_DIM,
                 bg=BG2).pack(anchor="w", padx=8, pady=(4,0))
        row2 = tk.Frame(f, bg=BG2); row2.pack(fill="x", padx=6, pady=2)
        self._sr_in_var = tk.StringVar()
        tk.Entry(row2, textvariable=self._sr_in_var, font=("Courier New",9),
                 bg=BG3, fg=TEXT, insertbackground=GREEN, relief="flat",
                 highlightbackground=BORDER, highlightthickness=1, width=22
                 ).pack(side="left", padx=(0,4))
        self._btn(row2, "…", self._browse_sr_in, w=2).pack(side="left")
        row3 = tk.Frame(f, bg=BG2); row3.pack(fill="x", padx=6, pady=2)
        tk.Label(row3, text="Sample rate (Hz or 'auto')", font=FU,
                 fg=TEXT_DIM, bg=BG2).pack(side="left", padx=(0,4))
        self._sr_conv_rate_var = tk.StringVar(value="auto")
        self._entry(row3, self._sr_conv_rate_var, 10).pack(side="left")
        self._btn(f, "CONVERT → .sr",
                  self._do_convert_sr, BLUE if HAS_SR else TEXT_DARK, w=26
                  ).pack(padx=6, pady=4, fill="x")
        self._btn(f, "PROBE FILE (list channels)",
                  self._do_probe, w=26
                  ).pack(padx=6, pady=2, fill="x")

    # ── BATCH tab ────────────────────────────────────────────────────────────

    def _build_tab_batch(self, nb):
        f = self._tab(nb, "BATCH")

        self._section(f, "BATCH CAPTURE SETTINGS")
        grid = tk.Frame(f, bg=BG2); grid.pack(fill="x", padx=6, pady=6)

        def gentry(row, col, var, w=6):
            e = tk.Entry(grid, textvariable=var, width=w, font=FM,
                         bg=BG3, fg=TEXT, insertbackground=GREEN,
                         relief="flat", highlightbackground=BORDER,
                         highlightthickness=1)
            e.grid(row=row, column=col, padx=(0,14), pady=2)

        def glbl(row, col, text):
            tk.Label(grid, text=text, font=FU, fg=TEXT_DIM, bg=BG2
                     ).grid(row=row, column=col, sticky="w", padx=(0,4), pady=2)

        glbl(0,0,"Trace count"); self._batch_n_var = tk.StringVar(value="10")
        gentry(0,1, self._batch_n_var)
        glbl(0,2,"Delay (s)"); self._batch_delay_var = tk.StringVar(value="1.0")
        gentry(0,3, self._batch_delay_var)

        glbl(1,0,"Output dir")
        row = tk.Frame(grid, bg=BG2); row.grid(row=1,column=1,columnspan=3,sticky="ew")
        self._batch_dir_var = tk.StringVar(value="captures")
        tk.Entry(row, textvariable=self._batch_dir_var, font=("Courier New",9),
                 bg=BG3, fg=TEXT, insertbackground=GREEN, relief="flat",
                 highlightbackground=BORDER, highlightthickness=1, width=18
                 ).pack(side="left", padx=(0,4))
        self._btn(row, "…", self._browse_batch_dir, w=2).pack(side="left")

        self._batch_srq_var = tk.BooleanVar(value=False)
        self._chk(f, "Use SRQ", self._batch_srq_var).pack(anchor="w", padx=8)

        self._btn(f, "▶  START BATCH", self._do_batch, GREEN, w=26
                  ).pack(padx=6, pady=10, fill="x")

        # Mini progress indicator for batch
        self._section(f, "BATCH PROGRESS")
        self._batch_prog = ttk.Progressbar(
            f, orient="horizontal", length=220, mode="determinate",
            style="green.Horizontal.TProgressbar")
        self._batch_prog.pack(padx=6, pady=4, fill="x")
        self._batch_lbl = tk.Label(f, text="", font=FSM,
                                   fg=TEXT_DIM, bg=BG2)
        self._batch_lbl.pack(anchor="w", padx=8)

    # ── right panel ──────────────────────────────────────────────────────────

    def _build_right(self, parent):
        # Vertical split: log on top, waveform on bottom
        pane = tk.PanedWindow(parent, orient="vertical", bg=BG,
                              sashwidth=5, sashrelief="flat")
        pane.pack(fill="both", expand=True, padx=4, pady=4)

        # ── Log ──────────────────────────────────────────────────────────
        log_frame = tk.LabelFrame(pane, text="  OUTPUT LOG  ",
                                  font=FUB, fg=AMBER, bg=BG,
                                  highlightbackground=BORDER,
                                  highlightthickness=1)
        pane.add(log_frame, minsize=140)

        tb = tk.Frame(log_frame, bg=BG); tb.pack(fill="x", padx=4, pady=(2,0))
        self._btn(tb, "CLEAR", self._clear_log, w=6).pack(side="right")
        self._btn(tb, "SAVE",  self._save_log,  w=5).pack(side="right", padx=(0,4))

        self._log_txt = scrolledtext.ScrolledText(
            log_frame, font=("Courier New",9), bg=BG3, fg=TEXT,
            insertbackground=GREEN, relief="flat",
            selectbackground=BORDER, wrap="word", state="disabled")
        self._log_txt.pack(fill="both", expand=True, padx=4, pady=(2,4))

        for tag, col in [("info",TEXT),("good",GREEN),("error",RED),
                         ("cmd",AMBER),("resp",BLUE),("section",TEXT_DIM)]:
            self._log_txt.tag_configure(tag, foreground=col)

        # ── Waveform ─────────────────────────────────────────────────────
        wave_frame = tk.LabelFrame(pane, text="  WAVEFORM VIEWER  ",
                                   font=FUB, fg=AMBER, bg=BG,
                                   highlightbackground=BORDER,
                                   highlightthickness=1)
        pane.add(wave_frame, minsize=160)

        wt = tk.Frame(wave_frame, bg=BG); wt.pack(fill="x", padx=4, pady=(2,0))
        self._btn(wt, "CLEAR", lambda: self._waveform.clear(), w=6
                  ).pack(side="right")

        self._waveform = WaveformCanvas(wave_frame)
        self._waveform.pack(fill="both", expand=True, padx=2, pady=(0,4))

    # ── status bar ───────────────────────────────────────────────────────────

    def _build_statusbar(self):
        bar = tk.Frame(self, bg=BG3, height=20)
        bar.pack(fill="x", side="bottom")
        self._status_lbl = tk.Label(bar, text="", font=FSM,
                                    fg=TEXT_DIM, bg=BG3, anchor="w")
        self._status_lbl.pack(side="left", padx=8)
        tk.Label(bar, text="HP 1631A Controller  |  Multi-Adapter GPIB",
                 font=FSM, fg=TEXT_DARK, bg=BG3).pack(side="right", padx=8)

    # ── helper widget factories ───────────────────────────────────────────

    def _tooltip(self, widget, text: str):
        """Attach a simple hover tooltip to a widget."""
        tip = None
        def _show(e):
            nonlocal tip
            x = widget.winfo_rootx() + 20
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            tip = tk.Toplevel(widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry(f"+{x}+{y}")
            tk.Label(tip, text=text, font=FSM, fg=BG, bg=AMBER,
                     relief="flat", padx=4, pady=2).pack()
        def _hide(e):
            nonlocal tip
            if tip:
                tip.destroy()
                tip = None
        widget.bind("<Enter>", _show)
        widget.bind("<Leave>", _hide)

    def _tab(self, nb, label):
        f = tk.Frame(nb, bg=BG2)
        nb.add(f, text=f"  {label}  ")
        return f

    def _section(self, parent, label):
        tk.Label(parent, text=label, font=FSM, fg=AMBER,
                 bg=BG2).pack(anchor="w", padx=8, pady=(8,1))
        tk.Frame(parent, bg=AMBER, height=1).pack(fill="x", padx=6, pady=(0,4))

    def _entry(self, parent, var, width):
        return tk.Entry(parent, textvariable=var, width=width, font=FM,
                        bg=BG3, fg=TEXT, insertbackground=GREEN,
                        relief="flat", highlightbackground=BORDER,
                        highlightthickness=1)

    def _btn(self, parent, text, cmd, color=TEXT_DIM, w=None):
        kw = dict(text=text, command=cmd, font=FUB,
                  fg=BG, bg=color, activebackground=color,
                  activeforeground=BG, relief="flat",
                  cursor="hand2", pady=3, padx=5)
        if w: kw["width"] = w
        b = tk.Button(parent, **kw)
        b.bind("<Enter>", lambda e, c=color: b.configure(bg=TEXT))
        b.bind("<Leave>", lambda e, c=color: b.configure(bg=c))
        return b

    def _chk(self, parent, text, var, state="normal"):
        return tk.Checkbutton(parent, text=text, variable=var, font=FU,
                              bg=BG2, fg=TEXT, selectcolor=BG3,
                              activebackground=BG2, activeforeground=GREEN,
                              state=state)

    # ── settings persistence ──────────────────────────────────────────────

    def _load_settings_to_ui(self):
        s = self._settings
        self._adapter_var.set(s.get("adapter", "Prologix"))
        self._resource_var.set(s.get("resource", ""))
        self._addr_var.set(s.get("addr", "5"))
        self._timeout_var.set(s.get("timeout", "5.0"))
        self._eos_var.set(s.get("eos", "2-LF"))
        self._cap_path_var.set(s.get("cap_path", "capture.txt"))
        self._sr_rate_var.set(s.get("sr_rate", "10000000"))
        self._csv_stem_var.set(s.get("csv_stem", "trace"))
        self._batch_n_var.set(s.get("batch_n", "10"))
        self._batch_delay_var.set(s.get("batch_delay", "1.0"))
        self._batch_dir_var.set(s.get("batch_dir", "captures"))
        self._srq_var.set(s.get("use_srq", False))
        if HAS_SR:
            self._sr_var.set(s.get("also_sr", True))
        self._on_adapter_change()
        # Port will be set after refresh_ports

    def _collect_settings(self) -> dict:
        return {
            "adapter":     self._adapter_var.get(),
            "resource":    self._resource_var.get(),
            "port":        self._port_var.get(),
            "addr":        self._addr_var.get(),
            "timeout":     self._timeout_var.get(),
            "eos":         self._eos_var.get(),
            "cap_path":    self._cap_path_var.get(),
            "sr_rate":     self._sr_rate_var.get(),
            "csv_stem":    self._csv_stem_var.get(),
            "batch_n":     self._batch_n_var.get(),
            "batch_delay": self._batch_delay_var.get(),
            "batch_dir":   self._batch_dir_var.get(),
            "use_srq":     self._srq_var.get(),
            "also_sr":     self._sr_var.get() if HAS_SR else False,
        }

    # ── queue polling ─────────────────────────────────────────────────────

    def _poll_queue(self):
        while True:
            try:
                tag, val = self._log_q.get_nowait()
            except queue.Empty:
                break
            if   tag == "connected":      self._set_connected(val)
            elif tag == "data_ready":     self._log("Acquisition complete.", "good")
            elif tag == "listing_data":   self._waveform.load_listing(val)
            elif tag == "instrument_id":  self._id_lbl.configure(text=val)
            elif tag == "status":         self._status_lbl.configure(text=val)
            elif tag == "acquiring":      self._set_acquiring(val)
            elif tag == "learn_channels":  self._waveform.load_channels(val)
            elif tag == "progress_start": self._progress.start(10)
            elif tag == "progress_done":  self._progress.stop(); self._progress["value"]=0
            elif tag == "progress_pct":
                self._batch_prog["value"] = val
                self._batch_lbl.configure(text=f"{val}%")
            else:
                self._log(val, tag)

        self.after(80, self._poll_queue)

    # ── log helpers ───────────────────────────────────────────────────────

    def _log(self, msg, tag="info"):
        self._log_txt.configure(state="normal")
        ts = time.strftime("%H:%M:%S")
        self._log_txt.insert("end", f"[{ts}] ", "section")
        self._log_txt.insert("end", msg + "\n", tag)
        self._log_txt.configure(state="disabled")
        self._log_txt.see("end")

    def _clear_log(self):
        self._log_txt.configure(state="normal")
        self._log_txt.delete("1.0", "end")
        self._log_txt.configure(state="disabled")

    def _save_log(self):
        p = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text","*.txt"),("All","*.*")])
        if p:
            with open(p, "w") as f:
                f.write(self._log_txt.get("1.0","end"))
            self._log(f"Log saved: {p}", "good")

    # ── connection ────────────────────────────────────────────────────────

    def _refresh_ports(self):
        ports = _list_serial_ports()
        if not ports:
            ports = ["(none)"]
        self._port_cb["values"] = ports
        saved = self._settings.get("port", "")
        if saved and saved in ports:
            self._port_var.set(saved)
        elif ports:
            self._port_var.set(ports[0])

    def _do_connect(self):
        if not HAS_EXT:
            messagebox.showerror("Missing file",
                f"hp1631a_extended.py not found.\nError: {_ext_err}")
            return
        adapter = self._adapter_var.get()
        port    = self._port_var.get()
        resource = self._resource_var.get().strip()
        try:
            addr    = int(self._addr_var.get())
            timeout = float(self._timeout_var.get())
            eos     = int(self._eos_var.get().split("-")[0])
        except ValueError:
            messagebox.showerror("Input error",
                                 "GPIB address must be integer; timeout must be float.")
            return
        self._worker.submit(self._worker.do_connect,
                            adapter, port, addr, timeout, eos, resource)

    def _do_disconnect(self):
        self._worker.submit(self._worker.do_disconnect)

    def _set_connected(self, state: bool):
        self._connected = state
        if state:
            self._lamp.configure(fg=GREEN)
            self._status_conn.configure(text="ONLINE", fg=GREEN)
            self._btn_conn.configure(state="disabled")
            self._btn_disc.configure(state="normal")
        else:
            self._lamp.configure(fg=GREEN_DIM)
            self._status_conn.configure(text="OFFLINE", fg=TEXT_DIM)
            self._id_lbl.configure(text="")
            self._btn_conn.configure(state="normal")
            self._btn_disc.configure(state="disabled")

    # ── actions ───────────────────────────────────────────────────────────

    def _send_raw(self):
        cmd = self._raw_var.get().strip()
        if cmd:
            self._worker.submit(self._worker.do_raw_cmd, cmd)
            self._raw_var.set("")

    def _do_capture(self):
        path   = self._cap_path_var.get().strip() or "capture.txt"
        srq    = self._srq_var.get()
        sr     = self._sr_var.get() and HAS_SR
        try: rate = int(self._sr_rate_var.get())
        except ValueError: rate = 0
        import os
        self._worker._learn_output_dir = os.path.dirname(os.path.abspath(path))
        self._worker.submit(self._worker.do_capture, path, srq, sr, rate)

    def _do_save_config(self):
        p = filedialog.asksaveasfilename(
            defaultextension=".cfg",
            filetypes=[("Config","*.cfg"),("All","*.*")])
        if p:
            self._worker.submit(self._worker.do_save_config, p)

    def _do_load_config(self):
        p = filedialog.askopenfilename(
            filetypes=[("Config","*.cfg"),("All","*.*")])
        if p:
            self._worker.submit(self._worker.do_load_config, p)

    def _do_csv_export(self):
        stem = self._csv_stem_var.get().strip() or "trace"
        srq  = self._csv_srq_var.get()
        self._worker.submit(self._worker.do_csv_export, stem, srq)

    def _do_convert_sr(self):
        if not HAS_SR:
            messagebox.showerror("Missing file",
                "hp1631a_to_sr.py not found in the same directory.")
            return
        in_path = self._sr_in_var.get().strip()
        if not in_path or not os.path.exists(in_path):
            messagebox.showerror("File not found",
                "Please select a valid input listing file.")
            return
        rate_str = self._sr_conv_rate_var.get().strip() or "auto"
        out_path = os.path.splitext(in_path)[0] + ".sr"
        self._log(f"Converting {in_path} → {out_path}…", "cmd")

        def _run():
            self._log_q.put(("progress_start", None))
            try:
                with open(in_path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
                ok = convert_to_sr(text, out_path, "auto", rate_str)
                tag = "good" if ok else "error"
                msg = f"Converted: {out_path}" if ok else "SR conversion failed."
                self._log_q.put((tag, msg))
                if ok:
                    # Load into waveform viewer
                    with open(in_path) as f: bundle = f.read()
                    sections = parse_capture_bundle(bundle)
                    disp = sections.get("timing") or sections.get("state") or ""
                    if disp:
                        self._log_q.put(("listing_data", disp))
            finally:
                self._log_q.put(("progress_done", None))

        threading.Thread(target=_run, daemon=True).start()

    def _do_probe(self):
        if not HAS_SR:
            messagebox.showerror("Missing file", "hp1631a_to_sr.py not found.")
            return
        in_path = self._sr_in_var.get().strip()
        if not in_path or not os.path.exists(in_path):
            messagebox.showerror("File not found",
                "Please select a listing file first.")
            return
        self._log(f"Probing: {in_path}", "cmd")

        def _run():
            self._log_q.put(("progress_start", None))
            try:
                with open(in_path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
                import io as _io, sys as _sys
                old = _sys.stdout; _sys.stdout = buf = _io.StringIO()
                try:
                    probe_listing(text)
                finally:
                    _sys.stdout = old
                for line in buf.getvalue().splitlines():
                    self._log_q.put(("info", line))
            finally:
                self._log_q.put(("progress_done", None))

        threading.Thread(target=_run, daemon=True).start()

    def _do_batch(self):
        try:
            n     = int(self._batch_n_var.get())
            delay = float(self._batch_delay_var.get())
        except ValueError:
            messagebox.showerror("Input error",
                "Count must be integer, delay must be float.")
            return
        out_dir = self._batch_dir_var.get().strip() or "captures"
        srq     = self._batch_srq_var.get()
        self._batch_prog["maximum"] = 100
        self._batch_prog["value"]   = 0
        self._worker.submit(self._worker.do_batch, n, out_dir, delay, srq)

    # ── file browsers ─────────────────────────────────────────────────────

    def _browse_cap_out(self):
        p = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text","*.txt"),("All","*.*")])
        if p: self._cap_path_var.set(p)

    def _browse_csv_stem(self):
        p = filedialog.asksaveasfilename(title="Choose base name (no extension)")
        if p: self._csv_stem_var.set(os.path.splitext(p)[0])

    def _browse_sr_in(self):
        p = filedialog.askopenfilename(
            filetypes=[("Text","*.txt"),("All","*.*")])
        if p: self._sr_in_var.set(p)

    def _browse_batch_dir(self):
        p = filedialog.askdirectory(title="Select batch output directory")
        if p: self._batch_dir_var.set(p)

    # ── close ─────────────────────────────────────────────────────────────

    def _set_acquiring(self, state: bool):
        """Enable/disable the CANCEL button based on acquisition state."""
        self._btn_cancel.configure(
            state="normal" if state else "disabled")

    def _do_cancel(self):
        self._worker.cancel()
        self._log("Cancel requested…", "error")

    def _open_diagnostics(self):
        DiagnosticsDialog(self, self._worker)

    def on_close(self):
        save_settings(self._collect_settings())
        if self._connected:
            self._worker.do_disconnect()
        self._worker.stop()
        self.destroy()


# ═══════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
