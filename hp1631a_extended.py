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
"""

import serial
import time
import struct
from typing import Optional

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

class PrologixGPIB:
    """
    Low-level Prologix GPIB-USB driver.

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


# ═══════════════════════════════════════════════════════════════════════════
#  HP 1631A/D instrument driver
# ═══════════════════════════════════════════════════════════════════════════

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

    def __init__(self, gpib: PrologixGPIB):
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

def connection_check(gpib: PrologixGPIB, analyzer: HP1631A):
    """Quick connection check: Prologix version, IFC, SDC, serial poll, ID."""
    print("=== HP 1631A Connection Check ===")
    gpib._raw_write("++ver")
    r = gpib.ser.readline().decode(errors="replace").strip()
    print(f"  Prologix firmware : {r or '(no response)'}")
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
    # The learn string already starts with "RC" header — send it as-is
    print(f"  Sending {len(data)} bytes to instrument…")
    analyzer.gpib._raw_write(f"++addr {analyzer.gpib.gpib_addr}")
    analyzer.gpib.ser.write(data)
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
