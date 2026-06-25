"""
hp1631a_lrn_to_sr.py  --  Convert HP 1631A binary TT learn string to sigrok .sr
=================================================================================
Converts a binary timing learn string (.lrn file saved from hp1631a_gui.py or
hp1631a_extended.py) directly to a sigrok v2 .sr session file for PulseView.

This is the binary path: it reads the raw TT acquisition data rather than
the ASCII listing text expected by hp1631a_to_sr.py.

Binary .lrn files are produced by:
  • The CAPTURE tab in hp1631a_gui.py   → <stem>_timing.lrn
  • capture_and_export()               → <stem>_timing.lrn
  • Manually via:  gpib.query_binary("TT")

Sigrok v2 .sr format reference:
  https://sigrok.org/wiki/File_format:Sigrok/v2

Requires: Python 3.8+, no external dependencies (stdlib only)

Usage
-----
  # Convert with auto-detected sample rate (from TT header):
  python hp1631a_lrn_to_sr.py trace_timing.lrn

  # Specify output path:
  python hp1631a_lrn_to_sr.py trace_timing.lrn -o trace.sr

  # Override the sample rate (Hz):
  python hp1631a_lrn_to_sr.py trace_timing.lrn --samplerate 10000000

  # Print header info without writing:
  python hp1631a_lrn_to_sr.py trace_timing.lrn --info

  # Name the channels (comma-separated, 8 or 16 names):
  python hp1631a_lrn_to_sr.py trace_timing.lrn --channels CLK,MOSI,MISO,CS,D4,D5,D6,D7

TT Learn String Binary Format  (HP 1631A/D Operating & Programming Manual, Ch.10)
----------------------------------------------------------------------------------
  Bytes  0- 1   ASCII header "RT"
  Bytes  2- 3   Byte count  (big-endian uint16; includes everything after this word
                              up to and including the 2-byte CRC)
  Byte   4      Number of timing channels  (8 or 16)
  Bytes  5- 6   Number of valid timing states  (big-endian uint16)
  Bytes  7- 8   Tracepoint index  (big-endian uint16)
  Byte   9      Glitch detect mode  (0=off, non-zero=on)
  Byte  10      Sample period index  (0–18; maps to clock rate; see TIMING_CLOCKS)
  Byte  11      Sample period units  (0=ns, 1=µs, 2=ms; redundant, derived from index)
  Bytes 12–47   Reserved / configuration fields
  Bytes 48–49   Number of trigger hits  (big-endian uint16)
  Bytes 50–51   Number of acquisition runs  (big-endian uint16)
  Bytes 52–N    Sample data
                  ≤ 8 channels:  1 byte per sample, bit 0 = ch1 … bit 7 = ch8
                  >8 channels:   2 bytes per sample (big-endian), bit 0 = ch1 … bit 15 = ch16
  Byte  N+1     Revision code
  Bytes N+2–N+3 CRC  (16-bit sum of all data bytes from byte 4 to revision byte, big-endian)

Sample period index → clock rate table  (TIMING_CLOCKS below):
  Index  0: 100 ns → 10.000 MHz
  Index  1: 200 ns →  5.000 MHz
  Index  2: 500 ns →  2.000 MHz
  Index  3:   1 µs →  1.000 MHz
  Index  4:   2 µs →  500 kHz
  Index  5:   5 µs →  200 kHz
  Index  6:  10 µs →  100 kHz
  Index  7:  20 µs →   50 kHz
  Index  8:  50 µs →   20 kHz
  Index  9: 100 µs →   10 kHz
  Index 10: 200 µs →    5 kHz
  Index 11: 500 µs →    2 kHz
  Index 12:   1 ms →    1 kHz
  Index 13:   2 ms →  500 Hz
  Index 14:   5 ms →  200 Hz
  Index 15:  10 ms →  100 Hz
  Index 16:  20 ms →   50 Hz
  Index 17:  50 ms →   20 Hz
  Index 18: 100 ms →   10 Hz
"""

import argparse
import os
import struct
import sys
import zipfile


