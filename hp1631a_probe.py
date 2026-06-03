"""
hp1631a_probe.py  --  Systematic command probe for the HP 1631A/D
==================================================================
Tests every command category from Chapter 10 in isolation.
Reports pass/fail with the exact bytes received so you know exactly
what the instrument supports and what syntax it expects.

Usage
-----
  python hp1631a_probe.py --port COM9 --addr 4

Each test is independent.  A failure in one does not affect the others.
The instrument is reset (IFC + SDC) before each group.
"""

import argparse
import sys
import time
import struct
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from hp1631a_extended import PrologixGPIB, HP1631A, LearnStringParser
except ImportError as e:
    print(f"Cannot import driver: {e}")
    sys.exit(1)


# ── Colour output (no dependencies) ───────────────────────────────────────
class C:
    RESET = "\033[0m"
    GREEN = "\033[92m"
    RED   = "\033[91m"
    AMBER = "\033[93m"
    BLUE  = "\033[94m"
    DIM   = "\033[90m"
    BOLD  = "\033[1m"

def ok(msg):    print(f"  {C.GREEN}✓  {msg}{C.RESET}")
def fail(msg):  print(f"  {C.RED}✗  {msg}{C.RESET}")
def warn(msg):  print(f"  {C.AMBER}⚠  {msg}{C.RESET}")
def info(msg):  print(f"  {C.BLUE}   {msg}{C.RESET}")
def hdr(msg):   print(f"\n{C.BOLD}{C.BLUE}── {msg} {'─'*(50-len(msg))}{C.RESET}")
def dim(msg):   print(f"  {C.DIM}{msg}{C.RESET}")


# ── Test helpers ───────────────────────────────────────────────────────────

def reset_instrument(gpib: PrologixGPIB, analyzer: HP1631A):
    """IFC + SDC + drain before each test group."""
    gpib.ifc()
    time.sleep(0.3)
    analyzer.clear()
    time.sleep(0.3)
    gpib._drain()


def send_and_read(gpib: PrologixGPIB, cmd: str,
                  delay: float = 0.4) -> tuple[bytes, str]:
    """Send cmd, read raw bytes and decoded string. Returns (raw, text)."""
    gpib._drain(0.05)
    gpib._raw_write(f"++addr {gpib.gpib_addr}")
    gpib._raw_write(cmd)
    time.sleep(delay)
    gpib._raw_write(f"++addr {gpib.gpib_addr}")
    gpib._raw_write("++read eoi")
    raw = gpib.ser.read(512)
    return raw, raw.decode(errors="replace").strip()


def send_and_read_binary(gpib: PrologixGPIB, cmd: str,
                          max_bytes: int = 8192,
                          delay: float = 1.0) -> bytes:
    """Send cmd, read binary response."""
    gpib._drain(0.05)
    gpib._raw_write(f"++addr {gpib.gpib_addr}")
    gpib._raw_write(cmd)
    time.sleep(delay)
    gpib._raw_write(f"++addr {gpib.gpib_addr}")
    gpib._raw_write("++read eoi")
    return gpib.ser.read(max_bytes)


# ── Test groups ────────────────────────────────────────────────────────────

def test_identification(gpib, analyzer):
    hdr("1. Identification")
    raw, text = send_and_read(gpib, "ID")
    if text and "HP1631" in text.upper():
        ok(f"ID; → {text!r}")
    elif text:
        warn(f"ID; → unexpected: {text!r}")
    else:
        fail("ID; → no response")
    dim(f"Raw bytes: {raw.hex()}")


