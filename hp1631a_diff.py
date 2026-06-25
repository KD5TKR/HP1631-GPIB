"""
hp1631a_diff.py  --  Capture comparison engine for the HP 1631A toolkit
=========================================================================
Compares two captures of the same target — typically "known-good" vs
"faulty" — at the channel/sample level, after automatically aligning
them in time.

Supports two input formats, matching what the rest of the toolkit
already produces:

  .lrn   Raw binary learn strings (TS state or TT timing), parsed with
         StateLearnString / TTLearnString from hp1631a_lrn_to_sr.py.

  .sr    Sigrok v2 capture files (as written by hp1631a_lrn_to_sr.py's
         write_sr_file()), parsed independently here without requiring
         sigrok/PulseView to be installed.

Why cross-correlation alignment
--------------------------------
Two captures of "the same" event on a board rarely start at the same
absolute sample index — pretrigger depth, trigger jitter, and even
re-arming the trace can all shift the trigger point by a handful of
samples. A naive index-0-to-index-0 diff against a misaligned capture
makes every single sample look like a divergence, which is useless.

Logic-analyzer channels are binary, not analog, so true FFT-based
cross-correlation is the wrong tool — it's built for continuous-valued
signals and is needlessly expensive here. Instead this module slides
one channel's bit-vector against the other over a search window and
scores each offset by Hamming distance (number of differing bits).
The offset with the minimum Hamming distance is the alignment point.
This is cheap (pure Python is fine up to tens of thousands of samples;
numpy is used opportunistically if present for larger captures) and
is the textbook correct metric for binary sequences.

The reference channel for alignment defaults to the channel with the
most transitions (edges) in the baseline capture, since a mostly-static
channel (e.g. a chip-select held low for the whole capture) gives the
correlation almost no signal to lock onto. It can be overridden.
"""

from __future__ import annotations

import struct
import zipfile
import io
from dataclasses import dataclass, field
from typing import Optional

try:
    import numpy as _np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

# Reuse the existing, already-correct learn-string parsers rather than
# re-implementing the binary format here.
from hp1631a_lrn_to_sr import TTLearnString, StateLearnString


# ═══════════════════════════════════════════════════════════════════════════
#  Loaded-capture container
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class LoadedCapture:
    """
    A capture normalized to (channel_name -> bit list) regardless of
    whether it came from a .lrn or .sr file, plus whatever metadata was
    available for header-level sanity checks.
    """
    source_path: str
    source_format: str            # "lrn" or "sr"
    mode: str                     # "state", "timing", or "unknown"
    channel_names: list           # ordered list of channel names
    channel_samples: dict         # name -> [0/1, ...]
    n_samples: int
    sample_rate_hz: int = 0       # 0 if not applicable / not known (state)
    crc_ok: Optional[bool] = None
    tracepoint: int = 0
    n_hits: int = 0
    n_runs: int = 0
    warnings: list = field(default_factory=list)

    def channel(self, name: str) -> list:
        return self.channel_samples.get(name, [])


# ═══════════════════════════════════════════════════════════════════════════
#  Loaders
# ═══════════════════════════════════════════════════════════════════════════