# ---------------------------------------------------------------------------
# HP 1631A timing clock table
# index → (period_ns, sample_rate_hz, human_readable_string)
# ---------------------------------------------------------------------------
TIMING_CLOCKS = [
    (       100,  10_000_000, "100 ns  (10 MHz)"),
    (       200,   5_000_000, "200 ns  (5 MHz)"),
    (       500,   2_000_000, "500 ns  (2 MHz)"),
    (     1_000,   1_000_000, "1 µs    (1 MHz)"),
    (     2_000,     500_000, "2 µs    (500 kHz)"),
    (     5_000,     200_000, "5 µs    (200 kHz)"),
    (    10_000,     100_000, "10 µs   (100 kHz)"),
    (    20_000,      50_000, "20 µs   (50 kHz)"),
    (    50_000,      20_000, "50 µs   (20 kHz)"),
    (   100_000,      10_000, "100 µs  (10 kHz)"),
    (   200_000,       5_000, "200 µs  (5 kHz)"),
    (   500_000,       2_000, "500 µs  (2 kHz)"),
    ( 1_000_000,       1_000, "1 ms    (1 kHz)"),
    ( 2_000_000,         500, "2 ms    (500 Hz)"),
    ( 5_000_000,         200, "5 ms    (200 Hz)"),
    (10_000_000,         100, "10 ms   (100 Hz)"),
    (20_000_000,          50, "20 ms   (50 Hz)"),
    (50_000_000,          20, "50 ms   (20 Hz)"),
    (100_000_000,         10, "100 ms  (10 Hz)"),
]


# ---------------------------------------------------------------------------
# Binary learn string parser
# ---------------------------------------------------------------------------

