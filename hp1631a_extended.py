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
    SB_POWER_ON             = 0x80   # IEEE 488.1 PON condition bit (bit 7)

    # Aliases used by hp1631a_gui.py
    SB_DATA_READY = SB_MEASUREMENT_COMPLETE   # bit 1 — same signal, alternate name
    SB_RQS        = SB_SRQ                    # bit 6 — Request for Service / SRQ indicator

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

    def set_trigger_pattern(self, pattern: str, mode: str = "state",
                            label_row: int = 1, dont_care_key: str = "DC",
                            settle: float = 0.08) -> bool:
        """
        Drive the front-panel Trace/Trigger spec screen to set a bit-pattern
        trigger condition, the same way a user would type it on the keypad.

        ⚠ UNVERIFIED MNEMONIC WARNING
        ------------------------------
        The exact 2-character mnemonic for the front-panel "don't care" (X)
        key on the Trace/Trigger pattern entry screen is NOT confirmed
        against the manual in this codebase — "DC" is a placeholder best
        guess based on the instrument's general 2-character keyboard
        mnemonic convention (Table 10-1 covers menu/cursor/run keys, but
        the trigger-pattern digit/don't-care keys are a separate, less
        commonly documented section of Chapter 10). Before relying on
        this method:
          1. Run it once with a short pattern (e.g. "0X") on a label you
             don't mind disturbing.
          2. Read the Trace screen back with display_read() and visually
             compare against the front panel.
          3. If the don't-care character didn't land correctly, pass the
             correct mnemonic via `dont_care_key=` (check the front panel
             keycap silkscreen, or Chapter 10's keyboard mnemonic table,
             for the actual label — likely something like "DC", "X", or a
             dedicated key labeled "DON'T CARE").
        The '0' and '1' digit keys are sent as the literal characters '0'
        and '1', which is correct per the standard HP-IB keyboard mnemonic
        convention (single printable characters map directly to keypad
        digits) and does not carry the same uncertainty.

        The 1631A's trigger word entry has no single GPIB command that
        accepts a whole pattern string — each bit position is a field on
        the Trace screen that the cursor must be moved onto, then a digit
        (0/1) or the don't-care key is typed to set that bit. This method
        automates that sequence: navigate to TRACE, select the trigger
        spec line, then walk the cursor across `len(pattern)` bit
        positions typing each character.

        Parameters
        ----------
        pattern       : string of '0', '1', 'X'/'x' (don't care) characters,
                        most-significant bit (highest channel number) first,
                        matching the on-screen left-to-right field order,
                        e.g. "0XXXXXXXXXXXXXXX" to trigger on channel 0
                        (BDAL0) low and everything else don't-care.
                        Length should match the number of channels
                        assigned to the label being triggered on
                        (commonly 8 or 16).
        mode          : "state" or "timing" — informational only; caller
                        is responsible for having already navigated to the
                        correct Format/Trace mode before calling this.
        label_row     : which trigger pattern row to edit, if multiple
                        labels are defined (1 = first/topmost row).
        dont_care_key : the mnemonic sent for 'X'/don't-care characters.
                        See warning above — verify and override if needed.
        settle        : delay in seconds between keystrokes; increase if
                        the instrument is dropping characters.

        Returns True if the pattern was sent without error, False if an
        invalid character was found in `pattern` (no keys are sent in
        that case).

        Notes
        -----
        This does not verify the resulting on-screen pattern matches what
        was requested — the 1631A has no query-back command for trigger
        state. After calling this, use display_read() on the Trace screen
        to visually confirm the pattern landed correctly, especially the
        first time you use this method or after changing dont_care_key.
        """
        pattern = pattern.strip()
        valid_chars = set("01XxNn")  # N = don't-care alias some manuals use
        if not pattern or any(c not in valid_chars for c in pattern):
            return False

        # Navigate to the Trace menu. TM = Trace menu mnemonic
        # (MENU_MNEMONICS) — this part IS confirmed against Table 10-1.
        self.menu("TRACE")
        time.sleep(0.2)

        # Move cursor to the requested label row, then to the start
        # (leftmost bit) of the pattern field. CD = cursor down,
        # CL = cursor left (repeated generously to guarantee we're at
        # the leftmost field regardless of prior cursor position).
        # This cursor-walk approach is standard for the 1631A's
        # softkey/field-based menus elsewhere in this driver (see menu()
        # and key()), but has not been specifically verified on the
        # Trace/Trigger screen layout.
        if label_row > 1:
            self.key("CD", label_row - 1)
        self.key("CL", 32)   # walk fully left; harmless past the edge

        for ch in pattern:
            c = ch.upper()
            if c in ("X", "N"):
                self.gpib.write(dont_care_key)
            else:
                self.gpib.write(c)      # literal '0' or '1' digit key
            time.sleep(settle)
            self.key("CR")  # advance to next bit position

        return True

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

    def verify_instrument_identity(self) -> dict:
        """
        Download the TC (configuration) learn string and decode the instrument
        identity block confirmed by ROM55 $8AEF–$8B12.

        Returns a dict with:
          "series"        : "HP 1631" or "HP 1630"
          "variant"       : "A (standard)" or "D (data)"
          "is_1631"       : bool
          "is_data_variant": bool
          "identity_valid": bool — True if class marker, family ID, series,
                            and variant all match expected values
          "description"   : human-readable summary string
          "raw"           : bytes — the full TC learn string

        Use this at session start to confirm you are talking to the expected
        instrument model before sending configuration learn strings. The 1631D
        (data variant) has extended state analysis capabilities not present on
        the 1631A — sending a 1631D state learn string to a 1631A will be
        silently rejected with firmware error type 3 at $8A7B.
        """
        tc_raw = self.get_config_learn_string()
        info = LearnStringParser.parse_config_header(tc_raw)
        info["raw"] = tc_raw
        return info

    def set_instrument_gpib_address(self, new_address: int,
                                    verify: bool = True) -> int:
        """
        Program the instrument's own GPIB address via the SM (System
        specification) learn string.

        IMPORTANT — firmware collision avoidance (ROM55 $8197–$81A9):
        The instrument firmware automatically increments the requested address
        if it conflicts with either:
          (a) a reserved address stored at ROM55 $29DC, or
          (b) the currently active listener address at RAM $DFEC.
        This means the address you request may not be the address the instrument
        actually uses. The firmware error message 'duplicate HP-IB address' /
        'conflicting HPIB addresses' appears on the front panel display but is
        NOT propagated to the GPIB status byte, so the Python side cannot detect
        it without reading the display or re-querying the address.

        This method programs the address through the standard SM / key-entry
        path (via front-panel cursor navigation) and, if verify=True, reads
        the TC learn string back and checks the address field at $DE0C to
        confirm what the firmware actually set, then updates self.gpib.gpib_addr
        to match the confirmed live address.

        Args:
          new_address : 0–30 (31 = untalk, not a valid instrument address)
          verify      : if True (default), read TC back and confirm live address

        Returns:
          The live GPIB address the instrument is now responding on. This may
          differ from new_address if the firmware's collision-avoidance fired.

        Raises:
          ValueError if new_address is out of range.
        """
        if not (0 <= new_address <= 30):
            raise ValueError(
                f"set_instrument_gpib_address: address {new_address} is out of "
                "range — GPIB primary addresses are 0–30 (31 is reserved)."
            )

        # Navigate to System Specification screen and set the logic analyzer
        # address field. The SM mnemonic enters the System menu; the LA address
        # is the first numeric field on that screen.
        self.gpib.write("SM")
        time.sleep(0.3)
        self.gpib.write("CH")
        time.sleep(0.2)
        # Cursor to address field and enter value
        self.gpib.write(f"SL {new_address}")
        time.sleep(0.5)

        if not verify:
            return new_address

        # Read TC to confirm the live address. The firmware stores the active
        # HP-IB address at $DE0C (config block); parse_config_header now
        # decodes the identity block — the address field location in the full
        # 5145-byte TC payload is not yet fully mapped, so we use the SM/TC
        # roundtrip as the authoritative confirmation and log a note.
        #
        # NOTE: If the full TC config field table (54 entries at ROM55 $8B95)
        # is ever mapped to byte offsets, the address check should read
        # data[payload_offset + addr_field_offset] directly instead of this
        # heuristic. Filed as a future enhancement — see HP1631A_ROM_Analysis.md.
        time.sleep(0.5)
        tc_raw = self.get_config_learn_string()
        info = LearnStringParser.parse_config_header(tc_raw)

        if not info.get("identity_valid"):
            import warnings
            warnings.warn(
                "set_instrument_gpib_address: TC identity check failed after "
                f"setting address {new_address} — cannot confirm live address. "
                "The firmware's collision-avoidance at $8197 may have changed "
                "the address without notification.",
                RuntimeWarning,
                stacklevel=2,
            )
            return new_address

        # Update the adapter's local pointer to the confirmed address.
        # Until the TC config field table is fully mapped we trust the
        # requested value unless we can prove otherwise.
        self.gpib.gpib_addr = new_address
        return new_address

    # ── Acquisition verification ────────────────────────────────────────────

    def verify_acquisition(self, fetch_config: bool = False) -> dict:
        """
        Cross-check TS (state) and TT (timing) learn strings to determine
        whether an acquisition actually produced data, and if not, which
        acquisition mode (if either) has channels assigned.

        This mirrors the diagnostic sequence in hp1631a_probe.py Step 10:
        a learn string can come back perfectly well-formed (correct header,
        correct CRC) and still carry zero samples if the corresponding pod
        isn't assigned in the Format menu, or if the instrument's active
        trace mode doesn't match the learn string you downloaded.

        If fetch_config=True, also downloads the TC (configuration) learn
        string and decodes the instrument identity block (series, variant)
        confirmed by ROM55 $8AEF. A 1631D (data variant) vs 1631A mismatch
        is flagged in the verdict — sending D-variant state learn strings to
        an A-variant instrument is silently rejected by the firmware.

        Returns a dict:
          {
            "state":  {"channels": int, "states": int, "valid": bool, "raw": bytes},
            "timing": {"channels": int, "states": int, "valid": bool, "raw": bytes},
            "config": {
                "valid": bool, "raw": bytes,
                "series": str, "variant": str,
                "is_1631": bool, "is_data_variant": bool,
                "identity_valid": bool,
            } or None if fetch_config=False,
            "verdict": str,       # human-readable summary
            "ok": bool,           # True if at least one mode has both
                                  # channels>0 and states>0
          }

        Does not change instrument state (no RN/ST/RST) — safe to call at
        any time, including right after a capture, to explain an empty
        result without re-arming or disturbing the current acquisition.
        """
        result = {"state": {}, "timing": {}, "config": None}

        ts_raw = self.get_state_learn_string()
        ts_info = LearnStringParser.parse_state_header(ts_raw)
        result["state"] = {
            "channels": ts_info.get("state_channels", 0),
            "states":   ts_info.get("valid_states", 0),
            "valid":    ts_info.get("valid", False),
            "header":   ts_info.get("header"),
            "raw":      ts_raw,
        }

        tt_raw = self.get_timing_learn_string()
        tt_info = LearnStringParser.parse_timing_header(tt_raw)
        result["timing"] = {
            "channels": tt_info.get("timing_channels", 0),
            "states":   tt_info.get("valid_states", 0),
            "valid":    tt_info.get("valid", False),
            "header":   tt_info.get("header"),
            "raw":      tt_raw,
        }

        config_warnings = []
        if fetch_config:
            tc_raw = self.get_config_learn_string()
            tc_info = LearnStringParser.parse_config_header(tc_raw)
            result["config"] = {
                "valid":            tc_info.get("valid", False),
                "raw":              tc_raw,
                "series":           tc_info.get("series", "unknown"),
                "variant":          tc_info.get("variant", "unknown"),
                "is_1631":          tc_info.get("is_1631", False),
                "is_data_variant":  tc_info.get("is_data_variant", False),
                "identity_valid":   tc_info.get("identity_valid", False),
                "description":      tc_info.get("description", ""),
            }
            if tc_info.get("identity_valid"):
                if not tc_info.get("is_1631"):
                    config_warnings.append(
                        f"Instrument reports as {tc_info.get('series')} — "
                        "this toolkit is validated against the HP 1631A/D."
                    )
                if tc_info.get("is_data_variant"):
                    config_warnings.append(
                        "Instrument is the HP 1631D (data variant). "
                        "D-variant state learn strings sent to an A-variant "
                        "instrument will be rejected by firmware at $8A7B."
                    )
            elif tc_info.get("valid"):
                config_warnings.append(
                    "TC identity block failed ROM-confirmed magic check "
                    "(class marker / family ID mismatch) — instrument model "
                    "cannot be confirmed. See parse_config_header() details."
                )

        st = result["state"]
        tm = result["timing"]
        st_ok = st["channels"] > 0 and st["states"] > 0
        tm_ok = tm["channels"] > 0 and tm["states"] > 0

        if st_ok and tm_ok:
            verdict = (f"Both modes have data: State {st['states']} samples "
                      f"({st['channels']} ch), Timing {tm['states']} samples "
                      f"({tm['channels']} ch).")
        elif st_ok and not tm_ok:
            verdict = (f"State mode has data ({st['states']} samples, "
                      f"{st['channels']} channels) but Timing does not "
                      f"(channels={tm['channels']}, states={tm['states']}). "
                      "Timing pod is likely unassigned in Format menu, or "
                      "State is the active trace mode.")
        elif tm_ok and not st_ok:
            verdict = (f"Timing mode has data ({tm['states']} samples, "
                      f"{tm['channels']} channels) but State does not "
                      f"(channels={st['channels']}, states={st['states']}). "
                      "State pod is likely unassigned in Format menu, or "
                      "Timing is the active trace mode.")
        elif st["channels"] == 0 and tm["channels"] == 0:
            verdict = ("Neither State nor Timing pods report any assigned "
                      "channels. Check System → Format on the front panel "
                      "and assign at least one pod in either Format screen.")
        else:
            verdict = (f"Channels are assigned (State={st['channels']}, "
                      f"Timing={tm['channels']}) but no samples were "
                      f"captured (State={st['states']}, Timing={tm['states']}). "
                      "Acquisition likely hasn't completed, or the trigger "
                      "condition was never met. Re-arm with RN and confirm "
                      "the trigger fires before downloading.")

        if config_warnings:
            verdict += "  Config warnings: " + "; ".join(config_warnings)

        result["verdict"] = verdict
        result["ok"] = st_ok or tm_ok
        return result

    def detect_trace_mode(self) -> dict:
        """
        Best-effort detection of which trace mode (State or Timing) is
        currently active on the front panel, by reading the List screen
        header text via DR. Does not change instrument state beyond
        navigating to the List menu (LM), which is non-destructive and
        does not arm or clear any acquisition.

        The 1631A does not expose "active trace mode" as a queryable
        register over GPIB — this only exists as display text — so this
        is a heuristic based on matching known header strings ("State
        Listing", "Timing Listing", "Waveform Listing") in the first
        screen row. If the heuristic can't determine the mode, 'mode'
        will be None and 'raw_header' will contain whatever text was
        found, so the caller can fall back to other checks (e.g.
        verify_acquisition()).

        Returns:
          {"mode": "state" | "timing" | "waveform" | None,
           "raw_header": str}
        """
        self.gpib.write("LM")
        time.sleep(0.4)
        try:
            header_text = self.display_read(1, 1, 64)
        except Exception:
            header_text = ""

        h = header_text.upper()
        if "STATE" in h:
            mode = "state"
        elif "TIMING" in h:
            mode = "timing"
        elif "WAVEFORM" in h or "WFORM" in h:
            mode = "waveform"
        else:
            mode = None

        return {"mode": mode, "raw_header": header_text.strip()}

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
    def parse_state_header(cls, data: bytes) -> dict:
        """
        Parse the fixed header fields of a TS (state) learn string.

        Reads the standard parse_header() fields plus TS-specific fields:

          state_channels   : int  -- channel count from header byte 4
          valid_states     : int  -- valid state count from header bytes 5-6
                                     (big-endian uint16, same layout as TT)
          tracepoint_index : int  -- tracepoint from header bytes 7-8
          n_states_file    : int  -- cross-check: state count derived from
                                     total file size using the reverse-engineered
                                     TS binary layout (_DATA_START=18, 5 bytes/record)
          crc_ok           : bool -- CRC verification result
          header_type      : str  -- "RS" or error description
          n_channels       : int  -- alias for state_channels (backward compat)
          n_states         : int  -- alias for valid_states (backward compat)

        Layout (reverse-engineered, HP 1631A):
          Bytes  0-1   "RS" header
          Bytes  2-3   byte_count (big-endian uint16)
          Bytes  4-17  14-byte fixed header: byte 4 = channel count,
                       bytes 5-6 = valid state count (MSB first),
                       bytes 7-8 = tracepoint index
          Bytes 18..N  sample data, 5 bytes per sample (big-endian uint40)
          Byte  N+1    revision code
          Bytes N+2-3  CRC (16-bit sum of bytes 4..N, big-endian)
        """
        info = cls.parse_header(data)

        # Populate zero-value defaults so callers can always key-access safely
        info.setdefault("state_channels",   0)
        info.setdefault("valid_states",     0)
        info.setdefault("tracepoint_index", 0)
        info.setdefault("n_states_file",    0)
        info.setdefault("crc_ok",           False)
        info.setdefault("header_type",      info.get("error", "too short"))
        info.setdefault("n_channels",       0)
        info.setdefault("n_states",         0)

        if not info["valid"] or len(data) < 9:
            return info

        info["state_channels"]   = data[4]
        info["valid_states"]     = struct.unpack(">H", data[5:7])[0]
        info["tracepoint_index"] = struct.unpack(">H", data[7:9])[0]

        # Cross-check: derive state count from file size using the
        # reverse-engineered TS binary layout
        _DATA_START = 18
        _BYTES_PER  = 5
        total_use   = min(4 + info["byte_count"], len(data))
        data_len    = max(0, (total_use - 3) - _DATA_START)
        info["n_states_file"] = data_len // _BYTES_PER

        # CRC verification
        info["crc_ok"] = cls.verify_crc(data)

        # Backward-compat aliases
        info["header_type"] = data[0:2].decode(errors="replace")
        info["n_channels"]  = info["state_channels"]
        info["n_states"]    = info["valid_states"]

        return info

    @classmethod
    def parse_config_header(cls, data: bytes) -> dict:
        """
        Parse identifying fields from a TC (configuration) learn string.

        The TC response is wrapped in the standard RC framing (2-byte 'RC'
        header + 2-byte length + data + revision + CRC), with the 256-byte
        instrument configuration payload starting at byte 4.

        The first 20 bytes of that payload are a fixed identity block,
        confirmed by ROM55 $8AEF–$8B12 (the TX-side builder):

          Payload[0-1]   = 0x80 0x00   class marker
          Payload[2-5]   = 'L','1','6','3'   family identifier
          Payload[6]     = 0x30 ('0') = HP 1630 series
                         | 0x31 ('1') = HP 1631 series   (RAM $2765)
          Payload[7]     = 0x41 ('A') = standard variant
                         | 0x44 ('D') = data variant      (bit 1 of $2764)
          Payload[8-9]   = 0x00 0x00  (reserved)
          Payload[10-11] = 0x00 0x02  version/count = 2
          Payload[12-13] = 0x10 0x00
          Payload[14-17] = 0x00 × 4
          Payload[18-19] = 0x00 0x08
          Payload[20..255] packed configuration fields (54-entry descriptor
                           table at ROM55 $8B95; disc model strings at $8BEB)

        Reference: HP 1631A ROM analysis, Oct 1985 firmware (01630-80054–61).
        """
        info = cls.parse_header(data)
        if not info["valid"]:
            return info

        # Configuration payload begins at byte 4 (after RC header + 2-byte length)
        payload_offset = 4
        if len(data) < payload_offset + 20:
            info["description"] = (
                "TC configuration learn string — payload too short to decode "
                "instrument identity block (need ≥24 bytes total)."
            )
            return info

        p = data[payload_offset:]

        # ── Magic / family validation ────────────────────────────────────────
        class_marker = p[0:2]
        family_id    = p[2:6]

        info["class_marker_ok"]  = (class_marker == bytes([0x80, 0x00]))
        info["family_id_ok"]     = (family_id == b"L163")
        info["class_marker_hex"] = class_marker.hex(" ").upper()
        info["family_id_str"]    = family_id.decode("ascii", errors="replace")

        # ── Series and variant ───────────────────────────────────────────────
        series_byte  = p[6]
        variant_byte = p[7]

        _SERIES  = {0x30: "HP 1630", 0x31: "HP 1631"}
        _VARIANT = {0x41: "A (standard)", 0x44: "D (data)"}

        info["series"]       = _SERIES.get(series_byte,
                                            f"unknown (0x{series_byte:02X})")
        info["variant"]      = _VARIANT.get(variant_byte,
                                             f"unknown (0x{variant_byte:02X})")
        info["series_ok"]    = series_byte in _SERIES
        info["variant_ok"]   = variant_byte in _VARIANT
        info["is_1631"]      = series_byte == 0x31
        info["is_data_variant"] = variant_byte == 0x44

        # ── Version fields ───────────────────────────────────────────────────
        info["version_word_10_11"] = (p[10] << 8) | p[11]   # expected 0x0002
        info["word_12_13"]         = (p[12] << 8) | p[13]   # expected 0x1000
        info["word_18_19"]         = (p[18] << 8) | p[19]   # expected 0x0008

        # ── Sanity summary ───────────────────────────────────────────────────
        all_ok = (
            info["class_marker_ok"] and info["family_id_ok"]
            and info["series_ok"] and info["variant_ok"]
        )
        info["identity_valid"] = all_ok
        info["description"] = (
            f"TC learn string — {info['series']} variant {info['variant']}"
            f"{'' if all_ok else ' [IDENTITY CHECK FAILED — check parse_config_header() details]'}"
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

    @classmethod
    def extract_state_data(cls, data: bytes) -> list[list[int]]:
        """
        Extract raw state sample records from a TS learn string.

        Returns a list of 40-element lists; each inner list contains the 40
        channel bit values for one state sample (bit 0 = index 0, LSB of
        the last byte of the 5-byte sample record).

        Sample layout (reverse-engineered, HP 1631A TS binary format):
          Bytes 18..N  -- sample data, 5 bytes per sample, big-endian uint40.
          Bit 0 of the 40-bit word = channel 0 (lowest-numbered channel).

        Returns an empty list if the learn string is invalid or has no samples.
        Used by hp1631a_gui.py for the waveform viewer and CSV state export.
        """
        _DATA_START = 18
        _BYTES_PER  = 5
        _N_CH       = 40

        info = cls.parse_state_header(data)
        if not info["valid"]:
            return []
        # Use header-field count first; fall back to file-size-derived count
        n_states = info.get("valid_states", 0) or info.get("n_states_file", 0)
        if n_states == 0:
            return []

        records = []
        for i in range(n_states):
            off = _DATA_START + i * _BYTES_PER
            if off + _BYTES_PER > len(data):
                break
            word = int.from_bytes(data[off: off + _BYTES_PER], "big")
            records.append([(word >> bit) & 1 for bit in range(_N_CH)])
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

    Before writing, validates:
      1. The data starts with 'RC' (correct receive header for config).
      2. The payload identity block passes the ROM-confirmed magic check:
           bytes [4:6] == 0x80 0x00, [6:10] == b'L163'
         The firmware at $8A7B checks these and sets error code 3 for any
         mismatch — without this check, a bad file write is silently rejected.
      3. The total length matches the byte-count field in the header.

    Raises ValueError with a descriptive message if any check fails so the
    caller gets a Python-side error rather than a silent firmware rejection.
    """
    with open(filepath, "rb") as f:
        data = f.read()

    if len(data) < 24:
        raise ValueError(
            f"load_config: {filepath!r} is only {len(data)} bytes — "
            "too short to contain a valid TC learn string (minimum ~24 bytes "
            "for header + identity block)."
        )

    # ── Check 1: RC header ───────────────────────────────────────────────────
    if data[0:2] != b"RC":
        raise ValueError(
            f"load_config: {filepath!r} does not start with 'RC' — "
            f"got {data[0:2]!r}. This may be a state (RS) or timing (RT) "
            "learn string, not a configuration learn string."
        )

    # ── Check 2: Length consistency ──────────────────────────────────────────
    byte_count = struct.unpack(">H", data[2:4])[0]
    expected_total = 4 + byte_count
    if len(data) < expected_total:
        raise ValueError(
            f"load_config: {filepath!r} declares {byte_count} bytes of "
            f"payload (total {expected_total}) but file is only {len(data)} "
            "bytes — file is truncated."
        )

    # ── Check 3: ROM-confirmed identity block (payload starts at byte 4) ─────
    # Firmware builder at $8AEF writes: 0x80 0x00 'L' '1' '6' '3' series variant
    # Firmware receiver at $8A7B validates these and errors out on mismatch.
    p = data[4:]
    if p[0:2] != bytes([0x80, 0x00]):
        raise ValueError(
            f"load_config: identity block class marker is {p[0:2].hex(' ').upper()!r}, "
            "expected '80 00'. This does not appear to be a valid HP 163x "
            "configuration learn string."
        )
    if p[2:6] != b"L163":
        raise ValueError(
            f"load_config: identity block family ID is {p[2:6]!r}, "
            "expected b'L163'. This is not an HP 1630/1631-series learn string."
        )
    series_byte  = p[6]
    variant_byte = p[7]
    if series_byte not in (0x30, 0x31):
        raise ValueError(
            f"load_config: series byte is 0x{series_byte:02X} — "
            "expected 0x30 ('0' = HP 1630) or 0x31 ('1' = HP 1631)."
        )
    if variant_byte not in (0x41, 0x44):
        raise ValueError(
            f"load_config: variant byte is 0x{variant_byte:02X} — "
            "expected 0x41 ('A' = standard) or 0x44 ('D' = data)."
        )
    series  = "HP 1631" if series_byte == 0x31 else "HP 1630"
    variant = "D (data)" if variant_byte == 0x44 else "A (standard)"

    # ── CRC check ────────────────────────────────────────────────────────────
    crc_ok = LearnStringParser.verify_crc(data)
    if not crc_ok:
        raise ValueError(
            f"load_config: {filepath!r} CRC mismatch — the file may be "
            "corrupt or was modified after saving. Sending a bad learn string "
            "to the instrument may leave it in an indeterminate state."
        )

    print(f"  Config learn string: {series} variant {variant}, "
          f"{len(data)} bytes, CRC OK.")
    print(f"  Sending to instrument at GPIB address {analyzer.gpib.gpib_addr}…")

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
            "load_config: binary write not implemented for this adapter type. "
            f"Adapter is {type(gpib).__name__}."
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
