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
from tkinter import ttk, scrolledtext, filedialog, messagebox, simpledialog
import threading
import queue
import time
import os
import sys
import json
import datetime

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
        LearnStringParser,
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

# ── hp1631a_lrn_to_sr ───────────────────────────────────────────────────────
try:
    from hp1631a_lrn_to_sr import (
        convert as convert_lrn_to_sr,
        convert_state as convert_state_lrn_to_sr,
        CHANNEL_PRESETS,
        TTLearnString,
        StateLearnString,
    )
    HAS_LRN_SR = True
except ImportError:
    HAS_LRN_SR = False
    CHANNEL_PRESETS = {}

# ── hp1631a_diff ────────────────────────────────────────────────────────────
try:
    from hp1631a_diff import (
        load_capture,
        diff_captures,
        DiffResult,
    )
    HAS_DIFF = True
except ImportError:
    HAS_DIFF = False

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
        "sr_preset": "(none)",
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
#  Target profiles  (#7 — named per-target capture configurations)
# ═══════════════════════════════════════════════════════════════════════════
#
# A "profile" bundles everything needed to re-point this tool at a
# previously-probed target without re-entering settings by hand: GPIB
# address, sample rate, the channel-label preset (or a custom channel
# name list) for .sr export, and an optional trigger pattern. Useful when
# rotating probe connections between several systems on the bench (e.g.
# a PDP-11/23, an AT&T 3B2, and a broadcast graphics board), each of
# which has a different, fixed acquisition setup worth not re-deriving
# every session.
#
# Stored as a flat dict of name -> profile in PROFILES_FILE, separate
# from hp1631a_gui.json (connection/UI state) so profiles aren't
# overwritten by the normal per-session settings autosave.

PROFILES_FILE = os.path.join(_SCRIPT_DIR, "hp1631a_profiles.json")

PROFILE_DEFAULTS = {
    "description":   "",
    "adapter":       "Prologix",
    "gpib_addr":     "5",
    "sr_rate":       "10000000",
    "sr_preset":     "(none)",
    "channel_names": "",   # comma-separated; used if sr_preset == "(none)"
    "trigger_pattern": "",
    "trigger_label_row": "1",
    "notes":         "",
}