class TTLearnString:
    """
    Parses a TT (timing) learn string binary blob.

    Attributes
    ----------
    raw             : original bytes
    valid           : True if the header, size, and CRC all check out
    error           : error message string if not valid
    n_channels      : number of timing channels (8 or 16)
    n_states        : number of valid sample states captured
    tracepoint      : tracepoint index within the acquisition buffer
    glitch_mode     : True if glitch detect was active
    clock_index     : sample period index into TIMING_CLOCKS (0–18)
    clock_index_raw : raw byte value (may be out of range)
    period_ns       : sample period in nanoseconds
    sample_rate_hz  : sample rate in Hz
    clock_str       : human-readable clock description
    n_hits          : number of trigger hits
    n_runs          : number of runs
    revision        : revision code byte
    crc_ok          : True if CRC verified
    samples         : list of int, one per sample state; bit N = channel N+1
    """

    HEADER = b"RT"

    def __init__(self, data: bytes):
        self.raw             = data
        self.valid           = False
        self.error           = None
        # Initialise all attributes to safe defaults so that early returns
        # inside _parse() never leave the object in a partially-constructed
        # state (avoids AttributeError in print_info / callers).
        self.n_channels      = 0
        self.n_states        = 0
        self.tracepoint      = 0
        self.glitch_mode     = False
        self.clock_index     = -1
        self.clock_index_raw = 0
        self.period_ns       = 0
        self.sample_rate_hz  = 0
        self.clock_str       = "(unknown)"
        self.n_hits          = 0
        self.n_runs          = 0
        self.revision        = 0
        self.crc_ok          = False
        self.samples         = []
        self._parse(data)

    def _parse(self, data: bytes):
        # Minimum viable size: 2(hdr)+2(count)+52(fixed fields)+1(rev)+2(crc) = 59
        if len(data) < 59:
            self.error = f"File too short ({len(data)} bytes); minimum is 59."
            return

        # ── Header ──────────────────────────────────────────────────────────
        if data[0:2] != self.HEADER:
            hdr = data[0:2]
            try:
                hdr_str = hdr.decode("ascii")
            except Exception:
                hdr_str = repr(hdr)
            # Provide helpful hint for wrong learn string type
            hints = {
                "RC": "This looks like a TC (configuration) learn string, not a TT.",
                "RS": "This looks like a TS (state) learn string, not a TT.",
                "RA": "This looks like a TA (analog) learn string, not a TT.",
            }
            hint = hints.get(hdr_str, "")
            self.error = (
                f"Expected 'RT' header, got {hdr_str!r}.  "
                + (hint if hint else "Is this actually a TT timing learn string?")
            )
            return

        # ── Byte count and size ─────────────────────────────────────────────
        byte_count = struct.unpack(">H", data[2:4])[0]
        total_expected = 4 + byte_count
        if len(data) < total_expected:
            self.error = (
                f"Truncated: header says {byte_count} bytes of payload "
                f"(total {total_expected}), but file is only {len(data)} bytes.  "
                "The download may have been cut short."
            )
            return

        # ── Fixed header fields ─────────────────────────────────────────────
        self.n_channels   = data[4]
        self.n_states     = struct.unpack(">H", data[5:7])[0]
        self.tracepoint   = struct.unpack(">H", data[7:9])[0]
        self.glitch_mode  = bool(data[9])
        clock_raw         = data[10]
        # units byte (data[11]) is redundant — clock_raw is the combined index
        self.clock_index_raw = clock_raw
        if 0 <= clock_raw < len(TIMING_CLOCKS):
            self.clock_index   = clock_raw
            period_ns, sr_hz, clock_str = TIMING_CLOCKS[clock_raw]
            self.period_ns     = period_ns
            self.sample_rate_hz = sr_hz
            self.clock_str     = clock_str
        else:
            # Unknown index — fall back to units/multiplier decoding
            units_byte = data[11]
            units_ns   = {0: 1, 1: 1_000, 2: 1_000_000}.get(units_byte, 1)
            # The multiplier byte likely encodes 1/2/5 in successive steps
            # Try to decode a plausible period
            mult_seq = [1, 2, 5, 10, 20, 50, 100, 200, 500]
            mult_val = mult_seq[clock_raw % len(mult_seq)] if clock_raw < 27 else clock_raw
            self.clock_index   = -1
            self.period_ns     = mult_val * units_ns
            self.sample_rate_hz = int(1e9 / self.period_ns) if self.period_ns > 0 else 1
            self.clock_str     = (
                f"{self.period_ns} ns  ({self.sample_rate_hz} Hz)  "
                f"[clock index {clock_raw} not in table — estimated]"
            )

        self.n_hits  = struct.unpack(">H", data[48:50])[0]
        self.n_runs  = struct.unpack(">H", data[50:52])[0]

        # ── CRC ─────────────────────────────────────────────────────────────
        self.revision = data[total_expected - 3]
        stored_crc    = struct.unpack(">H", data[total_expected - 2: total_expected])[0]
        computed_crc  = sum(data[4: total_expected - 2]) & 0xFFFF
        self.crc_ok   = (computed_crc == stored_crc)

        # ── Sample data ─────────────────────────────────────────────────────
        data_start       = 52
        bytes_per_sample = 2 if self.n_channels > 8 else 1
        self.samples     = []
        for i in range(self.n_states):
            offset = data_start + i * bytes_per_sample
            if offset + bytes_per_sample > total_expected - 3:
                # Hit revision/CRC area — stop
                break
            if bytes_per_sample == 1:
                self.samples.append(data[offset])
            else:
                # Big-endian 16-bit word
                self.samples.append(struct.unpack(">H", data[offset:offset+2])[0])

        if len(self.samples) < self.n_states:
            # Warn but proceed with what we got
            self.error = (
                f"Warning: expected {self.n_states} samples, "
                f"decoded {len(self.samples)} before hitting end of data.  "
                "File may be truncated."
            )
            # Don't set valid=False — partial data is still useful

        self.valid = True

    def channel_samples(self, ch_index: int) -> list:
        """
        Return a list of 0/1 values for channel ch_index (0-based).
        Channel 0 = bit 0 of each sample word.
        """
        return [(s >> ch_index) & 1 for s in self.samples]

    def print_info(self):
        """Print a human-readable summary of the learn string header."""
        print("HP 1631A Timing Learn String (TT / RT)")
        print("─" * 50)
        if self.error and not self.valid:
            print(f"  ERROR: {self.error}")
            # If we have no samples at all, nothing more to print
            if not self.samples:
                return
        elif self.error:
            print(f"  WARNING: {self.error}")
        print(f"  Header          : RT  (timing)")
        print(f"  Channels        : {self.n_channels}")
        print(f"  Valid states    : {self.n_states}")
        print(f"  Decoded samples : {len(self.samples)}")
        print(f"  Tracepoint      : {self.tracepoint}")
        print(f"  Glitch detect   : {'ON' if self.glitch_mode else 'off'}")
        print(f"  Sample period   : {self.clock_str}")
        print(f"  Sample rate     : {self.sample_rate_hz:,} Hz")
        print(f"  Trigger hits    : {self.n_hits}")
        print(f"  Runs            : {self.n_runs}")
        print(f"  Revision code   : 0x{self.revision:02X}")
        print(f"  CRC             : {'OK' if self.crc_ok else 'MISMATCH (data may be corrupt)'}")
        print(f"  Total file size : {len(self.raw)} bytes")
        # Show per-channel toggle counts
        print()
        print("  Channel activity:")
        for ch in range(self.n_channels):
            bits = self.channel_samples(ch)
            highs   = sum(bits)
            lows    = len(bits) - highs
            edges   = sum(1 for i in range(1, len(bits)) if bits[i] != bits[i-1])
            print(f"    CH{ch+1:02d}  "
                  f"high={highs:5d}  low={lows:5d}  edges={edges:4d}  "
                  f"({'active' if edges > 0 else 'static'})")


