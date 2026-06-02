"""
hp1631a_to_sr.py  --  Convert HP 1631A listing data to sigrok .sr format
=========================================================================
Converts the ASCII listing output from the HP 1631A logic analyzer
(captured via hp1631a_gpib.py or hp1631a_extended.py) into a sigrok
session file (.sr) that can be opened directly in PulseView.

The sigrok v2 .sr format is a ZIP archive containing:
  version       -- ASCII "2\\n"
  metadata      -- INI-style file describing channels, sample rate, etc.
  logic-1-1     -- Raw packed binary: one sample per byte-group (unitsize),
                   LSB of byte 0 = channel 0, bit 1 = channel 1, etc.

Reference: https://sigrok.org/wiki/File_format:Sigrok/v2

Requires: Python 3.x, no external dependencies (stdlib only)

Usage
-----
  # From a TLIST? (timing) capture file:
  python hp1631a_to_sr.py --input trace_timing.txt --output trace.sr \\
         --samplerate 10000000 --mode timing

  # From a SLIST? (state) capture file:
  python hp1631a_to_sr.py --input trace_state.txt --output trace.sr \\
         --samplerate 1000000 --mode state

  # From a raw capture bundle (produced by --capture in hp1631a_gpib.py):
  python hp1631a_to_sr.py --input hp1631a_capture.txt --output trace.sr \\
         --samplerate 10000000

  # Preview what channels were found without writing a file:
  python hp1631a_to_sr.py --input trace_timing.txt --probe

Notes on sample rate
--------------------
  The HP 1631A does not embed the sample rate in its ASCII listing output.
  You must supply it with --samplerate to match what was configured on the
  instrument front panel.

  Timing mode:  Use the instrument's timing clock rate.
                E.g. if set to 10 MHz, use --samplerate 10000000

  State  mode:  Use the state clock rate (the clock of the bus under test).
                E.g. for 1 MHz SPI, use --samplerate 1000000

  If the listing contains time-stamp values (ns/us columns), the converter
  can derive the sample rate automatically -- use --samplerate auto.

  PulseView will still display the waveform correctly if the sample rate is
  wrong, but protocol decoders that rely on timing (UART baud rate, SPI clock
  frequency, etc.) will give incorrect results.
"""

import argparse
import io
import os
import re
import struct
import sys
import zipfile
from typing import List, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# Listing file parser
# ---------------------------------------------------------------------------

def parse_capture_bundle(text: str) -> Dict[str, str]:
    """
    Parse a bundle file produced by hp1631a_gpib.py --capture.
    Returns a dict with keys 'state', 'timing', 'waveform' (may be empty strings).
    """
    sections: Dict[str, str] = {"state": "", "timing": "", "waveform": ""}
    current = None
    lines = []

    for line in text.splitlines():
        ls = line.strip()
        if ls == "--- STATE LISTING ---":
            if current and lines:
                sections[current] = "\n".join(lines)
            current = "state"
            lines = []
        elif ls == "--- TIMING LISTING ---":
            if current and lines:
                sections[current] = "\n".join(lines)
            current = "timing"
            lines = []
        elif ls == "--- WAVEFORM LISTING ---":
            if current and lines:
                sections[current] = "\n".join(lines)
            current = "waveform"
            lines = []
        elif ls.startswith("#") or ls.startswith("HP 1631A") or ls.startswith("Timestamp"):
            continue
        else:
            if current is not None:
                lines.append(line)

    if current and lines:
        sections[current] = "\n".join(lines)

    return sections


def parse_listing_columns(text: str) -> Tuple[List[str], List[List[str]]]:
    """
    Parse a whitespace-delimited ASCII listing into (headers, rows).

    The HP 1631A listing format:
      - First non-blank line: column headers (signal/label names)
      - Subsequent lines: one sample per row, values matching each column
      - Values are hex or binary integers, sometimes prefixed with 0x or 0b
      - A leading line number column may be present (purely numeric label)
      - Time-stamp columns appear as e.g. '1234ns' or '1.234us'

    Returns:
      headers  -- list of column name strings
      rows     -- list of rows; each row is a list of string values
    """
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return [], []

    # First non-blank, non-comment line is the header
    header_line = lines[0]
    headers = header_line.split()

    rows = []
    for data_line in lines[1:]:
        parts = data_line.split()
        if not parts:
            continue
        # Pad short rows (instrument occasionally omits trailing unchanged values)
        if len(parts) < len(headers):
            parts += ["0"] * (len(headers) - len(parts))
        rows.append(parts[:len(headers)])

    return headers, rows