def test_mask_and_status(gpib, analyzer):
    hdr("2. Mask Byte (MB) and Status Byte (SB / serial poll)")

    # Serial poll before setting mask (should be 0 since mask defaults to 0)
    sb_before = gpib.serial_poll()
    info(f"Serial poll before MB: {sb_before} (0x{sb_before:02X})")
    if sb_before == 0:
        ok("Serial poll = 0 before MB — mask is 0 at power-on (expected)")
    elif sb_before == 0x02:
        ok("Serial poll = 0x02 — Measurement Complete set from prior run "
           "(normal; will clear when RN is sent)")
    else:
        warn(f"Serial poll = 0x{sb_before:02X} before MB — non-zero, "
             f"may indicate prior activity")

    # Set mask for Measurement Complete (bit 1 = 2) + Error (bit 5 = 32)
    info("Sending MB 34; (Measurement Complete + Error)…")
    gpib.write("MB 34")
    time.sleep(0.2)
    ok("MB 34; sent — no error expected (MB takes no response)")

    # Serial poll after MB — still 0 since no measurement has run
    sb_after = gpib.serial_poll()
    info(f"Serial poll after MB: {sb_after} (0x{sb_after:02X})")
    dim(f"  Bit 1 (Measurement Complete) = {bool(sb_after & 0x02)}")
    dim(f"  Bit 4 (Not Busy)             = {bool(sb_after & 0x10)}")
    dim(f"  Bit 5 (Error)                = {bool(sb_after & 0x20)}")

    # SB command (direct — may abort output, use with care)
    info("Testing SB; command (direct status byte)…")
    gpib._drain(0.05)
    gpib.write("SB 1")
    time.sleep(0.3)
    gpib._raw_write(f"++addr {gpib.gpib_addr}")
    gpib._raw_write("++read eoi")
    raw_sb = gpib.ser.read(4)
    if raw_sb:
        ok(f"SB 1; → raw bytes: {raw_sb.hex()}  decimal: {list(raw_sb)}")
    else:
        warn("SB 1; → no response (may need data ready)")


def test_keyboard_mnemonics(gpib, analyzer):
    hdr("3. Keyboard Mnemonics (menu navigation)")

    tests = [
        ("SM",  "SYSTEM menu"),
        ("FM",  "FORMAT menu"),
        ("TM",  "TRACE menu"),
        ("LM",  "LIST menu"),
        ("WM",  "WFORM menu"),
    ]
    for mnemonic, desc in tests:
        gpib._drain(0.05)
        gpib.write(mnemonic)
        time.sleep(0.4)
        # Read KE to see if any key was buffered (navigation should work silently)
        raw, ke_resp = send_and_read(gpib, "KE", delay=0.3)
        if ke_resp and "??" not in ke_resp:
            ok(f"{mnemonic}; ({desc}) → KE echo: {ke_resp!r}")
        else:
            info(f"{mnemonic}; ({desc}) → sent (KE buffer empty = {ke_resp!r})")
        dim(f"  Watch instrument display to confirm navigation")
        time.sleep(0.2)


def test_cursor_navigation(gpib, analyzer):
    hdr("4. Cursor Navigation Mnemonics")
    # Navigate to a menu first
    gpib.write("LM")
    time.sleep(0.4)

    cursor_tests = [
        ("CH",  "Cursor Home"),
        ("CU",  "Cursor Up"),
        ("CD",  "Cursor Down"),
        ("CL",  "Cursor Left"),
        ("CR",  "Cursor Right"),
        ("RD",  "Roll Down"),
        ("RU",  "Roll Up"),
        ("NX",  "NEXT[]"),
        ("PV",  "PREV[]"),
    ]
    for mnemonic, desc in cursor_tests:
        gpib.write(mnemonic)
        time.sleep(0.15)
        info(f"{mnemonic}; ({desc}) sent")

    ok("All cursor mnemonics sent (check instrument display for response)")


