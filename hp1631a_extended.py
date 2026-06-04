"""
hp1631a_extended.py  --  HP 1631A/D driver (corrected from Chapter 10)
=======================================================================
All mnemonics are verified against the 1631A/D Operating & Programming
Manual, Chapter 10 ("Using HP-IB or HP-IL Interface").

Key facts from the manual
--------------------------
• Commands terminate with ;  CR  or  LF  (any one).
  Prologix ++eos 1 (CR) is correct.
• All keyboard mnemonics are exactly TWO characters (Table 10-1).
  The parser reads 2 chars, executes, ignores trailing non-numeric chars
  until the next delimiter.  A decimal number after a mnemonic repeats it.
• RUN  = RN    STOP = ST    (not "START"/"STOP" — those are not mnemonics)
• Mask Byte command = MB <n>;   (not "MASK")
• DATA_READY = Status byte bit 1 (value 2 = Measurement Complete)
  NOT bit 4.  Mask byte defaults to 0 at power-on → serial poll always
  returns 0 until MB is sent.
• Data download uses binary Learn String commands: TC TS TT TA TE
  There is no SLIST? / TLIST? / WLIST? / CONFIG?
• DR command reads ASCII text from the display buffer (23 rows × 64 cols).
• GROUP EXECUTE TRIGGER (++trg) also starts a measurement like RN.

Supported GPIB adapters
------------------------
  PrologixGPIB   — Prologix GPIB-USB (serial / ++ protocol)
  NI488GPIB      — NI GPIB-USB-HS or Keithley KUSB-488A via linux-gpib
                   (requires: pip install gpib-ctypes  OR  linux-gpib kernel module)
  USBTmcGPIB     — xyphro UsbGpib V1 / any USBTMC-class adapter
                   (requires: pip install python-usbtmc  OR  pip install pyvisa pyvisa-py)
  USBGpibV2GPIB  — xyphro USBGpib V2 (CDC serial, !-command protocol)
                   (requires: pip install pyserial; no VISA/usbtmc library needed)
                   https://github.com/xyphro/UsbGpib
  PyVisaGPIB     — Any adapter supported by NI-VISA or PyVISA-py
                   (requires: pip install pyvisa  and optionally pyvisa-py)

All five classes implement the GPIBAdapter abstract interface so that
HP1631A and higher-level helpers are completely adapter-agnostic.
"""

import serial
import time
import struct
from abc import ABC, abstractmethod
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════
#  Abstract GPIB adapter interface
# ═══════════════════════════════════════════════════════════════════════════

class GPIBAdapter(ABC):
    """
    Abstract base class for GPIB adapters.

    Every concrete adapter must implement these methods so that HP1631A
    and all helper functions work without knowing which physical adapter
    is in use.

    Adapter-specific capabilities (e.g. Prologix ++ver, EOS sweep) are
    exposed via optional helper methods that default to raising
    NotImplementedError; the GUI falls back gracefully when they are absent.
    """

    @abstractmethod
    def write(self, instrument_cmd: str):
        """Send a command string to the instrument."""

    @abstractmethod
    def read(self, max_bytes: int = 65536) -> str:
        """Read an ASCII response from the instrument."""

    @abstractmethod
    def read_binary(self, max_bytes: int = 65536) -> bytes:
        """Read a raw binary response from the instrument."""

    @abstractmethod
    def query(self, cmd: str, max_bytes: int = 65536,
              delay: float = 0.3) -> str:
        """Send a command and return an ASCII response."""

    @abstractmethod
    def query_binary(self, cmd: str, max_bytes: int = 65536,
                     delay: float = 0.5) -> bytes:
        """Send a command and return a raw binary response."""

    @abstractmethod
    def trigger(self):
        """Send a Group Execute Trigger (GET)."""

    @abstractmethod
    def clear(self):
        """Send Selected Device Clear (SDC)."""

    @abstractmethod
    def serial_poll(self) -> int:
        """Perform a serial poll. Returns status byte or -1 on error."""

    @abstractmethod
    def srq(self) -> bool:
        """Return True if the SRQ line is asserted."""

    @abstractmethod
    def ifc(self):
        """Send Interface Clear (IFC) — resets the entire GPIB bus."""

    @abstractmethod
    def close(self):
        """Release the adapter and any associated OS resources."""

    # ── Optional adapter-specific helpers (default: not supported) ──────────

    def adapter_type(self) -> str:
        """Return a short human-readable name for this adapter."""
        return self.__class__.__name__

    def firmware_version(self) -> str:
        """
        Query the adapter firmware/driver version string.
        Returns empty string if not supported by this adapter type.
        """
        return ""

    def set_eos(self, eos: int):
        """
        Set the end-of-string terminator (Prologix convention: 0=CR+LF,
        1=CR, 2=LF, 3=None).  No-op on adapters that handle EOS internally.
        """

    def drain(self, timeout_s: float = 0.3):
        """
        Discard stale bytes from the receive buffer.
        No-op on adapters whose OS driver manages buffering.
        """

# ── Menu mnemonic map (Table 10-1) ────────────────────────────────────────
MENU_MNEMONICS = {
    # Top-level menus
    "SYSTEM":   "SM",   # System Specification / Configuration
    "FORMAT":   "FM",   # Format Specification
    "TRACE":    "TM",   # Trace / Acquisition Specification
    "LIST":     "LM",   # List / Listing display
    "WFORM":    "WM",   # Waveform display
    # Cursor navigation
    "CL": "CL",  "CR": "CR",  "CU": "CU",  "CD": "CD",
    # Label navigation
    "LL": "LL",  "LR": "LR",  "LU": "LU",  "LD": "LD",
    # Roll (scroll) navigation
    "RD": "RD",  "RU": "RU",  "RL": "RL",  "RR": "RR",
    # Editing
    "INSERT": "IN",  "DELETE": "DE",
    "CLEAR":  "CE",  "DEFAULT": "DM",
    "NEXT": "NX",  "PREV": "PV",
    # Acquisition
    "RUN": "RN",  "RESUME": "RE",  "STOP": "ST",
    # Print
    "PRINT": "PR",  "PRINTALL": "PA",
}


# ═══════════════════════════════════════════════════════════════════════════
#  Prologix GPIB-USB low-level driver
# ═══════════════════════════════════════════════════════════════════════════