def load_lrn(path: str) -> LoadedCapture:
    """
    Load a raw .lrn binary learn string (TS or TT). Auto-detects state
    vs timing from the 2-byte header (RS vs RT), mirroring the
    auto-detection convert()/convert_state() already do in
    hp1631a_lrn_to_sr.py.
    """
    with open(path, "rb") as f:
        data = f.read()

    if len(data) < 2:
        raise ValueError(f"{path}: file too short to contain a learn-string header.")

    header = data[0:2]
    warnings = []

    if header == b"RT":
        ls = TTLearnString(data)
        mode = "timing"
        rate = ls.sample_rate_hz
    elif header == b"RS":
        ls = StateLearnString(data)
        mode = "state"
        rate = 0
    elif header == b"RC":
        raise ValueError(
            f"{path}: this is a TC (configuration) learn string, not a "
            "state or timing capture — nothing to diff."
        )
    else:
        raise ValueError(
            f"{path}: unrecognized learn-string header {header!r}. "
            "Expected 'RT' (timing) or 'RS' (state)."
        )

    if not ls.valid and not ls.samples:
        raise ValueError(f"{path}: {ls.error}")
    if ls.error:
        warnings.append(ls.error)
    if not ls.crc_ok:
        warnings.append("CRC mismatch — file may be corrupt or truncated.")

    names = [f"CH{i+1}" for i in range(ls.n_channels)]
    samples = {name: ls.channel_samples(i) for i, name in enumerate(names)}

    return LoadedCapture(
        source_path=path,
        source_format="lrn",
        mode=mode,
        channel_names=names,
        channel_samples=samples,
        n_samples=len(ls.samples),
        sample_rate_hz=rate,
        crc_ok=ls.crc_ok,
        tracepoint=ls.tracepoint,
        n_hits=ls.n_hits,
        n_runs=ls.n_runs,
        warnings=warnings,
    )


def load_sr(path: str) -> LoadedCapture:
    """
    Load a sigrok v2 .sr file as written by write_sr_file() in
    hp1631a_lrn_to_sr.py. Parsed independently of sigrok/libsigrok so
    this works without PulseView installed.

    .sr layout (v2, as produced by this toolkit):
      ZIP members: "version" ("2\\n"), "metadata" (INI), "logic-1-1"
      (packed binary, unitsize bytes/sample, bit N of byte (N//8) = chN).
    """
    warnings = []
    with zipfile.ZipFile(path, "r") as zf:
        names = zf.namelist()
        if "metadata" not in names:
            raise ValueError(f"{path}: not a valid .sr file (no 'metadata' member).")
        meta_text = zf.read("metadata").decode("utf-8", errors="replace")

        logic_member = next((n for n in names if n.startswith("logic-1-")), None)
        if logic_member is None:
            raise ValueError(f"{path}: no logic data member found in .sr archive.")
        logic_bytes = zf.read(logic_member)

    # ── Parse the metadata INI ──────────────────────────────────────────
    unitsize = 1
    samplerate_hz = 0
    probe_names = {}
    section = None
    for line in meta_text.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip().lower(), val.strip()
        if key == "unitsize":
            try:
                unitsize = int(val)
            except ValueError:
                pass
        elif key == "samplerate":
            samplerate_hz = _parse_samplerate(val)
        elif key.startswith("probe"):
            try:
                idx = int(key[len("probe"):])
                probe_names[idx] = val
            except ValueError:
                pass

    if not probe_names:
        raise ValueError(f"{path}: metadata has no probe definitions.")

    n_ch = max(probe_names) if probe_names else 0
    names = [probe_names.get(i + 1, f"CH{i+1}") for i in range(n_ch)]

    if unitsize <= 0:
        raise ValueError(f"{path}: invalid unitsize ({unitsize}) in metadata.")

    n_samples = len(logic_bytes) // unitsize
    if n_samples * unitsize != len(logic_bytes):
        warnings.append(
            f"logic data length ({len(logic_bytes)} bytes) is not a clean "
            f"multiple of unitsize ({unitsize}); trailing partial sample "
            f"truncated."
        )

    channel_samples = _unpack_logic(logic_bytes, unitsize, n_ch, n_samples)
    samples_dict = {names[i]: channel_samples[i] for i in range(n_ch)}

    return LoadedCapture(
        source_path=path,
        source_format="sr",
        mode="unknown",   # .sr files don't carry state/timing distinction
        channel_names=names,
        channel_samples=samples_dict,
        n_samples=n_samples,
        sample_rate_hz=samplerate_hz,
        crc_ok=None,
        warnings=warnings,
    )