def test_display_read(gpib, analyzer):
    hdr("5. DR — Display Read")

    # Navigate to SYSTEM menu for predictable content
    info("Navigating to SM (System menu)…")
    gpib.write("SM")
    time.sleep(0.5)

    # Read first row, first 64 chars
    info("DR 1 1 64; (first row of display)…")
    raw = send_and_read_binary(gpib, "DR 1 1 64", delay=0.5)
    if raw:
        # Strip inverse-video bit (bit 7) and trailing CR+LF
        plain = bytes(b & 0x7F for b in raw)
        text = plain.decode(errors="replace").rstrip("\r\n")
        ok(f"DR 1 1 64 → {len(raw)} bytes "
           f"({len(raw)-64} extra = CR+LF terminator)" if len(raw) > 64
           else f"DR 1 1 64 → {len(raw)} bytes")
        dim(f"  Row 1: {text!r}")
    else:
        fail("DR 1 1 64 → no response")

    # Read full screen
    info("DR 1 1 1472; (full screen)…")
    raw = send_and_read_binary(gpib, "DR 1 1 1472", max_bytes=1600, delay=0.8)
    if raw:
        # Strip inverse-video, trim CR+LF terminator
        plain = bytes(b & 0x7F for b in raw)
        text  = plain.decode(errors="replace").rstrip("\r\n")
        extra = len(raw) - 1472
        ok(f"DR 1 1 1472 → {len(raw)} bytes "
           f"({extra:+d} vs 1472; {extra} = CR+LF terminator)" if extra > 0
           else f"DR 1 1 1472 → {len(raw)} bytes")
        rows = [text[i:i+64].rstrip() for i in range(0, min(len(text), 1472), 64)]
        dim("  Screen content:")
        for i, row in enumerate(rows[:10], 1):
            if row.strip():
                dim(f"  Row {i:2d}: {row}")
    else:
        fail("DR 1 1 1472 → no response")


def test_ke_command(gpib, analyzer):
    hdr("6. KE — Key Echo Buffer")
    info("KE; (with no prior key press — should return '??')…")
    raw, text = send_and_read(gpib, "KE", delay=0.3)
    dim(f"  Raw: {raw.hex()}  Text: {text!r}")
    if "??" in text:
        ok("KE; → '??' (empty buffer as expected)")
    elif text:
        ok(f"KE; → {text!r} (key was buffered)")
    else:
        warn("KE; → no response")

    # Press a known key (SM) and check KE buffers it
    info("Pressing SM (System Menu) then checking KE…")
    gpib.write("SM")
    time.sleep(0.4)
    raw, text = send_and_read(gpib, "KE", delay=0.3)
    dim(f"  KE after SM: {text!r}")
    if text and "SM" in text.upper():
        ok(f"KE correctly echoed SM key: {text!r}")
    elif text and "??" not in text:
        ok(f"KE returned: {text!r} (front-panel key activity detected)")
    else:
        info(f"KE after SM: {text!r} (key may not have been buffered)")


def test_run_stop(gpib, analyzer):
    hdr("7. RN (RUN) and ST (STOP)")

    info("Setting MB 34 (Measurement Complete + Error mask)…")
    gpib.write("MB 34")
    time.sleep(0.2)

    sb_before = gpib.serial_poll()
    info(f"Status before RN: 0x{sb_before:02X}  "
         f"MEAS_COMPLETE={bool(sb_before&0x02)}  NOT_BUSY={bool(sb_before&0x10)}")

    info("Sending RN; (RUN)…")
    gpib.write("RN")
    time.sleep(0.5)

    sb_after_rn = gpib.serial_poll()
    info(f"Status after RN:  0x{sb_after_rn:02X}  "
         f"MEAS_COMPLETE={bool(sb_after_rn&0x02)}  NOT_BUSY={bool(sb_after_rn&0x10)}")

    if sb_after_rn != sb_before:
        ok("Status byte changed after RN → instrument is responding to RN")
    else:
        warn("Status byte unchanged after RN — may need a valid clock source to trigger")

    # Poll for 5 seconds to see if measurement completes
    info("Polling for Measurement Complete for 5 seconds…")
    deadline = time.time() + 5
    completed = False
    while time.time() < deadline:
        sb = gpib.serial_poll()
        if sb & 0x02:
            ok(f"Measurement Complete bit set! Status: 0x{sb:02X}")
            completed = True
            break
        time.sleep(0.5)
    if not completed:
        warn("Measurement Complete not seen in 5 s — instrument may be waiting for trigger")
        info("This is normal if no valid clock/data is connected to the probes")

    info("Sending ST; (STOP)…")
    gpib.write("ST")
    time.sleep(0.3)
    sb_stop = gpib.serial_poll()
    info(f"Status after ST:  0x{sb_stop:02X}")
    ok("ST; sent — check instrument display for STOP confirmation")