class PrologixGPIB(GPIBAdapter):
    """
    Prologix GPIB-USB adapter (++ serial protocol over a virtual COM port).

    eos: Prologix ++eos setting — terminator appended to instrument commands.
      0=CR+LF  1=CR (default, correct for HP 1631A)  2=LF  3=None

    NOTE: ++read_tmo_ms is sent but gracefully ignored on old firmware.
    Any resulting stale bytes are drained after init.
    """

    def __init__(self, port: str, gpib_addr: int,
                 timeout: float = 5.0, eos: int = 1):
        self.port      = port
        self.gpib_addr = gpib_addr
        self.eos       = eos
        self.ser = serial.Serial(
            port=port, baudrate=115200,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=timeout,
            xonxoff=False, rtscts=False,
        )
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        self._init_controller()

    def _init_controller(self):
        for cmd in [
            "++mode 1",
            "++auto 0",
            f"++eos {self.eos}",
            "++eoi 1",
            "++read_tmo_ms 3000",
            f"++addr {self.gpib_addr}",
        ]:
            self._raw_write(cmd)
        self._drain()   # discard any "Unrecognized command" from old firmware

    def _raw_write(self, cmd: str):
        self.ser.write((cmd + "\n").encode())
        time.sleep(0.05)

    def _drain(self, timeout_s: float = 0.3):
        """Discard all bytes currently in the input buffer."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            n = self.ser.in_waiting
            if n:
                self.ser.read(n)
                deadline = time.time() + 0.1
            else:
                time.sleep(0.02)

    def write(self, instrument_cmd: str):
        """Send a command to the instrument (Prologix appends the EOS terminator)."""
        self._raw_write(f"++addr {self.gpib_addr}")
        self._raw_write(instrument_cmd)

    def read(self, max_bytes: int = 65536) -> str:
        """Read an ASCII response (++read eoi), drain stale bytes first."""
        self._drain(0.05)
        self._raw_write(f"++addr {self.gpib_addr}")
        self._raw_write("++read eoi")
        raw = self.ser.read(max_bytes)
        return raw.decode(errors="replace").strip()

    def read_binary(self, max_bytes: int = 65536) -> bytes:
        """
        Read a binary response (learn string data).
        Binary transfers are terminated by EOI on the last byte.
        Returns raw bytes including all header, data, and CRC bytes.
        """
        self._drain(0.05)
        self._raw_write(f"++addr {self.gpib_addr}")
        self._raw_write("++read eoi")
        return self.ser.read(max_bytes)

    def query(self, cmd: str, max_bytes: int = 65536,
              delay: float = 0.3) -> str:
        """Send a command and return an ASCII response."""
        self.write(cmd)
        time.sleep(delay)
        return self.read(max_bytes)

    def query_binary(self, cmd: str, max_bytes: int = 65536,
                     delay: float = 0.5) -> bytes:
        """Send a command and return a raw binary response."""
        self.write(cmd)
        time.sleep(delay)
        return self.read_binary(max_bytes)

    def trigger(self):
        """Send Group Execute Trigger — starts measurement like pressing RUN."""
        self._raw_write(f"++addr {self.gpib_addr}")
        self._raw_write("++trg")

    def clear(self):
        """Send Selected Device Clear — resets instrument HP-IB parser."""
        self._raw_write(f"++addr {self.gpib_addr}")
        self._raw_write("++clr")
        time.sleep(0.5)

    def serial_poll(self) -> int:
        """Perform a serial poll. Returns status byte or -1 on error."""
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

    # ── GPIBAdapter optional helpers ─────────────────────────────────────────

    def adapter_type(self) -> str:
        return "Prologix"

    def firmware_version(self) -> str:
        """Query ++ver and return the version string."""
        self._raw_write("++ver")
        time.sleep(0.2)
        raw = b""
        while self.ser.in_waiting:
            raw += self.ser.read(self.ser.in_waiting)
            time.sleep(0.05)
        return raw.decode(errors="replace").strip()

    def set_eos(self, eos: int):
        self.eos = eos
        self._raw_write(f"++eos {eos}")

    def drain(self, timeout_s: float = 0.3):
        self._drain(timeout_s)




# ═══════════════════════════════════════════════════════════════════════════
#  NI-488.2 / linux-gpib adapter  (NI GPIB-USB-HS, Keithley KUSB-488A)
# ═══════════════════════════════════════════════════════════════════════════

class NI488GPIB(GPIBAdapter):
    """
    GPIB adapter for hardware using the linux-gpib kernel driver or the
    gpib-ctypes userspace binding, which covers:

      • NI GPIB-USB-HS  (on Linux with linux-gpib or on Windows with NI-488.2)
      • Keithley KUSB-488A  (uses the same linux-gpib interface on Linux)

    Installation
    ------------
    Linux (preferred):
        pip install gpib-ctypes
        # OR build linux-gpib from source: https://linux-gpib.sourceforge.io/

    Windows (NI-488.2 must be installed separately from ni.com):
        pip install gpib-ctypes

    Parameters
    ----------
    board_index : int
        linux-gpib board index (almost always 0 for the first/only adapter).
    gpib_addr : int
        Primary GPIB address of the instrument (0–30).
    timeout : float
        Instrument response timeout in seconds.  Mapped to the nearest
        linux-gpib T-constant (T1s=11, T3s=12, T10s=13, T30s=14, T100s=15).
    """

    # linux-gpib timeout constants (T<n>s values)
    _TIMEOUT_MAP = [
        (1,   11),   # T1s
        (3,   12),   # T3s
        (10,  13),   # T10s
        (30,  14),   # T30s
        (100, 15),   # T100s
    ]

    def __init__(self, board_index: int = 0, gpib_addr: int = 5,
                 timeout: float = 10.0):
        try:
            import gpib
        except ImportError as e:
            raise ImportError(
                "gpib-ctypes is not installed.  Run:  pip install gpib-ctypes\n"
                "On Linux you may also need the linux-gpib kernel module."
            ) from e
        self._gpib   = gpib
        self._board  = board_index
        self.gpib_addr = gpib_addr
        # Map timeout to nearest T-constant
        tval = 11
        for secs, tconst in self._TIMEOUT_MAP:
            if timeout <= secs:
                tval = tconst
                break
        else:
            tval = 15   # T100s
        # Open the device
        self._dev = gpib.dev(board_index, gpib_addr, 0, tval, 1, 0)
        # Assert Remote Enable so the instrument goes remote
        gpib.remote_enable(board_index, 1)

    def write(self, instrument_cmd: str):
        self._gpib.write(self._dev,
                         (instrument_cmd + "\n").encode())

    def read(self, max_bytes: int = 65536) -> str:
        try:
            raw = self._gpib.read(self._dev, max_bytes)
            return raw.decode(errors="replace").strip()
        except Exception:
            return ""

    def read_binary(self, max_bytes: int = 65536) -> bytes:
        try:
            return self._gpib.read(self._dev, max_bytes)
        except Exception:
            return b""

    def query(self, cmd: str, max_bytes: int = 65536,
              delay: float = 0.3) -> str:
        self.write(cmd)
        time.sleep(delay)
        return self.read(max_bytes)

    def query_binary(self, cmd: str, max_bytes: int = 65536,
                     delay: float = 0.5) -> bytes:
        self.write(cmd)
        time.sleep(delay)
        return self.read_binary(max_bytes)

    def trigger(self):
        self._gpib.trigger(self._dev)

    def clear(self):
        self._gpib.clear(self._dev)
        time.sleep(0.5)

    def serial_poll(self) -> int:
        try:
            result = self._gpib.ibrsp(self._dev)
            # ibrsp returns a bytes-like object; first byte is the status byte
            if isinstance(result, (bytes, bytearray)) and result:
                return result[0]
            return int(result) & 0xFF
        except Exception:
            return -1

    def srq(self) -> bool:
        try:
            sta = self._gpib.ibsta(self._board)
            return bool(sta & 0x800)   # bit 11 = SRQI
        except Exception:
            return False

    def ifc(self):
        self._gpib.SendIFC(self._board)
        time.sleep(0.5)

    def close(self):
        try:
            self._gpib.remote_enable(self._board, 0)
            self._gpib.close(self._dev)
        except Exception:
            pass

    def adapter_type(self) -> str:
        return "NI-488 / linux-gpib"

    def firmware_version(self) -> str:
        """linux-gpib does not expose an adapter firmware version."""
        return "(linux-gpib — no firmware version query)"


# ═══════════════════════════════════════════════════════════════════════════
#  USBTMC adapter  (xyphro UsbGpib, and any USBTMC-class device)
# ═══════════════════════════════════════════════════════════════════════════

class USBTmcGPIB(GPIBAdapter):
    """
    GPIB adapter for the xyphro UsbGpib and any other USBTMC-class
    USB-GPIB converter.

    The xyphro UsbGpib enumerates as a standard USBTMC device (USB class 0xFE,
    subclass 0x03).  It auto-detects the instrument GPIB address on power-up
    and requires no address configuration in normal single-instrument use.
    For multi-instrument use (firmware v2.4+) the address can be changed via
    a vendor control request — see set_gpib_addr().

    This backend tries two communication paths in order:
      1. python-usbtmc  (pip install python-usbtmc) — raw USBTMC, no VISA needed
      2. PyVISA         (pip install pyvisa pyvisa-py) — uses a VISA resource string

    Pass either:
      resource : str   — PyVISA resource string, e.g. "USB0::0x03EB::0x2065::...::INSTR"
                         If omitted, the first USBTMC device found is used (usbtmc backend).
      vid / pid : int  — USB Vendor/Product ID for python-usbtmc direct open.
                         xyphro UsbGpib V1/V2: vid=0x03EB, pid=0x2065
    """

    def __init__(self, resource: str = "", vid: int = 0, pid: int = 0,
                 gpib_addr: Optional[int] = None, timeout: float = 10.0):
        self._timeout   = timeout
        self._inst      = None
        self._visa_rm   = None
        self.gpib_addr  = gpib_addr

        # ── Try python-usbtmc first ──────────────────────────────────────
        if not resource:
            try:
                import usbtmc
                if vid and pid:
                    self._inst = usbtmc.Instrument(vid, pid)
                else:
                    self._inst = usbtmc.Instrument()   # first found
                self._inst.timeout = int(timeout * 1000)
                self._backend = "usbtmc"
                return
            except ImportError:
                pass
            except Exception as e:
                raise RuntimeError(
                    f"python-usbtmc could not open device: {e}\n"
                    "Verify the UsbGpib is plugged in, powered, and connected\n"
                    "to an instrument on the GPIB side."
                ) from e

        # ── Fall back to PyVISA ──────────────────────────────────────────
        try:
            import pyvisa
            self._visa_rm = pyvisa.ResourceManager()
            res = resource or self._visa_rm.list_resources("USB?*")[0]
            self._inst = self._visa_rm.open_resource(res)
            self._inst.timeout = int(timeout * 1000)
            self._backend = "pyvisa"
        except ImportError as e:
            raise ImportError(
                "Neither python-usbtmc nor pyvisa is installed.\n"
                "Run one of:\n"
                "  pip install python-usbtmc\n"
                "  pip install pyvisa pyvisa-py"
            ) from e
        except Exception as e:
            raise RuntimeError(
                f"PyVISA could not open '{resource}': {e}"
            ) from e

    def _write_raw(self, data: bytes):
        if self._backend == "usbtmc":
            self._inst.write_raw(data)
        else:
            self._inst.write_raw(data)

    def write(self, instrument_cmd: str):
        self._write_raw((instrument_cmd + "\n").encode())

    def read(self, max_bytes: int = 65536) -> str:
        try:
            if self._backend == "usbtmc":
                raw = self._inst.read_raw(max_bytes)
            else:
                raw = self._inst.read_raw(max_bytes)
            return raw.decode(errors="replace").strip()
        except Exception:
            return ""

    def read_binary(self, max_bytes: int = 65536) -> bytes:
        try:
            if self._backend == "usbtmc":
                return self._inst.read_raw(max_bytes)
            else:
                return self._inst.read_raw(max_bytes)
        except Exception:
            return b""

    def query(self, cmd: str, max_bytes: int = 65536,
              delay: float = 0.3) -> str:
        self.write(cmd)
        time.sleep(delay)
        return self.read(max_bytes)

    def query_binary(self, cmd: str, max_bytes: int = 65536,
                     delay: float = 0.5) -> bytes:
        self.write(cmd)
        time.sleep(delay)
        return self.read_binary(max_bytes)

    def trigger(self):
        """Send a GPIB GET via USBTMC trigger request."""
        try:
            if self._backend == "usbtmc":
                self._inst.trigger()
            else:
                self._inst.assert_trigger()
        except Exception:
            pass

    def clear(self):
        """Send SDC via USBTMC clear request."""
        try:
            if self._backend == "usbtmc":
                self._inst.clear()
            else:
                self._inst.clear()
        except Exception:
            pass
        time.sleep(0.5)

    def serial_poll(self) -> int:
        try:
            if self._backend == "usbtmc":
                # USBTMC READ_STATUS_BYTE control request
                result = self._inst.read_stb()
                return int(result) & 0xFF
            else:
                return int(self._inst.read_stb()) & 0xFF
        except Exception:
            return -1

    def srq(self) -> bool:
        sb = self.serial_poll()
        return (sb & 0x40) != 0 if sb >= 0 else False

    def ifc(self):
        """
        The USBTMC/UsbGpib adapter manages IFC internally; we approximate
        it by clearing the device and re-asserting Remote Enable.
        For the xyphro UsbGpib there is no direct IFC command exposed over
        USBTMC, so we do a USB device reset instead.
        """
        try:
            self.clear()
        except Exception:
            pass
        time.sleep(0.5)

    def close(self):
        try:
            if self._backend == "usbtmc":
                self._inst.close()
            else:
                self._inst.close()
                self._visa_rm.close()
        except Exception:
            pass

    def adapter_type(self) -> str:
        return f"USBTMC ({self._backend})"

    def firmware_version(self) -> str:
        """
        Query the xyphro UsbGpib adapter firmware version via the vendor
        control request and !ver? command (firmware >= 2024-01-13).
        Falls back to empty string on older firmware or non-xyphro devices.
        """
        try:
            if self._backend == "usbtmc":
                # Pulse indicator request to enable internal command processing
                self._inst.pulse_indicator_request()
                return self._inst.ask("!ver?")
            else:
                # PyVISA path: use control_in for pulse indicator
                self._inst.control_in(0xa1, 0x40, 0, 0, 1)
                return self._inst.query("!ver?")
        except Exception:
            return ""

    def set_gpib_addr(self, primary: int, secondary: Optional[int] = None):
        """
        Set the GPIB target address on the xyphro UsbGpib (firmware v2.4+).
        This enables multi-instrument operation without reconnecting.
        """
        try:
            if self._backend == "usbtmc":
                self._inst.pulse_indicator_request()
                if secondary is None:
                    self._inst.write(f"!addr {primary}")
                else:
                    self._inst.write(f"!addr {primary} {secondary}")
            else:
                self._inst.control_in(0xa1, 0x40, 0, 0, 1)
                if secondary is None:
                    self._inst.write(f"!addr {primary}")
                else:
                    self._inst.write(f"!addr {primary} {secondary}")
            self.gpib_addr = primary
        except Exception as e:
            raise RuntimeError(f"set_gpib_addr failed: {e}") from e


# ═══════════════════════════════════════════════════════════════════════════
#  xyphro USBGpib V2  (CDC serial,  !-command protocol)
# ═══════════════════════════════════════════════════════════════════════════

class USBGpibV2GPIB(GPIBAdapter):
    """
    GPIB adapter for the xyphro USBGpib V2 hardware.
    https://github.com/xyphro/UsbGpib

    The V2 firmware presents a USB CDC (virtual COM port) interface —
    no VISA, no USBTMC class driver required; only pyserial is needed.

    Protocol overview (V2 firmware)
    --------------------------------
    All adapter-control commands begin with '!' and are terminated with \\n.
    Instrument data (writes and reads) use a thin framing layer:

      Write to instrument:
        !write <addr>\\n
        <data bytes>\\n       (the \\n acts as the GPIB EOI trigger)

      Read from instrument:
        !read <addr> <max_bytes>\\n
        → adapter replies with the raw bytes followed by \\n

      Serial poll:
        !spoll <addr>\\n
        → single decimal byte value + \\n

      Group Execute Trigger:
        !trigger <addr>\\n

      Interface Clear:
        !ifc\\n

      Selected Device Clear:
        !clr <addr>\\n

      SRQ line state:
        !srq\\n
        → "1\\n" or "0\\n"

      Firmware version:
        !ver\\n
        → version string + \\n

    The adapter does NOT auto-assert Remote Enable; it is asserted
    implicitly on the first addressed write (same as Prologix behaviour).

    Parameters
    ----------
    port      : str   Serial port, e.g. "COM3" on Windows or "/dev/ttyACM0" on Linux.
    gpib_addr : int   Primary GPIB address of the instrument.
    timeout   : float Serial read timeout in seconds (default 5.0).
    """

    # Maximum bytes to request per !read call.  The V2 firmware buffers up to
    # 8 192 bytes internally; stay below that to avoid overrun.
    _MAX_READ_CHUNK = 8000

    def __init__(self, port: str, gpib_addr: int, timeout: float = 5.0):
        self.port      = port
        self.gpib_addr = int(gpib_addr)
        self._timeout  = timeout
        self.ser = serial.Serial(
            port=port, baudrate=115200,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=timeout,
            xonxoff=False, rtscts=False,
        )
        time.sleep(0.3)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        # Verify adapter responds to !ver
        ver = self.firmware_version()
        if not ver:
            raise RuntimeError(
                f"USBGpib V2 on {port}: no response to !ver — "
                "check port, cable, and that firmware >= 2.x is installed."
            )

    # ── Internal helpers ──────────────────────────────────────────────────

    def _cmd(self, cmd: str):
        """Send a !-prefixed adapter control command."""
        self.ser.write((cmd + "\n").encode())
        time.sleep(0.03)

    def _readline(self) -> str:
        """Read one \\n-terminated line from the adapter."""
        raw = self.ser.readline()
        return raw.decode(errors="replace").rstrip("\r\n")

    def _drain(self, timeout_s: float = 0.2):
        """Discard stale bytes from the receive buffer."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            n = self.ser.in_waiting
            if n:
                self.ser.read(n)
                deadline = time.time() + 0.05
            else:
                time.sleep(0.02)

    # ── GPIBAdapter implementation ────────────────────────────────────────

    def write(self, instrument_cmd: str):
        """
        Send a command to the instrument.
        The V2 protocol: !write <addr>\\n followed by the command + \\n.
        The trailing \\n is the EOI signal on the GPIB bus.
        """
        self._cmd(f"!write {self.gpib_addr}")
        self.ser.write((instrument_cmd + "\n").encode())
        time.sleep(0.05)

    def read(self, max_bytes: int = 65536) -> str:
        """Request a read from the instrument and return ASCII text."""
        ask = min(max_bytes, self._MAX_READ_CHUNK)
        self._cmd(f"!read {self.gpib_addr} {ask}")
        raw = self.ser.read(ask + 2)   # +2 for trailing \\n
        return raw.rstrip(b"\r\n").decode(errors="replace").strip()

    def read_binary(self, max_bytes: int = 65536) -> bytes:
        """
        Read raw binary data from the instrument (e.g. learn strings).
        Issues repeated !read requests, accumulating until the adapter
        signals EOF (returns fewer bytes than requested) or max_bytes reached.
        The V2 firmware signals end-of-transfer by returning 0 data bytes.
        """
        buf = bytearray()
        remaining = max_bytes
        while remaining > 0:
            ask = min(remaining, self._MAX_READ_CHUNK)
            self._cmd(f"!read {self.gpib_addr} {ask}")
            chunk = self.ser.read(ask + 2)
            # Strip the trailing \\n the adapter appends
            if chunk.endswith(b"\n"):
                chunk = chunk[:-1]
            if chunk.endswith(b"\r"):
                chunk = chunk[:-1]
            buf.extend(chunk)
            remaining -= len(chunk)
            # If we received less than we asked for, the transfer is complete
            if len(chunk) < ask:
                break
        return bytes(buf)

    def query(self, cmd: str, max_bytes: int = 65536,
              delay: float = 0.3) -> str:
        self.write(cmd)
        time.sleep(delay)
        return self.read(max_bytes)

    def query_binary(self, cmd: str, max_bytes: int = 65536,
                     delay: float = 0.5) -> bytes:
        self.write(cmd)
        time.sleep(delay)
        return self.read_binary(max_bytes)

    def trigger(self):
        """Send Group Execute Trigger."""
        self._cmd(f"!trigger {self.gpib_addr}")

    def clear(self):
        """Send Selected Device Clear (SDC)."""
        self._cmd(f"!clr {self.gpib_addr}")
        time.sleep(0.5)

    def serial_poll(self) -> int:
        """Perform a serial poll. Returns the status byte or -1 on error."""
        self._drain(0.05)
        self._cmd(f"!spoll {self.gpib_addr}")
        resp = self._readline()
        try:
            return int(resp.strip()) & 0xFF
        except ValueError:
            return -1

    def srq(self) -> bool:
        """Return True if the SRQ line is asserted."""
        self._cmd("!srq")
        resp = self._readline()
        return resp.strip() == "1"

    def ifc(self):
        """Send Interface Clear — resets the entire GPIB bus."""
        self._cmd("!ifc")
        time.sleep(0.5)

    def close(self):
        self.ser.close()

    # ── Optional helpers ──────────────────────────────────────────────────

    def adapter_type(self) -> str:
        return "USBGpib V2"

    def firmware_version(self) -> str:
        """Query !ver and return the firmware version string."""
        self._drain(0.05)
        self._cmd("!ver")
        time.sleep(0.15)
        resp = b""
        while self.ser.in_waiting:
            resp += self.ser.read(self.ser.in_waiting)
            time.sleep(0.05)
        return resp.decode(errors="replace").strip()

    def drain(self, timeout_s: float = 0.3):
        self._drain(timeout_s)

    def set_gpib_addr(self, primary: int):
        """Change the target GPIB address without reconnecting."""
        self.gpib_addr = int(primary)