# ---------------------------------------------------------------------------
# TS (State) learn string parser
# ---------------------------------------------------------------------------

class StateLearnString:
    """
    Parses a TS (state) learn string binary blob  ("RS" header).

    Binary format  (reverse-engineered from HP 1631A captured data;
    cross-referenced against Chapter 10 of the Operating & Programming Manual)
    --------------------------------------------------------------------------
    Bytes  0- 1   ASCII header "RS"
    Bytes  2- 3   Byte count  (big-endian uint16; payload length not counting
                               the 4-byte header+count word itself; includes
                               the 3-byte revision+CRC trailer)
    Bytes  4-17   14-byte fixed header (mostly reserved/unknown in observed
                  captures; all zeros for an empty acquisition)
    Bytes 18..N   Sample data: 5 bytes per state, big-endian uint40
                  Bit 0 (LSB of byte 4) = channel 0, bit 39 (MSB of byte 0)
                  = channel 39.  The HP 1631A maps its 4 pods (J/K/L/M,
                  8 channels each) and qualifier lines into these 40 bits;
                  the exact pod→bit mapping is configuration-dependent and
                  stored in the TC (configuration) learn string.
    Byte  N+1     Revision code
    Bytes N+2-3   CRC  (16-bit sum of bytes 4 through N inclusive, big-endian)

    The number of valid state samples is derived from the file size:
        n_states = (total_expected - DATA_START - 3) // BYTES_PER_SAMPLE
    where DATA_START=18 and BYTES_PER_SAMPLE=5.

    An empty acquisition (trigger never fired or no pods assigned) produces a
    minimal 21-byte file with n_states=0.

    Attributes
    ----------
    raw             : original bytes
    valid           : True if header and size are consistent
    error           : descriptive error / warning string, or None
    n_channels      : always 40 (full 40-bit sample word)
    n_states        : number of decoded state samples
    tracepoint      : tracepoint index (derived from header byte 13; partially
                      understood — treat as informational)
    n_hits          : trigger hit count (byte 14; partially understood)
    revision        : revision code byte
    crc_ok          : True if CRC verified (best-effort; algorithm not fully
                      confirmed against manual — mismatches are warnings only)
    samples         : list of int, one per state sample (40-bit value)
    """

    HEADER         = b"RS"
    DATA_START     = 18     # first sample byte offset
    BYTES_PER      = 5      # bytes per state sample
    N_CHANNELS_MAX = 40     # bits per sample word

    def __init__(self, data: bytes):
        self.raw        = data
        self.valid      = False
        self.error      = None
        self.n_channels = self.N_CHANNELS_MAX
        self.n_states   = 0
        self.tracepoint = 0
        self.n_hits     = 0
        self.n_runs     = 0
        self.revision   = 0
        self.crc_ok     = False
        self.samples    = []
        self._parse(data)

    def _parse(self, data: bytes):
        # Minimum: 4-byte prefix + 14-byte header + 3-byte trailer = 21
        if len(data) < 21:
            self.error = f"File too short ({len(data)} bytes); minimum is 21."
            return

        if data[0:2] != self.HEADER:
            hdr = data[0:2]
            try:
                hdr_str = hdr.decode("ascii")
            except Exception:
                hdr_str = repr(hdr)
            hints = {
                "RC": "This looks like a TC (configuration) learn string.",
                "RT": "This looks like a TT (timing) learn string, not a TS.",
                "RA": "This looks like a TA (analog) learn string.",
            }
            hint = hints.get(hdr_str, "")
            self.error = (
                f"Expected 'RS' header, got {hdr_str!r}.  "
                + (hint if hint else "Is this actually a TS state learn string?")
            )
            return

        byte_count     = struct.unpack(">H", data[2:4])[0]
        total_expected = 4 + byte_count

        if len(data) < total_expected:
            self.error = (
                f"Truncated: header says {byte_count} bytes of payload "
                f"(total {total_expected}), but file is only {len(data)} bytes.  "
                "The download may have been cut short."
            )
            return

        # ── Fixed header fields (bytes 4-17) ───────────────────────────────
        # Byte 13 and 14 carry partially-understood fields.  In all observed
        # full (1024-state) captures byte 13 = 0x23 and byte 14 = 0x04; in
        # empty captures both are 0x00.  Treat as informational metadata.
        self.tracepoint = data[13] if len(data) > 13 else 0
        self.n_hits     = data[14] if len(data) > 14 else 0
        self.n_runs     = 0  # not present in TS header (unlike TT)

        # ── CRC ─────────────────────────────────────────────────────────────
        self.revision = data[total_expected - 3]
        stored_crc    = struct.unpack(">H", data[total_expected - 2: total_expected])[0]
        computed_crc  = sum(data[4: total_expected - 2]) & 0xFFFF
        self.crc_ok   = (computed_crc == stored_crc)

        # ── Sample data ─────────────────────────────────────────────────────
        data_end = total_expected - 3   # byte just before revision
        data_len = data_end - self.DATA_START
        if data_len < 0:
            data_len = 0
        n_full = data_len // self.BYTES_PER

        self.n_states = n_full
        self.samples  = []
        for i in range(n_full):
            off = self.DATA_START + i * self.BYTES_PER
            self.samples.append(int.from_bytes(data[off: off + self.BYTES_PER], "big"))

        self.valid = True

    def channel_samples(self, ch_index: int) -> list:
        """
        Return a list of 0/1 values for channel ch_index (0-based).
        ch_index 0 = bit 0 (LSB of last byte of each sample).
        ch_index 39 = bit 39 (MSB of first byte of each sample).
        """
        return [(s >> ch_index) & 1 for s in self.samples]

    def active_channel_indices(self) -> list:
        """Return indices of channels that have at least one transition."""
        active = []
        for ch in range(self.n_channels):
            bits = self.channel_samples(ch)
            if any(bits[i] != bits[i - 1] for i in range(1, len(bits))):
                active.append(ch)
        return active

    def print_info(self):
        """Print a human-readable summary of the learn string header."""
        print("HP 1631A State Learn String (TS / RS)")
        print("─" * 50)
        if self.error and not self.valid:
            print(f"  ERROR: {self.error}")
            if not self.samples:
                return
        elif self.error:
            print(f"  WARNING: {self.error}")
        print(f"  Header          : RS  (state)")
        print(f"  Valid states    : {self.n_states}")
        print(f"  Decoded samples : {len(self.samples)}")
        print(f"  Channels (max)  : {self.n_channels}  (40-bit sample word)")
        print(f"  Revision code   : 0x{self.revision:02X}")
        print(f"  CRC             : {'OK' if self.crc_ok else 'MISMATCH (best-effort check)'}")
        print(f"  Total file size : {len(self.raw)} bytes")
        if self.samples:
            active = self.active_channel_indices()
            print(f"\n  Active channels (have transitions): {len(active)}")
            print(f"  Active indices : {active if active else '(none — all channels static)'}")
            print()
            print("  Channel activity:")
            for ch in range(self.n_channels):
                bits = self.channel_samples(ch)
                highs = sum(bits)
                lows  = len(bits) - highs
                edges = sum(1 for i in range(1, len(bits)) if bits[i] != bits[i - 1])
                if edges > 0 or highs > 0:
                    print(f"    CH{ch:02d}  "
                          f"high={highs:5d}  low={lows:5d}  edges={edges:4d}  "
                          f"({'active' if edges > 0 else 'static-high'})")


