# HP 1631A Logic Analyzer Controller

A Python toolkit for remotely controlling and capturing data from the HP 1631A/D logic analyzer over GPIB. Covers the full workflow from instrument connection through data capture, binary learn string decoding, capture comparison, and export to [PulseView](https://sigrok.org/wiki/PulseView) via the sigrok `.sr` format.

All instrument commands are verified against Chapter 10 of the *HP 1631A/D Operating & Programming Manual*.

---

## Files

| File | Description |
|---|---|
| `hp1631a_gui.py` | Tkinter GUI front-end — connection panel, capture tabs, waveform viewer |
| `hp1631a_extended.py` | Instrument driver and GPIB adapter library |
| `hp1631a_lrn_to_sr.py` | Convert binary TT/TS learn string (`.lrn` file) to sigrok `.sr` |
| `hp1631a_to_sr.py` | Convert ASCII listing output (screen text via DR) to sigrok `.sr` |
| `hp1631a_diff.py` | Compare two captures with automatic trigger-point alignment |

---

## Requirements

- Python 3.8+
- [pyserial](https://pypi.org/project/pyserial/) — required for Prologix and USBGpib V2 adapters
- One of the following depending on your GPIB adapter (see [Adapter Support](#adapter-support)):
  - `gpib-ctypes` — for NI GPIB-USB-HS / Keithley KUSB-488A via linux-gpib
  - `python-usbtmc` or `pyvisa` + `pyvisa-py` — for xyphro UsbGpib V1 (USBTMC)
  - `pyvisa` — for PyVISA-compatible adapters

```
pip install pyserial
```

No additional libraries are needed for the Prologix or USBGpib V2 adapters, or for the converter and diff scripts.

---

## Adapter Support

Five GPIB adapter types are supported. All implement a common `GPIBAdapter` interface, so the instrument driver and GUI are fully adapter-agnostic.

| Adapter | Class | Notes |
|---|---|---|
| Prologix GPIB-USB | `PrologixGPIB` | Serial `++` protocol. Windows: `COM3`, Linux: `/dev/ttyUSB0` |
| xyphro USBGpib V2 | `USBGpibV2GPIB` | CDC serial, `!`-command protocol. Linux: `/dev/ttyACM0` |
| NI GPIB-USB-HS / Keithley KUSB-488A | `NI488GPIB` | Requires `gpib-ctypes` or linux-gpib kernel module |
| xyphro UsbGpib V1 / any USBTMC device | `USBTmcGPIB` | Requires `python-usbtmc` or PyVISA |
| Any VISA resource | `PyVisaGPIB` | Pass any VISA resource string; works with NI-VISA or pyvisa-py |

### Linux serial port enumeration

On Linux the GUI automatically discovers GPIB adapter ports by globbing both `/dev/ttyUSB*` (Prologix, CP210x/FTDI) and `/dev/ttyACM*` (USBGpib V2, CDC ACM), sorted ACM-first. No manual entry is needed.

---

## GUI (`hp1631a_gui.py`)

A self-contained Tkinter application with a green-phosphor CRT color scheme. Run it directly:

```
python hp1631a_gui.py
```

### Connection bar

Select adapter type, serial port (auto-populated), GPIB address (factory default **5**), and timeout. The EOS terminator selector is active only for Prologix. Click **CONNECT** to open the adapter and query instrument ID; the online lamp turns green on success.

### Tabs

**CONTROL**
- **RN** (RUN), **RE** (RESUME), **ST** (STOP) — acquisition control with cancel support
- **⚠ CLEAR STUCK TRANSFER** — sends IFC + SDC to recover from the "Awaiting HP-IB transfer" state
- **CHECK**, **POLL**, **IFC**, **SDC** — diagnostics and bus control
- Menu navigation mnemonics (SM, FM, TM, LM, WM) and cursor/scroll keys (CU/CD/CL/CR, RU/RD)
- Binary learn string download buttons: **TC** (config), **TS** (state), **TT** (timing), **TE** (everything)
- **READ SCREEN** — reads the current 23×64 display via the DR command
- **Direct command** entry with response display

**CAPTURE**
- Pre-capture trace mode detection: reads the List screen header via DR to identify whether the instrument is in State, Timing, or Waveform mode before arming, and logs a warning if the detected mode won't produce the expected data
- **TRIGGER PATTERN BUILDER** — dialog for entering a bit-pattern trigger condition (0/1/X per channel) via the Trace/Trigger screen keyboard mnemonics
- **TARGET PROFILES** — save and load named capture configurations (GPIB address, sample rate, channel preset/names, trigger pattern) for rotating between multiple targets on the bench without re-entering settings
- **▶ CAPTURE** — triggers a full acquisition (RN), waits for Measurement Complete, then downloads TC + TS + TT learn strings and a DR screen read; saves a `_capture.json` sidecar recording capture metadata (mode, channel counts, connection info, files produced)
- Optional SRQ-based or polled end-of-acquisition detection
- Optional `.sr` export alongside the binary `.lrn` files; automatically routes State vs. Timing based on the active channel preset
- **Glitch detect** support: save the current TC configuration as a `glitch_config.lrn` (with glitch mode enabled on the instrument first), then arm future captures with it — the downloaded TT header is checked to confirm glitch capture was actually active
- **VERIFY ACQUISITION** — cross-checks TS and TT learn strings to identify which mode produced data and why the other may be empty; includes a raw TT hex dump and header decode

**EXPORT**
- **CAPTURE & EXPORT CSV**: runs a capture and decodes TT timing and TS state learn strings to per-channel CSV files
- **CONVERT → .sr**: converts any previously saved learn string or capture bundle to sigrok format
- **PROBE FILE**: lists channels and detected sample rate without writing

**BATCH**
- Captures N traces in a loop with a configurable inter-trace delay
- Saves each trace as a numbered set of learn string files (TT, TS)
- Optional **Target Profile** for per-trace `.sr` export using a saved channel preset and sample rate

### Waveform viewer

A scrollable canvas timing diagram renders decoded TT timing or TS state learn string data with per-channel colour rows, adjustable zoom (1–20 px/sample), a click-to-place sample cursor, and mouse-wheel scrolling. State channels are filtered to show only those with at least one transition (static/unconnected channels are hidden automatically).

### Settings persistence

Last-used connection parameters, file paths, and capture options are saved to `hp1631a_gui.json` in the script directory and restored on next launch. Target profiles are stored separately in `hp1631a_profiles.json`.

---

## Driver library (`hp1631a_extended.py`)

Can be used independently of the GUI for scripted capture workflows.

### Quick start

```python
from hp1631a_extended import PrologixGPIB, USBGpibV2GPIB, HP1631A, capture_and_export

# Prologix on Linux
gpib = PrologixGPIB("/dev/ttyUSB0", gpib_addr=5, timeout=10.0, eos=1)

# USBGpib V2 on Linux
gpib = USBGpibV2GPIB("/dev/ttyACM0", gpib_addr=5, timeout=10.0)

analyzer = HP1631A(gpib)
analyzer.set_mask(34)          # enable Measurement Complete + Error SRQ bits
print(analyzer.identify())     # → "HP1631A" or "HP1631D"

files = capture_and_export(analyzer, "trace")
# Saves: trace_config.lrn, trace_state.lrn, trace_timing.lrn, trace_screen.txt
```

### Factory function

```python
from hp1631a_extended import open_gpib_adapter

gpib = open_gpib_adapter("prologix",  port="/dev/ttyUSB0", gpib_addr=5)
gpib = open_gpib_adapter("usbgpibv2", port="/dev/ttyACM0", gpib_addr=5)
gpib = open_gpib_adapter("ni488",     gpib_addr=5)
gpib = open_gpib_adapter("usbtmc")
gpib = open_gpib_adapter("pyvisa",    resource="GPIB0::5::INSTR")
```

### HP1631A methods

**Acquisition control**

| Method | Command | Description |
|---|---|---|
| `start()` | `RN` | Start / re-arm acquisition |
| `stop()` | `ST` | Halt acquisition |
| `resume()` | `RE` | Resume after single trigger |
| `trigger()` | GET | Group Execute Trigger (same as RUN) |
| `reset()` | `RST` | Reset to power-up condition |
| `set_mask(value)` | `MB` | Set SRQ mask byte (default 34 = Measurement Complete + Error) |
| `wait_for_measurement_complete()` | — | Poll serial poll until bit 1 set |

**Learn string download**

| Method | Command | Description |
|---|---|---|
| `get_config_learn_string()` | `TC` | Configuration (~5145 bytes) |
| `get_state_learn_string()` | `TS` | State acquisition data |
| `get_timing_learn_string()` | `TT` | Timing acquisition data |
| `get_analog_learn_string()` | `TA` | Analog data |
| `get_everything_learn_string()` | `TE` | All combined |

**Display**

| Method | Command | Description |
|---|---|---|
| `display_read(row, col, count)` | `DR` | Read up to 1472 bytes from the 23×64 display buffer |
| `read_full_screen()` | `DR` | Read full 23×64 display as a plain ASCII string |
| `read_full_screen_rows()` | `DR` | Read full display as a list of 23 row strings |
| `read_listing_pages(pages)` | `DR`+`RD` | Read multiple listing pages by scrolling |

**Diagnostics and verification**

| Method | Description |
|---|---|
| `verify_instrument_identity()` | Downloads TC and decodes the ROM-confirmed identity block (series HP 1630/1631, variant A/D) — use at session start to confirm you are talking to the expected model |
| `verify_acquisition()` | Cross-checks TS and TT learn strings; returns per-mode channel/sample counts and a verdict string explaining empty results |
| `detect_trace_mode()` | Reads the List screen header via DR to identify the active trace mode (state/timing/waveform); heuristic, non-destructive |
| `set_instrument_gpib_address(addr)` | Programs the instrument's own GPIB address via the SM screen; handles the firmware's collision-avoidance at ROM $8197 and re-confirms the live address via TC readback |

**Trigger**

| Method | Description |
|---|---|
| `set_trigger_pattern(pattern, ...)` | Drives the Trace/Trigger screen to set a bit-pattern trigger by sending keystrokes (0/1/X per channel). See ⚠ warning in docstring regarding the don't-care mnemonic — verify the result on the front panel before relying on it. |

### Key HP 1631A facts (from manual Chapter 10)

- Commands terminate with `;`, CR, or LF
- All keyboard mnemonics are exactly **two characters** (Table 10-1): `RN` = RUN, `ST` = STOP, `RE` = RESUME
- The SRQ mask byte (`MB` command) defaults to 0 at power-on — serial poll always returns 0 until `MB 34` is sent
- Data download uses binary learn string commands: **TC** (config), **TS** (state), **TT** (timing), **TA** (analog), **TE** (everything). There is no `SLIST?`, `TLIST?`, `WLIST?`, or `CONFIG?`
- `DR row col count` reads ASCII text from the 23×64 display buffer

### Binary learn string formats

**TT timing learn string**

| Bytes | Field |
|---|---|
| 0–1 | ASCII header `RT` |
| 2–3 | Byte count (big-endian uint16) |
| 4 | Number of timing channels (8 or 16) |
| 5–6 | Number of valid states (big-endian uint16) |
| 7–8 | Tracepoint index |
| 9 | Glitch detect mode (0=off) |
| 10 | Sample period index (0–18; see clock table below) |
| 11 | Sample period units |
| 48–49 | Trigger hit count |
| 50–51 | Acquisition run count |
| 52–N | Sample data (1 byte/sample ≤8 ch; 2 bytes/sample >8 ch) |
| N+1 | Revision code |
| N+2–N+3 | CRC (16-bit sum, big-endian) |

**TS state learn string** (reverse-engineered)

| Bytes | Field |
|---|---|
| 0–1 | ASCII header `RS` |
| 2–3 | Byte count (big-endian uint16) |
| 4 | Number of state channels |
| 5–6 | Number of valid states (big-endian uint16) |
| 7–8 | Tracepoint index |
| 18–N | Sample data (5 bytes/sample, big-endian uint40, 40 channels) |
| N+1 | Revision code |
| N+2–N+3 | CRC (16-bit sum, big-endian) |

**TC configuration learn string** — the first 20 bytes of the payload (starting at byte 4) are a fixed identity block confirmed by ROM55 $8AEF–$8B12: class marker `0x80 0x00`, family ID `L163`, series byte (`0x30`=HP 1630, `0x31`=HP 1631), variant byte (`0x41`=A standard, `0x44`=D data).

**Sample period index → clock rate (TT):**

| Index | Period | Rate |
|---|---|---|
| 0 | 100 ns | 10 MHz |
| 1 | 200 ns | 5 MHz |
| 2 | 500 ns | 2 MHz |
| 3 | 1 µs | 1 MHz |
| 4 | 2 µs | 500 kHz |
| 5 | 5 µs | 200 kHz |
| 6 | 10 µs | 100 kHz |
| 7–18 | 20 µs … 100 ms | 50 kHz … 10 Hz |

### LearnStringParser

`LearnStringParser` provides static/class methods for decoding binary learn strings without a live instrument connection:

| Method | Description |
|---|---|
| `parse_header(data)` | Decode the 4-byte framing header (type, byte count, total length) |
| `verify_crc(data)` | Verify the 16-bit trailing CRC |
| `parse_timing_header(data)` | Decode TT fields (channels, states, sample period, glitch mode, hits, runs) |
| `parse_state_header(data)` | Decode TS fields (channels, states, tracepoint); also returns `crc_ok`, file-size-derived `n_states_file` cross-check, and backward-compat aliases `n_channels`/`n_states` |
| `parse_config_header(data)` | Decode TC identity block — series, variant, ROM-confirmed magic values; flags 1631A vs 1631D mismatch |
| `extract_timing_data(data)` | Extract TT sample records as a list of per-channel bit lists |
| `extract_state_data(data)` | Extract TS sample records as a list of 40-element bit lists (one per state sample) |

---

## Binary `.lrn` → PulseView converter (`hp1631a_lrn_to_sr.py`)

Converts binary TT timing or TS state learn string files to a sigrok v2 `.sr` session file.

```
# Convert timing learn string with auto-detected sample rate:
python hp1631a_lrn_to_sr.py trace_timing.lrn

# Specify output path:
python hp1631a_lrn_to_sr.py trace_timing.lrn -o trace.sr

# Override sample rate:
python hp1631a_lrn_to_sr.py trace_timing.lrn --samplerate 10000000

# Print header info only (no output file):
python hp1631a_lrn_to_sr.py trace_timing.lrn --info

# Label channels explicitly:
python hp1631a_lrn_to_sr.py trace_timing.lrn --channels CLK,MOSI,MISO,CS,D4,D5,D6,D7

# Use a built-in channel preset:
python hp1631a_lrn_to_sr.py trace_timing.lrn --preset hc11-19

# Omit channels that never change:
python hp1631a_lrn_to_sr.py trace_timing.lrn --skip-static

# Convert a state learn string:
python hp1631a_lrn_to_sr.py trace_state.lrn --preset lsi11-16 --samplerate 10000000
```

The sample rate is decoded automatically from the TT header clock index table. Use `--samplerate` to override, or to supply the clock rate for state captures (which carry no timing information). The `--info` flag prints a full header summary including per-channel toggle and edge counts.

### Channel presets (`--preset`)

| Preset | Mode | Description |
|---|---|---|
| `lsi11-16` | State | LSI-11 / Q-bus: BDAL00–BDAL15 on pods J+K |
| `lsi11-ctrl` | State | LSI-11 / Q-bus: BDAL00–BDAL15 plus QBUS control signals (SYNC, DIN, DOUT, RPLY, WTBT, BS7, SACK, REF) on pod L |
| `hc11-19` | Timing | Motorola 68HC11: AD0–AD7, A8–A15, AS, E, R/W (19 channels across pods J, K, L) |

Custom channel names can be provided with `--channels` instead of a preset.

---

## Capture comparison engine (`hp1631a_diff.py`)

Compares two captures of the same target — typically a known-good baseline against a suspect — at the channel/sample level, with automatic trigger-point alignment.

### Why alignment matters

Two captures of "the same" event rarely start at the same absolute sample index due to pretrigger depth and trigger jitter. A naive index-0 diff of a misaligned pair flags every sample as a divergence. `hp1631a_diff.py` slides the candidate against the baseline over a configurable search window and selects the offset with the minimum Hamming distance (number of differing bits). NumPy is used opportunistically for large captures; pure Python otherwise.

### Supported file formats

- **`.lrn`** — raw binary TS state or TT timing learn strings
- **`.sr`** — sigrok v2 files as produced by `hp1631a_lrn_to_sr.py`; parsed directly without requiring PulseView to be installed

### CLI usage

```
# Compare two captures (auto-alignment, all common channels):
python hp1631a_diff.py baseline.lrn candidate.lrn

# Specify the alignment reference channel:
python hp1631a_diff.py baseline.lrn candidate.lrn --reference-channel BDAL00

# Widen the alignment search window:
python hp1631a_diff.py baseline.lrn candidate.lrn --search-window 500

# Compare specific channels only:
python hp1631a_diff.py baseline.lrn candidate.lrn --channels SYNC,DIN,DOUT,RPLY

# Skip cross-correlation (captures already sample-aligned):
python hp1631a_diff.py baseline.sr candidate.sr --no-align

# Export all divergence records to CSV:
python hp1631a_diff.py baseline.lrn candidate.lrn -o divergences.csv
```

### Output

The summary report shows baseline/candidate file info (mode, channel count, sample count), alignment offset and confidence score, and per-channel comparison: mismatch count, mismatch percentage, and first divergence sample index. Warnings are issued for CRC failures, mode mismatches (state vs. timing), and channels present in only one capture.

If the two captures were exported with different channel presets, the diff falls back to positional pairing (by channel index) and flags this prominently.

### Python API

```python
from hp1631a_diff import load_capture, diff_captures

baseline  = load_capture("known_good.lrn")
candidate = load_capture("suspect.lrn")

result = diff_captures(baseline, candidate)
print(result.summary)

for channel, sample_idx in result.divergence_records():
    print(f"{channel} diverges at sample {sample_idx}")
```

---

## ASCII listing → PulseView converter (`hp1631a_to_sr.py`)

Converts ASCII listing text captured from the HP 1631A display (via the DR command) to sigrok `.sr` format. This is the text path; for binary learn strings use `hp1631a_lrn_to_sr.py` instead.

```
# Timing listing, explicit sample rate:
python hp1631a_to_sr.py --input trace_timing.txt --output trace.sr \
       --samplerate 10000000 --mode timing

# State listing:
python hp1631a_to_sr.py --input trace_state.txt --output trace.sr \
       --samplerate 1000000 --mode state

# Capture bundle (all three sections in one file):
python hp1631a_to_sr.py --input capture.txt --output trace.sr \
       --samplerate auto

# Preview channels without writing:
python hp1631a_to_sr.py --input trace_timing.txt --probe
```

### Capture bundle format

The GUI's CAPTURE tab saves a bundle file containing all three listing sections separated by markers:

```
--- STATE LISTING ---
...
--- TIMING LISTING ---
...
--- WAVEFORM LISTING ---
...
```

When `--mode auto` is used (the default), the converter prefers the TIMING section.

### Sample rate detection

The ASCII listing does not embed the sample rate. Pass `--samplerate <Hz>` to match the timing clock configured on the instrument. Use `--samplerate auto` to attempt detection from timestamp columns (`ns`/`µs`/`ms` suffixes) if present. If auto-detection fails it falls back to 1 MHz with a warning.

### Multi-bit bus expansion

If a listing column contains values wider than 1 bit (e.g. an 8-bit data bus grouped under one label), the converter automatically expands it into individual single-bit channels named `<label>0` … `<label>N`.

---

## Workflow summary

```
HP 1631A  ──GPIB──►  hp1631a_extended.py  ──►  *.lrn  (binary learn strings)
                               │                  │
                      hp1631a_gui.py         hp1631a_lrn_to_sr.py
                               │                  │
                               └──────────────────┘
                                        │
                                    trace.sr          *.lrn / *.sr
                                        │                   │
                                    PulseView         hp1631a_diff.py
                                                            │
                                                    divergence report / CSV
```

1. Connect via `hp1631a_gui.py` or directly via `hp1631a_extended.py`
2. Optionally verify instrument identity: `analyzer.verify_instrument_identity()`
3. Run acquisition (RN), wait for Measurement Complete
4. Download binary learn strings (TC, TS, TT) via CAPTURE tab or `capture_and_export()`
5. If the capture looks empty, use **VERIFY ACQUISITION** in the GUI or `analyzer.verify_acquisition()`
6. Convert `*_timing.lrn` or `*_state.lrn` to `trace.sr` using `hp1631a_lrn_to_sr.py`, or use the GUI's built-in export
7. Open `trace.sr` in PulseView and apply protocol decoders
8. To compare against a baseline: `python hp1631a_diff.py known_good.lrn new_capture.lrn`

---

## Troubleshooting

**"Awaiting HP-IB transfer" on the instrument front panel**  
This occurs when a configuration-download command (SFORMAT, TFORMAT, STRIGGER, TTRIGGER) is sent without the required data block. Use the **⚠ CLEAR STUCK TRANSFER** button in the CONTROL tab (sends IFC + SDC).

**Serial poll always returns 0**  
The SRQ mask byte defaults to 0 at power-on and is cleared by RST. Send `MB 34` after connecting. The driver does this automatically on connect; if it gets lost, reconnect or use the Direct Command entry.

**Learn string downloads but contains zero samples**  
Use **VERIFY ACQUISITION** in the CAPTURE tab (or `analyzer.verify_acquisition()`). The most common causes: the timing or state pod isn't assigned in the Format menu, or the active trace mode (State vs. Timing) doesn't match the learn string you're downloading.

**No response to ID command**  
Verify the HP-IB address on the instrument: SYSTEM → CONFIG → HP-IB ADDRESS (factory default is **5**). Increase timeout to 10 s and run the Connection Diagnostics check.

**Prologix firmware older than 6.107**  
`++read_tmo_ms` is not supported on old firmware; the driver drains the resulting error bytes automatically. Update from [prologix.biz](http://prologix.biz/) if you experience repeated connection issues.

**Linux: port not appearing in the dropdown**  
The GUI globs `/dev/ttyACM*` and `/dev/ttyUSB*` directly. If the device still doesn't appear, check `dmesg | tail` after plugging in. You may need to add yourself to the `dialout` group: `sudo usermod -aG dialout $USER` (then log out and back in).

**Diff alignment confidence is low**  
Try specifying a busier reference channel with `--reference-channel`, widening the search with `--search-window`, or verifying both files are the same mode (state vs. timing). If the captures were exported with different channel presets, the diff falls back to positional comparison and warns you.

---

## License

MIT