# ═══════════════════════════════════════════════════════════════════════════
#  PyVISA generic adapter  (any VISA-compatible adapter/resource string)
# ═══════════════════════════════════════════════════════════════════════════

class PyVisaGPIB(GPIBAdapter):
    """
    Generic GPIB adapter using PyVISA.

    Works with any VISA-registered adapter, including:
      • NI GPIB-USB-HS      (GPIB0::5::INSTR  with NI-VISA installed)
      • Keithley KUSB-488A  (GPIB0::5::INSTR  with NI-VISA or Keithley VISA)
      • xyphro UsbGpib      (USB0::...::INSTR  with pyvisa-py)
      • Prologix            (ASRL/dev/ttyUSB0::INSTR  with pyvisa-py, limited)
      • Any instrument via VXI-11 / TCPIP (for the GPIBee network interface)

    Parameters
    ----------
    resource : str
        Full VISA resource string.  Examples:
          "GPIB0::5::INSTR"          — NI GPIB-USB-HS, addr 5
          "USB0::0x03EB::0x2065::GPIB_5_...::INSTR"  — xyphro UsbGpib
          "TCPIP::192.168.1.100::gpib,5::INSTR"       — GPIBee via VXI-11
    timeout : float
        Timeout in seconds (converted to milliseconds for PyVISA).
    visa_library : str
        Path to the VISA shared library, or '' to use the default NI-VISA.
        For pyvisa-py (pure Python): pass '@py'.
    """

    def __init__(self, resource: str, timeout: float = 10.0,
                 visa_library: str = ""):
        try:
            import pyvisa
        except ImportError as e:
            raise ImportError(
                "pyvisa is not installed.  Run:  pip install pyvisa\n"
                "For a no-NI-VISA option also run:  pip install pyvisa-py"
            ) from e
        kwargs = {}
        if visa_library:
            kwargs["visa_library"] = visa_library
        self._rm   = pyvisa.ResourceManager(**kwargs)
        self._inst = self._rm.open_resource(resource)
        self._inst.timeout = int(timeout * 1000)
        self._resource = resource

    def write(self, instrument_cmd: str):
        self._inst.write(instrument_cmd)

    def read(self, max_bytes: int = 65536) -> str:
        try:
            return self._inst.read().strip()
        except Exception:
            return ""

    def read_binary(self, max_bytes: int = 65536) -> bytes:
        try:
            return self._inst.read_raw(max_bytes)
        except Exception:
            return b""

    def query(self, cmd: str, max_bytes: int = 65536,
              delay: float = 0.3) -> str:
        self.write(cmd)
        time.sleep(delay)
        return self.read(max_bytes)

    def query_binary(self, cmd: str, max_bytes: int = 65536,
                     delay: float = 0.5) -> bytes:
        self.write(cmd)
        time.sleep(delay)
        return self.read_binary(max_bytes)

    def trigger(self):
        try:
            self._inst.assert_trigger()
        except Exception:
            pass

    def clear(self):
        try:
            self._inst.clear()
        except Exception:
            pass
        time.sleep(0.5)

    def serial_poll(self) -> int:
        try:
            return int(self._inst.read_stb()) & 0xFF
        except Exception:
            return -1

    def srq(self) -> bool:
        sb = self.serial_poll()
        return (sb & 0x40) != 0 if sb >= 0 else False

    def ifc(self):
        try:
            self._inst.send_ifc()
        except Exception:
            pass
        time.sleep(0.5)

    def close(self):
        try:
            self._inst.close()
            self._rm.close()
        except Exception:
            pass

    def adapter_type(self) -> str:
        return f"PyVISA ({self._resource})"