def parse_value(val_str: str) -> Optional[int]:
    """
    Parse a sample value string from the listing into an integer.
    Handles: decimal, hex (0x prefix or bare hex digits), binary (0b prefix),
             and single bit values '0'/'1'/'H'/'L'/'X'/'Z'.
    Returns None for undefined / high-Z values.
    """
    s = val_str.strip().upper()
    if s in ("X", "Z", "U", "?", "-"):
        return None   # undefined / high-impedance -- treat as 0
    if s in ("H", "1"):
        return 1
    if s in ("L", "0"):
        return 0
    try:
        if s.startswith("0X"):
            return int(s, 16)
        if s.startswith("0B"):
            return int(s, 2)
        # Try hex first (listing may use bare hex without prefix)
        if re.fullmatch(r"[0-9A-F]+", s):
            # Ambiguous: could be decimal or hex.
            # Prefer decimal for single-digit values, hex for multi-char values
            # with A-F present; otherwise decimal.
            if re.search(r"[A-F]", s):
                return int(s, 16)
            return int(s, 10)
        return int(s, 10)
    except ValueError:
        return None


def extract_timestamp_ns(val_str: str) -> Optional[float]:
    """
    Extract a floating-point time value in nanoseconds from a timestamp string.
    Handles: '1234ns', '1.234us', '1.234ms', '1.234s', '1234' (bare ns assumed).
    Returns None if the string does not look like a timestamp.
    """
    s = val_str.strip().lower()
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(ns|us|ms|s)?", s)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2) or "ns"
    multipliers = {"ns": 1.0, "us": 1e3, "ms": 1e6, "s": 1e9}
    return value * multipliers[unit]