def _parse_samplerate(val: str) -> int:
    """Parse '10 MHz' / '500 kHz' / '1000000 Hz' style strings."""
    val = val.strip()
    parts = val.split()
    if len(parts) != 2:
        try:
            return int(float(val))
        except ValueError:
            return 0
    num_str, unit = parts
    try:
        num = float(num_str)
    except ValueError:
        return 0
    mult = {"hz": 1, "khz": 1_000, "mhz": 1_000_000, "ghz": 1_000_000_000}
    return int(num * mult.get(unit.lower(), 1))


def _unpack_logic(logic_bytes: bytes, unitsize: int, n_ch: int,
                  n_samples: int) -> list:
    """
    Unpack packed sigrok logic data into a list of per-channel bit lists.
    Bit b of byte (b // 8) within each unitsize-byte sample word = channel b.
    """
    if HAS_NUMPY and n_samples > 5000:
        arr = _np.frombuffer(
            logic_bytes[: n_samples * unitsize], dtype=_np.uint8
        ).reshape(n_samples, unitsize)
        channels = []
        for ch in range(n_ch):
            byte_off, bit_off = divmod(ch, 8)
            bits = (arr[:, byte_off] >> bit_off) & 1
            channels.append(bits.tolist())
        return channels

    channels = [[0] * n_samples for _ in range(n_ch)]
    for s in range(n_samples):
        base = s * unitsize
        for ch in range(n_ch):
            byte_off, bit_off = divmod(ch, 8)
            channels[ch][s] = (logic_bytes[base + byte_off] >> bit_off) & 1
    return channels


