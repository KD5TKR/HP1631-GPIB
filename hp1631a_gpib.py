"""
hp1631a_gpib.py  --  HP 1631A Logic Analyzer remote control via Prologix GPIB-USB
====================================================================================
Requires:  Python 3.x, pyserial  (pip install pyserial)

Hardware setup
--------------
  1. Connect the Prologix GPIB-USB to the HP 1631A's HP-IB (GPIB) rear-panel port.
  2. Plug the Prologix into a USB port on your Windows PC.
  3. Install the FTDI VCP driver if Windows does not assign a COM port automatically.
     (Device Manager -> Ports to find the COM number, e.g. COM3)
  4. On the HP 1631A front panel, set the HP-IB address via:
       SYSTEM -> CONFIG -> HP-IB ADDRESS  (factory default is 5)

Prologix command quick-reference (sent as ASCII over the virtual serial port)
  ++mode 1        -> Controller mode  (always set this first)
  ++addr <n>      -> Address the instrument at GPIB address n
  ++auto 0        -> Manual read mode (recommended for this instrument)
  ++read eoi      -> Read until EOI is asserted  (instrument signals end of data)
  ++read 10       -> Read until LF (line-feed, ASCII 10)
  ++clr           -> Send Selected Device Clear (SDC) to reset the instrument parser
  ++trg           -> Send Group Execute Trigger (GET)
  ++srq           -> Query SRQ line state (1=asserted, 0=not asserted)
  ++spoll         -> Serial poll — reads the status byte from the instrument

HP 1631A / 1630-series HP-IB command summary (from Chapter 9 of the manual)
  The 1631A uses a KEYWORD-BASED command language, NOT SCPI.
  Commands are terminated with a newline (\\n).  Responses are terminated with \\n + EOI.

  Acquisition control
    START           Start / re-arm the analyzer
    STOP            Stop acquisition

  Status / identification
    ID?             Returns the instrument ID string (e.g. "HP1631A,0,0")
    ERR?            Returns the error status byte

  Menu / display navigation  (same as front-panel key presses)
    MENU <name>     Go to a named menu.  Names: SYSTEM, STATE, TIMING, MIXED,
                    FORMAT, TRIGGER, LISTING, CHART, COMPARE, WAVEFORM

  Data output  (after acquisition is complete)
    SLIST?          State listing data (ASCII)
    TLIST?          Timing listing data (ASCII)
    WLIST?          Waveform listing data (ASCII)

  Machine configuration / label assignment
    LABEL <ch>,<name>   Assign a text label to a channel group
    EDGE <ch>,<edge>    Set trigger edge (POS or NEG)
    CLOCK <rate>        Set state clock rate (e.g. CLOCK 10MHZ)

  NOTE: The exact mnemonic set depends on firmware revision.  Chapter 9 of
  the 01631-90904 manual is the authoritative reference.  The KE5FX GPIB Toolkit's
  "ibquery" / "ibterm" utilities can be used to interactively probe the instrument
  before automating it with this script.

Usage examples
--------------
  python hp1631a_gpib.py --port COM3 --addr 5 --id
  python hp1631a_gpib.py --port COM3 --addr 5 --start
  python hp1631a_gpib.py --port COM3 --addr 5 --capture --output trace.txt
  python hp1631a_gpib.py --port COM3 --addr 5 --interactive
"""

import serial
import time
import sys
import argparse

# ---------------------------------------------------------------------------
# Low-level Prologix GPIB-USB driver
# ---------------------------------------------------------------------------