def load_profiles() -> dict:
    try:
        with open(PROFILES_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_profiles(profiles: dict):
    try:
        with open(PROFILES_FILE, "w") as f:
            json.dump(profiles, f, indent=2)
    except Exception:
        pass


def make_profile(**overrides) -> dict:
    """Return a new profile dict with PROFILE_DEFAULTS filled in, then
    overridden by any keyword arguments provided."""
    p = dict(PROFILE_DEFAULTS)
    p.update(overrides)
    return p


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
    def do_capture(self, path, use_srq, also_sr, sr_rate, preset=None,
                   glitch_arm=False, glitch_config=None):
        if not self._ready(): return
        self._status("Capturing…")
        self._log("── Capture ───────────────────────────────────", "section")

        # Best-effort trace mode detection (#3): the GUI always downloads
        # both TT and TS, so this is informational rather than blocking —
        # but it lets us warn *before* arming if the front panel is
        # clearly showing a mode that won't produce timing data, etc.
        try:
            mode_info = self.analyzer.detect_trace_mode()
            mode = mode_info["mode"]
            if mode == "state":
                self._log(
                    "Trace mode appears to be STATE (List screen header: "
                    f"{mode_info['raw_header']!r}). Timing (TT) download "
                    "may come back empty unless Timing mode is also armed.",
                    "info")
            elif mode == "timing":
                self._log(
                    "Trace mode appears to be TIMING (List screen header: "
                    f"{mode_info['raw_header']!r}). State (TS) download "
                    "may come back empty unless State mode is also armed.",
                    "info")
            elif mode == "waveform":
                self._log(
                    "List screen is showing WAVEFORM (analog) data, not "
                    "State or Timing digital listings.", "info")
        except Exception:
            pass  # detection is best-effort; never block capture on it

        # ── Glitch detect arming ──────────────────────────────────────────────
        # Strategy: send a previously saved glitch-enabled TC config as RC
        # before arming RN.  The user saves a "glitch ON" TC via the
        # SAVE NOW button (which calls do_save_glitch_config below).  This
        # approach is reliable because we are sending the exact bytes the
        # instrument itself produced when glitch mode was enabled — no
        # manual byte-offset patching required.
        if glitch_arm:
            cfg_path = glitch_config or os.path.join(
                getattr(self, "_learn_output_dir", "."), "glitch_config.lrn")
            if os.path.exists(cfg_path):
                self._log(f"Glitch arm: sending config from {cfg_path}…", "info")
                try:
                    with open(cfg_path, "rb") as f:
                        gc_data = f.read()
                    if len(gc_data) >= 4 and gc_data[0:2] == b"RC":
                        from hp1631a_extended import (
                            PrologixGPIB, USBTmcGPIB, PyVisaGPIB, NI488GPIB)
                        gpib = self.gpib
                        if isinstance(gpib, PrologixGPIB):
                            gpib._raw_write(f"++addr {gpib.gpib_addr}")
                            gpib.ser.write(gc_data)
                        elif isinstance(gpib, (USBTmcGPIB, PyVisaGPIB)):
                            gpib._inst.write_raw(gc_data)
                        elif isinstance(gpib, NI488GPIB):
                            gpib._gpib.write(gpib._dev, gc_data)
                        else:
                            self._log("  Glitch config: adapter type does not support "
                                      "binary RC write — skipping.", "error")
                        time.sleep(0.8)
                        self._log("  Glitch config sent (RC).", "good")
                    else:
                        self._log(f"  Glitch config invalid (expected RC header) — "
                                  "skipping.", "error")
                except Exception as e:
                    self._log(f"  Glitch arm failed: {e}", "error")
            else:
                self._log(
                    f"Glitch arm: config file not found ({cfg_path}).  "
                    "Use SAVE NOW in the Capture tab after enabling glitch "
                    "capture on the instrument.", "error")

        # Re-assert SRQ mask and log current status before starting
        self.gpib.write("MB 34")
        time.sleep(0.15)
        sb_pre = self.gpib.serial_poll()
        self._log(
            f"Pre-RN status byte: 0x{sb_pre:02X}  "
            f"MEAS_COMPLETE={bool(sb_pre & HP1631A.SB_MEASUREMENT_COMPLETE)}  "
            f"NOT_BUSY={bool(sb_pre & HP1631A.SB_NOT_BUSY)}  "
            f"ERROR={bool(sb_pre & HP1631A.SB_ERROR)}", "info")

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

        sb_post = self.gpib.serial_poll()
        self._log(
            f"Post-wait status byte: 0x{sb_post:02X}  "
            f"MEAS_COMPLETE={bool(sb_post & HP1631A.SB_MEASUREMENT_COMPLETE)}  "
            f"NOT_BUSY={bool(sb_post & HP1631A.SB_NOT_BUSY)}", "info")

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
        tt_info = {}
        if len(tt) >= 4:
            p = stem + "_timing.lrn"
            with open(p, "wb") as f: f.write(tt)
            files_saved.append(p)
            header = tt[0:2].decode(errors="replace")
            count  = (tt[2] << 8) | tt[3]
            self._log(f"  Timing: {len(tt)} B  header={header!r}  → {p}", "good")
            tt_info = LearnStringParser.parse_timing_header(tt)
            tt_ch = tt_info.get("timing_channels", 0)
            tt_st = tt_info.get("valid_states", 0)
            glitch_actual = tt_info.get("glitch_mode", False)
            self._log(
                f"  CH={tt_ch}  States={tt_st}  "
                f"Runs={tt_info.get('runs')}  "
                f"Glitch={'ON' if glitch_actual else 'off'}", "info")
            # Verify glitch state against arming intent
            if glitch_arm and not glitch_actual:
                self._log(
                    "  ⚠ Glitch arm was ON but glitch capture is OFF in "
                    "the downloaded TT.  The saved config may pre-date "
                    "enabling glitch mode on the instrument.  Use "
                    "SAVE NOW to refresh the glitch config.", "error")
            elif glitch_arm and glitch_actual:
                self._log("  Glitch capture confirmed active.", "good")
            if tt_ch == 0 or tt_st == 0:
                self._log(
                    f"  ⚠ Timing learn string is empty (CH={tt_ch}, States={tt_st}). "
                    "Either the timing pod isn't assigned in Format, or "
                    "State (not Timing) is the active trace mode. "
                    "Use VERIFY ACQUISITION for a full cross-check.", "error")
            samples = LearnStringParser.extract_timing_data(tt)
            if samples and tt_ch:
                chs = [(f"CH{c}", [(s[c] if c < len(s) else 0) for s in samples])
                       for c in range(tt_ch)]
                self.log_q.put(("learn_channels", chs))
                # Pass sample rate to waveform viewer for cursor delta time display
                if HAS_LRN_SR:
                    try:
                        _tt_obj = TTLearnString(tt)
                        if _tt_obj.sample_rate_hz > 0:
                            self.log_q.put(("wave_samplerate", _tt_obj.sample_rate_hz))
                    except Exception:
                        pass

        self._log("TS; → (State data)…", "info")
        ts = self.gpib.query_binary("TS", max_bytes=65536, delay=1.5)
        ts_info = {}
        if len(ts) >= 4:
            p = stem + "_state.lrn"
            with open(p, "wb") as f: f.write(ts)
            files_saved.append(p)
            ts_info = LearnStringParser.parse_state_header(ts)
            ts_ch = ts_info.get("state_channels", 0)
            ts_st = ts_info.get("valid_states", 0)
            self._log(f"  State: {len(ts)} B  CH={ts_ch}  States={ts_st}  → {p}", "good")
            if ts_ch == 0 or ts_st == 0:
                self._log(
                    f"  ⚠ State learn string is empty (CH={ts_ch}, States={ts_st}). "
                    "Either the state pod isn't assigned in Format, or "
                    "Timing (not State) is the active trace mode.", "error")
            else:
                # Push state channel data to the waveform viewer.
                # extract_state_data() returns 1024×40 bit records; filter to
                # only channels that have at least one transition (i.e. only
                # the probed/connected pod channels), and apply preset names.
                state_records = LearnStringParser.extract_state_data(ts)
                if state_records and HAS_LRN_SR:
                    _n_bits = 40
                    _preset_names = (
                        list(CHANNEL_PRESETS.get(preset, []))
                        if preset and preset in CHANNEL_PRESETS else []
                    )
                    # Pad with generic names
                    while len(_preset_names) < _n_bits:
                        _preset_names.append(f"S{len(_preset_names)}")
                    state_chs = []
                    for _ci in range(_n_bits):
                        _bits = [(rec[_ci] if _ci < len(rec) else 0) for rec in state_records]
                        _edges = sum(1 for _j in range(1, len(_bits)) if _bits[_j] != _bits[_j-1])
                        if _edges > 0:   # skip static (unconnected) channels
                            state_chs.append((_preset_names[_ci], _bits))
                    if state_chs:
                        self._log(f"  Waveform: {len(state_chs)} active state channel(s)", "info")
                        self.log_q.put(("learn_channels", state_chs))
                    else:
                        self._log("  (All state bits static — nothing to show in waveform viewer)", "info")
                elif state_records:
                    # HAS_LRN_SR is False but extract_state_data exists in extended
                    state_chs = []
                    for _ci in range(40):
                        _bits = [(rec[_ci] if _ci < len(rec) else 0) for rec in state_records]
                        _edges = sum(1 for _j in range(1, len(_bits)) if _bits[_j] != _bits[_j-1])
                        if _edges > 0:
                            state_chs.append((f"S{_ci}", _bits))
                    if state_chs:
                        self.log_q.put(("learn_channels", state_chs))

        # Cross-mode summary: tell the user which mode (if either) actually
        # captured data, so an empty TT alongside a populated TS (or vice
        # versa) is immediately explained rather than silently logged.
        tt_ok = tt_info.get("timing_channels", 0) > 0 and tt_info.get("valid_states", 0) > 0
        ts_ok = ts_info.get("state_channels", 0)  > 0 and ts_info.get("valid_states", 0)  > 0
        if tt_ok and not ts_ok and ts_info:
            self._log("  → Timing data is good; State mode is not configured/active.", "info")
        elif ts_ok and not tt_ok and tt_info:
            self._log("  → State data is good; Timing mode is not configured/active.", "info")
        elif not tt_ok and not ts_ok:
            self._log(
                "  ⚠ Neither Timing nor State produced data. Run VERIFY ACQUISITION "
                "in the CONTROL tab for a full diagnostic.", "error")

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

        # ── Capture config sidecar (#6) ────────────────────────────────────
        # Save a small JSON file alongside the .lrn/.txt outputs recording
        # everything needed to understand this capture later: which mode
        # was detected/active, channel/state counts for both TT and TS,
        # the GPIB connection used, the preset and sample rate applied for
        # .sr export, and the list of files this capture produced. This is
        # the "wait, what was I even capturing" answer for future-you.
        sidecar_path = stem + "_capture.json"
        try:
            tc_summary = None
            if len(tc) >= 4:
                tc_info = LearnStringParser.parse_config_header(tc)
                tc_summary = {
                    "valid": tc_info.get("valid", False),
                    "bytes": len(tc),
                }

            sidecar = {
                "schema_version": 1,
                "captured_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "capture_stem": stem,
                "detected_trace_mode": locals().get("mode"),
                "connection": {
                    "adapter": type(self.gpib).__name__ if self.gpib else None,
                    "gpib_address": getattr(self.gpib, "gpib_addr", None),
                },
                "config": tc_summary,
                "timing": {
                    "channels": tt_info.get("timing_channels", 0),
                    "states":   tt_info.get("valid_states", 0),
                    "runs":     tt_info.get("runs"),
                    "ok":       tt_ok,
                } if tt_info else None,
                "state": {
                    "channels": ts_info.get("state_channels", 0),
                    "states":   ts_info.get("valid_states", 0),
                    "ok":       ts_ok,
                } if ts_info else None,
                "sr_export": {
                    "requested": bool(also_sr),
                    "preset": preset,
                    "samplerate_hz": sr_rate,
                } if also_sr else None,
                "screen_nonempty_rows": non_empty,
                "files": [os.path.basename(f) for f in files_saved],
            }
            with open(sidecar_path, "w", encoding="utf-8") as f:
                json.dump(sidecar, f, indent=2)
            files_saved.append(sidecar_path)
            self._log(f"  Config record: → {sidecar_path}", "info")
        except Exception as e:
            self._log(f"  (Could not write capture config sidecar: {e})", "info")

        self._log(f"Capture complete. {len(files_saved)} file(s) saved.", "good")

        if also_sr:
            sr_path = stem + ".sr"
            self._status("Converting to .sr…")
            self._log("Converting to sigrok .sr…", "info")

            # Presets are tagged by which capture mode they target. The
            # LSI-11/Q-bus presets are built for synchronous STATE capture
            # (clocked on BCLK via J/K/L); the HC11 preset is built for
            # async TIMING capture (free-running clock resolving AS/E
            # edges). Route to the matching .lrn file and converter
            # automatically so the user doesn't have to know this.
            state_presets = {"lsi11-16", "lsi11-ctrl"}
            use_state = preset in state_presets

            if use_state:
                lrn_path = stem + "_state.lrn"
                mode_label = "state"
            else:
                lrn_path = stem + "_timing.lrn"
                mode_label = "timing"

            ok2 = False
            if HAS_LRN_SR and os.path.exists(lrn_path):
                if use_state:
                    ok2 = convert_state_lrn_to_sr(
                        lrn_path=lrn_path,
                        output_path=sr_path,
                        samplerate_override=(sr_rate if sr_rate > 0 else 1_000_000),
                        preset=preset,
                    )
                else:
                    ok2 = convert_lrn_to_sr(
                        lrn_path=lrn_path,
                        output_path=sr_path,
                        preset=preset,
                    )
            elif HAS_SR:
                # Fallback: ASCII screen text → .sr (requires user-supplied rate)
                screen_path = stem + "_screen.txt"
                if os.path.exists(screen_path):
                    with open(screen_path, encoding="utf-8", errors="replace") as f:
                        txt = f.read()
                    ok2 = convert_to_sr(txt, sr_path, "auto", str(sr_rate))
                else:
                    self._log("SR fallback: screen text file not found.", "error")
            else:
                self._log(
                    "hp1631a_lrn_to_sr.py not found — cannot convert to .sr.  "
                    "Place it in the same directory as this script.",
                    "error")
            if ok2:
                self._log(f"Sigrok: {sr_path}  ({mode_label} mode"
                          f"{', preset=' + preset if preset else ''})", "good")
                if preset == "hc11-19":
                    self._log(
                        "  Note: the HC11 decoder requires all 19 channels "
                        "(AD0-7, A8-15, AS, E, R/W) to run — if fewer were "
                        "captured, decode will fail in PulseView even though "
                        "this .sr file was written successfully.", "info")
            elif HAS_LRN_SR or HAS_SR:
                # Try to give a more specific reason than "failed"
                if os.path.exists(lrn_path) and os.path.getsize(lrn_path) < 60:
                    self._log(
                        f"SR conversion failed: {mode_label} learn string is "
                        f"too short ({os.path.getsize(lrn_path)} bytes) — "
                        "States=0 means no data was captured. "
                        "Check that the trigger fired and the acquisition completed.",
                        "error")
                elif not os.path.exists(lrn_path):
                    self._log(
                        f"SR conversion failed: {lrn_path} not found. "
                        f"{'State' if use_state else 'Timing'} learn string "
                        "may not have downloaded — check the log above.",
                        "error")
                else:
                    self._log("SR conversion failed — check log output above.", "error")

        # screen_text was already queued to the waveform panel above
        self._log("── Capture complete ──────────────────────────", "section")
        self._status("Ready")

    # ── config save / load ─────────────────────────────────────────────────
    def do_save_glitch_config(self, path):
        """Download TC learn string and save as a glitch-capture config file."""
        if not self._ready(): return
        self._status("Saving glitch config…")
        self._log("Saving glitch config: downloading TC learn string…", "info")
        tc = self.gpib.query_binary("TC", max_bytes=6000, delay=0.8)
        if len(tc) < 4:
            self._log("TC returned no data — not connected?", "error")
            self._status("Ready")
            return
        if tc[0:2] not in (b"RC", b"TC"):
            try:
                hdr = tc[0:2].decode("ascii", errors="replace")
            except Exception:
                hdr = repr(tc[0:2])
            self._log(f"Unexpected TC header {hdr!r} (expected 'RC').", "error")
            self._status("Ready")
            return
        with open(path, "wb") as f:
            f.write(tc)
        # Parse and report the glitch mode from the embedded TT timing header
        glitch_on = self._check_tc_glitch_mode(tc)
        glitch_str = ("ON" if glitch_on else "OFF") if glitch_on is not None else "unknown"
        self._log(
            f"Glitch config saved: {path}  ({len(tc)} bytes)  "
            f"Glitch mode in config: {glitch_str}", "good")
        if glitch_on is False:
            self._log(
                "  ⚠ The saved config has glitch capture OFF.  "
                "Enable glitch mode on the instrument (Format menu) first, "
                "then click SAVE NOW again.", "error")
        self._status("Ready")

    @staticmethod
    def _check_tc_glitch_mode(tc_data: bytes):
        """
        Attempt to determine whether glitch capture is enabled in a TC blob
        by searching for any byte pattern that could be the TT-format glitch
        flag embedded within the TC configuration data.

        The HP 1631A TC learn string embeds the complete instrument state.
        The timing format glitch flag (byte 9 from the start of any TT
        sub-block) appears at a fixed but firmware-version-dependent offset
        within TC.  We use a best-effort scan: look for the TT header magic
        bytes "RT" followed by a plausible byte-count and read byte 9 from
        that position.  Returns True/False/None (None = pattern not found).
        """
        if not tc_data or len(tc_data) < 14:
            return None
        # Search for "RT" within the TC payload (after the 4-byte header)
        for i in range(4, len(tc_data) - 10):
            if tc_data[i:i+2] == b"RT" and i + 9 < len(tc_data):
                return bool(tc_data[i + 9])
        return None

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

        # ── State CSV ─────────────────────────────────────────────────────
        self._log("TS; → State learn string…", "info")
        ts = self.gpib.query_binary("TS", max_bytes=65536, delay=1.5)
        if len(ts) >= 21:
            lrn_path = stem + "_state.lrn"
            with open(lrn_path, "wb") as f: f.write(ts)
            ts_info = LearnStringParser.parse_state_header(ts)
            ts_st = ts_info.get("valid_states", 0)
            self._log(f"  {len(ts)} B  States={ts_st}  → {lrn_path}", "good")
            state_records = LearnStringParser.extract_state_data(ts)
            if state_records:
                # Find active (non-static) bit positions
                active_chs = []
                for _ci in range(40):
                    _bits = [(rec[_ci] if _ci < len(rec) else 0) for rec in state_records]
                    _edges = sum(1 for _j in range(1, len(_bits)) if _bits[_j] != _bits[_j-1])
                    if _edges > 0:
                        active_chs.append((_ci, _bits))
                if active_chs:
                    csv_path = stem + "_state.csv"
                    headers = [f"S{ci}" for ci, _ in active_chs]
                    with open(csv_path, "w", newline="", encoding="utf-8") as f:
                        w = _csv.writer(f)
                        w.writerow(["state"] + headers)
                        for idx in range(len(state_records)):
                            w.writerow([idx] + [_bits[idx] for _, _bits in active_chs])
                    self._log(f"  {len(state_records)} states, "
                              f"{len(active_chs)} active channels → {csv_path}", "good")
                    # Push to waveform viewer
                    state_chs = [(f"S{ci}", _bits) for ci, _bits in active_chs]
                    self.log_q.put(("learn_channels", state_chs))
                else:
                    self._log("  (All 40 state bits static — no CSV written)", "info")
            else:
                self._log(f"  State learn string empty ({ts_st} states) — no CSV", "info")
        else:
            self._log(f"TS: only {len(ts)} bytes — skipping state decode", "error")

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
    def do_batch(self, count, out_dir, delay, use_srq, profile=None):
        """
        Capture `count` traces in a loop, saving binary learn strings
        (config/timing/state + screen text) for each, the same way
        do_capture() does for a single trace.

        profile : optional dict from PROFILE_DEFAULTS (see load_profiles())
                  — if given, its sr_preset/sr_rate/channel_names are
                  applied to each trace's .sr export, and gpib_addr is
                  logged for the record even though changing it on a
                  live connection isn't done automatically (the user is
                  expected to have already connected at the right address;
                  see _apply_profile_to_connection_fields in the App class).
        """
        if not self._ready(): return
        os.makedirs(out_dir, exist_ok=True)
        self._log(f"── Batch: {count} traces → {out_dir} ──────────────", "section")

        preset = None
        sr_rate = 10_000_000
        channel_names = None
        if profile:
            self._log(f"Using profile: {profile.get('description') or '(unnamed)'}",
                      "info")
            raw_preset = profile.get("sr_preset", "(none)")
            preset = None if raw_preset in (None, "(none)", "") else raw_preset
            try:
                sr_rate = int(profile.get("sr_rate", 10_000_000))
            except (TypeError, ValueError):
                sr_rate = 10_000_000
            cn = profile.get("channel_names", "")
            if cn and not preset:
                channel_names = [n.strip() for n in cn.split(",") if n.strip()]

        for n in range(1, count + 1):
            self._status(f"Batch trace {n}/{count}…")
            self._log(f"Trace {n}/{count}", "info")

            self.gpib.write("MB 34")
            time.sleep(0.15)
            self.gpib.write("RN")
            self._cancel.clear()
            ok = (self.analyzer.wait_for_srq(120) if use_srq
                  else self._wait_cancellable(120))
            if not ok:
                msg = "Cancelled — stopping batch." if self._cancel.is_set() \
                      else f"Trace {n}: timeout, skipping."
                self._log(msg, "error")
                if self._cancel.is_set():
                    break
                continue

            stem = os.path.join(out_dir, f"trace_{n:03d}")

            tt = self.gpib.query_binary("TT", max_bytes=65536, delay=1.5)
            ts = self.gpib.query_binary("TS", max_bytes=65536, delay=1.5)

            saved = []
            if len(tt) >= 4:
                p = stem + "_timing.lrn"
                with open(p, "wb") as f: f.write(tt)
                saved.append(p)
            if len(ts) >= 4:
                p = stem + "_state.lrn"
                with open(p, "wb") as f: f.write(ts)
                saved.append(p)

            tt_info = LearnStringParser.parse_timing_header(tt) if len(tt) >= 4 else {}
            ts_info = LearnStringParser.parse_state_header(ts) if len(ts) >= 4 else {}
            tt_ok = tt_info.get("timing_channels", 0) > 0 and tt_info.get("valid_states", 0) > 0
            ts_ok = ts_info.get("state_channels", 0) > 0 and ts_info.get("valid_states", 0) > 0

            if not saved:
                self._log(f"  Trace {n}: no learn string data received.", "error")
            else:
                self._log(f"  Trace {n}: {len(saved)} file(s) "
                          f"(TT {'ok' if tt_ok else 'empty'}, "
                          f"TS {'ok' if ts_ok else 'empty'}) → {stem}*", "good")

            # Optional per-trace .sr export using the profile's preset/rate
            if HAS_LRN_SR and (preset or channel_names):
                use_state = preset in {"lsi11-16", "lsi11-ctrl"}
                lrn_path = (stem + "_state.lrn") if use_state else (stem + "_timing.lrn")
                if os.path.exists(lrn_path):
                    sr_path = stem + ".sr"
                    try:
                        if use_state:
                            convert_state_lrn_to_sr(
                                lrn_path=lrn_path, output_path=sr_path,
                                samplerate_override=sr_rate,
                                channel_names=channel_names, preset=preset)
                        else:
                            convert_lrn_to_sr(
                                lrn_path=lrn_path, output_path=sr_path,
                                channel_names=channel_names, preset=preset)
                        self._log(f"  Trace {n}: .sr → {sr_path}", "good")
                    except Exception as e:
                        self._log(f"  Trace {n}: .sr export failed: {e}", "error")

            self.log_q.put(("progress_pct", int(100 * n / count)))
            if n < count:
                time.sleep(delay)

        self._log("── Batch complete ────────────────────────────", "section")
        self._status("Ready")

    # ── waveform download ──────────────────────────────────────────────────
    def do_get_timing(self):
        """Legacy stub — download timing via TT learn string instead."""
        if not self._ready(): return
        self.do_learn_string("TT")

    def do_get_waveform(self):
        """Legacy stub — read current screen display instead."""
        if not self._ready(): return
        self.do_display_read()

    def do_verify_acquisition(self):
        """
        Step-by-step acquisition state verification.
        Checks SRQ mask, serial polls, then does a raw TT download and
        dumps the first bytes so the caller can see exactly what arrived.
        """
        if not self._ready(): return
        self._log("── Verify Acquisition ────────────────────────", "section")

        # Step 1: current status byte without touching MB
        sb = self.gpib.serial_poll()
        self._log(
            f"Step 1 — Serial poll (current MB):  0x{sb:02X}  "
            f"MEAS_COMPLETE={bool(sb & HP1631A.SB_MEASUREMENT_COMPLETE)}  "
            f"NOT_BUSY={bool(sb & HP1631A.SB_NOT_BUSY)}  "
            f"ERROR={bool(sb & HP1631A.SB_ERROR)}  "
            f"SRQ={bool(sb & HP1631A.SB_SRQ)}", "info")

        # Step 2: set MB 34 and re-poll
        self.gpib.write("MB 34")
        time.sleep(0.2)
        sb2 = self.gpib.serial_poll()
        self._log(
            f"Step 2 — After MB 34, serial poll:  0x{sb2:02X}  "
            f"MEAS_COMPLETE={bool(sb2 & HP1631A.SB_MEASUREMENT_COMPLETE)}  "
            f"NOT_BUSY={bool(sb2 & HP1631A.SB_NOT_BUSY)}  "
            f"ERROR={bool(sb2 & HP1631A.SB_ERROR)}", "info")

        if sb2 & HP1631A.SB_MEASUREMENT_COMPLETE:
            self._log("  → Measurement Complete flag IS set — data should be ready.", "good")
        elif sb2 & HP1631A.SB_NOT_BUSY:
            self._log("  → NOT_BUSY set but MEAS_COMPLETE not set — "
                      "acquisition may not have run or trigger not met.", "error")
        else:
            self._log("  → Neither flag set — instrument may still be acquiring "
                      "or MB mask was cleared.", "error")

        # Step 3: TC/TS/TT cross-check — determines which acquisition mode
        # (if either) actually has channels assigned and samples captured.
        # This is the same check hp1631a_probe.py performs standalone,
        # now available directly from the GUI without leaving the app.
        self._log("Step 3 — Cross-checking State and Timing learn strings…", "info")
        result = self.analyzer.verify_acquisition()

        st = result["state"]
        tm = result["timing"]
        self._log(
            f"  State  (TS): header={st['header']!r}  "
            f"channels={st['channels']}  states={st['states']}",
            "good" if (st["channels"] > 0 and st["states"] > 0) else "error")
        self._log(
            f"  Timing (TT): header={tm['header']!r}  "
            f"channels={tm['channels']}  states={tm['states']}",
            "good" if (tm["channels"] > 0 and tm["states"] > 0) else "error")
        self._log(f"  Verdict: {result['verdict']}",
                  "good" if result["ok"] else "error")

        # Step 4: raw TT download and hex dump of first 64 bytes
        self._log("Step 4 — Sending TT and reading raw bytes…", "info")
        self.gpib.write("TT")
        time.sleep(2.0)
        raw = self.gpib.read_binary(max_bytes=65536)
        self._log(f"  Raw bytes received: {len(raw)}", "info")

        if len(raw) == 0:
            self._log("  → Nothing received. Check GPIB address, cable, and "
                      "that the instrument is in Remote mode.", "error")
        elif len(raw) < 4:
            self._log(f"  → Only {len(raw)} byte(s): {raw.hex()}  "
                      "(too short for a valid learn string header)", "error")
        else:
            header = raw[0:2]
            byte_count = (raw[2] << 8) | raw[3]
            expected_total = 4 + byte_count
            self._log(
                f"  Header       : {header!r}  "
                f"({'RT = timing ✓' if header == b'RT' else 'NOT RT — wrong learn string type!'})",
                "good" if header == b"RT" else "error")
            self._log(
                f"  Byte count   : {byte_count}  "
                f"(total expected: {expected_total},  received: {len(raw)})", "info")

            if len(raw) >= 6:
                n_ch     = raw[4]
                n_states = (raw[5] << 8) | raw[6]
                self._log(f"  Channels     : {n_ch}", "info")
                self._log(
                    f"  Valid states : {n_states}  "
                    f"({'data present ✓' if n_states > 0 else '← ZERO — no sample data in buffer'})",
                    "good" if n_states > 0 else "error")
                if len(raw) >= 11:
                    clock_idx = raw[10]
                    clocks = [
                        "100ns/10MHz","200ns/5MHz","500ns/2MHz","1µs/1MHz",
                        "2µs/500kHz","5µs/200kHz","10µs/100kHz","20µs/50kHz",
                        "50µs/20kHz","100µs/10kHz","200µs/5kHz","500µs/2kHz",
                        "1ms/1kHz","2ms/500Hz","5ms/200Hz","10ms/100Hz",
                        "20ms/50Hz","50ms/20Hz","100ms/10Hz",
                    ]
                    clk_str = clocks[clock_idx] if clock_idx < len(clocks) else f"index {clock_idx} (unknown)"
                    self._log(f"  Clock index  : {clock_idx} → {clk_str}", "info")

            dump = " ".join(f"{b:02X}" for b in raw[:64])
            remaining = f"  … (+{len(raw)-64} more)" if len(raw) > 64 else ""
            self._log(f"  First 64 bytes: {dump}{remaining}", "info")

            if len(raw) < expected_total:
                self._log(
                    f"  ⚠ Transfer appears truncated: got {len(raw)} of {expected_total} expected bytes. "
                    "Try increasing adapter timeout.", "error")
            elif len(raw) >= expected_total:
                self._log("  Transfer length matches header byte count ✓", "good")

        self._log("── Verify complete ───────────────────────────", "section")
        self._status("Ready")


    def do_set_trigger(self, pattern: str, label_row: int = 1,
                       dont_care_key: str = "DC"):
        """
        Send a channel-by-channel trigger pattern to the instrument's
        Trace/Trigger spec screen via HP1631A.set_trigger_pattern().

        See the ⚠ UNVERIFIED MNEMONIC WARNING in
        HP1631A.set_trigger_pattern() — the don't-care key mnemonic is a
        best guess. This method logs the result and reminds the user to
        visually confirm the pattern landed correctly on the front panel
        or via the Trace screen display read.
        """
        if not self._ready(): return
        self._status("Setting trigger pattern…")
        self._log("── Set Trigger Pattern ───────────────────────", "section")
        self._log(f"Pattern: {pattern}  (label row {label_row}, "
                  f"don't-care key={dont_care_key!r})", "cmd")

        ok = self.analyzer.set_trigger_pattern(
            pattern, label_row=label_row, dont_care_key=dont_care_key)

        if not ok:
            self._log(
                f"  Invalid pattern characters in {pattern!r} — only "
                "0, 1, X (or N) are allowed.", "error")
            self._status("Ready")
            return

        self._log("  Pattern keystrokes sent.", "good")
        self._log(
            "  ⚠ This mnemonic sequence has not been independently "
            "verified against the manual's trigger-entry section. "
            "Check the front panel (or use READ SCREEN on the Trace "
            "menu) to confirm the pattern landed correctly before "
            "relying on it for a capture.", "info")
        self._status("Ready")

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

    Cursor A (amber)  : left-click to place.
    Cursor B (cyan)   : right-click to place.  When both are set the info
                        bar shows Δ samples and (if sample rate is known) Δt.
    Middle-click      : clear both cursors.

    Bus groups        : shown as hex-value rows below the channel rows.
                        Defined via BusGroupDialog (opened from the toolbar).

    Pattern search    : type a pattern in the search bar, press Find/Next.
                        Syntax:  CH1=1,CH2=0,CH3=X   (name=value)
                                 1X0X11              (positional, X=don't-care)
                        Matches are marked with green tick marks at the top.
    """

    ROW_H     = 28   # pixels per channel row
    BUS_ROW_H = 22   # pixels per bus-group row
    LABEL_W   = 90   # pixels reserved for the channel name column
    SIG_H     = 18   # signal trace height within the row
    PAD_TOP   = 8    # top padding above first row
    MIN_PX_PER_SAMPLE = 1

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=BG, **kw)
        self._channels    = []     # list of (name, [0/1 samples])
        self._zoom        = 4      # pixels per sample
        self._samplerate  = 0      # Hz; 0 = unknown (disables time display)
        self._divergences = None   # optional dict: channel_name -> set(sample_indices)
        self._diff_offset = 0      # baseline sample index

        # Cursor state
        self._cursor_x  = None    # cursor A (amber), left-click
        self._cursor_x2 = None    # cursor B (cyan),  right-click

        # Bus groups  [{"name": str, "indices": [int], "color": str}]
        self._bus_groups = []

        # Pattern search
        self._search_matches = []  # list of sample indices
        self._search_cur     = -1  # index into _search_matches

        # ── Scrollbars ────────────────────────────────────────────────────────
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

        # ── Search bar ────────────────────────────────────────────────────────
        search_row = tk.Frame(self, bg=BG)
        search_row.pack(side="bottom", fill="x")
        tk.Label(search_row, text="SEARCH", font=FSM, fg=TEXT_DIM, bg=BG
                 ).pack(side="left", padx=(4, 2))
        self._search_var = tk.StringVar()
        tk.Entry(search_row, textvariable=self._search_var, font=FSM,
                 bg=BG3, fg=GREEN, insertbackground=GREEN, relief="flat",
                 highlightbackground=BORDER, highlightthickness=1, width=22
                 ).pack(side="left", padx=(0, 2))
        self._btn_find = tk.Button(
            search_row, text="▶ FIND", font=FSM, fg=TEXT, bg=BG3,
            relief="flat", bd=0, activebackground=BG2, activeforeground=GREEN,
            command=self._do_search, padx=4)
        self._btn_find.pack(side="left", padx=(0, 2))
        self._btn_next = tk.Button(
            search_row, text="→ NEXT", font=FSM, fg=TEXT, bg=BG3,
            relief="flat", bd=0, activebackground=BG2, activeforeground=GREEN,
            command=self._do_next_match, padx=4)
        self._btn_next.pack(side="left", padx=(0, 2))
        self._search_lbl = tk.Label(search_row, text="", font=FSM,
                                    fg=TEXT_DIM, bg=BG)
        self._search_lbl.pack(side="left", padx=4)
        # Bind Enter key in search entry
        self._search_var.trace_add("write", lambda *_: None)

        # ── Zoom controls & info label ─────────────────────────────────────────
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
        tk.Label(ctrl, text="px/smp", font=FSM, fg=TEXT_DIM, bg=BG
                 ).pack(side="left", padx=(0, 6))
        # Clear cursors button
        tk.Button(ctrl, text="CLR CUR", font=FSM, fg=TEXT_DIM, bg=BG3,
                  relief="flat", bd=0, activebackground=BG2,
                  command=self._clear_cursors, padx=3
                  ).pack(side="left", padx=(0, 6))
        self._info_lbl = tk.Label(ctrl, text="", font=FSM, fg=TEXT_DIM, bg=BG)
        self._info_lbl.pack(side="left")

        # ── Canvas bindings ───────────────────────────────────────────────────
        self._canvas.bind("<ButtonPress-1>",   self._on_click)
        self._canvas.bind("<ButtonPress-3>",   self._on_right_click)
        self._canvas.bind("<ButtonPress-2>",   self._on_middle_click)
        self._canvas.bind("<MouseWheel>",       self._on_wheel)
        self._canvas.bind("<Button-4>",         self._on_wheel)
        self._canvas.bind("<Button-5>",         self._on_wheel)
        # Enter key in search box
        for child in search_row.winfo_children():
            if isinstance(child, tk.Entry):
                child.bind("<Return>", lambda e: self._do_search())

    # ── public API ─────────────────────────────────────────────────────────

    def set_samplerate(self, hz: int):
        """Tell the waveform viewer the sample rate so cursor deltas show Δt."""
        self._samplerate = max(0, int(hz))
        self._update_info()

    def set_bus_groups(self, groups: list):
        """
        Set bus groups.  groups is a list of dicts:
          {"name": str, "indices": [int], "color": str}
        where indices are channel indices into self._channels.
        """
        self._bus_groups = [g for g in groups
                            if g.get("indices") and g.get("name")]
        self._draw()

    def get_channel_count(self) -> int:
        return len(self._channels)

    def get_channel_names(self) -> list:
        return [name for name, _ in self._channels]

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

        self._update_info()
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

    # ── internal helpers ──────────────────────────────────────────────────────

    def _update_info(self):
        """Refresh the info label (channel count, samples, cursor delta)."""
        ch  = len(self._channels)
        smp = len(self._channels[0][1]) if self._channels else 0
        base = (f"{ch} ch  │  {smp} smp"
                if self._channels else "No waveform data")

        if self._cursor_x is not None and self._cursor_x2 is not None:
            zoom = int(self._zoom_var.get())
            s1 = max(0, int((self._cursor_x  - self.LABEL_W) // zoom))
            s2 = max(0, int((self._cursor_x2 - self.LABEL_W) // zoom))
            delta_smp = abs(s2 - s1)
            if self._samplerate > 0:
                dt = delta_smp / self._samplerate
                if dt < 1e-6:
                    t_str = f"{dt*1e9:.1f} ns"
                elif dt < 1e-3:
                    t_str = f"{dt*1e6:.2f} µs"
                elif dt < 1.0:
                    t_str = f"{dt*1e3:.3f} ms"
                else:
                    t_str = f"{dt:.4f} s"
                base += f"  │  Δ={delta_smp} smp  {t_str}"
            else:
                base += f"  │  Δ={delta_smp} smp"

        self._info_lbl.configure(text=base)

    def _clear_cursors(self):
        self._cursor_x  = None
        self._cursor_x2 = None
        self._update_info()
        self._draw()

    def _scroll_to_sample(self, sample_idx: int):
        """Scroll the canvas so sample_idx is roughly centred."""
        if not self._channels:
            return
        zoom   = int(self._zoom_var.get())
        n      = len(self._channels[0][1])
        total_w = self.LABEL_W + n * zoom + 20
        x      = self.LABEL_W + sample_idx * zoom
        half_w = max(1, self._canvas.winfo_width() // 2)
        frac   = max(0.0, min(1.0, (x - half_w) / max(1, total_w)))
        self._canvas.xview_moveto(frac)

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
        n_bus     = len(self._bus_groups)
        total_w   = self.LABEL_W + n_samples * zoom + 20
        total_h   = (self.PAD_TOP
                     + len(self._channels) * self.ROW_H
                     + n_bus * self.BUS_ROW_H
                     + 20)

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

            # Divergence highlight (drawn behind the trace, in front of
            # row background) — a translucent-look red column per
            # mismatched sample, using thin rectangles since tkinter
            # canvas has no real alpha blending.
            if self._divergences and name in self._divergences:
                div_set = self._divergences[name]
                if div_set:
                    # Coalesce consecutive divergent samples into runs so
                    # we draw far fewer rectangles on long fault regions.
                    sorted_idx = sorted(div_set)
                    run_start = sorted_idx[0]
                    prev = sorted_idx[0]
                    for idx in sorted_idx[1:] + [None]:
                        if idx is not None and idx == prev + 1:
                            prev = idx
                            continue
                        x0 = self.LABEL_W + run_start * zoom
                        x1 = self.LABEL_W + (prev + 1) * zoom
                        c.create_rectangle(
                            x0, y_top + 1, x1, y_top + self.ROW_H - 1,
                            fill="#5a1620", outline="")
                        if idx is not None:
                            run_start = prev = idx

            # Channel label (tinted red if this channel has any divergences,
            # so it's visible even when scrolled away from the highlight)
            has_div = bool(self._divergences and self._divergences.get(name))
            label_colour = "#ff6b6b" if has_div else colour
            c.create_text(4, y_top + self.ROW_H // 2,
                          text=name[:12] + (" ⚠" if has_div else ""),
                          fill=label_colour,
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

        # ── Bus group rows ────────────────────────────────────────────────────
        bus_y0 = self.PAD_TOP + len(self._channels) * self.ROW_H
        for grp_idx, grp in enumerate(self._bus_groups):
            gy_top = bus_y0 + grp_idx * self.BUS_ROW_H
            gy_mid = gy_top + self.BUS_ROW_H // 2
            grp_color = grp.get("color", CYAN)

            # Row background
            c.create_rectangle(0, gy_top, total_w, gy_top + self.BUS_ROW_H,
                                fill="#0b1218", outline="")
            # Label
            c.create_text(4, gy_mid, text=(grp["name"])[:12],
                          fill=grp_color, font=("Courier New", 7, "bold"), anchor="w")
            c.create_line(self.LABEL_W - 2, gy_top,
                          self.LABEL_W - 2, gy_top + self.BUS_ROW_H, fill=BORDER)

            # Compute combined hex value at each sample position
            indices = [i for i in grp.get("indices", [])
                       if 0 <= i < len(self._channels)]
            if not indices:
                continue

            n_bits = len(indices)
            vals = []
            for s_idx in range(n_samples):
                word = 0
                for bit_pos, ch_idx in enumerate(indices):
                    _, bits = self._channels[ch_idx]
                    if s_idx < len(bits):
                        word |= bits[s_idx] << bit_pos
                vals.append(word)

            # Draw hex value segments (runs of the same value)
            seg_start = 0
            seg_val   = vals[0] if vals else 0
            hex_digits = max(1, (n_bits + 3) // 4)
            def _draw_bus_seg(x0, x1, val, gy_top, gy_mid, color):
                w = x1 - x0
                if w < 2:
                    return
                # Draw segment box with two diagonal "corners" at each edge
                pad = min(4, w // 4)
                gy_lo = gy_top + 1
                gy_hi = gy_top + self.BUS_ROW_H - 1
                # Trapezoid outline
                pts = [x0 + pad, gy_lo,  x1 - pad, gy_lo,
                       x1,       gy_mid,  x1 - pad, gy_hi,
                       x0 + pad, gy_hi,   x0,       gy_mid,
                       x0 + pad, gy_lo]
                c.create_polygon(pts, fill="#0f1f2e", outline=color)
                # Hex text (only if wide enough)
                if w > 14:
                    hex_str = f"{val:0{hex_digits}X}"
                    c.create_text((x0 + x1) // 2, gy_mid, text=hex_str,
                                  fill=color,
                                  font=("Courier New", 7, "bold"), anchor="center")

            for s_idx in range(1, n_samples):
                if vals[s_idx] != seg_val:
                    x0 = self.LABEL_W + seg_start * zoom
                    x1 = self.LABEL_W + s_idx * zoom
                    _draw_bus_seg(x0, x1, seg_val, gy_top, gy_mid, grp_color)
                    seg_start = s_idx
                    seg_val   = vals[s_idx]
            # Last segment
            x0 = self.LABEL_W + seg_start * zoom
            x1 = self.LABEL_W + n_samples * zoom
            _draw_bus_seg(x0, x1, seg_val, gy_top, gy_mid, grp_color)

        # ── Search match tick marks ───────────────────────────────────────────
        if self._search_matches:
            for m_idx in self._search_matches:
                mx = self.LABEL_W + m_idx * zoom + zoom // 2
                # Draw a small green triangle tick at top of waveform
                c.create_polygon(mx - 3, 0, mx + 3, 0, mx, 6,
                                 fill="#39d353", outline="")
            # Highlight current match
            if 0 <= self._search_cur < len(self._search_matches):
                cur_m = self._search_matches[self._search_cur]
                cmx = self.LABEL_W + cur_m * zoom + zoom // 2
                c.create_line(cmx, 0, cmx, total_h,
                              fill="#39d353", dash=(2, 4), tags="search_cur")

        # ── Cursor A (amber, left-click) ──────────────────────────────────────
        if self._cursor_x is not None:
            c.create_line(self._cursor_x, 0, self._cursor_x, total_h,
                          fill=AMBER, dash=(3, 3), tags="cursor_a")
            sn = max(0, int((self._cursor_x - self.LABEL_W) // zoom))
            c.create_text(self._cursor_x + 3, 2,
                          text=f"A:{sn}", fill=AMBER, font=FSM, anchor="nw")

        # ── Cursor B (cyan, right-click) ──────────────────────────────────────
        if self._cursor_x2 is not None:
            c.create_line(self._cursor_x2, 0, self._cursor_x2, total_h,
                          fill=CYAN, dash=(3, 3), tags="cursor_b")
            sn2 = max(0, int((self._cursor_x2 - self.LABEL_W) // zoom))
            c.create_text(self._cursor_x2 + 3, 12,
                          text=f"B:{sn2}", fill=CYAN, font=FSM, anchor="nw")

        # ── Delta label between cursors ───────────────────────────────────────
        if self._cursor_x is not None and self._cursor_x2 is not None:
            zoom = int(self._zoom_var.get())
            s1   = max(0, int((self._cursor_x  - self.LABEL_W) // zoom))
            s2   = max(0, int((self._cursor_x2 - self.LABEL_W) // zoom))
            ds   = abs(s2 - s1)
            mid_x = (self._cursor_x + self._cursor_x2) / 2
            if self._samplerate > 0:
                dt = ds / self._samplerate
                if dt < 1e-6:
                    t_str = f"{dt*1e9:.0f}ns"
                elif dt < 1e-3:
                    t_str = f"{dt*1e6:.2f}µs"
                elif dt < 1.0:
                    t_str = f"{dt*1e3:.3f}ms"
                else:
                    t_str = f"{dt:.4f}s"
                delta_lbl = f"Δ{ds}  {t_str}"
            else:
                delta_lbl = f"Δ{ds} smp"
            c.create_text(mid_x, 22, text=delta_lbl,
                          fill="#ffffff", font=FSM, anchor="center")

    # ── event handlers ────────────────────────────────────────────────────────

    def _on_zoom(self, _=None):
        self._update_info()
        self._draw()

    def _on_click(self, event):
        self._cursor_x = self._canvas.canvasx(event.x)
        self._update_info()
        self._draw()

    def _on_right_click(self, event):
        self._cursor_x2 = self._canvas.canvasx(event.x)
        self._update_info()
        self._draw()

    def _on_middle_click(self, event):
        self._cursor_x  = None
        self._cursor_x2 = None
        self._update_info()
        self._draw()

    def _on_wheel(self, event):
        if event.num == 4 or event.delta > 0:
            self._canvas.xview_scroll(-3, "units")
        else:
            self._canvas.xview_scroll(3, "units")

    # ── pattern search ────────────────────────────────────────────────────────

    def _parse_search_pattern(self, pattern: str):
        """
        Parse a search pattern string into a list of (samples_list, value) pairs.
        value is 0 or 1; -1 means don't-care (X).

        Supported formats
        -----------------
        Positional bit string: "1X0X11"
          Each character maps to the corresponding channel (index 0,1,2…).
          '0'/'1' match; 'X'/'x' = don't-care.

        Named conditions: "CH1=1,CH3=0,CLK=X"
          Name matches are case-insensitive.  Multiple conditions separated
          by comma or semicolon.  Unknown channel names are silently ignored.
        """
        if not pattern or not self._channels:
            return []

        pat = pattern.strip()
        # Detect positional style: only '0', '1', 'X', 'x', ' '
        if all(c in "01Xx " for c in pat):
            bits = [c for c in pat.replace(" ", "").upper() if c in "01X"]
            conds = []
            for i, bit in enumerate(bits):
                if i >= len(self._channels):
                    break
                val = -1 if bit == "X" else int(bit)
                conds.append((self._channels[i][1], val))
            return conds

        # Named style
        ch_map = {name.lower(): samples
                  for name, samples in self._channels}
        conds = []
        for part in pat.replace(";", ",").split(","):
            part = part.strip()
            if "=" not in part:
                continue
            name, val_s = part.split("=", 1)
            name  = name.strip().lower()
            val_s = val_s.strip().lower()
            if name not in ch_map:
                continue
            val = -1 if val_s == "x" else (int(val_s) if val_s in ("0","1") else None)
            if val is None:
                continue
            conds.append((ch_map[name], val))
        return conds

    def _do_search(self):
        """Find all sample positions matching the search pattern."""
        pattern = self._search_var.get().strip()
        if not pattern or not self._channels:
            self._search_matches = []
            self._search_cur     = -1
            self._search_lbl.configure(text="")
            self._draw()
            return

        conds = self._parse_search_pattern(pattern)
        if not conds:
            self._search_lbl.configure(text="bad pattern")
            return

        n_smp = len(self._channels[0][1])
        matches = []
        for si in range(n_smp):
            if all(
                (val == -1 or (si < len(samples) and samples[si] == val))
                for samples, val in conds
            ):
                matches.append(si)

        self._search_matches = matches
        self._search_cur     = 0 if matches else -1
        count = len(matches)
        self._search_lbl.configure(
            text=f"{count} match{'es' if count != 1 else ''}"
                 if count else "no matches")

        if matches:
            self._scroll_to_sample(matches[0])
        self._draw()

    def _do_next_match(self):
        """Advance to the next search match."""
        if not self._search_matches:
            self._do_search()
            return
        self._search_cur = (self._search_cur + 1) % len(self._search_matches)
        self._scroll_to_sample(self._search_matches[self._search_cur])
        n = len(self._search_matches)
        self._search_lbl.configure(
            text=f"{self._search_cur + 1}/{n}")
        self._draw()

    # ── data loading ──────────────────────────────────────────────────────────

    def load_channels(self, channels: list, divergences: dict = None):
        """
        Load pre-parsed channel data directly (from learn string decoder).
        channels: list of (name, [0/1 sample, ...]) tuples.
        divergences: optional dict of channel_name -> set/list of sample
          indices (in this channel data's own index space) to highlight
          as mismatches, e.g. from a DiffResult. Pass None to clear any
          previous highlighting.
        """
        self._channels    = channels
        self._divergences = (
            {name: set(idxs) for name, idxs in divergences.items()}
            if divergences else None
        )
        # Clear search matches on new data
        self._search_matches = []
        self._search_cur     = -1
        self._update_info()
        self._draw()

    def clear(self):
        self._channels       = []
        self._divergences    = None
        self._cursor_x       = None
        self._cursor_x2      = None
        self._search_matches = []
        self._search_cur     = -1
        self._update_info()
        self._canvas.delete("all")


# ═══════════════════════════════════════════════════════════════════════════
#  Bus group editor dialog
# ═══════════════════════════════════════════════════════════════════════════

class BusGroupDialog(tk.Toplevel):
    """
    Dialog for defining named bus groups over currently loaded waveform channels.

    A bus group combines multiple single-bit channels into a labelled row in
    the waveform viewer that shows the combined hex value at each sample.
    This is useful for displaying multiplexed buses (address/data lines, etc.)
    without manually computing the hex value yourself.

    Groups are session-scoped — they are applied to the WaveformCanvas
    immediately and cleared when the canvas is cleared or a new capture loads
    (though you can reopen this dialog to redefine them).
    """

    _GROUP_COLORS = [CYAN, "#bc8cff", "#ffa657", "#79c0ff",
                     "#7ee787", AMBER, "#ff7b72", "#a5d6ff"]

    def __init__(self, parent, waveform_canvas: WaveformCanvas):
        super().__init__(parent)
        self.title("Bus Group Editor")
        self.configure(bg=BG)
        self.resizable(True, True)
        self._wc     = waveform_canvas
        self._groups = list(waveform_canvas._bus_groups)  # working copy
        self._build()
        self.grab_set()
        self.minsize(520, 360)

    # ── build ────────────────────────────────────────────────────────────────

    def _build(self):
        tk.Label(self, text="BUS GROUP EDITOR",
                 font=FT, fg=AMBER, bg=BG).pack(pady=(10, 2))
        tk.Label(
            self,
            text="Select channels from the right list to form a named bus group.\n"
                 "Channels are combined LSB-first (first selected = bit 0).\n"
                 "The hex value is shown per sample in the waveform viewer.",
            font=FSM, fg=TEXT_DIM, bg=BG, justify="center",
        ).pack(pady=(0, 8))

        # ── main split: group list (left) | editor (right) ────────────────────
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=4)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        # Left: group list
        lf = tk.Frame(body, bg=BG2, bd=1, relief="solid")
        lf.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        tk.Label(lf, text="GROUPS", font=FSM, fg=AMBER, bg=BG2
                 ).pack(anchor="w", padx=6, pady=(4, 0))
        self._grp_lb = tk.Listbox(
            lf, font=FMS, bg=BG3, fg=TEXT, selectbackground=BG2,
            selectforeground=GREEN, activestyle="none",
            highlightthickness=0, relief="flat")
        self._grp_lb.pack(fill="both", expand=True, padx=4, pady=4)
        self._grp_lb.bind("<<ListboxSelect>>", self._on_grp_select)

        grp_btn_row = tk.Frame(lf, bg=BG2)
        grp_btn_row.pack(fill="x", padx=4, pady=(0, 4))
        tk.Button(grp_btn_row, text="+ NEW", font=FSM, fg=GREEN, bg=BG3,
                  relief="flat", bd=0, command=self._new_group, padx=4
                  ).pack(side="left", padx=(0, 2))
        tk.Button(grp_btn_row, text="DEL", font=FSM, fg=RED, bg=BG3,
                  relief="flat", bd=0, command=self._delete_group, padx=4
                  ).pack(side="left")

        # Right: group editor
        rf = tk.Frame(body, bg=BG2, bd=1, relief="solid")
        rf.grid(row=0, column=1, sticky="nsew")
        tk.Label(rf, text="EDIT GROUP", font=FSM, fg=AMBER, bg=BG2
                 ).pack(anchor="w", padx=6, pady=(4, 0))

        name_row = tk.Frame(rf, bg=BG2)
        name_row.pack(fill="x", padx=6, pady=(4, 2))
        tk.Label(name_row, text="Name:", font=FU, fg=TEXT_DIM, bg=BG2
                 ).pack(side="left", padx=(0, 4))
        self._name_var = tk.StringVar()
        tk.Entry(name_row, textvariable=self._name_var, font=FU,
                 bg=BG3, fg=TEXT, insertbackground=GREEN, relief="flat",
                 highlightbackground=BORDER, highlightthickness=1, width=18
                 ).pack(side="left")

        color_row = tk.Frame(rf, bg=BG2)
        color_row.pack(fill="x", padx=6, pady=(0, 4))
        tk.Label(color_row, text="Color:", font=FU, fg=TEXT_DIM, bg=BG2
                 ).pack(side="left", padx=(0, 4))
        self._color_var = tk.StringVar(value=self._GROUP_COLORS[0])
        for col in self._GROUP_COLORS:
            b = tk.Button(color_row, bg=col, width=2, relief="flat",
                          command=lambda c=col: self._set_color(c))
            b.pack(side="left", padx=1)
        self._color_lbl = tk.Label(color_row, text="  ●  ",
                                   fg=self._GROUP_COLORS[0], bg=BG2,
                                   font=FU)
        self._color_lbl.pack(side="left")

        tk.Label(rf, text="Channels (select to include, first = bit 0):",
                 font=FSM, fg=TEXT_DIM, bg=BG2).pack(anchor="w", padx=6)
        ch_frame = tk.Frame(rf, bg=BG2)
        ch_frame.pack(fill="both", expand=True, padx=6, pady=4)
        ch_sb = tk.Scrollbar(ch_frame, orient="vertical")
        self._ch_lb = tk.Listbox(
            ch_frame, font=FMS, bg=BG3, fg=TEXT,
            selectbackground=BG2, selectforeground=GREEN,
            activestyle="none", highlightthickness=0, relief="flat",
            selectmode=tk.MULTIPLE, yscrollcommand=ch_sb.set)
        ch_sb.config(command=self._ch_lb.yview)
        ch_sb.pack(side="right", fill="y")
        self._ch_lb.pack(fill="both", expand=True)

        # Populate channel list
        ch_names = self._wc.get_channel_names()
        for i, name in enumerate(ch_names):
            self._ch_lb.insert("end", f"[{i:02d}] {name}")

        tk.Button(rf, text="SAVE GROUP", font=FU, fg=TEXT, bg=GREEN_DIM,
                  relief="flat", bd=0, command=self._save_group, padx=6
                  ).pack(pady=(0, 6))

        # ── bottom buttons ────────────────────────────────────────────────────
        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(fill="x", padx=10, pady=(4, 10))
        tk.Button(btn_row, text="APPLY", font=FU, fg=BG, bg=GREEN,
                  relief="flat", bd=0, padx=8,
                  command=self._apply).pack(side="right", padx=(4, 0))
        tk.Button(btn_row, text="CANCEL", font=FU, fg=TEXT, bg=BG3,
                  relief="flat", bd=0, padx=8,
                  command=self.destroy).pack(side="right")
        tk.Button(btn_row, text="CLEAR ALL", font=FU, fg=RED, bg=BG3,
                  relief="flat", bd=0, padx=8,
                  command=self._clear_all).pack(side="left")

        self._refresh_grp_lb()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _refresh_grp_lb(self):
        self._grp_lb.delete(0, "end")
        for g in self._groups:
            color = g.get("color", CYAN)
            label = f"{'●'} {g['name']}  [{len(g['indices'])} ch]"
            self._grp_lb.insert("end", label)
            idx = self._grp_lb.size() - 1
            self._grp_lb.itemconfig(idx, fg=color)

    def _on_grp_select(self, _=None):
        sel = self._grp_lb.curselection()
        if not sel:
            return
        g = self._groups[sel[0]]
        self._name_var.set(g["name"])
        self._set_color(g.get("color", CYAN))
        # Select channels in list
        self._ch_lb.selection_clear(0, "end")
        for idx in g.get("indices", []):
            self._ch_lb.selection_set(idx)

    def _new_group(self):
        self._grp_lb.selection_clear(0, "end")
        self._ch_lb.selection_clear(0, "end")
        n = len(self._groups) + 1
        self._name_var.set(f"BUS{n}")
        col = self._GROUP_COLORS[(n - 1) % len(self._GROUP_COLORS)]
        self._set_color(col)

    def _delete_group(self):
        sel = self._grp_lb.curselection()
        if not sel:
            return
        del self._groups[sel[0]]
        self._refresh_grp_lb()

    def _save_group(self):
        name    = self._name_var.get().strip()
        color   = self._color_var.get()
        indices = list(self._ch_lb.curselection())
        if not name:
            messagebox.showwarning("Bus Group", "Enter a group name.", parent=self)
            return
        if not indices:
            messagebox.showwarning("Bus Group", "Select at least one channel.", parent=self)
            return

        # Find existing group with same name or update the selected one
        sel = self._grp_lb.curselection()
        g = {"name": name, "indices": indices, "color": color}
        if sel:
            self._groups[sel[0]] = g
        else:
            self._groups.append(g)
        self._refresh_grp_lb()

    def _set_color(self, color: str):
        self._color_var.set(color)
        self._color_lbl.configure(fg=color)

    def _clear_all(self):
        if messagebox.askyesno("Bus Group Editor", "Clear all groups?", parent=self):
            self._groups = []
            self._refresh_grp_lb()

    def _apply(self):
        self._wc.set_bus_groups(self._groups)
        self.destroy()

    def _entry(self, parent, var, width=12):
        return tk.Entry(parent, textvariable=var, font=FU,
                        bg=BG3, fg=TEXT, insertbackground=GREEN,
                        relief="flat", highlightbackground=BORDER,
                        highlightthickness=1, width=width)


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
      6. Acquisition cross-check — TC/TS/TT channel & state counts
    Each step can be run independently, and results are shown inline.
    """

    STEPS = [
        ("1  Adapter / firmware",       "step_adapter_info"),
        ("2  Bus reset (IFC+SDC)",      "step_bus_reset"),
        ("3  Serial poll",              "step_serial_poll"),
        ("4  EOS sweep (Prologix only)","step_eos_sweep"),
        ("5  ID command variants",      "step_id_variants"),
        ("6  Acquisition cross-check",  "step_acquisition_check"),
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

    def step_acquisition_check(self):
        self._append("\u2500\u2500 Step 6: Acquisition cross-check \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500", "section")
        if not self._need_connection(): return
        self._append(
            "Downloading TS (state) and TT (timing) learn strings to determine "
            "which acquisition mode, if either, currently has data. This does "
            "not arm a new acquisition (no RN/ST sent) \u2014 it only reports on "
            "whatever is presently in the instrument's buffers.", "info")

        result = self.worker.analyzer.verify_acquisition()
        st = result["state"]
        tm = result["timing"]

        self._append(
            f"  State  (TS): header={st['header']!r}  "
            f"channels={st['channels']}  states={st['states']}",
            "good" if (st["channels"] > 0 and st["states"] > 0) else "error")
        self._append(
            f"  Timing (TT): header={tm['header']!r}  "
            f"channels={tm['channels']}  states={tm['states']}",
            "good" if (tm["channels"] > 0 and tm["states"] > 0) else "error")

        self._append(f"\n  Verdict: {result['verdict']}",
                     "good" if result["ok"] else "error")

        if not result["ok"]:
            self._append(
                "\nNext steps:\n"
                "  \u2022 On the front panel, press the menu/format key and confirm\n"
                "    whether State Format or Timing Format is showing pod\n"
                "    assignments (Off vs TTL/ECL).\n"
                "  \u2022 Confirm the active Trace mode (State Trace vs Timing Trace)\n"
                "    matches the mode you intend to capture.\n"
                "  \u2022 Re-arm with RN, confirm the trigger condition is actually\n"
                "    met (watch the front panel activity/trigger indicator),\n"
                "    then re-run this check.", "error")


# ═══════════════════════════════════════════════════════════════════════════
#  Trigger pattern builder dialog
# ═══════════════════════════════════════════════════════════════════════════

class TriggerBuilderDialog(tk.Toplevel):
    """
    Channel-by-channel 0/1/X trigger pattern grid.

    Lets the user click through each channel bit (cycling 0 → 1 → X → 0…)
    instead of hand-typing a pattern string on the front panel, then sends
    the result via HP1631A.set_trigger_pattern().

    NOTE: the underlying mnemonic for the don't-care key is unverified —
    see the warning in HP1631A.set_trigger_pattern() and do_set_trigger().
    This dialog surfaces that same warning before sending.
    """

    STATES = ["0", "1", "X"]
    STATE_COLORS = {"0": TEXT_DIM, "1": GREEN, "X": AMBER}

    def __init__(self, parent, worker):
        super().__init__(parent)
        self.title("Trigger Pattern Builder")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.worker = worker
        self._bit_vars = []   # list of tk.StringVar, index 0 = channel 0 / LSB
        self._n_channels = 16
        self._build()
        self.grab_set()

    def _build(self):
        tk.Label(self, text="TRIGGER PATTERN BUILDER",
                 font=FT, fg=AMBER, bg=BG).pack(pady=(10, 2))
        tk.Label(
            self,
            text="Click each bit to cycle 0 → 1 → X (don't care).\n"
                 "Pattern is sent most-significant channel first.",
            font=FSM, fg=TEXT_DIM, bg=BG, justify="center"
        ).pack(pady=(0, 8))

        # Channel count selector
        ch_row = tk.Frame(self, bg=BG)
        ch_row.pack(pady=(0, 6))
        tk.Label(ch_row, text="Channels:", font=FU, fg=TEXT, bg=BG
                 ).pack(side="left", padx=(0, 6))
        self._nch_var = tk.StringVar(value="16")
        for n in ("8", "16"):
            b = tk.Radiobutton(
                ch_row, text=n, value=n, variable=self._nch_var,
                font=FU, fg=TEXT, bg=BG, selectcolor=BG3,
                activebackground=BG, activeforeground=GREEN,
                command=self._rebuild_grid)
            b.pack(side="left", padx=4)

        # Label row selector (for multi-label trigger screens)
        row_row = tk.Frame(self, bg=BG)
        row_row.pack(pady=(0, 8))
        tk.Label(row_row, text="Label row:", font=FU, fg=TEXT, bg=BG
                 ).pack(side="left", padx=(0, 6))
        self._row_var = tk.StringVar(value="1")
        self._entry(row_row, self._row_var, 4).pack(side="left")

        # Bit grid
        self._grid_frame = tk.Frame(self, bg=BG)
        self._grid_frame.pack(padx=12, pady=(0, 8))
        self._rebuild_grid()

        # Don't-care key override (advanced / troubleshooting)
        adv = tk.Frame(self, bg=BG)
        adv.pack(pady=(0, 8))
        tk.Label(adv, text="Don't-care key mnemonic:", font=FSM,
                 fg=TEXT_DIM, bg=BG).pack(side="left", padx=(0, 4))
        self._dc_var = tk.StringVar(value="DC")
        self._entry(adv, self._dc_var, 6).pack(side="left")
        tk.Label(adv, text="(unverified — see warning below)", font=FSM,
                 fg=RED, bg=BG).pack(side="left", padx=(4, 0))

        # Pattern preview
        self._preview_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._preview_var, font=FM,
                 fg=GREEN, bg=BG3, relief="flat", padx=8, pady=4
                 ).pack(fill="x", padx=12, pady=(0, 8))
        self._update_preview()

        # Warning
        tk.Label(
            self,
            text="⚠ The don't-care key mnemonic is a best guess, not\n"
                 "confirmed against the manual. Verify the pattern landed\n"
                 "correctly on the front panel after sending.",
            font=FSM, fg=RED, bg=BG, justify="center"
        ).pack(pady=(0, 8))

        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(pady=(0, 10))
        send_btn = tk.Button(
            btn_row, text="▶  SEND PATTERN", command=self._send,
            font=FUB, fg=BG, bg=GREEN, activebackground=GREEN,
            activeforeground=BG, relief="flat", cursor="hand2",
            pady=5, padx=16)
        send_btn.pack(side="left", padx=4)
        tk.Button(
            btn_row, text="Close", command=self.destroy,
            font=FU, fg=BG, bg=TEXT_DIM, relief="flat",
            cursor="hand2", pady=5, padx=12
        ).pack(side="left", padx=4)

    def _entry(self, parent, var, width):
        return tk.Entry(parent, textvariable=var, width=width, font=FM,
                        bg=BG3, fg=TEXT, insertbackground=GREEN,
                        relief="flat", highlightbackground=BORDER,
                        highlightthickness=1)

    def _rebuild_grid(self):
        for w in self._grid_frame.winfo_children():
            w.destroy()
        self._n_channels = int(self._nch_var.get())
        self._bit_vars = [tk.StringVar(value="X") for _ in range(self._n_channels)]

        # Header row: channel numbers, MSB (highest channel) on the left
        # to match the on-screen left-to-right field order described in
        # HP1631A.set_trigger_pattern().
        for col, ch in enumerate(reversed(range(self._n_channels))):
            tk.Label(self._grid_frame, text=str(ch), font=FSM,
                     fg=TEXT_DIM, bg=BG, width=2
                     ).grid(row=0, column=col, padx=1)

        self._bit_buttons = []
        for col, ch in enumerate(reversed(range(self._n_channels))):
            btn = tk.Button(
                self._grid_frame, text="X", width=2, font=FUB,
                fg=BG, bg=self.STATE_COLORS["X"],
                activebackground=AMBER, activeforeground=BG,
                relief="flat", cursor="hand2",
                command=lambda c=ch: self._cycle_bit(c))
            btn.grid(row=1, column=col, padx=1, pady=2)
            self._bit_buttons.append((ch, btn))

        self._update_preview()

    def _cycle_bit(self, ch_index):
        var = self._bit_vars[ch_index]
        cur = var.get()
        nxt = self.STATES[(self.STATES.index(cur) + 1) % len(self.STATES)]
        var.set(nxt)
        # Update the corresponding button's label/color
        for ch, btn in self._bit_buttons:
            if ch == ch_index:
                btn.configure(text=nxt, bg=self.STATE_COLORS[nxt])
                break
        self._update_preview()

    def _pattern_string(self) -> str:
        # MSB (highest channel number) first, matching the grid header order
        return "".join(
            self._bit_vars[ch].get()
            for ch in reversed(range(self._n_channels))
        )

    def _update_preview(self):
        p = self._pattern_string()
        self._preview_var.set(f"Pattern:  {p}")

    def _send(self):
        pattern = self._pattern_string()
        try:
            label_row = int(self._row_var.get())
        except ValueError:
            label_row = 1
        dc_key = self._dc_var.get().strip() or "DC"
        self.worker.submit(
            self.worker.do_set_trigger, pattern,
            label_row=label_row, dont_care_key=dc_key)


# ═══════════════════════════════════════════════════════════════════════════
#  Target profile manager dialog  (#7)
# ═══════════════════════════════════════════════════════════════════════════

class ProfileManagerDialog(tk.Toplevel):
    """
    Create, edit, delete, and apply named target profiles (see
    PROFILE_DEFAULTS / load_profiles() / save_profiles()).

    A profile bundles GPIB address, sample rate, .sr channel preset (or
    custom channel name list), an optional trigger pattern, and free-text
    notes — everything needed to quickly re-point the tool at a
    previously-probed target (e.g. "PDP-11/23 BDAL bus", "3B2 WE32100
    bus") without re-entering settings by hand each session.

    "Apply" copies the profile's connection/export fields into the main
    window's CONNECTION and CAPTURE tab fields (it does not reconnect
    automatically — the user still clicks CONNECT, since changing the
    GPIB address on a live connection isn't something to do silently).
    """

    def __init__(self, parent, app):
        super().__init__(parent)
        self.title("Target Profiles")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.app = app
        self._selected_name = None
        self._build()
        self._refresh_list()
        self.grab_set()

    def _build(self):
        tk.Label(self, text="TARGET PROFILES", font=FT, fg=AMBER, bg=BG
                 ).pack(pady=(10, 2))
        tk.Label(
            self,
            text="Save and reload per-target capture setups (GPIB address,\n"
                 "sample rate, channel preset/names, trigger pattern).",
            font=FSM, fg=TEXT_DIM, bg=BG, justify="center"
        ).pack(pady=(0, 8))

        body = tk.Frame(self, bg=BG)
        body.pack(padx=12, pady=(0, 8), fill="both")

        # ── Left: profile list ──────────────────────────────────────────
        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="y", padx=(0, 10))

        self._listbox = tk.Listbox(
            left, width=22, height=12, font=FU, bg=BG3, fg=TEXT,
            selectbackground=GREEN_DIM, selectforeground=TEXT,
            relief="flat", highlightbackground=BORDER, highlightthickness=1,
            exportselection=False)
        self._listbox.pack(fill="y")
        self._listbox.bind("<<ListboxSelect>>", self._on_select)

        list_btns = tk.Frame(left, bg=BG)
        list_btns.pack(fill="x", pady=(4, 0))
        self._btn(list_btns, "New", self._new_profile, w=8).pack(side="left", padx=1)
        self._btn(list_btns, "Delete", self._delete_profile, w=8).pack(side="left", padx=1)

        # ── Right: editor fields ────────────────────────────────────────
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        self._fields = {}
        field_defs = [
            ("description",       "Description"),
            ("adapter",           "Adapter"),
            ("gpib_addr",         "GPIB address"),
            ("sr_rate",           "Sample rate (Hz)"),
            ("sr_preset",         "Channel preset"),
            ("channel_names",     "Custom channel names"),
            ("trigger_pattern",   "Trigger pattern"),
            ("trigger_label_row", "Trigger label row"),
        ]
        for row, (key, label) in enumerate(field_defs):
            tk.Label(right, text=label, font=FSM, fg=TEXT_DIM, bg=BG
                     ).grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar()
            if key == "sr_preset":
                widget = ttk.Combobox(
                    right, textvariable=var,
                    values=["(none)"] + sorted(CHANNEL_PRESETS),
                    state="readonly", width=20, font=FU)
            else:
                widget = tk.Entry(
                    right, textvariable=var, width=22, font=FM,
                    bg=BG3, fg=TEXT, insertbackground=GREEN, relief="flat",
                    highlightbackground=BORDER, highlightthickness=1)
            widget.grid(row=row, column=1, sticky="w", padx=(6, 0), pady=2)
            self._fields[key] = var

        tk.Label(right, text="Notes", font=FSM, fg=TEXT_DIM, bg=BG
                 ).grid(row=len(field_defs), column=0, sticky="nw", pady=2)
        self._notes_text = tk.Text(
            right, width=28, height=4, font=FU, bg=BG3, fg=TEXT,
            insertbackground=GREEN, relief="flat",
            highlightbackground=BORDER, highlightthickness=1, wrap="word")
        self._notes_text.grid(row=len(field_defs), column=1, sticky="w",
                              padx=(6, 0), pady=2)

        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(pady=(4, 10))
        self._btn(btn_row, "💾  Save", self._save_profile, GREEN, w=10
                  ).pack(side="left", padx=4)
        self._btn(btn_row, "▶  Apply to connection", self._apply_profile,
                  BLUE, w=20).pack(side="left", padx=4)
        tk.Button(
            btn_row, text="Close", command=self.destroy,
            font=FU, fg=BG, bg=TEXT_DIM, relief="flat",
            cursor="hand2", pady=5, padx=12
        ).pack(side="left", padx=4)

    def _btn(self, parent, text, cmd, color=TEXT_DIM, w=None):
        kw = dict(width=w) if w else {}
        b = tk.Button(parent, text=text, command=cmd, font=FU, fg=BG,
                      bg=color, activebackground=color, activeforeground=BG,
                      relief="flat", cursor="hand2", pady=4, **kw)
        return b

    def _refresh_list(self):
        self._listbox.delete(0, "end")
        for name in sorted(self.app._profiles):
            self._listbox.insert("end", name)

    def _on_select(self, event=None):
        sel = self._listbox.curselection()
        if not sel:
            return
        name = self._listbox.get(sel[0])
        self._selected_name = name
        p = self.app._profiles.get(name, dict(PROFILE_DEFAULTS))
        for key, var in self._fields.items():
            var.set(str(p.get(key, PROFILE_DEFAULTS.get(key, ""))))
        self._notes_text.delete("1.0", "end")
        self._notes_text.insert("1.0", p.get("notes", ""))
        self._name_in_progress = name

    def _new_profile(self):
        name = simpledialog.askstring(
            "New Profile", "Profile name (e.g. 'PDP-11/23 BDAL bus'):",
            parent=self)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if name in self.app._profiles:
            messagebox.showerror("Profile exists",
                                 f"A profile named {name!r} already exists.")
            return
        self.app._profiles[name] = make_profile()
        save_profiles(self.app._profiles)
        self._refresh_list()
        if hasattr(self.app, "_refresh_batch_profiles"):
            self.app._refresh_batch_profiles()
        # Select the new entry
        items = list(self._listbox.get(0, "end"))
        if name in items:
            idx = items.index(name)
            self._listbox.selection_clear(0, "end")
            self._listbox.selection_set(idx)
            self._on_select()

    def _delete_profile(self):
        sel = self._listbox.curselection()
        if not sel:
            return
        name = self._listbox.get(sel[0])
        if not messagebox.askyesno("Delete profile",
                                   f"Delete profile {name!r}? This cannot be undone."):
            return
        self.app._profiles.pop(name, None)
        save_profiles(self.app._profiles)
        self._refresh_list()
        if hasattr(self.app, "_refresh_batch_profiles"):
            self.app._refresh_batch_profiles()
        self._selected_name = None

    def _save_profile(self):
        name = getattr(self, "_name_in_progress", None) or self._selected_name
        if not name:
            messagebox.showinfo("No profile selected",
                                "Click 'New' to create a profile first, "
                                "or select one from the list.")
            return
        p = {key: var.get() for key, var in self._fields.items()}
        p["notes"] = self._notes_text.get("1.0", "end").strip()
        self.app._profiles[name] = p
        save_profiles(self.app._profiles)
        self._refresh_list()
        if hasattr(self.app, "_refresh_batch_profiles"):
            self.app._refresh_batch_profiles()
        # Re-select after refresh (listbox order may shift)
        items = list(self._listbox.get(0, "end"))
        if name in items:
            idx = items.index(name)
            self._listbox.selection_clear(0, "end")
            self._listbox.selection_set(idx)

    def _apply_profile(self):
        name = self._selected_name
        if not name or name not in self.app._profiles:
            messagebox.showinfo("No profile selected",
                                "Select a profile from the list first.")
            return
        p = self.app._profiles[name]
        self.app.apply_profile_to_fields(p)
        messagebox.showinfo(
            "Profile applied",
            f"Profile {name!r} applied to the connection and capture "
            "fields. Click CONNECT to use the new GPIB address.")


# ═══════════════════════════════════════════════════════════════════════════
#  Capture diff dialog
# ═══════════════════════════════════════════════════════════════════════════

class DiffDialog(tk.Toplevel):
    """
    Compare two captures (.lrn or .sr) — typically a known-good baseline
    against a suspect candidate from a faulty board — using
    hp1631a_diff.py's cross-correlation alignment and per-channel
    Hamming-distance comparison.

    Workflow: pick baseline + candidate files, optionally override the
    alignment reference channel and search window, RUN DIFF. Results are
    shown as a text summary plus a per-channel list; selecting a
    diverged channel and clicking "Show in waveform viewer" loads both
    captures' channels into the main window's WaveformCanvas with the
    mismatched sample ranges highlighted in red.
    """

    def __init__(self, parent, app):
        super().__init__(parent)
        self.title("Compare Captures (Diff)")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(620, 560)
        self.app = app
        self._result = None        # most recent DiffResult
        self._baseline_cap = None  # most recent LoadedCapture
        self._candidate_cap = None
        self._build()
        self.grab_set()

    # ── build ──────────────────────────────────────────────────────────────

    def _build(self):
        tk.Label(self, text="COMPARE CAPTURES", font=FT, fg=AMBER, bg=BG
                 ).pack(pady=(10, 2))
        tk.Label(
            self,
            text="Diff a known-good baseline against a candidate capture.\n"
                 "Trigger-point/pretrigger skew is corrected automatically\n"
                 "before comparing — see the alignment line in the summary.",
            font=FSM, fg=TEXT_DIM, bg=BG, justify="center"
        ).pack(pady=(0, 8))

        if not HAS_DIFF:
            tk.Label(
                self, fg=RED, bg=BG, font=FU, justify="center",
                text="hp1631a_diff.py was not found in the same directory.\n"
                     "Capture comparison is unavailable."
            ).pack(pady=20)
            tk.Button(self, text="Close", command=self.destroy, font=FU,
                      fg=BG, bg=TEXT_DIM, relief="flat", cursor="hand2",
                      pady=5, padx=12).pack(pady=10)
            return

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        # ── File pickers ─────────────────────────────────────────────────
        files = tk.Frame(body, bg=BG)
        files.pack(fill="x", pady=(0, 6))

        self._baseline_var = tk.StringVar()
        self._candidate_var = tk.StringVar()

        self._file_row(files, "BASELINE (known-good)", self._baseline_var,
                       self._browse_baseline)
        self._file_row(files, "CANDIDATE (suspect)", self._candidate_var,
                       self._browse_candidate)

        # ── Options ─────────────────────────────────────────────────────
        opts = tk.Frame(body, bg=BG)
        opts.pack(fill="x", pady=(2, 6))

        tk.Label(opts, text="Reference channel", font=FSM, fg=TEXT_DIM,
                 bg=BG).grid(row=0, column=0, sticky="w", padx=(0, 4))
        self._ref_chan_var = tk.StringVar(value="(auto)")
        self._ref_chan_cb = ttk.Combobox(
            opts, textvariable=self._ref_chan_var, width=14, font=FU,
            state="readonly", values=["(auto)"])
        self._ref_chan_cb.grid(row=0, column=1, sticky="w", padx=(0, 14))

        tk.Label(opts, text="Search window (±samples)", font=FSM,
                 fg=TEXT_DIM, bg=BG).grid(row=0, column=2, sticky="w", padx=(0, 4))
        self._search_win_var = tk.StringVar(value="auto")
        tk.Entry(opts, textvariable=self._search_win_var, width=8, font=FM,
                 bg=BG3, fg=TEXT, insertbackground=GREEN, relief="flat",
                 highlightbackground=BORDER, highlightthickness=1
                 ).grid(row=0, column=3, sticky="w")

        self._btn(body, "▶  RUN DIFF", self._do_diff, GREEN, w=30
                  ).pack(fill="x", pady=(2, 8))

        # ── Summary text ────────────────────────────────────────────────
        sum_frame = tk.LabelFrame(body, text="  SUMMARY  ", font=FUB,
                                  fg=AMBER, bg=BG, highlightbackground=BORDER,
                                  highlightthickness=1)
        sum_frame.pack(fill="both", expand=True, pady=(0, 6))
        self._summary_txt = scrolledtext.ScrolledText(
            sum_frame, font=("Courier New", 9), bg=BG3, fg=TEXT,
            insertbackground=GREEN, relief="flat", wrap="word",
            height=10, state="disabled")
        self._summary_txt.pack(fill="both", expand=True, padx=4, pady=4)
        self._summary_txt.tag_configure("warn", foreground=AMBER)
        self._summary_txt.tag_configure("bad", foreground=RED)
        self._summary_txt.tag_configure("good", foreground=GREEN)

        # ── Diverged channel list ───────────────────────────────────────
        list_frame = tk.LabelFrame(body, text="  DIVERGED CHANNELS  ",
                                   font=FUB, fg=AMBER, bg=BG,
                                   highlightbackground=BORDER,
                                   highlightthickness=1)
        list_frame.pack(fill="both", expand=False, pady=(0, 6))
        self._diverge_list = tk.Listbox(
            list_frame, height=5, font=FU, bg=BG3, fg=TEXT,
            selectbackground=GREEN_DIM, selectforeground=TEXT,
            relief="flat", highlightthickness=0)
        self._diverge_list.pack(fill="both", expand=True, padx=4, pady=4)

        # ── Bottom buttons ──────────────────────────────────────────────
        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(pady=(0, 10))
        self._btn(btn_row, "📈  Show in waveform viewer",
                  self._show_in_waveform, BLUE, w=26
                  ).pack(side="left", padx=4)
        self._btn(btn_row, "💾  Export divergences (CSV)",
                  self._export_csv, w=24).pack(side="left", padx=4)
        tk.Button(btn_row, text="Close", command=self.destroy, font=FU,
                  fg=BG, bg=TEXT_DIM, relief="flat", cursor="hand2",
                  pady=5, padx=12).pack(side="left", padx=4)

    def _file_row(self, parent, label, var, browse_cmd):
        tk.Label(parent, text=label, font=FSM, fg=TEXT_DIM, bg=BG
                 ).pack(anchor="w")
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(1, 4))
        tk.Entry(row, textvariable=var, font=("Courier New", 9), bg=BG3,
                 fg=TEXT, insertbackground=GREEN, relief="flat",
                 highlightbackground=BORDER, highlightthickness=1
                 ).pack(side="left", fill="x", expand=True, padx=(0, 4))
        self._btn(row, "…", browse_cmd, w=3).pack(side="left")

    def _btn(self, parent, text, cmd, color=TEXT_DIM, w=None):
        kw = dict(text=text, command=cmd, font=FUB, fg=BG, bg=color,
                  activebackground=color, activeforeground=BG,
                  relief="flat", cursor="hand2", pady=4, padx=5)
        if w: kw["width"] = w
        b = tk.Button(parent, **kw)
        b.bind("<Enter>", lambda e, c=color: b.configure(bg=TEXT))
        b.bind("<Leave>", lambda e, c=color: b.configure(bg=c))
        return b

    # ── file browsing ─────────────────────────────────────────────────────

    _FILETYPES = [("Learn string / sigrok capture", "*.lrn *.sr"),
                  ("Learn string", "*.lrn"), ("Sigrok capture", "*.sr"),
                  ("All files", "*.*")]

    def _browse_baseline(self):
        p = filedialog.askopenfilename(title="Select baseline capture",
                                       filetypes=self._FILETYPES)
        if p:
            self._baseline_var.set(p)
            self._refresh_ref_channel_choices()

    def _browse_candidate(self):
        p = filedialog.askopenfilename(title="Select candidate capture",
                                       filetypes=self._FILETYPES)
        if p:
            self._candidate_var.set(p)

    def _refresh_ref_channel_choices(self):
        """Populate the reference-channel dropdown from the baseline file,
        without requiring a full diff run first."""
        path = self._baseline_var.get().strip()
        if not path or not os.path.exists(path):
            return
        try:
            cap = load_capture(path)
        except Exception:
            return
        values = ["(auto)"] + cap.channel_names
        self._ref_chan_cb["values"] = values
        self._ref_chan_var.set("(auto)")

    # ── run diff ───────────────────────────────────────────────────────────

    def _do_diff(self):
        base_path = self._baseline_var.get().strip()
        cand_path = self._candidate_var.get().strip()

        if not base_path or not os.path.exists(base_path):
            messagebox.showerror("Missing file", "Select a valid baseline capture.")
            return
        if not cand_path or not os.path.exists(cand_path):
            messagebox.showerror("Missing file", "Select a valid candidate capture.")
            return

        ref = self._ref_chan_var.get().strip()
        ref = None if ref in ("", "(auto)") else ref

        win_str = self._search_win_var.get().strip().lower()
        if win_str in ("", "auto"):
            search_window = None
        else:
            try:
                search_window = int(win_str)
            except ValueError:
                messagebox.showerror("Input error",
                    "Search window must be an integer or 'auto'.")
                return

        self._set_summary("Loading and comparing…", "warn")
        self.update_idletasks()

        def _run():
            try:
                baseline = load_capture(base_path)
                candidate = load_capture(cand_path)
                result = diff_captures(
                    baseline, candidate, reference_channel=ref,
                    search_window=search_window)
            except Exception as e:
                self.after(0, lambda: self._set_summary(f"ERROR: {e}", "bad"))
                return
            self.after(0, lambda: self._on_diff_done(baseline, candidate, result))

        threading.Thread(target=_run, daemon=True).start()

    def _on_diff_done(self, baseline, candidate, result):
        self._baseline_cap = baseline
        self._candidate_cap = candidate
        self._result = result

        diverged = [cd for cd in result.channel_diffs
                   if cd.in_baseline and cd.in_candidate and cd.mismatches > 0]
        tag = "bad" if diverged else "good"
        self._set_summary(result.summary, tag)

        self._diverge_list.delete(0, "end")
        for cd in sorted(diverged, key=lambda c: c.first_divergence or 0):
            self._diverge_list.insert(
                "end",
                f"{cd.name:<14} first@{cd.first_divergence:<6} "
                f"{cd.mismatches}/{cd.compared_samples} ({cd.mismatch_pct:.1f}%)")

        if result.alignment.score < 0.6 or result.alignment.positional_fallback:
            messagebox.showwarning(
                "Low-confidence alignment",
                result.alignment.confidence_note or
                "Alignment confidence is low — review the summary before "
                "trusting the per-channel results.")

        # Also log to the main window so it's captured in the persistent log.
        self.app._log(
            f"Diff: {os.path.basename(baseline.source_path)} vs "
            f"{os.path.basename(candidate.source_path)} — "
            f"{len(diverged)} channel(s) diverged "
            f"(offset={result.alignment.offset:+d}, "
            f"score={result.alignment.score:.2f})",
            "good" if not diverged else "error")

    def _set_summary(self, text, tag="info"):
        self._summary_txt.configure(state="normal")
        self._summary_txt.delete("1.0", "end")
        self._summary_txt.insert("1.0", text)
        self._summary_txt.configure(state="disabled")

    # ── result actions ────────────────────────────────────────────────────

    def _show_in_waveform(self):
        if not self._result or not self._baseline_cap or not self._candidate_cap:
            messagebox.showinfo("No results", "Run a diff first.")
            return

        result = self._result
        baseline = self._baseline_cap
        offset = result.alignment.offset

        # Build the channel list the waveform viewer will render: the
        # baseline's channels, trimmed to the aligned overlap region, so
        # the divergence sample indices line up 1:1 with what's drawn.
        channels = []
        divergences = {}
        for cd in result.channel_diffs:
            if not (cd.in_baseline and cd.in_candidate):
                continue
            # cd.name may be "X" or "X↔Y" for positional-fallback pairs;
            # recover the baseline-side name to pull samples from.
            base_name = cd.name.split("↔")[0]
            if base_name not in baseline.channel_samples:
                continue
            bits = baseline.channel_samples[base_name]
            start = offset if offset >= 0 else 0
            trimmed = bits[start:start + cd.compared_samples]
            channels.append((cd.name, trimmed))
            if cd.divergence_indices:
                divergences[cd.name] = cd.divergence_indices

        if not channels:
            messagebox.showinfo("Nothing to show",
                                "No comparable channels in this diff result.")
            return

        self.app._waveform.load_channels(channels, divergences=divergences)
        self.app._log(
            f"Loaded diff result into waveform viewer "
            f"({len(divergences)} channel(s) highlighted).", "info")

    def _export_csv(self):
        if not self._result:
            messagebox.showinfo("No results", "Run a diff first.")
            return
        records = self._result.divergence_records()
        if not records:
            messagebox.showinfo("Nothing to export", "No divergences found.")
            return
        p = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All", "*.*")])
        if not p:
            return
        import csv as _csv
        with open(p, "w", newline="") as f:
            w = _csv.writer(f)
            w.writerow(["channel", "sample_index"])
            for name, idx in records:
                w.writerow([name, idx])
        self.app._log(f"Divergences exported: {p}", "good")


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
        self._profiles = load_profiles()
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

        row2b = tk.Frame(f, bg=BG2); row2b.pack(fill="x", padx=6, pady=2)
        tk.Label(row2b, text="Channel preset", font=FU, fg=TEXT_DIM,
                 bg=BG2).pack(side="left", padx=(0,4))
        preset_choices = ["(none)"] + sorted(CHANNEL_PRESETS)
        self._sr_preset_var = tk.StringVar(value="(none)")
        preset_cb = ttk.Combobox(row2b, textvariable=self._sr_preset_var,
                                 values=preset_choices, state="readonly",
                                 width=14, font=FU)
        preset_cb.pack(side="left")
        tk.Label(
            f,
            text="  hc11-19 needs all 19 channels (3 pods); lsi11-16\n"
                 "  needs 16 (2 pods). Fewer channels than the preset\n"
                 "  defines = extra labels are dropped with a warning.",
            font=FSM, fg=TEXT_DIM, bg=BG2, justify="left"
        ).pack(anchor="w", padx=8, pady=(0,2))

        # ── Glitch detect ─────────────────────────────────────────────────────
        self._section(f, "GLITCH DETECT ARM")
        gf = tk.Frame(f, bg=BG2); gf.pack(fill="x", padx=6, pady=2)
        self._glitch_var = tk.BooleanVar(value=False)
        self._chk(gf, "Arm glitch capture (send saved config before each RN)",
                  self._glitch_var).pack(anchor="w")
        gc_path_row = tk.Frame(f, bg=BG2); gc_path_row.pack(fill="x", padx=6, pady=2)
        tk.Label(gc_path_row, text="Glitch config:", font=FU, fg=TEXT_DIM,
                 bg=BG2).pack(side="left", padx=(0, 4))
        self._glitch_cfg_var = tk.StringVar(
            value=os.path.join(_SCRIPT_DIR, "glitch_config.lrn"))
        tk.Entry(gc_path_row, textvariable=self._glitch_cfg_var, font=FSM,
                 bg=BG3, fg=TEXT, insertbackground=GREEN, relief="flat",
                 highlightbackground=BORDER, highlightthickness=1, width=20
                 ).pack(side="left", padx=(0, 2))
        self._btn(gc_path_row, "…", self._browse_glitch_cfg, w=2
                  ).pack(side="left", padx=(0, 4))
        self._btn(gc_path_row, "SAVE NOW", self._save_glitch_config,
                  AMBER, w=9).pack(side="left")
        tk.Label(
            f,
            text="  SAVE NOW downloads TC and saves it as the glitch config.\n"
                 "  Set up glitch capture on the instrument first, then save.",
            font=FSM, fg=TEXT_DIM, bg=BG2, justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 2))

        self._section(f, "TRIGGER")
        self._btn(f, "⊞  TRIGGER PATTERN BUILDER",
                  lambda: TriggerBuilderDialog(self, self._worker),
                  BLUE, w=28).pack(padx=6, pady=4, fill="x")

        self._section(f, "TARGET PROFILES")
        self._btn(f, "📋  MANAGE PROFILES",
                  self._open_profile_manager,
                  BLUE, w=28).pack(padx=6, pady=4, fill="x")

        self._section(f, "RUN")
        self._btn(f, "▶  CAPTURE", self._do_capture, GREEN, w=24
                  ).pack(padx=6, pady=4, fill="x")
        row3 = tk.Frame(f, bg=BG2); row3.pack(fill="x", padx=6, pady=2)
        self._btn(row3, "GET TIMING",  lambda: self._worker.submit(
            self._worker.do_get_timing),   w=12).pack(side="left", padx=(0,4))
        self._btn(row3, "GET WAVEFORM",lambda: self._worker.submit(
            self._worker.do_get_waveform), w=12).pack(side="left")
        self._btn(f, "🔍  VERIFY ACQUISITION", lambda: self._worker.submit(
            self._worker.do_verify_acquisition), w=24,
                  ).pack(padx=6, pady=(0,4), fill="x")

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

        tk.Frame(f, bg=BORDER, height=1).pack(fill="x", padx=6, pady=4)
        self._section(f, "COMPARE CAPTURES")
        tk.Label(f, text="Diff a known-good baseline against a suspect\n"
                        "capture, with automatic trigger-point alignment.",
                 font=FSM, fg=TEXT_DIM, bg=BG2, justify="left"
                 ).pack(anchor="w", padx=8, pady=(2,4))
        self._btn(f, "⇄  DIFF CAPTURES",
                  self._open_diff_dialog, AMBER if HAS_DIFF else TEXT_DARK,
                  w=26).pack(padx=6, pady=2, fill="x")

    def _open_diff_dialog(self):
        if not HAS_DIFF:
            messagebox.showerror(
                "Unavailable",
                "hp1631a_diff.py was not found alongside this script.")
            return
        DiffDialog(self, self)

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

        self._section(f, "TARGET PROFILE  (optional)")
        prof_row = tk.Frame(f, bg=BG2); prof_row.pack(fill="x", padx=6, pady=2)
        self._batch_profile_var = tk.StringVar(value="(none)")
        self._batch_profile_cb = ttk.Combobox(
            prof_row, textvariable=self._batch_profile_var,
            values=["(none)"], state="readonly", width=20, font=FU)
        self._batch_profile_cb.pack(side="left", padx=(0,4))
        self._btn(prof_row, "↻", self._refresh_batch_profiles, w=2
                  ).pack(side="left")
        tk.Label(
            f,
            text="  If set, each trace is also exported to .sr using the\n"
                 "  profile's channel preset/names and sample rate.",
            font=FSM, fg=TEXT_DIM, bg=BG2, justify="left"
        ).pack(anchor="w", padx=8, pady=(0,2))
        self._refresh_batch_profiles()

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
        self._btn(wt, "⬡ GROUPS",
                  lambda: BusGroupDialog(self, self._waveform),
                  BLUE, w=10
                  ).pack(side="right", padx=(0, 4))

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
        saved_preset = s.get("sr_preset", "(none)")
        if saved_preset in (["(none)"] + sorted(CHANNEL_PRESETS)):
            self._sr_preset_var.set(saved_preset)
        self._csv_stem_var.set(s.get("csv_stem", "trace"))
        self._batch_n_var.set(s.get("batch_n", "10"))
        self._batch_delay_var.set(s.get("batch_delay", "1.0"))
        self._batch_dir_var.set(s.get("batch_dir", "captures"))
        self._srq_var.set(s.get("use_srq", False))
        if HAS_SR:
            self._sr_var.set(s.get("also_sr", True))
        self._on_adapter_change()
        # Port will be set after refresh_ports

    def apply_profile_to_fields(self, profile: dict):
        """
        Copy a target profile's connection/export fields into the live UI
        fields (CONNECTION bar's adapter/address, CAPTURE tab's sample
        rate and channel preset). Does not reconnect — the user still
        clicks CONNECT, since silently changing the GPIB address on an
        already-open connection is more likely to surprise than help.
        """
        if profile.get("adapter"):
            self._adapter_var.set(profile["adapter"])
            self._on_adapter_change()
        if profile.get("gpib_addr"):
            self._addr_var.set(profile["gpib_addr"])
        if profile.get("sr_rate"):
            self._sr_rate_var.set(profile["sr_rate"])
        preset = profile.get("sr_preset", "(none)")
        if preset in (["(none)"] + sorted(CHANNEL_PRESETS)):
            self._sr_preset_var.set(preset)
        self._log(
            f"Profile applied: adapter={profile.get('adapter')}  "
            f"addr={profile.get('gpib_addr')}  "
            f"rate={profile.get('sr_rate')}  preset={preset}", "info")

    def _open_profile_manager(self):
        ProfileManagerDialog(self, self)

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
            "sr_preset":   self._sr_preset_var.get(),
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
            elif tag == "wave_samplerate": self._waveform.set_samplerate(int(val))
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
        preset = self._sr_preset_var.get()
        if preset == "(none)":
            preset = None
        glitch_arm = self._glitch_var.get()
        glitch_cfg = self._glitch_cfg_var.get().strip() if glitch_arm else None
        import os
        self._worker._learn_output_dir = os.path.dirname(os.path.abspath(path))
        self._worker.submit(self._worker.do_capture, path, srq, sr, rate,
                            preset=preset,
                            glitch_arm=glitch_arm, glitch_config=glitch_cfg)

    def _browse_glitch_cfg(self):
        p = filedialog.askopenfilename(
            title="Glitch capture config",
            filetypes=[("Learn string","*.lrn"),("All","*.*")])
        if p:
            self._glitch_cfg_var.set(p)

    def _save_glitch_config(self):
        """Download current TC and save as the glitch config file."""
        path = self._glitch_cfg_var.get().strip()
        if not path:
            messagebox.showwarning("Glitch Config",
                                   "Set a path for the glitch config file first.")
            return
        self._worker.submit(self._worker.do_save_glitch_config, path)

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

        profile = None
        pname = self._batch_profile_var.get()
        if pname and pname != "(none)":
            profile = self._profiles.get(pname)

        self._worker.submit(self._worker.do_batch, n, out_dir, delay, srq,
                            profile=profile)

    def _refresh_batch_profiles(self):
        names = ["(none)"] + sorted(self._profiles)
        self._batch_profile_cb["values"] = names
        if self._batch_profile_var.get() not in names:
            self._batch_profile_var.set("(none)")

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