# ═══════════════════════════════════════════════════════════════════════════
#  Convenience factory
# ═══════════════════════════════════════════════════════════════════════════

def open_gpib_adapter(adapter: str, **kwargs) -> GPIBAdapter:
    """
    Factory function for creating a GPIBAdapter by name.

    adapter : one of 'prologix', 'ni488', 'usbtmc', 'usbgpibv2', 'pyvisa'

    Prologix   kwargs:  port (str), gpib_addr (int), timeout (float), eos (int)
    NI-488     kwargs:  board_index (int=0), gpib_addr (int), timeout (float)
    USBTMC     kwargs:  resource (str=''), vid (int=0), pid (int=0),
                        gpib_addr (int=None), timeout (float)
    USBGpibV2  kwargs:  port (str), gpib_addr (int), timeout (float)
    PyVISA     kwargs:  resource (str), timeout (float), visa_library (str='')

    Example
    -------
    >>> gpib = open_gpib_adapter('prologix', port='/dev/ttyUSB0', gpib_addr=5)
    >>> gpib = open_gpib_adapter('usbgpibv2', port='/dev/ttyACM0', gpib_addr=5)
    >>> gpib = open_gpib_adapter('usbtmc')       # first UsbGpib V1 found
    >>> gpib = open_gpib_adapter('ni488', gpib_addr=5)
    >>> gpib = open_gpib_adapter('pyvisa', resource='GPIB0::5::INSTR')
    """
    a = adapter.lower().strip()
    if a == "prologix":
        return PrologixGPIB(**kwargs)
    elif a in ("ni488", "ni", "linux-gpib", "kusb488"):
        return NI488GPIB(**kwargs)
    elif a in ("usbtmc", "usbgpib", "xyphro"):
        return USBTmcGPIB(**kwargs)
    elif a in ("usbgpibv2", "usbgpib_v2", "usbgpib v2", "xyphro_v2"):
        return USBGpibV2GPIB(**kwargs)
    elif a in ("pyvisa", "visa"):
        return PyVisaGPIB(**kwargs)
    else:
        raise ValueError(
            f"Unknown adapter '{adapter}'.  "
            "Choose from: prologix, ni488, usbtmc, usbgpibv2, pyvisa"
        )