class PrologixGPIB:
    """
    Thin wrapper around the Prologix GPIB-USB virtual serial port.
    The Prologix appears as a plain COM port (FTDI chip).
    Baud rate is irrelevant for USB but pyserial requires a value; 115200 is conventional.

    eos controls the line terminator appended to commands sent to the instrument
    over GPIB (Prologix ++eos setting):
      0 = CR+LF   1 = CR   2 = LF (default)   3 = none
    If the HP 1631A responds with "Unrecognized Command" to ID?, try eos=1 (CR).
    """

    def __init__(self, port: str, gpib_addr: int,
                 timeout: float = 5.0, eos: int = 2):
        self.port      = port
        self.gpib_addr = gpib_addr
        self.eos       = eos        # 0=CR+LF  1=CR  2=LF  3=none
        self.ser = serial.Serial(
            port=port,
            baudrate=115200,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
            xonxoff=False,
            rtscts=False,
        )
        time.sleep(0.2)             # let USB-serial enumeration settle
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        self._init_controller()

    def _init_controller(self):
        """Put Prologix into Controller mode and address our instrument."""
        for cmd in [
            "++mode 1",
            "++auto 0",
            f"++eos {self.eos}",
            "++eoi 1",
            "++read_tmo_ms 3000",
            f"++addr {self.gpib_addr}",
        ]:
            self._raw_write(cmd)
        # Drain any "Unrecognized command" from old Prologix firmware
        # that does not support ++read_tmo_ms, plus any prior-session leftovers.
        self._drain()

    def _raw_write(self, cmd: str):
        """Write a raw string to the serial port."""
        self.ser.write((cmd + "\n").encode())
        time.sleep(0.05)

    def _drain(self, timeout_s: float = 0.3):
        """
        Read and discard all bytes in the serial input buffer.
        Prevents stale Prologix error messages from corrupting the next read.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            waiting = self.ser.in_waiting
            if waiting:
                self.ser.read(waiting)
                deadline = time.time() + 0.1
            else:
                time.sleep(0.02)

    def write(self, instrument_cmd: str):
        """Send a command string to the addressed GPIB instrument."""
        self._raw_write(f"++addr {self.gpib_addr}")
        self._raw_write(instrument_cmd)

    def read(self, max_bytes: int = 65536) -> str:
        """
        Request a response from the instrument and return it as a string.
        Uses '++read eoi' so we read until the instrument asserts EOI.
        Drains stale bytes first.
        """
        self._drain(0.05)
        self._raw_write(f"++addr {self.gpib_addr}")
        self._raw_write("++read eoi")
        raw = self.ser.read(max_bytes)
        return raw.decode(errors="replace").strip()

    def query(self, cmd: str, max_bytes: int = 65536,
              delay: float = 0.3) -> str:
        """Write a query command and return the response."""
        self.write(cmd)
        time.sleep(delay)
        return self.read(max_bytes)

    def clear(self):
        """Send Selected Device Clear (SDC) — resets the instrument's parser."""
        self._raw_write(f"++addr {self.gpib_addr}")
        self._raw_write("++clr")
        time.sleep(0.5)

    def trigger(self):
        """Send Group Execute Trigger (GET)."""
        self._raw_write(f"++addr {self.gpib_addr}")
        self._raw_write("++trg")

    def serial_poll(self) -> int:
        """Perform a serial poll and return the status byte."""
        self._raw_write(f"++addr {self.gpib_addr}")
        self._raw_write("++spoll")
        resp = self.ser.readline().decode(errors="replace").strip()
        try:
            return int(resp)
        except ValueError:
            return -1

    def srq(self) -> bool:
        """Return True if the SRQ line is asserted."""
        self._raw_write("++srq")
        resp = self.ser.readline().decode(errors="replace").strip()
        return resp.strip() == "1"

    def close(self):
        self.ser.close()


# ---------------------------------------------------------------------------
# HP 1631A instrument class
# ---------------------------------------------------------------------------

