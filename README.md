# HP 1631A Logic Analyzer Controller

A Python toolkit for remotely controlling and capturing data from the HP 1631A/D logic analyzer over GPIB. Covers the full workflow from instrument connection through data capture, binary learn string decoding, and export to [PulseView](https://sigrok.org/wiki/PulseView) via the sigrok `.sr` format.

All instrument commands are verified against Chapter 10 of the *HP 1631A/D Operating & Programming Manual*.

---

## Files

| File | Description |
|---|---|
| `hp1631a_gui.py` | Tkinter GUI front-end — connection panel, capture tabs, waveform viewer |
| `hp1631a_extended.py` | Instrument driver and GPIB adapter library |
| `hp1631a_to_sr.py` | Convert ASCII listing output (screen text via DR) to sigrok `.sr` |
| `hp1631a_lrn_to_sr.py` | Convert binary TT learn string (`.lrn` file) to sigrok `.sr` |

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

No additional libraries are needed for the Prologix or USBGpib V2 adapters, or for the converter scripts.

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

A self-contained Tkinter application. Run it directly:

```
python hp1631a_gui.py
```

![Green-phosphor CRT color scheme](https://i.imgur.com/placeholder.png)

### Connection bar

Select adapter type, serial port (auto-populated), GPIB address (factory default **5**), and timeout. The EOS terminator selector is active only for Prologix. Click **CONNECT** to open the adapter and query instrument ID; the online lamp turns green on success.

### Tabs

**CONTROL**
- **RN** (RUN), **RE** (RESUME), **ST** (STOP) — acquisition control
- Menu navigation mnemonics (SM, FM, TM, LM, WM) and cursor/scroll keys (CU/CD/CL/CR, RU/RD)
- Binary learn string download buttons: **TC** (config), **TS** (state), **TT** (timing), **TE** (everything)
- **READ SCREEN** — reads the current 23×64 display via the DR command
- **⚠ CLEAR STUCK TRANSFER** — sends IFC + SDC to recover from the "Awaiting HP-IB transfer" state
- **CONNECTION DIAGNOSTICS** — step-through dialog covering adapter firmware, bus reset, serial poll, EOS sweep (Prologix only), and ID command variants
- **Direct command** entry with response display

**CAPTURE**
- Triggers a full acquisition (RN), waits for Measurement Complete, then downloads TC + TS + TT learn strings and a DR screen read
- Optionally exports a PulseView `.sr` file alongside the binary `.lrn` files
- Supports SRQ-based or polled end-of-acquisition detection

**EXPORT**
- **CAPTURE & EXPORT CSV**: runs a capture and decodes the TT timing learn string to a per-channel CSV file
- **CONVERT → .sr**: converts any previously saved listing or capture bundle to sigrok format
- **PROBE FILE**: lists channels and detected sample rate without writing

**BATCH**
- Captures N traces in a loop with a configurable inter-trace delay
- Saves each trace as a numbered set of learn string files

### Waveform viewer

A scrollable canvas timing diagram renders decoded TT learn string data with per-channel colour rows, adjustable zoom (1–20 px/sample), a click-to-place sample cursor, and mouse-wheel scrolling.

### Settings persistence

Last-used connection parameters, file paths, and capture options are saved to `hp1631a_gui.json` in the script directory and restored on next launch.

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

### Key HP 1631A facts (from manual Chapter 10)

- Commands terminate with `;`, CR, or LF
- All keyboard mnemonics are exactly **two characters** (Table 10-1): `RN` = RUN, `ST` = STOP, `RE` = RESUME
- The SRQ mask byte (`MB` command) defaults to 0 at power-on — serial poll always returns 0 until `MB 34` is sent
- Data download uses binary learn string commands: **TC** (config), **TS** (state), **TT** (timing), **TA** (analog), **TE** (everything). There is no `SLIST?`, `TLIST?`, `WLIST?`, or `CONFIG?`
- `DR row col count` reads ASCII text from the 23×64 display buffer

### Binary learn string format (TT timing)

| Bytes | Field |
|---|---|
| 0–1 | ASCII header `RT` |
| 2–3 | Byte count (big-endian uint16) |
| 4 | Number of timing channels (8 or 16) |
| 5–6 | Number of valid states (big-endian uint16) |
| 7–8 | Tracepoint index |
| 9 | Glitch detect mode |
| 10 | Sample period index (0–18; see clock table below) |
| 11 | Sample period units (redundant) |
| 48–49 | Trigger hit count |
| 50–51 | Acquisition run count |
| 52–N | Sample data (1 byte/sample ≤8 ch; 2 bytes/sample >8 ch) |
| N+1 | Revision code |
| N+2–N+3 | CRC (16-bit sum, big-endian) |

**Sample period index → clock rate:**

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

---

## Binary `.lrn` → PulseView converter (`hp1631a_lrn_to_sr.py`)

Converts a binary TT timing learn string file (produced by the GUI's CAPTURE tab or by calling `gpib.query_binary("TT")` directly) to a sigrok v2 `.sr` session file.

```
# Convert with auto-detected sample rate from TT header:
python hp1631a_lrn_to_sr.py trace_timing.lrn

# Specify output path:
python hp1631a_lrn_to_sr.py trace_timing.lrn -o trace.sr

# Override sample rate:
python hp1631a_lrn_to_sr.py trace_timing.lrn --samplerate 10000000

# Print header info only (no output file):
python hp1631a_lrn_to_sr.py trace_timing.lrn --info

# Label the channels:
python hp1631a_lrn_to_sr.py trace_timing.lrn --channels CLK,MOSI,MISO,CS,D4,D5,D6,D7

# Omit channels that never change:
python hp1631a_lrn_to_sr.py trace_timing.lrn --skip-static
```

The `--info` flag prints a full header summary including per-channel toggle counts and edge counts — useful for quickly verifying a capture before converting.

The sample rate is decoded automatically from the TT header clock index table. Use `--samplerate` to override if the instrument was running at a rate not covered by the standard table.

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
                                    trace.sr
                                        │
                                    PulseView
```

1. Connect via `hp1631a_gui.py` or directly via `hp1631a_extended.py`
2. Run acquisition (RN), wait for Measurement Complete
3. Download binary learn strings (TC, TS, TT) via CAPTURE tab or `capture_and_export()`
4. Convert `*_timing.lrn` to `trace.sr` using `hp1631a_lrn_to_sr.py`, or use the GUI's built-in export
5. Open `trace.sr` in PulseView and apply protocol decoders

---

## Troubleshooting

**"Awaiting HP-IB transfer" on the instrument front panel**  
This occurs when a configuration-download command (SFORMAT, TFORMAT, STRIGGER, TTRIGGER) is sent without the required data block. Use the **⚠ CLEAR STUCK TRANSFER** button in the CONTROL tab (sends IFC + SDC), or run Steps 1–3 in the Connection Diagnostics dialog.

**Serial poll always returns 0**  
The SRQ mask byte defaults to 0 at power-on and is cleared by RST. Send `MB 34` after connecting. The driver does this automatically on connect; if it gets lost, reconnect or use the Direct Command entry.

**No response to ID command**  
Verify the HP-IB address on the instrument: SYSTEM → CONFIG → HP-IB ADDRESS (factory default is **5**). Run the Connection Diagnostics EOS sweep (Prologix only) and ID variants steps. Increase timeout to 10 s.

**Prologix firmware older than 6.107**  
`++read_tmo_ms` is not supported on old firmware; the driver drains the resulting error bytes automatically. Update from [prologix.biz](http://prologix.biz/) if you experience repeated connection issues.

**Linux: port not appearing in the dropdown**  
The GUI globs `/dev/ttyACM*` and `/dev/ttyUSB*` directly. If the device still doesn't appear, check `dmesg | tail` after plugging in. You may need to add yourself to the `dialout` group: `sudo usermod -aG dialout $USER` (then log out and back in).

---

## License

MIT