# ---------------------------------------------------------------------------
# .sr file writer  (adapted from hp1631a_to_sr.py)
# ---------------------------------------------------------------------------

def write_sr_file(output_path: str,
                  channels: list,
                  samplerate: int):
    """
    Write a sigrok v2 .sr file from a list of (name, [0/1 samples]) tuples.

    File layout
    -----------
    ZIP members:
      version    "2\\n"
      metadata   INI describing device, channels, and sample rate
      logic-1-1  packed binary: unitsize bytes per sample,
                 bit 0 of byte 0 = ch1, bit 1 = ch2, … across bytes for >8ch
    """
    n_ch  = len(channels)
    n_smp = len(channels[0][1]) if channels else 0
    if n_ch == 0 or n_smp == 0:
        raise ValueError("No channel data to write.")

    unitsize = (n_ch + 7) // 8

    # Packed binary
    logic = bytearray(n_smp * unitsize)
    for ch_idx, (_, samples) in enumerate(channels):
        byte_off = ch_idx // 8
        bit_off  = ch_idx % 8
        for smp_idx, bit in enumerate(samples):
            if bit:
                logic[smp_idx * unitsize + byte_off] |= (1 << bit_off)

    # Sample rate string for metadata
    if samplerate >= 1_000_000 and samplerate % 1_000_000 == 0:
        sr_str = f"{samplerate // 1_000_000} MHz"
    elif samplerate >= 1_000 and samplerate % 1_000 == 0:
        sr_str = f"{samplerate // 1_000} kHz"
    else:
        sr_str = f"{samplerate} Hz"

    meta = "\n".join([
        "[global]",
        "sigrok version = 0.5.2",
        "",
        "[device 1]",
        "capturefile = logic-1",
        f"unitsize = {unitsize}",
        f"total probes = {n_ch}",
        f"samplerate = {sr_str}",
    ] + [f"probe{i+1} = {name}" for i, (name, _) in enumerate(channels)]) + "\n"

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("version",   "2\n")
        zf.writestr("metadata",  meta)
        zf.writestr("logic-1-1", bytes(logic))

    size_kb = os.path.getsize(output_path) / 1024
    print(f"  Written : {output_path}")
    print(f"  Channels: {n_ch}   Samples: {n_smp:,}   Size: {size_kb:.1f} KB")