def test_group_execute_trigger(gpib, analyzer):
    hdr("8. GET — Group Execute Trigger (++trg)")

    info("Setting MB 34 (Measurement Complete + Error mask)…")
    gpib.write("MB 34")
    time.sleep(0.2)

    sb_before = gpib.serial_poll()
    info(f"Status before ++trg: 0x{sb_before:02X}")

    info("Sending ++trg (Group Execute Trigger)…")
    gpib._raw_write(f"++addr {gpib.gpib_addr}")
    gpib._raw_write("++trg")
    time.sleep(0.5)

    sb_after = gpib.serial_poll()
    info(f"Status after ++trg:  0x{sb_after:02X}")
    if sb_after != sb_before:
        ok("Status changed after ++trg → GET is working")
    else:
        warn("Status unchanged — may need clock/data on probes for measurement to complete")

    # Stop again
    gpib.write("ST")
    time.sleep(0.3)


def test_learn_strings(gpib, analyzer, download=True):
    hdr("9. Learn String Commands (TC, TS, TT)")

    if not download:
        info("Skipping binary download (use --download to enable)")
        return

    # TC — Transmit Configuration
    info("TC; (Transmit Configuration — expect ~5145 bytes)…")
    raw = send_and_read_binary(gpib, "TC", max_bytes=6000, delay=1.0)
    if len(raw) >= 4:
        header = raw[0:2].decode(errors="replace")
        count  = struct.unpack(">H", raw[2:4])[0]
        ok(f"TC → {len(raw)} bytes  header={header!r}  count={count}")
        if header == "RC":
            ok("Header is 'RC' (Receive Configuration) — correct")
        else:
            warn(f"Unexpected header: {header!r} (expected 'RC')")
        if abs(len(raw) - 5145) < 20:
            ok(f"Length {len(raw)} is close to expected 5145")
        else:
            warn(f"Length {len(raw)} differs from expected 5145")
    else:
        fail(f"TC → only {len(raw)} bytes received")

    # TT — Transmit Timing
    info("TT; (Transmit Timing — size depends on memory used)…")
    raw = send_and_read_binary(gpib, "TT", max_bytes=16384, delay=1.5)
    if len(raw) >= 4:
        header = raw[0:2].decode(errors="replace")
        count  = struct.unpack(">H", raw[2:4])[0]
        ok(f"TT → {len(raw)} bytes  header={header!r}  count={count}")
        if header == "RT":
            ok("Header is 'RT' (Receive Timing) — correct")
        else:
            warn(f"Unexpected header: {header!r} (expected 'RT')")
        info_dict = LearnStringParser.parse_timing_header(raw)
        if info_dict.get("valid"):
            n_ch     = info_dict.get("timing_channels", 0)
            n_states = info_dict.get("valid_states", 0)
            dim(f"  Timing channels : {n_ch}")
            dim(f"  Valid states    : {n_states}")
            dim(f"  Sample period   : {info_dict.get('sample_period_str')}")
            dim(f"  Runs            : {info_dict.get('runs')}")
            if n_ch == 0 and n_states == 0:
                info("  Zero channels/states is normal when no timing pods are")
                info("  configured (FM → Timing Format) and no probes are connected.")
                info("  Set up the format and connect probes, then TT will contain data.")
            elif n_states > 0:
                ok(f"  {n_states} timing states captured — data is present")
    else:
        fail(f"TT → only {len(raw)} bytes")

    # TS — Transmit State
    info("TS; (Transmit State — size depends on capture)…")
    raw = send_and_read_binary(gpib, "TS", max_bytes=65536, delay=2.0)
    if len(raw) >= 4:
        header = raw[0:2].decode(errors="replace")
        count  = struct.unpack(">H", raw[2:4])[0]
        ok(f"TS → {len(raw)} bytes  header={header!r}  count={count}")
        if header == "RS":
            ok("Header is 'RS' (Receive State) — correct")
        else:
            warn(f"Unexpected header: {header!r} (expected 'RS')")
    else:
        fail(f"TS → only {len(raw)} bytes")


