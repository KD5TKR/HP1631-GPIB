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
# Top-level conversion
# ---------------------------------------------------------------------------

def convert(lrn_path: str,
            output_path: str,
            samplerate_override: int = 0,
            channel_names: list = None,
            skip_static: bool = False,
            verbose: bool = False) -> bool:
    """
    Load a TT .lrn file and write a .sr file.

    Parameters
    ----------
    lrn_path          : path to the binary .lrn file
    output_path       : path for the output .sr file
    samplerate_override : if > 0, use this Hz value instead of decoded rate
    channel_names     : optional list of channel name strings (8 or 16 names)
    skip_static       : if True, omit channels that never change
    verbose           : print per-channel detail
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
    n_ch = tt.n_channels
    if channel_names:
        if len(channel_names) < n_ch:
            # Pad with generic names
            channel_names = channel_names + [
                f"CH{i+1}" for i in range(len(channel_names), n_ch)
            ]
        names = channel_names[:n_ch]
    else:
        names = [f"CH{i+1}" for i in range(n_ch)]

    channels = []
    skipped  = []
    for i in range(n_ch):
        samples = tt.channel_samples(i)
        highs   = sum(samples)
        edges   = sum(1 for j in range(1, len(samples)) if samples[j] != samples[j-1])
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