# ---------------------------------------------------------------------------
# Named channel presets
# ---------------------------------------------------------------------------

#  CHANNEL_PRESETS maps preset name → list of channel name strings.
#
#  Timing presets  (used with TT learn strings / convert()):
#    Channel index 0 = CH1 in PulseView = bit 0 of the TT sample word (LSB
#    of the 8-bit / 16-bit sample, i.e. the probe physically labelled "1" on
#    the HP 1631A pod J connector).
#
#  State presets  (used with TS learn strings / convert_state()):
#    Channel index 0 = bit 0 of the 40-bit sample (LSB of the last byte of
#    the 5-byte sample record).  The HP 1631A state analyzer stores its four
#    pods in the 40-bit word; the exact bit-to-pod mapping depends on the
#    Format setup saved in the TC learn string.
#
#    The default channel ordering observed in captured files is:
#      Bits  0- 7  = Pod J (J8=bit7 … J1=bit0)
#      Bits  8-15  = Pod K (K8=bit15 … K1=bit8)
#      Bits 16-23  = Pod L (L8=bit23 … L1=bit16)
#      Bits 24-31  = Pod M (M8=bit31 … M1=bit24)
#      Bits 32-39  = qualifier / extended channels

CHANNEL_PRESETS = {
    # ── LSI-11 / QBUS — BDAL bus lines (16-channel state capture) ────────────
    # Pods J+K → BDAL00-BDAL15 (bidirectional address/data bus)
    # Pods L+M → generic placeholders (not connected / ignored)
    # Qualifier lines (bits 32-39) unused
    "lsi11-16": [
        "BDAL00", "BDAL01", "BDAL02", "BDAL03",
        "BDAL04", "BDAL05", "BDAL06", "BDAL07",   # Pod J, bits 0-7
        "BDAL08", "BDAL09", "BDAL10", "BDAL11",
        "BDAL12", "BDAL13", "BDAL14", "BDAL15",   # Pod K, bits 8-15
        "L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8",          # Pod L
        "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8",          # Pod M
        "Q0", "Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7",          # qualifiers
    ],

    # ── LSI-11 / QBUS — control signals (state capture) ──────────────────────
    # Pods J+K → BDAL00-BDAL15 (bidirectional address/data bus)
    # Pod L → QBUS control lines
    # Pod M → additional control / unused
    "lsi11-ctrl": [
        "BDAL00", "BDAL01", "BDAL02", "BDAL03",
        "BDAL04", "BDAL05", "BDAL06", "BDAL07",   # Pod J, bits 0-7
        "BDAL08", "BDAL09", "BDAL10", "BDAL11",
        "BDAL12", "BDAL13", "BDAL14", "BDAL15",   # Pod K, bits 8-15
        "SYNC", "DIN", "DOUT", "RPLY",
        "WTBT", "BS7", "SACK", "REF",             # Pod L: QBUS control
        "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8",          # Pod M
        "Q0", "Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7",          # qualifiers
    ],

    # ── Motorola HC11 / 68HC11 — timing capture (16+3 channels) ─────────────
    # Pod J → AD0-AD7  (multiplexed address/data)
    # Pod K → A8-A15   (upper address lines)
    # Pod L channels 0-2 → control: AS, E, R/W
    "hc11-19": [
        "AD0",  "AD1",  "AD2",  "AD3",
        "AD4",  "AD5",  "AD6",  "AD7",            # Pod J (bits 0-7)
        "A8",   "A9",   "A10",  "A11",
        "A12",  "A13",  "A14",  "A15",            # Pod K (bits 8-15)
        "AS",   "E",    "R/W",                     # Pod L first 3
    ],
}