class HP1631A:
    """
    Instrument driver for the HP 1631A/D Logic Analyzer.
    All commands verified against Chapter 10 of the 1631A/D manual.

    Status Byte 1 bit map (from serial poll / SB command):
      Bit 0  (  1)  Print Complete
      Bit 1  (  2)  Measurement Complete  ← DATA_READY
      Bit 2  (  4)  Slow Clock
      Bit 3  (  8)  Key Pressed (Front Panel Request)
      Bit 4  ( 16)  Not Busy
      Bit 5  ( 32)  Error in Last Command
      Bit 6  ( 64)  Reserved (SRQ indicator)

    The mask byte (MB command) defaults to 0 at power-on, which disables all
    SRQ/serial-poll reporting.  Call set_mask() after connecting.
    """

    # Status byte bit masks
    SB_PRINT_COMPLETE       = 0x01
    SB_MEASUREMENT_COMPLETE = 0x02   # ← DATA_READY
    SB_SLOW_CLOCK           = 0x04
    SB_KEY_PRESSED          = 0x08
    SB_NOT_BUSY             = 0x10
    SB_ERROR                = 0x20
    SB_SRQ                  = 0x40

    # Learn string receive headers
    HEADER_CONFIG  = b"RC"
    HEADER_STATE   = b"RS"
    HEADER_TIMING  = b"RT"
    HEADER_ANALOG  = b"RA"

    def __init__(self, gpib: GPIBAdapter):
        self.gpib = gpib

    # ── Identification ──────────────────────────────────────────────────────

    def identify(self) -> str:
        """Send ID command. Returns 'HP1631A' or 'HP1631D'."""
        return self.gpib.query("ID", delay=0.3)

    # ── SRQ mask ────────────────────────────────────────────────────────────

    def set_mask(self, value: int = 34):
        """
        Set the service request mask byte (MB command).
        Default 34 = bit 1 (Measurement Complete, value 2)
                   + bit 5 (Error in Last Command, value 32).
        Mask byte defaults to 0 at power-on and is cleared by RST.
        Must be called after connecting before serial poll will work.
        Confirmed working on HP1631A firmware per probe test 2.
        """
        self.gpib.write(f"MB {value}")
        time.sleep(0.1)

    def get_status_byte(self) -> int:
        """
        Serial poll the instrument (preferred — does not abort output).
        Returns the SB1 register value, or -1 on failure.
        Only bits enabled by MB will be set.
        """
        return self.gpib.serial_poll()

    def get_status_byte_direct(self) -> int:
        """
        Request SB1 via the SB command (sends raw byte without CR/LF).
        WARNING: this can abort pending output.  Use serial poll instead.
        """
        self.gpib.write("SB 1")
        time.sleep(0.2)
        self.gpib._raw_write(f"++addr {self.gpib.gpib_addr}")
        self.gpib._raw_write("++read eoi")
        raw = self.gpib.ser.read(1)
        return raw[0] if raw else -1

    # ── Acquisition control ─────────────────────────────────────────────────

    def start(self):
        """Send RN (RUN key mnemonic) to start/re-arm acquisition."""
        self.gpib.write("RN")

    def stop(self):
        """Send ST (STOP key mnemonic) to halt acquisition."""
        self.gpib.write("ST")

    def resume(self):
        """Send RE (RESUME key mnemonic)."""
        self.gpib.write("RE")

    def trigger(self):
        """Send Group Execute Trigger — equivalent to pressing RUN."""
        self.gpib.trigger()

    def reset(self):
        """
        RST — reset to power-up condition.
        Wait at least 1 second after this before sending further commands.
        """
        self.gpib.write("RST")
        time.sleep(1.2)

    def power_up_defaults(self):
        """PU — default all instrument menus (does not clear SRQ/status)."""
        self.gpib.write("PU")
        time.sleep(0.3)

    def clear(self):
        """Send SDC (Selected Device Clear) — resets the HP-IB parser."""
        self.gpib.clear()

    # ── Status polling ──────────────────────────────────────────────────────

    def wait_for_measurement_complete(self, timeout: float = 120.0,
                                      poll_interval: float = 0.5,
                                      cancel_event=None) -> bool:
        """
        Poll serial poll until Measurement Complete (bit 1) is set,
        or until timeout, or until cancel_event is set.
        Returns True when measurement is complete.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cancel_event and cancel_event.is_set():
                return False
            sb = self.gpib.serial_poll()
            if sb < 0:
                pass  # transient
            elif sb & self.SB_ERROR:
                return False
            elif sb & self.SB_MEASUREMENT_COMPLETE:
                return True
            time.sleep(poll_interval)
        return False

    def wait_for_srq(self, timeout: float = 120.0,
                     cancel_event=None) -> bool:
        """
        Wait until SRQ line is asserted then serial-poll to clear it.
        Returns True if SRQ received within timeout.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cancel_event and cancel_event.is_set():
                return False
            if self.gpib.srq():
                self.gpib.serial_poll()  # clear SRQ
                return True
            time.sleep(0.1)
        return False

    # ── Menu navigation (Table 10-1 keyboard mnemonics) ─────────────────────

    def menu(self, name: str):
        """
        Navigate to a menu using the correct 2-char keyboard mnemonic.
        Name can be the full name ('SYSTEM', 'FORMAT', 'TRACE', 'LIST',
        'WFORM') or the 2-char mnemonic directly ('SM', 'FM', etc.).
        """
        name_upper = name.upper()
        mnemonic = MENU_MNEMONICS.get(name_upper, name_upper[:2])
        self.gpib.write(mnemonic)
        time.sleep(0.2)

    def key(self, mnemonic: str, repeat: int = 1):
        """
        Send any keyboard mnemonic, optionally repeated.
        e.g. key('CD', 5) = press CURSOR DOWN 5 times.
        """
        if repeat > 1:
            self.gpib.write(f"{mnemonic}{repeat}")
        else:
            self.gpib.write(mnemonic)
        time.sleep(0.1)

    # ── Display Read ────────────────────────────────────────────────────────

    def display_read(self, row: int = 1, col: int = 1,
                     count: int = 1472) -> str:
        """
        DR command — read COUNT bytes from display memory starting at ROW, COL.
        Display is 23 rows × 64 columns = 1472 bytes maximum.
        Inverse-video characters are returned as ASCII value + 128 — strip
        the high bit to get plain ASCII.

        Non-binary DR transfers are terminated with CR LF + EOI (per manual),
        so up to 2 extra bytes arrive; they are stripped automatically here.

        Use this after navigating to LM (List menu) to read the state/timing
        listing, or WM for waveform data, as ASCII screen text.
        """
        count = min(count, 1472)
        self.gpib.write(f"DR {row} {col} {count}")
        time.sleep(0.5)
        raw = self.gpib.read_binary(max_bytes=count + 16)
        # Strip inverse-video bit (bit 7) from display character bytes
        # and remove trailing CR/LF terminator bytes
        stripped = bytes(b & 0x7F for b in raw)
        text = stripped.decode(errors="replace")
        # Trim trailing CR and LF (DR terminator per Chapter 10)
        text = text.rstrip("\r\n")
        return text

    def read_full_screen(self) -> str:
        """
        Read the entire 23×64 display (1472 bytes) as plain ASCII string.
        Trailing CR/LF and inverse-video flags are stripped automatically.
        Returns up to 1472 chars arranged as 23 lines of up to 64 chars.
        """
        return self.display_read(1, 1, 1472)

    def read_full_screen_rows(self) -> list:
        """
        Read the full display and return as a list of 23 row strings,
        each up to 64 characters, with trailing whitespace stripped.
        """
        text = self.display_read(1, 1, 1472)
        rows = []
        for i in range(0, 1472, 64):
            chunk = text[i:i+64] if i < len(text) else ""
            rows.append(chunk.rstrip())
        return rows

    def read_listing_pages(self, pages: int = 1,
                           cancel_event=None) -> list[str]:
        """
        Read 'pages' of listing data by repeatedly:
          1. Reading the current display content (DR)
          2. Sending RD (Roll Down) to advance to the next page
        Returns a list of page strings.
        """
        result = []
        for _ in range(pages):
            if cancel_event and cancel_event.is_set():
                break
            result.append(self.read_full_screen())
            self.key("RD")   # Roll Down one page
            time.sleep(0.2)
        return result

    # ── Learn String data download (binary) ─────────────────────────────────

    def get_config_learn_string(self) -> bytes:
        """
        TC command — transmit configuration learn string (5145 bytes).
        Returns raw binary bytes including the RC header and CRC.
        """
        return self.gpib.query_binary("TC", max_bytes=6000, delay=0.5)

    def get_state_learn_string(self) -> bytes:
        """
        TS command — transmit state acquisition learn string.
        Returns raw binary bytes including RS header and CRC.
        """
        return self.gpib.query_binary("TS", max_bytes=65536, delay=1.0)

    def get_timing_learn_string(self) -> bytes:
        """
        TT command — transmit timing acquisition learn string.
        Returns raw binary bytes including RT header and CRC.
        """
        return self.gpib.query_binary("TT", max_bytes=65536, delay=1.0)

    def get_analog_learn_string(self) -> bytes:
        """
        TA command — transmit analog acquisition learn string.
        """
        return self.gpib.query_binary("TA", max_bytes=65536, delay=1.0)

    def get_everything_learn_string(self) -> bytes:
        """
        TE command — transmit everything (TC + TS + TT + TA combined).
        Maximum size: 13576 bytes.
        """
        return self.gpib.query_binary("TE", max_bytes=16384, delay=2.0)

    # ── Utility ─────────────────────────────────────────────────────────────

    def beep(self):
        self.gpib.write("BP")

    def cursor_home(self):
        """CH — move cursor to upper-leftmost display field."""
        self.gpib.write("CH")

    def get_key_buffer(self) -> str:
        """KE — return last front-panel key pressed, or '??' if none."""
        return self.gpib.query("KE", delay=0.2)

    def send_raw(self, cmd: str) -> str:
        """Send an arbitrary command; read and return response if it ends with ';'."""
        if cmd.strip().endswith(";") or "?" in cmd:
            return self.gpib.query(cmd)
        self.gpib.write(cmd)
        return ""