class HP1631A:
    """
    High-level driver for the HP 1631A Logic Analyzer.

    Command syntax reference: HP 1631A Operating & Programming Manual, Chapter 9
    (document 01631-90904).

    The 1631A uses a keyword-based command language that predates SCPI.
    Commands are plain ASCII mnemonics, optionally followed by parameters.
    Queries end with '?'.
    """

    # Status byte bit masks (from Chapter 9)
    SB_DATA_READY   = 0x10   # Bit 4: acquisition complete, data available
    SB_ERROR        = 0x20   # Bit 5: error has occurred
    SB_RQS          = 0x40   # Bit 6: device is requesting service (SRQ)
    SB_POWER_ON     = 0x80   # Bit 7: power-on / reset occurred

    def __init__(self, gpib: PrologixGPIB):
        self.gpib = gpib

    # ------------------------------------------------------------------
    # Basic communication
    # ------------------------------------------------------------------

    def identify(self) -> str:
        """Query the instrument ID string."""
        return self.gpib.query("ID?")

    def error_status(self) -> str:
        """Query the error status register."""
        return self.gpib.query("ERR?")

    def clear(self):
        """Send SDC to reset the instrument's parser (does NOT clear acquired data)."""
        self.gpib.clear()

    # ------------------------------------------------------------------
    # Acquisition control
    # ------------------------------------------------------------------

    def start(self):
        """Arm / start the analyzer.  Equivalent to pressing RUN on the front panel."""
        self.gpib.write("START")

    def stop(self):
        """Stop / halt the analyzer."""
        self.gpib.write("STOP")

    def wait_for_data(self, poll_interval: float = 0.5, timeout: float = 60.0) -> bool:
        """
        Poll the serial poll status byte until the DATA READY bit is set,
        or until timeout (seconds) is reached.
        Returns True if data is ready, False on timeout.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            sb = self.gpib.serial_poll()
            if sb < 0:
                print(f"  [warn] serial poll returned unexpected value, retrying...")
            elif sb & self.SB_ERROR:
                print(f"  [error] instrument reported an error (status byte 0x{sb:02X})")
                return False
            elif sb & self.SB_DATA_READY:
                return True
            time.sleep(poll_interval)
        print("  [timeout] instrument did not signal data ready within timeout period")
        return False

    # ------------------------------------------------------------------
    # Data retrieval
    # ------------------------------------------------------------------

    def get_state_listing(self) -> str:
        """
        Retrieve the state (synchronous) listing data as ASCII.
        The instrument must have completed an acquisition first.
        """
        return self.gpib.query("SLIST?", max_bytes=262144)

    def get_timing_listing(self) -> str:
        """Retrieve the timing (asynchronous) listing data as ASCII."""
        return self.gpib.query("TLIST?", max_bytes=262144)

    def get_waveform_listing(self) -> str:
        """Retrieve the waveform listing data as ASCII."""
        return self.gpib.query("WLIST?", max_bytes=262144)

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def set_menu(self, menu_name: str):
        """
        Navigate to a front-panel menu by name.
        Valid names: SYSTEM, STATE, TIMING, MIXED, FORMAT,
                     TRIGGER, LISTING, CHART, COMPARE, WAVEFORM
        """
        self.gpib.write(f"MENU {menu_name}")
        time.sleep(0.2)

    def set_label(self, channel: int, name: str):
        """Assign a text label to a channel group (1-indexed)."""
        self.gpib.write(f"LABEL {channel},{name}")

    def send_raw(self, cmd: str) -> str:
        """
        Send an arbitrary command string.  If the command ends with '?',
        the response is read and returned.  Otherwise returns empty string.
        """
        if cmd.strip().endswith("?"):
            return self.gpib.query(cmd)
        else:
            self.gpib.write(cmd)
            return ""


# ---------------------------------------------------------------------------
# CLI actions
# ---------------------------------------------------------------------------

def cmd_id(analyzer: HP1631A):
    print("Querying instrument ID...")
    resp = analyzer.identify()
    print(f"  ID response: {resp!r}")


def cmd_start(analyzer: HP1631A):
    print("Sending START command...")
    analyzer.start()
    print("  Acquisition started.  Waiting for data ready...")
    if analyzer.wait_for_data(timeout=120):
        print("  Data ready.")
    else:
        print("  Timed out or error waiting for data.")


def cmd_capture(analyzer: HP1631A, output_file: str):
    """Start acquisition, wait for completion, download all listing data."""
    print("Starting capture...")
    analyzer.start()
    print("  Waiting for trigger and data capture to complete...")
    if not analyzer.wait_for_data(timeout=120):
        print("  Aborting — instrument did not signal data ready.")
        return

    print("  Retrieving state listing data...")
    slist = analyzer.get_state_listing()
    print("  Retrieving timing listing data...")
    tlist = analyzer.get_timing_listing()
    print("  Retrieving waveform listing data...")
    wlist = analyzer.get_waveform_listing()

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("=== HP 1631A Capture ===\n")
        f.write(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("--- STATE LISTING (SLIST?) ---\n")
        f.write(slist + "\n\n")
        f.write("--- TIMING LISTING (TLIST?) ---\n")
        f.write(tlist + "\n\n")
        f.write("--- WAVEFORM LISTING (WLIST?) ---\n")
        f.write(wlist + "\n")

    print(f"  Data saved to: {output_file}")


def cmd_interactive(gpib: PrologixGPIB, analyzer: HP1631A):
    """
    Simple interactive terminal.
    Type a command and press Enter.  Commands ending with '?' are queried
    and the response is printed.  Type 'exit' or Ctrl-C to quit.

    Special meta-commands (not sent to the instrument):
      !clr   -> Send SDC (Selected Device Clear)
      !poll  -> Serial poll (print status byte)
      !srq   -> Check SRQ line
    """
    print("HP 1631A Interactive Terminal")
    print("  Commands ending with '?' are queried and the response displayed.")
    print("  !clr = SDC,  !poll = serial poll,  !srq = SRQ state,  exit = quit\n")

    while True:
        try:
            line = input("1631A> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not line:
            continue
        if line.lower() in ("exit", "quit"):
            break
        elif line == "!clr":
            analyzer.clear()
            print("  [SDC sent]")
        elif line == "!poll":
            sb = gpib.serial_poll()
            print(f"  Status byte: 0x{sb:02X} ({sb})")
            print(f"    DATA_READY={bool(sb & HP1631A.SB_DATA_READY)}"
                  f"  ERROR={bool(sb & HP1631A.SB_ERROR)}"
                  f"  RQS={bool(sb & HP1631A.SB_RQS)}")
        elif line == "!srq":
            print(f"  SRQ asserted: {gpib.srq()}")
        else:
            resp = analyzer.send_raw(line)
            if resp:
                print(f"  -> {resp}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="HP 1631A Logic Analyzer GPIB control via Prologix GPIB-USB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--port",   required=True,
                        help="Windows COM port for the Prologix (e.g. COM3)")
    parser.add_argument("--addr",   type=int, default=5,
                        help="GPIB address of the HP 1631A (default: 5)")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="Serial read timeout in seconds (default: 5.0)")

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--id",          action="store_true",
                        help="Query the instrument ID string")
    action.add_argument("--start",       action="store_true",
                        help="Send START and wait for data ready")
    action.add_argument("--capture",     action="store_true",
                        help="Full capture: start, wait, download all listing data")
    action.add_argument("--interactive", action="store_true",
                        help="Interactive command terminal")
    action.add_argument("--raw",         metavar="CMD",
                        help="Send a single raw command (e.g. --raw 'ID?')")

    parser.add_argument("--output", default="hp1631a_capture.txt",
                        help="Output filename for --capture (default: hp1631a_capture.txt)")

    args = parser.parse_args()

    print(f"Opening {args.port}, GPIB addr {args.addr} ...")
    try:
        gpib = PrologixGPIB(args.port, args.addr, timeout=args.timeout)
    except serial.SerialException as e:
        print(f"ERROR: Could not open serial port {args.port}: {e}")
        sys.exit(1)

    analyzer = HP1631A(gpib)

    try:
        if args.id:
            cmd_id(analyzer)
        elif args.start:
            cmd_start(analyzer)
        elif args.capture:
            cmd_capture(analyzer, args.output)
        elif args.interactive:
            cmd_interactive(gpib, analyzer)
        elif args.raw:
            resp = analyzer.send_raw(args.raw)
            if resp:
                print(resp)
    finally:
        gpib.close()
        print("Port closed.")


if __name__ == "__main__":
    main()