def _resolve_channel_names(preset: str, channel_names: list, n_ch: int) -> list:
    """
    Return a list of exactly n_ch channel name strings, resolved in this order:
    1. channel_names (explicit list) if provided
    2. CHANNEL_PRESETS[preset] if preset is given and in the dict
    3. generic "CH1", "CH2", … fallback
    Pads with generic names if the resolved list is shorter than n_ch.
    """
    if channel_names:
        names = list(channel_names)
    elif preset and preset in CHANNEL_PRESETS:
        names = list(CHANNEL_PRESETS[preset])
    else:
        names = []

    if len(names) < n_ch:
        names = names + [f"CH{i+1}" for i in range(len(names), n_ch)]
    return names[:n_ch]


# ---------------------------------------------------------------------------
# Top-level conversion — timing (TT learn string → .sr)
# ---------------------------------------------------------------------------

def convert(lrn_path: str,
            output_path: str,
            samplerate_override: int = 0,
            channel_names: list = None,
            preset: str = None,
            skip_static: bool = False,
            verbose: bool = False) -> bool:
    """
    Load a TT timing .lrn file and write a sigrok .sr file.

    Parameters
    ----------
    lrn_path            : path to the binary .lrn file
    output_path         : path for the output .sr file
    samplerate_override : if > 0, use this Hz value instead of decoded rate
    channel_names       : explicit list of channel name strings (8 or 16)
    preset              : name of a CHANNEL_PRESETS entry; used when
                          channel_names is not provided
    skip_static         : if True, omit channels that never change
    verbose             : print per-channel detail
    """
    # Load
    try:
        with open(lrn_path, "rb") as f:
            data = f.read()
    except OSError as e:
        print(f"ERROR: Cannot read {lrn_path}: {e}")
        return False

    print(f"Input   : {lrn_path}  ({len(data)} bytes)")

    # Parse
    tt = TTLearnString(data)

    if not tt.valid and not tt.samples:
        print(f"ERROR: {tt.error}")
        return False

    if tt.error:
        print(f"WARNING: {tt.error}")

    # Sample rate
    if samplerate_override > 0:
        samplerate = samplerate_override
        print(f"Rate    : {samplerate:,} Hz  (override)")
    else:
        samplerate = tt.sample_rate_hz
        print(f"Rate    : {samplerate:,} Hz  (from header: {tt.clock_str})")

    if not tt.crc_ok:
        print("WARNING : CRC mismatch — the file may be corrupt or incomplete.")

    print(f"Channels: {tt.n_channels}   "
          f"Samples: {len(tt.samples):,}   "
          f"Tracepoint: {tt.tracepoint}   "
          f"Glitch: {'ON' if tt.glitch_mode else 'off'}")

    # Build channel list
    n_ch  = tt.n_channels
    names = _resolve_channel_names(preset, channel_names, n_ch)
    if preset:
        print(f"Preset  : {preset}")

    channels = []
    skipped  = []
    for i in range(n_ch):
        samples   = tt.channel_samples(i)
        highs     = sum(samples)
        edges     = sum(1 for j in range(1, len(samples)) if samples[j] != samples[j-1])
        is_static = (edges == 0)

        if skip_static and is_static:
            skipped.append(names[i])
            continue

        channels.append((names[i], samples))

        if verbose:
            print(f"  {names[i]:<12}  high={highs:5d}  edges={edges:4d}  "
                  f"{'[static — kept]' if is_static else ''}")

    if skipped:
        print(f"Skipped {len(skipped)} static channels: {', '.join(skipped)}")

    if not channels:
        print("ERROR: All channels are static and --skip-static is set.")
        return False

    print(f"Writing {len(channels)} channels…")
    write_sr_file(output_path, channels, samplerate)
    print(f"\nDone.  Open {output_path} in PulseView.")
    return True


# ---------------------------------------------------------------------------
# Top-level conversion — state (TS learn string → .sr)
# ---------------------------------------------------------------------------

