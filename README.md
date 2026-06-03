# hp1631a-gpib

Python toolkit for remote control, data capture, and waveform export of the
**HP 1631A / 1631D Logic Analyzer** via a
[Prologix GPIB-USB](http://prologix.biz/) adapter.

All commands are verified against the *HP 1631A/D Operating & Programming
Manual*, Chapter 10 ("Using HP-IB or HP-IL Interface").

---

## Features

| Module | Purpose |
|---|---|
| `hp1631a_extended.py` | Low-level Prologix driver, full HP 1631A instrument driver, binary learn-string parser, high-level capture helpers |
| `hp1631a_gui.py` | Tkinter GUI — connection panel, CONTROL / CAPTURE / EXPORT / BATCH tabs, canvas waveform viewer, colour-coded log |
| `hp1631a_to_sr.py` | Converts HP 1631A ASCII listing data to sigrok `.sr` session files for [PulseView](https://sigrok.org/wiki/PulseView) |

**Instrument control**
- Start (`RN`), Stop (`ST`), Resume (`RE`), and Group Execute Trigger
- Full menu navigation via Chapter 10 Table 10-1 two-character keyboard mnemonics
- Serial poll with configurable SRQ mask (`MB` command)
- Selected Device Clear (SDC) and Interface Clear (IFC) for bus recovery
- Direct command entry and key-echo buffer read (`KE`)

**Data download**
- Binary learn strings: `TC` (configuration, ~5145 bytes), `TS` (state), `TT` (timing), `TA` (analog), `TE` (everything)
- Display read (`DR`) — full 23×64 character screen capture with inverse-video stripping
- Learn-string header parsing and 16-bit CRC verification
- Timing data extraction to per-channel sample arrays

**Export**
- Save captures as raw `.lrn` binary learn-string files
- CSV export of decoded timing channel data
- Screen text export (`.txt`)
- One-step conversion to sigrok `.sr` for PulseView, including multi-bit bus expansion

**GUI extras**
- Green-phosphor CRT colour theme
- Canvas-based timing diagram with per-channel colours, zoom slider, and click cursor
- Step-through connection diagnostics dialog (Prologix firmware check, bus reset, serial poll, EOS sweep, ID variant sweep)
- Emergency "Clear Stuck Transfer" button for `WARNING Awaiting HP-IB transfer` recovery
- Settings persistence across sessions (`hp1631a_gui.json`)
- Batch capture loop with configurable count and inter-capture delay

---

## Hardware Requirements

- HP 1631A or 1631D logic analyzer with the HP-IB (GPIB) interface option
- [Prologix GPIB-USB](http://prologix.biz/) adapter (firmware 6.107+ recommended for `++read_tmo_ms` support; older firmware works with automatic drain)
- Standard GPIB cable
- Host PC running Python 3.8+

The instrument's default GPIB address is **5**. Verify or change it via
`SYSTEM → CONFIG → HP-IB ADDRESS` on the front panel.

---

## Software Requirements

```
pip install pyserial
```

The GUI (`hp1631a_gui.py`) requires `tkinter`, which is included with most
Python distributions. The sigrok converter (`hp1631a_to_sr.py`) uses only the
Python standard library.

---

## Quick Start

### GUI

```bash
python hp1631a_gui.py
```

1. Select the COM port of your Prologix adapter and set the GPIB address (default 5).
2. Click **CONNECT**. The online lamp turns green and the instrument ID appears.
3. Use the **CONTROL** tab to navigate menus, run acquisitions, and download learn strings.
4. Use the **CAPTURE** tab for a full single-shot capture (config + state + timing + screen text).
5. Optionally tick *Also export .sr for PulseView* to convert automatically after capture.

If the instrument becomes unresponsive (the display shows `WARNING Awaiting HP-IB transfer`),
click **⚠ CLEAR STUCK TRANSFER (IFC + SDC)** before sending any further commands.

### Command line — sigrok conversion

```bash
# Convert a timing listing to PulseView format
python hp1631a_to_sr.py --input trace_timing.txt --output trace.sr --samplerate 10000000

# Auto-detect sample rate from timestamp columns
python hp1631a_to_sr.py --input trace_timing.txt --output trace.sr --samplerate auto

# Preview channels without writing a file
python hp1631a_to_sr.py --input trace_timing.txt --probe
```

### Scripted capture

```python
from hp1631a_extended import PrologixGPIB, HP1631A, connection_check, capture_and_export

gpib = PrologixGPIB("/dev/ttyUSB0", gpib_addr=5)
analyzer = HP1631A(gpib)

connection_check(gpib, analyzer)
files = capture_and_export(analyzer, output_stem="trace_001")
print(files)
gpib.close()
```

---

## File Overview

```
hp1631a_extended.py   Core driver and parser
hp1631a_gui.py        Tkinter GUI application
hp1631a_to_sr.py      Sigrok .sr export converter
hp1631a_gui.json      GUI settings (auto-created on first run)
```

Capture outputs use a common stem with type suffixes:

```
<stem>_config.lrn     TC binary learn string (instrument configuration)
<stem>_state.lrn      TS binary learn string (state acquisition data)
<stem>_timing.lrn     TT binary learn string (timing acquisition data)
<stem>_screen.txt     DR display read (ASCII screen text)
<stem>_timing.csv     Decoded timing channel data (CSV export)
<stem>.sr             Sigrok session file for PulseView
```

---

## Key Technical Notes

These points are verified against the HP 1631A/D manual and confirmed on
hardware; they differ from several commonly circulated examples:

- **Command terminator:** The instrument accepts `;`, CR, or LF. Prologix `++eos 1` (CR) is correct for most setups.
- **Mnemonics are exactly two characters** (Table 10-1). `RUN` = `RN`, `STOP` = `ST`. There are no `START`, `STOP`, `SLIST?`, `TLIST?`, `WLIST?`, or `CONFIG?` commands.
- **Data download is binary.** Use `TC`, `TS`, `TT`, `TA`, `TE` — not text listing commands.
- **SRQ mask defaults to 0 at power-on.** Send `MB 34` after connecting (bit 1 = Measurement Complete + bit 5 = Error) before serial poll will return meaningful values.
- **`DATA_READY` is status byte bit 1 (value 2)**, not bit 4.
- **Display read** (`DR row col count`) returns up to 1472 bytes (23 rows × 64 columns). Characters with bit 7 set are inverse-video; mask with `0x7F` to get plain ASCII.
- **Group Execute Trigger** (`++trg`) starts acquisition identically to `RN`.

---

## Diagnostics

The GUI includes a step-through **Connection Diagnostics** dialog
(`CONTROL → ⚑ CONNECTION DIAGNOSTICS`) that runs:

1. Prologix firmware version check (warns if < 6.107)
2. Bus reset (IFC + SDC)
3. Serial poll — confirms GPIB address and cabling
4. EOS terminator sweep — tries CR+LF / CR / LF / None in sequence
5. ID command variants — `ID`, `ID?`, `*IDN?`

---

## License

MIT License

Copyright (c) 2025

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

## References

- *HP 1631A/D Logic Analyzer Operating & Programming Manual* — Chapter 10, "Using HP-IB or HP-IL Interface"
- [Prologix GPIB-USB Controller](http://prologix.biz/)
- [sigrok / PulseView](https://sigrok.org/)
- [Sigrok .sr v2 file format](https://sigrok.org/wiki/File_format:Sigrok/v2)