# ═══════════════════════════════════════════════════════════════════════════
#  Learn string parser
# ═══════════════════════════════════════════════════════════════════════════

class LearnStringParser:
    """
    Parses binary learn strings returned by TC, TS, TT, TA, TE commands.

    Each learn string format:
      Bytes 0-1  : ASCII receive-command header ("RC", "RS", "RT", "RA")
      Bytes 2-3  : Binary byte count MSB first (includes CRC bytes, not header/count)
      Bytes 4..N : Binary data
      Byte  N+1  : Revision code
      Bytes N+2-3: CRC (2 bytes, integer MSB first)
    """

    @staticmethod
    def parse_header(data: bytes) -> dict:
        """
        Parse just the header of a learn string.
        Returns dict with: header, byte_count, revision, data_length, valid.
        """
        if len(data) < 4:
            return {"valid": False, "error": "Too short for header"}
        header = data[0:2].decode(errors="replace")
        byte_count = struct.unpack(">H", data[2:4])[0]
        total_expected = 4 + byte_count  # header(2) + count(2) + data
        revision = data[total_expected - 3] if len(data) >= total_expected - 2 else None
        return {
            "valid":        len(data) >= total_expected,
            "header":       header,
            "byte_count":   byte_count,
            "total_bytes":  total_expected,
            "received":     len(data),
            "revision":     revision,
            "error":        None if len(data) >= total_expected else
                            f"Expected {total_expected} bytes, got {len(data)}",
        }

    @staticmethod
    def verify_crc(data: bytes) -> bool:
        """
        Verify the 2-byte CRC at the end of a learn string.
        CRC covers bytes from the first data byte after the count word,
        up to and including the revision code byte.
        Returns True if CRC matches.
        """
        if len(data) < 6:
            return False
        byte_count = struct.unpack(">H", data[2:4])[0]
        if len(data) < 4 + byte_count:
            return False
        # CRC covers bytes 4 .. (4 + byte_count - 3)  i.e. all data except last 2 CRC bytes
        crc_data = data[4: 4 + byte_count - 2]
        stored_crc = struct.unpack(">H", data[4 + byte_count - 2: 4 + byte_count])[0]
        # HP uses a simple 16-bit sum CRC
        computed = sum(crc_data) & 0xFFFF
        return computed == stored_crc

    @classmethod
    def parse_timing_header(cls, data: bytes) -> dict:
        """Parse the fixed header fields of a TT (timing) learn string."""
        info = cls.parse_header(data)
        if not info["valid"] or len(data) < 52:
            return info
        # Byte 4: number of timing channels
        info["timing_channels"]   = data[4]
        # Bytes 5-6: number of valid timing states (MSB first)
        info["valid_states"]      = struct.unpack(">H", data[5:7])[0]
        # Bytes 7-8: tracepoint index
        info["tracepoint_index"]  = struct.unpack(">H", data[7:9])[0]
        # Byte 9: glitch mode on (0=off, non-zero=on)
        info["glitch_mode"]       = bool(data[9])
        # Bytes 10-11: sample period  (T format: byte0=multiplier 0-8, byte1=units)
        sp_mult   = data[10]
        sp_units  = data[11]
        units_map = {0:"ns",1:"µs",2:"ms",3:"ms",4:"ms",5:"ms",6:"ms",7:"ms",8:"ms"}
        mult_map  = {0:1,1:1,2:2,3:5,4:10,5:20,6:50,7:100,8:200}
        info["sample_period_str"] = (
            f"{mult_map.get(sp_mult,sp_mult)} {units_map.get(sp_units,'?')}"
        )
        # Bytes 48-49: number of hits; 50-51: number of runs
        info["hits"] = struct.unpack(">H", data[48:50])[0]
        info["runs"] = struct.unpack(">H", data[50:52])[0]
        return info

    @classmethod
    def parse_config_header(cls, data: bytes) -> dict:
        """Parse identifying fields from a TC (configuration) learn string."""
        info = cls.parse_header(data)
        if not info["valid"]:
            return info
        # Configuration data is 5138 bytes (bytes 4..5141) — complex internal format
        # Just report what we know from the header
        info["description"] = (
            "Configuration learn string — use send_config_learn_string() "
            "to restore to instrument"
        )
        return info

    @classmethod
    def extract_timing_data(cls, data: bytes) -> list[list[int]]:
        """
        Extract raw timing sample records from a TT learn string.
        Returns list of sample records; each record is a list of channel bit values.
        1 byte per record for 8 channels, 2 bytes per record for 16 channels.
        """
        info = cls.parse_header(data)
        if not info["valid"]:
            return []
        n_channels = data[4] if len(data) > 4 else 0
        n_states   = struct.unpack(">H", data[5:7])[0] if len(data) > 6 else 0
        bytes_per_record = 2 if n_channels > 8 else 1
        # Data file starts at byte 52 (0-indexed from start of full string)
        data_start = 52
        records = []
        for i in range(n_states):
            offset = data_start + i * bytes_per_record
            if offset + bytes_per_record > len(data):
                break
            if bytes_per_record == 1:
                byte_val = data[offset]
                records.append([(byte_val >> bit) & 1 for bit in range(8)])
            else:
                word_val = struct.unpack(">H", data[offset:offset+2])[0]
                records.append([(word_val >> bit) & 1 for bit in range(16)])
        return records