def convert_state(lrn_path: str,
                  output_path: str,
                  samplerate_override: int = 1_000_000,
                  channel_names: list = None,
                  preset: str = None,
                  skip_static: bool = True,
                  verbose: bool = False) -> bool:
    """
    Load a TS state .lrn file and write a sigrok .sr file.

    State mode does not have an internal clock rate — the HP 1631A clocks its
    state samples on external bus transitions.  samplerate_override is
    therefore required (or defaults to 1 MHz) purely so PulseView can draw a
    valid timeline.  The exported waveform is one sample per captured bus state
    (cycle-accurate but not time-accurate unless the bus clock is known).

    Parameters
    ----------
    lrn_path            : path to the binary TS .lrn file (RS header)
    output_path         : path for the output .sr file
    samplerate_override : Hz value for the PulseView timeline (default 1 MHz)
    channel_names       : explicit list of channel name strings (up to 40)
    preset              : name of a CHANNEL_PRESETS entry
    skip_static         : if True (default), omit channels that never change;
                          this is on by default because most of the 40 bit
                          positions will be unused (pods not connected)
    verbose             : print per-channel detail
    """
    # Load
    try:
        with open(lrn_path, "rb") as f:
            data = f.read()
    except OSError as e:
        print(f"ERROR: Cannot read {lrn_path}: {e}")
        return False

    print(f"Input   : {lrn_path}  ({len(data)} bytes)")

    # Parse
    ts = StateLearnString(data)

    if not ts.valid and not ts.samples:
        print(f"ERROR: {ts.error}")
        return False

    if ts.error:
        print(f"WARNING: {ts.error}")

    if ts.n_states == 0:
        print("ERROR: No state samples in file (empty acquisition).")
        return False

    if not ts.crc_ok:
        print("WARNING : CRC mismatch — the file may be corrupt or incomplete.")

    print(f"States  : {ts.n_states:,}")
    print(f"Bits/sample: {ts.N_CHANNELS_MAX}  (full 40-bit TS sample word)")
    print(f"Rate    : {samplerate_override:,} Hz  (user-supplied timeline)")
    if preset:
        print(f"Preset  : {preset}")

    # Resolve channel names (up to 40, one per bit position)
    names = _resolve_channel_names(preset, channel_names, ts.N_CHANNELS_MAX)

    # Build channel list — skip static (unconnected) channels by default
    channels = []
    skipped  = []
    for i in range(ts.N_CHANNELS_MAX):
        samples   = ts.channel_samples(i)
        highs     = sum(samples)
        edges     = sum(1 for j in range(1, len(samples)) if samples[j] != samples[j-1])
        is_static = (edges == 0)

        if skip_static and is_static:
            skipped.append(names[i])
            continue

        channels.append((names[i], samples))

        if verbose:
            print(f"  {names[i]:<12}  high={highs:5d}/{ts.n_states}  edges={edges:4d}  "
                  f"{'[static — kept]' if is_static else ''}")

    if skipped and verbose:
        print(f"Skipped {len(skipped)} static channels (unconnected pods / "
              f"unused bit positions): {', '.join(skipped)}")
    elif skipped:
        print(f"Skipped {len(skipped)} static channels.")

    if not channels:
        print("ERROR: All 40 bit positions are static.  "
              "The capture may be empty or all probes may be unconnected.  "
              "Re-run with skip_static=False to include all 40 channels.")
        return False

    print(f"Writing {len(channels)} active channels…")
    write_sr_file(output_path, channels, samplerate_override)
    print(f"\nDone.  Open {output_path} in PulseView.")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert HP 1631A binary TT learn string (.lrn) to sigrok .sr for PulseView",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input",
                        help="Input .lrn file (binary TT timing learn string)")
    parser.add_argument("-o", "--output",
                        help="Output .sr file (default: <input>.sr)")
    parser.add_argument("-r", "--samplerate", type=int, default=0,
                        help="Override sample rate in Hz "
                             "(default: auto-decoded from TT header).\n"
                             "E.g. --samplerate 10000000 for 10 MHz.")
    parser.add_argument("-c", "--channels",
                        help="Comma-separated channel names, e.g. "
                             "CLK,MOSI,MISO,CS,D4,D5,D6,D7\n"
                             "Default: CH1 … CH8 or CH16")
    parser.add_argument("--skip-static", action="store_true",
                        help="Omit channels that never change value "
                             "(all-0 or all-1 throughout the capture)")
    parser.add_argument("--info", action="store_true",
                        help="Print header info and channel activity; "
                             "do not write a .sr file")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print per-channel detail during conversion")

    args = parser.parse_args()

    # --info mode
    if args.info:
        try:
            with open(args.input, "rb") as f:
                data = f.read()
        except OSError as e:
            print(f"ERROR: {e}")
            sys.exit(1)
        tt = TTLearnString(data)
        tt.print_info()
        return

    # Output path
    output = args.output
    if not output:
        base   = os.path.splitext(args.input)[0]
        output = base + ".sr"

    # Channel names
    channel_names = None
    if args.channels:
        channel_names = [n.strip() for n in args.channels.split(",")]

    ok = convert(
        lrn_path=args.input,
        output_path=output,
        samplerate_override=args.samplerate,
        channel_names=channel_names,
        skip_static=args.skip_static,
        verbose=args.verbose,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