def test_utility_commands(gpib, analyzer):
    hdr("10. Utility Commands (BP, CH, DB, PU)")

    for cmd, desc in [("BP", "Beep"), ("CH", "Cursor Home"), ("DB", "Display Blank")]:
        gpib.write(cmd)
        time.sleep(0.3)
        ok(f"{cmd}; ({desc}) sent — check instrument for effect")

    # PU — power-up defaults
    info("PU; (Power-Up Defaults)…")
    gpib.write("PU")
    time.sleep(0.5)
    sb = gpib.serial_poll()
    info(f"Status after PU: 0x{sb:02X}")
    ok("PU; sent")

    # Restore mask (PU doesn't clear it per manual)
    gpib.write("MB 34")
    time.sleep(0.1)
    ok("MB 34 re-applied after PU")


def test_invalid_commands(gpib, analyzer):
    hdr("11. Invalid Commands (verify error detection)")
    # These should generate Error in Last Command (bit 5 = 0x20)

    gpib.write("MB 34")
    time.sleep(0.1)

    invalid = [
        ("MENU WAVEFORM", "old incorrect command"),
        ("START",          "was used incorrectly for RN"),
        ("SLIST",          "does not exist"),
        ("CONFIG",         "does not exist"),
        ("MASK 48",        "was used incorrectly for MB"),
    ]
    for cmd, note in invalid:
        gpib._drain(0.05)
        gpib.write(cmd)
        time.sleep(0.3)
        sb = gpib.serial_poll()
        error_set = bool(sb & 0x20)
        status = (f"{C.RED}ERROR bit set{C.RESET}" if error_set
                  else f"{C.DIM}no error bit (may be silently ignored){C.RESET}")
        print(f"  {cmd:<20} ({note:<35}) → status 0x{sb:02X}  {status}")


# ── Main ───────────────────────────────────────────────────────────────────

def test_acquisition_cycle(gpib, analyzer):
    hdr("12. Full Acquisition Cycle  (RN → poll → TT)")
    info("This test shows the complete data-capture workflow.")
    info("If no probes are connected, Measurement Complete will not fire")
    info("until the instrument times out or is triggered manually.")

    info("Setting MB 2; (Measurement Complete only — minimal mask)…")
    gpib.write("MB 2")
    time.sleep(0.2)

    # Clear any previous Measurement Complete by reading status
    sb = gpib.serial_poll()
    info(f"Status before RN: 0x{sb:02X}  MC={bool(sb&0x02)}")

    info("Sending RN; (RUN)…")
    gpib.write("RN")
    time.sleep(0.3)

    sb_running = gpib.serial_poll()
    info(f"Status during run: 0x{sb_running:02X}  "
         f"MC={bool(sb_running&0x02)}  NOT_BUSY={bool(sb_running&0x10)}")
    if not (sb_running & 0x02):
        ok("Measurement Complete cleared — instrument is actively running")
    else:
        info("Measurement Complete still set — no new run started yet")

    info("Polling for Measurement Complete (10 second window)…")
    info("→ To trigger: press RUN on the front panel or connect a clock source")
    deadline = time.time() + 10
    completed = False
    while time.time() < deadline:
        sb = gpib.serial_poll()
        if sb & 0x02:
            ok(f"Measurement Complete! Status: 0x{sb:02X}  "
               f"Runs≈{10 if sb&0x10 else '?'}")
            completed = True
            break
        remaining = int(deadline - time.time())
        print(f"  Waiting... {remaining}s  0x{sb:02X}", end="\r", flush=True)
        time.sleep(0.5)
    print()

    if completed:
        info("Downloading TT learn string…")
        raw = send_and_read_binary(gpib, "TT", max_bytes=65536, delay=1.5)
        if len(raw) >= 4:
            hdr2 = raw[0:2].decode(errors="replace")
            cnt  = (raw[2] << 8) | raw[3]
            info_d = LearnStringParser.parse_timing_header(raw)
            ok(f"TT → {len(raw)} bytes  header={hdr2!r}  "
               f"states={info_d.get('valid_states')}  "
               f"channels={info_d.get('timing_channels')}")
            fname = f"cycle_test_timing.lrn"
            with open(fname, "wb") as f: f.write(raw)
            ok(f"Saved: {fname}")
        else:
            warn(f"TT returned only {len(raw)} bytes")

        info("Reading display after capture (DR 1 1 1472)…")
        time.sleep(0.3)
        raw_dr = send_and_read_binary(gpib, "DR 1 1 1472", max_bytes=1600, delay=0.8)
        if raw_dr:
            plain = bytes(b & 0x7F for b in raw_dr).decode(errors="replace").rstrip("\r\n")
            rows = [plain[i:i+64].rstrip() for i in range(0, len(plain), 64)]
            ok(f"Screen read: {sum(1 for r in rows if r.strip())} non-empty rows")
            for i, r in enumerate(rows[:6], 1):
                if r.strip(): dim(f"  Row {i:2d}: {r}")
    else:
        info("Acquisition did not complete in 10 s (no probes/clock connected)")
        info("To test a full cycle: connect probes, configure format via FM;,")
        info("set up timing via TM;, then rerun with  --tests 12")

    gpib.write("ST")
    time.sleep(0.2)
    ok("ST; sent (STOP)")