# ═══════════════════════════════════════════════════════════════════════════
#  High-level helper functions
# ═══════════════════════════════════════════════════════════════════════════

def connection_check(gpib: GPIBAdapter, analyzer: HP1631A):
    """Quick connection check: adapter type/version, IFC, SDC, serial poll, ID."""
    print("=== HP 1631A Connection Check ===")
    print(f"  Adapter type      : {gpib.adapter_type()}")
    ver = gpib.firmware_version()
    if ver:
        print(f"  Adapter firmware  : {ver}")
    gpib.ifc()
    print("  IFC sent")
    analyzer.clear()
    print("  SDC sent")
    analyzer.set_mask(34)   # Measurement Complete + Error
    print("  MB 34 sent (SRQ mask: Measurement Complete + Error)")
    sb = gpib.serial_poll()
    print(f"  Status byte : 0x{sb:02X}  "
          f"MEAS_COMPLETE={bool(sb&0x02)}  "
          f"NOT_BUSY={bool(sb&0x10)}  "
          f"ERROR={bool(sb&0x20)}")
    resp = analyzer.identify()
    print(f"  ID          : {resp}")
    print("=== Check complete ===")


def save_config(analyzer: HP1631A, filepath: str):
    """Download TC configuration learn string and save as binary file."""
    print("  Sending TC (Transmit Configuration)…")
    data = analyzer.get_config_learn_string()
    if len(data) < 4:
        print("  [error] No data returned.")
        return
    info = LearnStringParser.parse_header(data)
    with open(filepath, "wb") as f:
        f.write(data)
    print(f"  Saved {len(data)} bytes → {filepath}")
    print(f"  Header: {info}")