def detect_samplerate_from_timestamps(headers: List[str],
                                      rows: List[List[str]]) -> Optional[int]:
    """
    Attempt to derive the sample rate from timestamp columns in the listing.
    Looks for a column whose values match a time-stamp pattern (contain 'ns',
    'us', etc.) and computes the median inter-sample interval.
    Returns the sample rate in Hz, or None if detection fails.
    """
    ts_col = None
    for i, h in enumerate(headers):
        # Common time column names in 1631A listings
        if h.upper() in ("TIME", "TS", "TIMESTAMP", "T"):
            ts_col = i
            break

    if ts_col is None:
        # Try to auto-detect by looking at first data row
        for i, h in enumerate(headers):
            if rows and extract_timestamp_ns(rows[0][i]) is not None:
                ts_col = i
                break

    if ts_col is None:
        return None

    timestamps = []
    for row in rows:
        ts = extract_timestamp_ns(row[ts_col])
        if ts is not None:
            timestamps.append(ts)

    if len(timestamps) < 2:
        return None

    intervals = [timestamps[i+1] - timestamps[i]
                 for i in range(len(timestamps) - 1)
                 if timestamps[i+1] > timestamps[i]]
    if not intervals:
        return None

    # Median interval in ns
    intervals.sort()
    median_ns = intervals[len(intervals) // 2]
    if median_ns <= 0:
        return None

    rate = int(round(1e9 / median_ns))
    return rate


def identify_signal_columns(headers: List[str],
                             rows: List[List[str]]) -> List[Tuple[int, str]]:
    """
    Identify which columns contain signal (logic) data vs. metadata columns
    (line numbers, timestamps, etc.).

    Returns a list of (column_index, channel_name) tuples for signal columns.

    A column is considered a metadata column only if:
      - Its header name is a known metadata keyword, OR
      - Its values consistently look like timestamps (contain 'ns','us','ms','s'
        unit suffixes) — bare integers like '0','1','100' are NOT timestamps.
    """
    # Known metadata column header names (case-insensitive)
    META_HEADERS = {"LINE", "NUM", "#", "ROW", "TIME", "TS", "TIMESTAMP", "T",
                    "STATE", "STATES", "SAMPLE", "INDEX", "ADDR", "ADDRESS"}

    signal_cols = []
    for i, h in enumerate(headers):
        hu = h.upper()

        # 1. Skip by header name
        if hu in META_HEADERS:
            continue

        # 2. Skip if the majority of sample values in this column look like
        #    explicit timestamp strings (must have a unit suffix to qualify).
        if rows:
            ts_count = 0
            check_rows = rows[:min(10, len(rows))]
            for row in check_rows:
                val = row[i]
                # Only count as timestamp if it has an explicit unit suffix
                if re.search(r'(ns|us|ms)\b', val, re.IGNORECASE):
                    ts_count += 1
            if ts_count >= len(check_rows) // 2 + 1:
                continue   # majority of values are suffixed timestamps

        signal_cols.append((i, h))

    return signal_cols


# ---------------------------------------------------------------------------
# Multi-bit channel expansion
# ---------------------------------------------------------------------------

def expand_channel_to_bits(col_idx: int, col_name: str,
                            rows: List[List[str]]) -> List[Tuple[str, List[int]]]:
    """
    A single HP 1631A listing column may represent a multi-bit bus (e.g. an
    8-bit data bus grouped under one label).  This function examines the
    values in the column and, if they are wider than 1 bit, expands them into
    individual single-bit channels.

    The 1631A typically groups channels under user-assigned labels.  A value
    of 0xFF in an 8-bit group becomes channels D0..D7 all high.

    Returns a list of (bit_channel_name, [sample_values...]) tuples.
    Each sample_value is 0 or 1.

    If the column is already 1-bit wide, returns a single entry.
    """
    values = []
    max_val = 0
    for row in rows:
        v = parse_value(row[col_idx])
        if v is None:
            v = 0
        values.append(v)
        if v > max_val:
            max_val = v

    if max_val <= 1:
        # Single-bit channel
        return [(col_name, values)]

    # Determine bit width
    if max_val == 0:
        width = 1
    else:
        width = max_val.bit_length()

    # Expand: bit 0 is the LSB, named <label>0 .. <label>(width-1)
    result = []
    for bit in range(width):
        bit_name = f"{col_name}{bit}"
        bit_samples = [(v >> bit) & 1 for v in values]
        result.append((bit_name, bit_samples))

    return result


# ---------------------------------------------------------------------------
# .sr file writer
# ---------------------------------------------------------------------------

def write_sr_file(output_path: str,
                  channels: List[Tuple[str, List[int]]],
                  samplerate: int,
                  sigrok_version_str: str = "0.5.2"):
    """
    Write a sigrok v2 .sr session file.

    Parameters
    ----------
    output_path     : Path to the output .sr file.
    channels        : List of (channel_name, [sample_0, sample_1, ...]) tuples.
                      Each sample must be 0 or 1.
    samplerate      : Sample rate in Hz.
    sigrok_version_str : Reported sigrok version in metadata (cosmetic only).

    File structure
    --------------
    ZIP contains:
      version    -- "2\\n"
      metadata   -- INI describing channels and sample rate
      logic-1-1  -- packed binary data

    Binary format (logic-1-1)
    -------------------------
    unitsize = ceil(num_channels / 8) bytes per sample.
    Samples are stored sequentially.  Within each sample word:
      Byte 0, bit 0  = channel 1 (probe1)
      Byte 0, bit 1  = channel 2 (probe2)
      ...continuing across bytes for more than 8 channels.
    All multi-byte values are little-endian.
    """
    n_channels = len(channels)
    if n_channels == 0:
        raise ValueError("No channels to write.")

    n_samples = len(channels[0][1])
    if n_samples == 0:
        raise ValueError("No samples to write.")

    # unitsize: bytes per sample (1 byte per 8 channels)
    unitsize = (n_channels + 7) // 8

    # --- Build packed binary sample data ---
    logic_data = bytearray(n_samples * unitsize)

    for ch_idx, (ch_name, samples) in enumerate(channels):
        byte_offset = ch_idx // 8
        bit_offset  = ch_idx % 8
        for sample_idx, bit_val in enumerate(samples):
            if bit_val:
                logic_data[sample_idx * unitsize + byte_offset] |= (1 << bit_offset)

    # --- Build metadata ---
    # Samplerate string: sigrok uses "Hz", "kHz", "MHz" suffixes in metadata
    if samplerate >= 1_000_000 and samplerate % 1_000_000 == 0:
        sr_str = f"{samplerate // 1_000_000} MHz"
    elif samplerate >= 1_000 and samplerate % 1_000 == 0:
        sr_str = f"{samplerate // 1_000} kHz"
    else:
        sr_str = f"{samplerate} Hz"

    meta_lines = [
        "[global]",
        f"sigrok version = {sigrok_version_str}",
        "",
        "[device 1]",
        "capturefile = logic-1",
        f"unitsize = {unitsize}",
        f"total probes = {n_channels}",
        f"samplerate = {sr_str}",
    ]
    for ch_idx, (ch_name, _) in enumerate(channels):
        meta_lines.append(f"probe{ch_idx + 1} = {ch_name}")

    metadata_text = "\n".join(meta_lines) + "\n"

    # --- Write the ZIP (.sr) file ---
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("version", "2\n")
        zf.writestr("metadata", metadata_text)
        zf.writestr("logic-1-1", bytes(logic_data))

    size_kb = os.path.getsize(output_path) / 1024
    print(f"  Written: {output_path}  ({n_channels} channels, "
          f"{n_samples} samples, {size_kb:.1f} KB)")


# ---------------------------------------------------------------------------
# Top-level conversion logic
# ---------------------------------------------------------------------------

def convert(input_text: str,
            output_path: str,
            mode: str,
            samplerate_arg: str,
            max_channels: int = 64,
            verbose: bool = False) -> bool:
    """
    Parse input listing text and write a .sr file.

    mode        : 'auto' (detect from bundle), 'state', 'timing', 'waveform'
    samplerate_arg : integer string (Hz), or 'auto'
    """

    # --- Select the right listing section ---
    if "--- STATE LISTING ---" in input_text or "--- TIMING LISTING ---" in input_text:
        bundle = parse_capture_bundle(input_text)
        if mode == "auto":
            # Prefer timing listing (more time information), fall back to state
            listing_text = bundle["timing"] or bundle["state"] or bundle["waveform"]
            if bundle["timing"]:
                print("  Bundle detected: using TIMING LISTING section.")
            elif bundle["state"]:
                print("  Bundle detected: using STATE LISTING section.")
            else:
                print("  Bundle detected: using WAVEFORM LISTING section.")
        else:
            listing_text = bundle.get(mode, "")
            if not listing_text:
                print(f"  [error] No '{mode}' section found in the bundle file.")
                return False
    else:
        listing_text = input_text

    headers, rows = parse_listing_columns(listing_text)

    if not headers:
        print("  [error] Could not parse any column headers from the listing.")
        print("  Check that the input file contains SLIST?, TLIST?, or WLIST? output.")
        return False

    if verbose:
        print(f"  Columns found: {headers}")
        print(f"  Sample rows:   {len(rows)}")

    if not rows:
        print("  [error] Listing contains headers but no data rows.")
        return False

    # --- Determine sample rate ---
    if samplerate_arg == "auto":
        detected = detect_samplerate_from_timestamps(headers, rows)
        if detected:
            samplerate = detected
            print(f"  Sample rate auto-detected from timestamps: {samplerate} Hz")
        else:
            samplerate = 1_000_000   # 1 MHz fallback
            print(f"  [warn] Could not auto-detect sample rate; defaulting to "
                  f"{samplerate} Hz.  Use --samplerate to specify.")
    else:
        try:
            samplerate = int(samplerate_arg)
        except ValueError:
            print(f"  [error] Invalid --samplerate value: {samplerate_arg!r}")
            return False

    # --- Identify signal columns ---
    signal_cols = identify_signal_columns(headers, rows)
    if not signal_cols:
        print("  [error] No signal columns identified.  "
              "All columns appear to be metadata (timestamps, line numbers).")
        print(f"  Headers: {headers}")
        return False

    if verbose:
        print(f"  Signal columns: {[name for _, name in signal_cols]}")

    # --- Expand multi-bit columns to individual bit channels ---
    channels: List[Tuple[str, List[int]]] = []
    for col_idx, col_name in signal_cols:
        bit_channels = expand_channel_to_bits(col_idx, col_name, rows)
        channels.extend(bit_channels)
        if len(channels) >= max_channels:
            print(f"  [warn] Reached --max-channels limit ({max_channels}); "
                  f"truncating remaining channels.")
            channels = channels[:max_channels]
            break

    print(f"  Channels to write: {len(channels)}")
    if verbose:
        for i, (name, samples) in enumerate(channels):
            highs = sum(samples)
            print(f"    [{i+1:2d}] {name:<16}  {len(samples)} samples  "
                  f"({highs} high / {len(samples)-highs} low)")

    # --- Write .sr file ---
    write_sr_file(output_path, channels, samplerate)
    return True


def probe_listing(input_text: str):
    """Print a summary of what channels and samples were found, without writing."""
    if "--- STATE LISTING ---" in input_text or "--- TIMING LISTING ---" in input_text:
        bundle = parse_capture_bundle(input_text)
        for section_name in ("timing", "state", "waveform"):
            text = bundle.get(section_name, "")
            if not text:
                continue
            headers, rows = parse_listing_columns(text)
            signal_cols = identify_signal_columns(headers, rows)
            print(f"\n  [{section_name.upper()} LISTING]")
            print(f"    Total columns : {len(headers)}")
            print(f"    Data rows     : {len(rows)}")
            print(f"    Signal columns: {[n for _, n in signal_cols]}")

            detected = detect_samplerate_from_timestamps(headers, rows)
            if detected:
                print(f"    Detected rate : {detected} Hz")
            else:
                print(f"    Detected rate : (not detectable from timestamps)")
    else:
        headers, rows = parse_listing_columns(input_text)
        signal_cols = identify_signal_columns(headers, rows)
        detected = detect_samplerate_from_timestamps(headers, rows)
        print(f"\n  Total columns : {len(headers)}")
        print(f"  Data rows     : {len(rows)}")
        print(f"  All headers   : {headers}")
        print(f"  Signal columns: {[n for _, n in signal_cols]}")
        if detected:
            print(f"  Detected rate : {detected} Hz")
        else:
            print(f"  Detected rate : (not detectable from timestamps)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert HP 1631A listing data to sigrok .sr format for PulseView",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--input",  "-i", required=True,
                        help="Input file: listing text captured from the HP 1631A "
                             "(SLIST?, TLIST?, WLIST?, or bundle from --capture)")
    parser.add_argument("--output", "-o",
                        help="Output .sr file path (default: <input>.sr)")
    parser.add_argument("--samplerate", "-r", default="auto",
                        help="Sample rate in Hz, e.g. 10000000 for 10 MHz.  "
                             "Use 'auto' to derive from timestamps in the listing "
                             "(default: auto)")
    parser.add_argument("--mode", "-m",
                        choices=["auto", "state", "timing", "waveform"],
                        default="auto",
                        help="Which listing section to convert when the input is a "
                             "bundle file (default: auto — prefers timing)")
    parser.add_argument("--max-channels", type=int, default=64,
                        help="Maximum number of channels to include (default: 64)")
    parser.add_argument("--probe", action="store_true",
                        help="Print a summary of channels found; do not write a file")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print detailed channel and sample information")

    args = parser.parse_args()

    # Read input
    try:
        with open(args.input, "r", encoding="utf-8", errors="replace") as f:
            input_text = f.read()
    except OSError as e:
        print(f"ERROR: Cannot read {args.input}: {e}")
        sys.exit(1)

    if args.probe:
        probe_listing(input_text)
        return

    # Default output path
    output_path = args.output
    if not output_path:
        base = os.path.splitext(args.input)[0]
        output_path = base + ".sr"

    print(f"Input  : {args.input}")
    print(f"Output : {output_path}")
    print(f"Mode   : {args.mode}")
    print(f"Rate   : {args.samplerate}")
    print()

    ok = convert(
        input_text=input_text,
        output_path=output_path,
        mode=args.mode,
        samplerate_arg=args.samplerate,
        max_channels=args.max_channels,
        verbose=args.verbose,
    )

    if ok:
        print(f"\nDone.  Open {output_path} in PulseView.")
    else:
        print("\nConversion failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