def main():
    p = argparse.ArgumentParser(
        description="HP 1631A/D systematic command probe",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("--port",     required=True, help="COM port (e.g. COM9)")
    p.add_argument("--addr",     type=int, default=4, help="GPIB address (default 4)")
    p.add_argument("--timeout",  type=float, default=8.0)
    p.add_argument("--eos",      type=int, default=1,
                   help="EOS terminator: 0=CR+LF 1=CR 2=LF 3=None (default 1)")
    p.add_argument("--download", action="store_true",
                   help="Also download binary learn strings (TC, TS, TT)")
    p.add_argument("--tests",    nargs="*",
                   help="Run only specified test numbers, e.g. --tests 1 5 9")
    args = p.parse_args()

    print(f"\n{C.BOLD}HP 1631A/D Command Probe{C.RESET}")
    print(f"Port={args.port}  Addr={args.addr}  EOS={args.eos}\n")

    try:
        gpib = PrologixGPIB(args.port, args.addr,
                             timeout=args.timeout, eos=args.eos)
        analyzer = HP1631A(gpib)
    except Exception as e:
        print(f"{C.RED}Could not open {args.port}: {e}{C.RESET}")
        sys.exit(1)

    all_tests = [
        (1,  "Identification",         lambda: test_identification(gpib, analyzer)),
        (2,  "Mask & Status Byte",     lambda: test_mask_and_status(gpib, analyzer)),
        (3,  "Menu Navigation",        lambda: test_keyboard_mnemonics(gpib, analyzer)),
        (4,  "Cursor Navigation",      lambda: test_cursor_navigation(gpib, analyzer)),
        (5,  "Display Read (DR)",      lambda: test_display_read(gpib, analyzer)),
        (6,  "KE Key Echo",            lambda: test_ke_command(gpib, analyzer)),
        (7,  "RN / ST Acquisition",    lambda: test_run_stop(gpib, analyzer)),
        (8,  "GET (++trg)",            lambda: test_group_execute_trigger(gpib, analyzer)),
        (9,  "Learn Strings",          lambda: test_learn_strings(gpib, analyzer, args.download)),
        (10, "Utility Commands",       lambda: test_utility_commands(gpib, analyzer)),
        (11, "Invalid Commands",       lambda: test_invalid_commands(gpib, analyzer)),
        (12, "Full Acquisition Cycle", lambda: test_acquisition_cycle(gpib, analyzer)),
    ]

    selected = (set(int(x) for x in args.tests) if args.tests
                else set(n for n, _, _ in all_tests))

    try:
        for num, name, fn in all_tests:
            if num not in selected:
                continue
            reset_instrument(gpib, analyzer)
            try:
                fn()
            except Exception as e:
                fail(f"Test {num} ({name}) exception: {e}")
                import traceback
                traceback.print_exc()

        print(f"\n{C.BOLD}{C.GREEN}Probe complete.{C.RESET}")
        print("Review the output above and compare with chapter10_findings.md")
        print("to determine which commands are working and which need adjustment.\n")

    finally:
        gpib.close()


if __name__ == "__main__":
    main()