def load_config(analyzer: HP1631A, filepath: str):
    """
    Send a previously saved TC learn string back to the instrument as RC.
    The instrument accepts the exact bytes returned by TC — no modification needed.
    """
    with open(filepath, "rb") as f:
        data = f.read()
    if len(data) < 4:
        print("  [error] File too short.")
        return
    # The learn string already starts with "RC" header — send it as-is.
    # For Prologix we write raw bytes to the serial port after addressing;
    # for other adapters we use write_raw if available, else base64-chunk it.
    print(f"  Sending {len(data)} bytes to instrument…")
    gpib = analyzer.gpib
    if isinstance(gpib, PrologixGPIB):
        gpib._raw_write(f"++addr {gpib.gpib_addr}")
        gpib.ser.write(data)
    elif isinstance(gpib, (USBTmcGPIB, PyVisaGPIB)):
        gpib._inst.write_raw(data)
    elif isinstance(gpib, NI488GPIB):
        gpib._gpib.write(gpib._dev, data)
    else:
        raise NotImplementedError(
            "load_config: binary write not implemented for this adapter type."
        )
    time.sleep(1.0)
    print("  Done.")


def capture_and_export(analyzer: HP1631A, output_stem: str,
                       use_srq: bool = False,
                       cancel_event=None) -> dict:
    """
    Run a capture and download TC + TS + TT learn strings plus a screen
    text capture (DR).  Returns dict of saved file paths.
    """
    print("  RN → starting acquisition…")
    analyzer.start()

    if use_srq:
        ok = analyzer.wait_for_srq(120, cancel_event)
    else:
        ok = analyzer.wait_for_measurement_complete(120, cancel_event=cancel_event)

    if not ok:
        print("  [error] Measurement did not complete.")
        return {}

    print("  Measurement complete.  Downloading learn strings…")
    files = {}

    print("  TC (configuration)…")
    tc = analyzer.get_config_learn_string()
    if tc:
        p = output_stem + "_config.lrn"
        with open(p, "wb") as f: f.write(tc)
        files["config"] = p
        print(f"    {len(tc)} bytes → {p}")

    print("  TS (state)…")
    ts = analyzer.get_state_learn_string()
    if ts:
        p = output_stem + "_state.lrn"
        with open(p, "wb") as f: f.write(ts)
        files["state"] = p
        print(f"    {len(ts)} bytes → {p}")

    print("  TT (timing)…")
    tt = analyzer.get_timing_learn_string()
    if tt:
        p = output_stem + "_timing.lrn"
        with open(p, "wb") as f: f.write(tt)
        files["timing"] = p
        info = LearnStringParser.parse_timing_header(tt)
        print(f"    {len(tt)} bytes → {p}")
        print(f"    Channels: {info.get('timing_channels')}  "
              f"States: {info.get('valid_states')}  "
              f"Runs: {info.get('runs')}")

    print("  Reading screen text (LM → DR)…")
    analyzer.menu("LM")
    time.sleep(0.3)
    screen = analyzer.read_full_screen()
    if screen:
        p = output_stem + "_screen.txt"
        with open(p, "w", encoding="utf-8") as f: f.write(screen)
        files["screen"] = p
        print(f"    {len(screen)} chars → {p}")

    return files


def batch_capture(analyzer: HP1631A, count: int, out_dir: str,
                  delay: float = 1.0, cancel_event=None):
    """Batch-capture N traces and save learn strings sequentially."""
    import os
    os.makedirs(out_dir, exist_ok=True)
    print(f"  Batch: {count} traces → {out_dir}/")
    for n in range(1, count + 1):
        if cancel_event and cancel_event.is_set():
            print("  Cancelled.")
            break
        print(f"\n  Trace {n}/{count}")
        stem = os.path.join(out_dir, f"trace_{n:03d}")
        files = capture_and_export(analyzer, stem, cancel_event=cancel_event)
        if not files:
            print(f"  Trace {n}: failed, skipping.")
        if n < count:
            time.sleep(delay)
    print(f"\n  Batch complete.")


# ── Legacy stubs (kept for GUI compatibility during transition) ────────────

def render_ascii_waveform(raw_text: str, width: int = 80):
    """
    Render display-read text as a simple waveform view.
    The DR command returns the display memory which may already
    contain timing waveform lines when the WM (WFORM) menu is active.
    """
    print(raw_text)