def load_capture(path: str) -> LoadedCapture:
    """Dispatch to load_lrn or load_sr based on file extension."""
    lower = path.lower()
    if lower.endswith(".sr"):
        return load_sr(path)
    elif lower.endswith(".lrn") or lower.endswith(".bin"):
        return load_lrn(path)
    else:
        # Fall back to sniffing: zip files are .sr, "RS"/"RT" headers are .lrn
        with open(path, "rb") as f:
            head = f.read(4)
        if head[:2] == b"PK":
            return load_sr(path)
        elif head[:2] in (b"RS", b"RT"):
            return load_lrn(path)
        raise ValueError(
            f"{path}: cannot determine file type from extension or "
            f"header bytes {head!r}. Expected .lrn or .sr."
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Alignment (Hamming-distance cross-correlation)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class AlignmentResult:
    offset: int            # samples to shift `candidate` so it lines up with `baseline`
                            # positive => candidate's data starts `offset` samples
                            # later than baseline's (skip `offset` baseline samples)
    score: float            # 0.0 (no match) .. 1.0 (perfect match) over the overlap
    reference_channel: str
    search_window: int
    overlap_samples: int
    method: str             # "hamming-slide" or "no-alignment-needed" / "skipped"
    confidence_note: str = ""
    positional_fallback: bool = False   # True if channel names didn't match and
                                        # alignment/diffing fell back to position-based pairing


def pick_reference_channel(cap: LoadedCapture, requested: Optional[str] = None) -> str:
    """
    Choose the channel to align on: the requested one if given and
    present, else whichever channel has the most transitions (edges) —
    a static channel (chip-select tied low, etc.) gives Hamming-distance
    alignment nothing to lock onto.
    """
    if requested:
        if requested in cap.channel_samples:
            return requested
        raise ValueError(
            f"Reference channel {requested!r} not found in {cap.source_path}. "
            f"Available: {', '.join(cap.channel_names)}"
        )

    best_name, best_edges = None, -1
    for name in cap.channel_names:
        bits = cap.channel_samples[name]
        edges = sum(1 for i in range(1, len(bits)) if bits[i] != bits[i - 1])
        if edges > best_edges:
            best_name, best_edges = name, edges
    if best_name is None:
        raise ValueError("Capture has no channels to align on.")
    return best_name


def _hamming_distance(a: list, b: list) -> int:
    if HAS_NUMPY and len(a) > 2000:
        av = _np.asarray(a, dtype=_np.uint8)
        bv = _np.asarray(b, dtype=_np.uint8)
        return int(_np.count_nonzero(av != bv))
    return sum(1 for x, y in zip(a, b) if x != y)


def align_captures(baseline: LoadedCapture, candidate: LoadedCapture,
                   reference_channel: Optional[str] = None,
                   search_window: Optional[int] = None) -> AlignmentResult:
    """
    Find the best sample-offset alignment of `candidate` against
    `baseline` by minimizing Hamming distance on one reference channel
    over a search window of candidate-vs-baseline shifts.

    search_window defaults to min(200, 10% of the shorter capture's
    length) on each side of zero offset — generous enough to absorb
    pretrigger/trigger-jitter differences without an expensive full
    O(n^2) search over the entire capture.
    """
    ref_name = pick_reference_channel(baseline, reference_channel)
    positional_fallback = ref_name not in candidate.channel_samples

    base_bits = baseline.channel_samples[ref_name]
    if positional_fallback:
        # Channel names don't line up between the two captures (e.g. one
        # was exported with a different preset's labels, or with generic
        # CH1..CHn names while the other has custom names). Fall back to
        # comparing by position: the Nth channel in baseline's order
        # against the Nth channel in candidate's order. This still lets
        # alignment work, but the caller should be told loudly, since a
        # silent positional fallback could mask a real channel-mapping
        # mistake (e.g. probes connected to the wrong pod).
        try:
            ref_idx = baseline.channel_names.index(ref_name)
            positional_name = candidate.channel_names[ref_idx]
            cand_bits = candidate.channel_samples[positional_name]
        except (ValueError, IndexError):
            return AlignmentResult(
                offset=0, score=0.0, reference_channel=ref_name,
                search_window=0, overlap_samples=0, method="skipped",
                confidence_note=(
                    f"Reference channel {ref_name!r} is not present in the "
                    f"candidate capture, and there is no channel at the "
                    f"same position to fall back to. Candidate channels: "
                    f"{', '.join(candidate.channel_names) or '(none)'}. "
                    "Specify --reference-channel explicitly, or check that "
                    "both captures were exported with matching channel "
                    "presets."
                ),
            )
    else:
        cand_bits = candidate.channel_samples[ref_name]

    if not base_bits or not cand_bits:
        return AlignmentResult(
            offset=0, score=0.0, reference_channel=ref_name,
            search_window=0, overlap_samples=0, method="skipped",
            confidence_note="One or both captures have zero samples on "
                            "the reference channel; alignment skipped.",
        )

    shorter = min(len(base_bits), len(cand_bits))
    if search_window is None:
        search_window = max(16, min(200, shorter // 10))

    best_offset, best_dist, best_overlap = 0, None, 0

    for offset in range(-search_window, search_window + 1):
        if offset >= 0:
            # candidate starts `offset` samples later than baseline:
            # compare baseline[offset:] against candidate[0:]
            a = base_bits[offset:]
            b = cand_bits
        else:
            a = base_bits
            b = cand_bits[-offset:]
        n = min(len(a), len(b))
        if n < max(8, search_window // 4):
            continue  # too little overlap at this offset to be meaningful
        dist = _hamming_distance(a[:n], b[:n])
        if best_dist is None or dist < best_dist or (
            dist == best_dist and n > best_overlap
        ):
            best_offset, best_dist, best_overlap = offset, dist, n

    if best_dist is None:
        return AlignmentResult(
            offset=0, score=0.0, reference_channel=ref_name,
            search_window=search_window, overlap_samples=0,
            method="skipped",
            confidence_note="No offset in the search window produced "
                            "sufficient overlap; captures may be too "
                            "short or the search window too small.",
        )

    score = 1.0 - (best_dist / best_overlap) if best_overlap else 0.0

    note = ""
    if positional_fallback:
        note = (
            f"Channel names differ between captures — aligned and compared "
            f"by POSITION instead (baseline {ref_name!r} vs candidate "
            f"{positional_name!r}, same index in each capture's channel "
            f"list). Verify this positional pairing is actually correct "
            f"for your probe setup before trusting per-channel results."
        )
    if score < 0.6:
        note = (note + "  " if note else "") + (
            "Low alignment confidence — the best-found offset still "
            "disagrees on a large fraction of samples. The two captures "
            "may genuinely differ a lot (that could be exactly the fault "
            "you're chasing), may use different channel orderings, or "
            "the reference channel may be a poor choice (try specifying "
            "one explicitly)."
        )
    elif best_offset == search_window or best_offset == -search_window:
        note = (note + "  " if note else "") + (
            f"Best offset ({best_offset}) is at the edge of the search "
            f"window (±{search_window}); the true alignment may lie "
            "outside the window. Re-run with a larger search_window if "
            "the captures are known to have large trigger-point skew."
        )

    return AlignmentResult(
        offset=best_offset, score=score, reference_channel=ref_name,
        search_window=search_window, overlap_samples=best_overlap,
        method="hamming-slide", confidence_note=note,
        positional_fallback=positional_fallback,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Diff
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ChannelDiff:
    name: str
    in_baseline: bool
    in_candidate: bool
    compared_samples: int = 0
    mismatches: int = 0
    first_divergence: Optional[int] = None   # index into the *aligned/overlap* range
    divergence_indices: list = field(default_factory=list)  # capped list, see MAX_DIVERGENCE_RECORDS

    @property
    def mismatch_pct(self) -> float:
        if self.compared_samples == 0:
            return 0.0
        return 100.0 * self.mismatches / self.compared_samples


@dataclass
class DiffResult:
    baseline_path: str
    candidate_path: str
    alignment: AlignmentResult
    overlap_samples: int
    channel_diffs: list           # list[ChannelDiff], in baseline channel order
    channels_only_in_baseline: list
    channels_only_in_candidate: list
    header_warnings: list         # mode/channel-count/CRC mismatches found before diffing
    summary: str

    def divergence_records(self):
        """
        Flatten all channel divergences into (channel, sample_index,
        baseline_value, candidate_value) tuples, sorted by sample index
        then channel — convenient for GUI rendering or CSV export.
        """
        records = []
        for cd in self.channel_diffs:
            for idx in cd.divergence_indices:
                records.append((cd.name, idx))
        records.sort(key=lambda r: (r[1], r[0]))
        return records


MAX_DIVERGENCE_RECORDS_PER_CHANNEL = 5000  # safety cap for pathological all-different cases


def diff_captures(baseline: LoadedCapture, candidate: LoadedCapture,
                  reference_channel: Optional[str] = None,
                  search_window: Optional[int] = None,
                  channels: Optional[list] = None) -> DiffResult:
    """
    Full comparison pipeline: header sanity checks, alignment, then
    per-channel Hamming diff over the aligned overlap region.

    channels: optional explicit list of channel names to compare (by
    name, matched in both captures). Defaults to the intersection of
    both captures' channel names, in baseline order.
    """
    header_warnings = []

    if baseline.mode != "unknown" and candidate.mode != "unknown" \
       and baseline.mode != candidate.mode:
        header_warnings.append(
            f"Mode mismatch: baseline is {baseline.mode!r}, candidate is "
            f"{candidate.mode!r}. Comparing a State capture against a "
            f"Timing capture is rarely meaningful — double-check you "
            f"selected the files you intended."
        )

    if baseline.crc_ok is False:
        header_warnings.append(
            f"Baseline ({baseline.source_path}) failed its CRC check — "
            "treat results with caution, the file may be corrupt."
        )
    if candidate.crc_ok is False:
        header_warnings.append(
            f"Candidate ({candidate.source_path}) failed its CRC check — "
            "treat results with caution, the file may be corrupt."
        )

    base_set = set(baseline.channel_names)
    cand_set = set(candidate.channel_names)
    only_base = [c for c in baseline.channel_names if c not in cand_set]
    only_cand = [c for c in candidate.channel_names if c not in base_set]

    alignment = align_captures(baseline, candidate, reference_channel, search_window)

    if alignment.positional_fallback:
        # No channel names are shared between the two captures (e.g.
        # different preset labels were used for each export) — pair
        # channels by index instead, capped to the shorter channel list.
        # Flagged loudly here and in the alignment note, since a silent
        # positional fallback could mask a real probe/channel-mapping
        # mistake.
        n_pair = min(len(baseline.channel_names), len(candidate.channel_names))
        pairs = [(baseline.channel_names[i], candidate.channel_names[i])
                for i in range(n_pair)]
        header_warnings.append(
            "Baseline and candidate channel names do not overlap — "
            "compared by POSITION (index in each capture's channel list) "
            "instead of by name. See alignment note above."
        )
        if channels:
            header_warnings.append(
                "--channels was given but is ignored in positional-fallback "
                "mode (channel names don't match between files)."
            )
    else:
        if only_base:
            header_warnings.append(
                f"{len(only_base)} channel(s) present only in baseline: "
                f"{', '.join(only_base)}"
            )
        if only_cand:
            header_warnings.append(
                f"{len(only_cand)} channel(s) present only in candidate: "
                f"{', '.join(only_cand)}"
            )

        if channels:
            compare_names = [c for c in channels if c in base_set and c in cand_set]
            missing = [c for c in channels if c not in compare_names]
            if missing:
                header_warnings.append(
                    f"Requested channel(s) not present in both captures, "
                    f"skipped: {', '.join(missing)}"
                )
        else:
            compare_names = [c for c in baseline.channel_names if c in cand_set]
        pairs = [(name, name) for name in compare_names]

    paired_base_names = {b for b, _ in pairs}
    channel_diffs = []
    overlap_n = 0

    # Channels that exist in only one capture (skipped from comparison,
    # but still reported so the GUI/CLI can show them as "not compared").
    if not alignment.positional_fallback:
        for name in only_base:
            channel_diffs.append(ChannelDiff(name=name, in_baseline=True, in_candidate=False))
        for name in only_cand:
            channel_diffs.append(ChannelDiff(name=name, in_baseline=False, in_candidate=True))

    for base_name, cand_name in pairs:
        base_bits = baseline.channel_samples[base_name]
        cand_bits = candidate.channel_samples[cand_name]

        offset = alignment.offset
        if offset >= 0:
            a = base_bits[offset:]
            b = cand_bits
        else:
            a = base_bits
            b = cand_bits[-offset:]

        n = min(len(a), len(b))
        overlap_n = max(overlap_n, n)

        mismatches = 0
        first_div = None
        div_indices = []
        for i in range(n):
            if a[i] != b[i]:
                mismatches += 1
                if first_div is None:
                    first_div = i
                if len(div_indices) < MAX_DIVERGENCE_RECORDS_PER_CHANNEL:
                    div_indices.append(i)

        # Display name: if positionally paired with a differently-named
        # candidate channel, show both so it's clear what was compared.
        display_name = base_name if base_name == cand_name else f"{base_name}↔{cand_name}"

        channel_diffs.append(ChannelDiff(
            name=display_name, in_baseline=True, in_candidate=True,
            compared_samples=n, mismatches=mismatches,
            first_divergence=first_div, divergence_indices=div_indices,
        ))

    summary = _build_summary(baseline, candidate, alignment, channel_diffs,
                             header_warnings, overlap_n)

    return DiffResult(
        baseline_path=baseline.source_path,
        candidate_path=candidate.source_path,
        alignment=alignment,
        overlap_samples=overlap_n,
        channel_diffs=channel_diffs,
        channels_only_in_baseline=only_base,
        channels_only_in_candidate=only_cand,
        header_warnings=header_warnings,
        summary=summary,
    )


def _build_summary(baseline, candidate, alignment, channel_diffs,
                   header_warnings, overlap_n) -> str:
    lines = []
    lines.append(f"Baseline : {baseline.source_path}  "
                 f"({baseline.mode}, {len(baseline.channel_names)} ch, "
                 f"{baseline.n_samples} samples)")
    lines.append(f"Candidate: {candidate.source_path}  "
                 f"({candidate.mode}, {len(candidate.channel_names)} ch, "
                 f"{candidate.n_samples} samples)")
    lines.append("")
    lines.append(
        f"Alignment: offset={alignment.offset:+d} samples  "
        f"(reference channel {alignment.reference_channel}, "
        f"score={alignment.score:.3f}, overlap={alignment.overlap_samples})"
    )
    if alignment.confidence_note:
        lines.append(f"  ⚠ {alignment.confidence_note}")
    lines.append("")

    compared = [cd for cd in channel_diffs if cd.in_baseline and cd.in_candidate]
    if not compared:
        lines.append("No common channels were compared.")
    else:
        identical = [cd for cd in compared if cd.mismatches == 0]
        diverged = [cd for cd in compared if cd.mismatches > 0]
        lines.append(
            f"Compared {len(compared)} channel(s) over {overlap_n} "
            f"overlapping samples: {len(identical)} identical, "
            f"{len(diverged)} diverged."
        )
        if diverged:
            lines.append("")
            lines.append("Diverged channels (first divergence @ sample, mismatch %):")
            for cd in sorted(diverged, key=lambda c: c.first_divergence or 0):
                lines.append(
                    f"  {cd.name:<10}  first @ {cd.first_divergence:>6}   "
                    f"{cd.mismatches:>6}/{cd.compared_samples} "
                    f"({cd.mismatch_pct:.1f}%)"
                )

    if header_warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in header_warnings:
            lines.append(f"  ⚠ {w}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    import argparse

    ap = argparse.ArgumentParser(
        description="Compare two HP 1631A captures (.lrn or .sr) with "
                    "automatic trigger-point alignment.",
    )
    ap.add_argument("baseline", help="Known-good / reference capture (.lrn or .sr)")
    ap.add_argument("candidate", help="Capture to compare against the baseline")
    ap.add_argument("--reference-channel", default=None,
                    help="Channel name to align on (default: busiest channel "
                         "in baseline)")
    ap.add_argument("--search-window", type=int, default=None,
                    help="±samples to search for alignment (default: auto)")
    ap.add_argument("--channels", default=None,
                    help="Comma-separated list of channels to compare "
                         "(default: all channels common to both files)")
    ap.add_argument("--no-align", action="store_true",
                    help="Skip cross-correlation alignment; compare at "
                         "offset 0 (use when captures are already known "
                         "to be sample-aligned)")
    ap.add_argument("-o", "--output-csv", default=None,
                    help="Write all divergence records to this CSV file")
    args = ap.parse_args()

    baseline = load_capture(args.baseline)
    candidate = load_capture(args.candidate)
    channels = args.channels.split(",") if args.channels else None

    if args.no_align:
        result = diff_captures(baseline, candidate, channels=channels,
                               search_window=0)
    else:
        result = diff_captures(baseline, candidate,
                               reference_channel=args.reference_channel,
                               search_window=args.search_window,
                               channels=channels)

    print(result.summary)

    if args.output_csv:
        import csv
        with open(args.output_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["channel", "sample_index"])
            for name, idx in result.divergence_records():
                w.writerow([name, idx])
        print(f"\nDivergence records written: {args.output_csv}")


if __name__ == "__main__":
    main()
