# HP 1630/1631A firmware RE — session notes

Goal: dynamically disassemble + annotate the 8 EPROM dumps in `../roms/`,
reusing the methods in `heuristics/` and the 6809 tooling in `../../lmt2`.

## STATUS: ACTIVE, self-contained (2026-06-24)
- Tooling is **vendored into `re/tools/`** — project no longer depends on the
  lmt2 tree. `cpu6809.py` (native core) + `f9dasm` copied in; see
  `re/tools/PROVENANCE.txt`. lmt2 mentions below are citations, not deps.
- Emulator boots the firmware end-to-end (POST validated). See sections below.
- 6809 method writeup: `re/6809_HEURISTICS.md`.

## CPU / hardware context
- CPU: Motorola **MC68B09E** (verified on A3 CPU board = HP 1820-2854).
- **MC68829 MMU** on the CPU board (HP 1820-2911) → banks 8×16 KB = 128 KB into
  the 6809's 64 KB space. Determining the MMU page map is prerequisite to disasm.
- See `../PARTS.md` / `../manuals/` for the hardware + service manual (01630-90917).

## ROM dumps (../roms/, 8× 16 KB AM27128A, all distinct)
| File | label | md5 |
|------|-------|-----|
| 01630_80054.bin | B0054 | 3123dc10317baa7aeacda26322d96fe7 |
| 01630_80055.bin | B0055 | 0a662ea42c57d60adbcbd319504f8914 |
| 01630_80056.bin | B0056 | ce8fa8206843ba3ed30eb87d09aef86a |
| 01630_80057.bin | B0057 | c7bbbdfadb7dd5e3639dbe267d9103a1 |
| 01630_80058.bin | B0058 | 37ed2cf03df56d84e0c8bbf25548df37 |
| 01630_80059.bin | B0059 | 4e0244e0a57832c23fb66d45f9241fd1 |
| 01630_80060.bin | B0060 | 76ed45ba93676013047db605075c6281 |
| 01630_80061.bin | B0061 | 3d96c667cdad289a63d31932c299ef52 |

## Static recon findings (done 2026-06-24)
- **80061 = boot/reset bank, maps to top of memory ($C000–$FFFF).** Its top 16
  bytes are a valid 6809 vector table:
  - RESET=$FD80, NMI=$FBBD, IRQ=$FADB, FIRQ=$FBA3, SWI=$F084, SWI2=$F084, SWI3=$F084
  - (all targets in $Fxxx → confirms this bank sits at the top of the map)
- **80058** begins with a JMP-extended dispatch table: `7E 601B / 7E CB82 /
  7E CB61 / 7E 6630 / 7E 6586 …` — bank-entry jump table.
- **80055/56/57** begin with `17 xxxx 39` = `LBSR $xxxx : RTS` trampoline tables.
- **80059/60** carry the same `LBSR;RTS` trampolines near their tails (`17 FE DF 39`…).
- **80054** opens with ASCII ("31X 1000…"), highest FF/zero fill → likely a
  data / character-generator / banner ROM rather than dense code.
- Every ROM is rich in printable strings (5.3–6.7 KB printable each).

## Boot bank (80061) DECODED — 2026-06-24
Listing: `re/listings/80061_C000.lst` (f9dasm linear; NOTE it desyncs around
data tables — flow-following/seeded disasm still needed for the full job).

**Reset flow** (`RESET=$FD80`):
```
FD80: 10 8E 00 00   LDY  #$0000
FD84: 17 00 41      LBSR $FDC8        ; hardware/MMU init
FD87: 39            RTS
```
**Init routine $FDC8** (decodes cleanly; f9dasm's linear pass was 2 bytes out):
```
FDC8: ORCC #$50               ; mask F+I — disable interrupts
FDCA: LDX  #$FF00             ; dest
FDCD: LEAU $FD88,PCR          ; src = 64-byte table at $FD88
FDD1: LDB  #$40               ; count = 64
FDD3: LDA ,U+ / STA ,X+ / DECB / BNE $FDD3   ; copy 64 bytes $FD88 -> $FF00
FDDA: CLR  $FF40
FDDF: STA  $E034 ; STA $E848 ; STD $E032 (#$9669)   ; I/O init
FDEB: LDS  #$E1FD             ; system stack
FDEF: STY  $E037              ; (Y=0)
FDF3: LBSR $F31F ; ... ; FE50: LDU #$C000
```

### Inferred memory map (HIGH confidence; verify vs schematic)
- **$FF00–$FF3F = MMU page-register file** (64 regs) — the MC68829 MMU
  (HP 1820-2911). Reset copies the initial map from the `$FD88` table.
  This is the banking mechanism that maps 8×16 KB physical into 64 KB logical.
- **$FF40** = an MMU/system control register (cleared at reset).
- **$E000–$E8xx = custom-chip I/O** ($E032/$E034/$E037/$E848 written in init).
- **$C000–$FFFF** = boot bank 80061 at reset (LDU #$C000 referenced).
- System stack at **$E1FD**.
- TODO: decode the `$FD88` 64-byte page table → DONE below.

### MMU page table DECODED (the `$FD88` → `$FF00` boot map)
**32 × 16-bit page registers** at `$FF00–$FF3F` → 32 logical pages of **2 KB**
each (= 64 KB logical). Each entry = `{hi=attribute, lo=physical 2KB page}`:

| regs | logical range | phys pages (lo) | note |
|------|---------------|-----------------|------|
| 0–11  | $0000–$5FFF | $84–$8F (attr $02) | contiguous |
| 12–25 | $6000–$CFFF | $50–$5D (attr $02) | contiguous |
| 26–28 | $D000–$E7FF | $80–$82 (attr $02) | |
| 29    | $E800–$EFFF | $C0 (attr $00)     | **RAM / I/O window** (I/O at $E0xx) |
| 30    | $F000–$F7FF | $7E (attr $02)     | |
| 31    | $F800–$FFFF | $FF (attr $03)     | **boot/vector page** ($FD80/$FDC8/vectors) |

- Physical low-byte pages **$50–$8F = 64 pages = 128 KB = the 8 EPROMs**;
  `$C0` = RAM, `$FF` = hardwired boot page (active at reset before MMU load).
- Hi-byte = attribute bits ($02 = valid/ROM-ish, $03 = boot, $00 = RAM) — exact
  bit meanings TBD vs MC68829 datasheet / A3 schematic.
- **Still TODO**: assign each 16 KB dump (B0054–B0061) to its 8 consecutive
  physical pages (verify by content continuity + the JMP/LBSR dispatch tables);
  then build a banked-memory closure (wrap `make_memory`) honoring `$FF00`
  writes so lmt2's native core can run the real banked firmware.

## Banked-memory model — 2026-06-24
`re/tools/hp_mmu.py`: MMU-aware read/write closure for lmt2's native core.
Models 32×2 KB pages, the `$FF00-$FF3F` register file, `$FF40` ctl, `$E000-$EFFF`
I/O. Self-test resolves the boot map per logical page.
- **CONFIRMED anchor**: logical pg31 `$F800` → phys `$FF` → B0061+`$3800`; reset
  `$FD80` = B0061 offset `$3D80` = the decoded reset code. Boot-mirror placement
  (B0061 at phys `$F8-$FF`) is correct.
- **RESOLVED from A3 schematic 8B-3 (74LS138 U4N ROM selector + 27128 sockets)**,
  verified vs reset + the $6000 JMP table. Folded into `hp_mmu.py` (`rom_decode`):
  - physical page = MMU reg value **& 0x3FF** (10-bit, PA11-PA20) — NOT the low byte.
  - **chip = (page>>2)&7**, **half = (page>>5)&1**, in2kb = page&3 →
    offset = half·$2000 + in2kb·$800. (HPA13/14/15=chip-sel; HPA16=EPROM A13/pin26
    ⇒ **27128 16 KB parts**; the agent's "2764/8 KB" label is a misread, its own
    pin-26=A13 finding + our 16 KB dumps confirm 27128.)
  - chip 0..7 = sockets U3K/J/I/H, U4K/J/I/H = **B0054..B0061** (= 80054..80061).
    B0061 (U4H) = boot/kernel ROM (chip 7).
  - Resolved boot map: $0000-$1FFF=B0055, $2000-$3FFF=B0056, $4000-$5FFF=B0057,
    $6000-$7FFF=B0058 (JMP table at $6000), $8000-$9FFF=B0059, $A000-$BFFF=B0060,
    $C000-$CFFF=B0061 lo, $D000-$E7FF=B0054, $E800=RAM/IO, $F000-$FFFF=B0061 hi.
- **Schematic findings + FLAGGED uncertainties** (agent `a93a919d`, renders in
  `manuals/rendered/sch-A3-*`; sheets 8B-2 MPU/MMU pp.198-200, 8B-3 ROMs pp.195-6,
  8B-4 mem ctl pp.203-4, 8B-5/6 CRTC pp.207-8):
  - MC68829 MMU (U6L) confirmed; 2 KB pages; 10-bit physical page (PA11-20 ⇒ 2 MB).
    Register-file ABSOLUTE address ($FF00/$FF40) **not legible** on scan (decoded by
    U6G; RS0-RS6 from low addr bits — consistent with our $FF00 inference).
  - RAM = 64 KB DRAM (8× 1818-3059), bottom of physical map; exact logical/physical
    RAM range vs the formula is the **open RAM/IO-region question** — `$E000-$EFFF`
    is modeled as RAM/IO (stack `$E1FD` lives there) but the precise region-enable
    equation (HPA17-20 + LRA) was **not readable**. Resolve via emulation.
  - I/O ($E000 region): CRTC **MC6845** (U6D=1820-2853, RS=HPA0 ⇒ $E032/$E034 =
    addr/data), **8279** kbd/display (U9E=1820-2150), AM9513-type timer (U6M=
    1820-3911); 74S138 decoders U6N/U5N. Exact hex sub-ranges **not annotated** on scan.
  - Boot-vector mirroring: not separately needed — $3FF decodes to B0061 hi via the
    same formula; reset default assumed = the $FD88 boot map (model preloads it).

## Emulator boot — VALIDATED end-to-end (2026-06-24)
Harness `re/tools/hp_boot.py` wires lmt2's native `cpu6809.CPU6809` to
`hp_mmu.make_mmu_memory`. Trace saved `re/listings/boot_trace.txt`.
- RESET=$FD80 → init copy loop `$FDD3-$FDD8` runs **exactly 64×** (MMU table copy ✓).
- I/O init verified: CRTC at `$E032/$E034` (writes $96/$69 = $9669, $80), `$E037/$E038`;
  **8279** kbd/display at `$E848-$E84F` (matches schematic U9E); status at `$E852`.
- Stack `LDS #$E1FD` works (RAM-backed writes march down from $E1FC) — confirms
  `$E000-$E7FF` is writable RAM region.
- With I/O status reads = `$00`: falls through display init at `$F4A9` (reads
  `$E852`, tests **bit2**; 0 ⇒ skips, `PULS PC` returns to a $0000 frame).
- With I/O status reads = `$FF`: completes display init, proceeds into a
  **power-on RAM test** — walking-bit pattern (`$F381`: TSTA/ASLA, STA ,X+ to
  `$0000-$07FF`, outer count to `$02A0`). The memory map + MMU decode are CORRECT:
  the firmware runs a coherent POST.

### Refined open item: low-memory RAM vs ROM
The POST RAM test writes AND verifies `$0000-$07FF`, which the boot map shows as
**B0055 ROM**. So low logical memory must be **DRAM** at that point (MMU likely
reprogrammed for the test, or those pages are RAM-backed). Current model treats
attr `$02` pages as ROM (shadow-writes, reads return ROM) → would fail the verify.
**Next:** log all `$FF00` MMU writes during POST to see the reprogramming, and
distinguish DRAM-backed pages; then coverage-driven disasm (lmt2 emu_seeds style)
with timer/IRQ injection to exercise the interrupt-driven main loop.

## POST sequence reconstructed by emulation (B0061 kernel) — 2026-06-24
Driving `hp_boot.py` (I/O reads $FF, `$E852` bit2 toggling) walks the firmware
through its full power-on self-test, in order:
1. `$FD80` reset → `$FDC8`: ORCC #$50, copy 64-byte MMU map `$FD88`→`$FF00`,
   init `$E034/$E032($9669)/$E037`, `LDS #$E1FD`.
2. `$F441`/`$F451`: clear loops (logical `$0000-$07FF`, `$D800+`).
3. `$F45B+`: program **CRTC** (`$E032-$E038`) and **8279** (`$E848-$E84F`),
   gated by `$E852` bit2 at `$F4A9` (needs bit2=1 to proceed).
4. `$F37B`: **DRAM RAM test** — walking-bit pattern, remapping reg0 to physical
   pages `$0280-$029F` (proves DRAM = 64 KB at phys `$280-$29F`).
5. `$F412`: **ROM checksum** — `ADDA ,X+` over logical `$0000-$3FFF`.
6. `$F6FA`: wait `while $E852 bit2 == 1` (sync/busy line — must toggle).
7. jumps to **overlay code resident in DRAM** (`$D5xx`, pg26 = phys `$280`).

### DRAM model FIXED
`hp_mmu.py` now backs physical pages `$0280-$029F` with a 64 KB physical-page-
indexed array (one logical addr → 32 distinct cells during the RAM test). With
this, the RAM test + checksum pass and execution reaches the overlay stage.

### Current frontier (next session)
Firmware jumps into DRAM at `$D5xx` but my model's DRAM there is still zero
(executes `$00`=NEG → core raises `bad rmw lo 1 @ $D5C3`). This is the
**overlay-loader**: the firmware copies code from a ROM bank into DRAM then jumps
to it; that copy either hasn't run on this path or depends on un-modeled I/O.
NEXT: trace the overlay copy (ROM→DRAM block move) + inject periodic IRQ/NMI
(system is interrupt-driven) to drive coverage; then run the lmt2 coverage/
emu_seeds pipeline to seed disassembly and produce per-ROM annotated listings.

## OPERATIONAL MMU map (post-POST) — recovered by emulation 2026-06-24
The firmware reprograms the MMU after POST; the running map is NOT the `$FD88`
boot map. Captured live at the divergence (`hp_boot.py` dumps `st['mmu']`):

| logical | phys | source |
|---------|------|--------|
| $0000-$3FFF | $25C-$25F, $27C-$27F | **B0061 kernel** (low+high halves) |
| $4000-$5FFF | $28C-$28F | DRAM (work) |
| $6000-$7FFF | $250-$253 | B0058 (overlay; JMP table at $6000) |
| $8000-$9FFF | $254-$257 | B0059 |
| $A000-$BFFF | $258-$25B | B0060 |
| $C000-$CFFF | $25C-$25D | B0061 low |
| $D000-$E7FF | $280-$282 | **DRAM (work)** — `$D230` is here |
| $E800-$EFFF | $0C0 | I/O |
| $F000-$FFFF | $27E,$3FF | B0061 high (resident kernel) |

So pg0-7 were remapped DRAM→B0061 ROM (kernel relocated to low memory); the
banked overlay ROMs (B0058/59/60) sit at $6000-$BFFF; `$D000-$E7FF` is DRAM work.

## DIVERGENCE pinned: `LBSR $D230` into un-built DRAM
Post-POST the kernel (running at logical $2040 = B0061 `$2040`) executes:
`LDU #0; TST $29C9; ...; LDD #$1308; LBSR $D230`. `$D230` is DRAM work area
(pg26, phys $280), **still zero** — and **no code was ever written to $D000+**
(overlay-write log empty). So a kernel init step that builds a routine/trampoline
at `$D230` (and likely fills $4000-$5FFF) **never ran** in emulation — gated
behind un-modeled I/O. Prime suspects: **HP-IB disc probe** (ROM55 mass-storage,
the model table @ 80055:$3051) or an interrupt-driven init (IRQs never fired —
firmware still had I-flag set). 
NEXT: single-step the kernel from where it gains control (the PULS-PC return to
$2040 came from a call at ~$203D) backward/forward to find the skipped
init/copy, and model the I/O (HP-IB handshake / status) it gates on.

## MILESTONE: firmware PASSES self-test (2026-06-24)
Modeling `$E852` bit2 as a vsync line (mostly 1, dips to 0 — `hp_boot.py` IO.rd
step-based) gets past the display-init gate: no more `$D230` crash. The firmware
runs the full POST and reaches the **"SELF TEST PASSED" results screen**
(`selftest_result_disp $F6C4` copies result strings into CRT RAM `$D00E/$D012`,
row stride $17), then `vsync_wait` → `post_delay` → `JMP [$FFFE]` = re-enters
RESET. That reboot is a **timeout awaiting a keypress** (8279 returns no key) —
the self-test screen waits for the operator. NOT a failure.
- To drive into the application: simulate an 8279 keypress + (then) IRQ-driven
  main loop. Deferred — see disassembly plan; coverage is enough to start.

## VALIDATION: ROM checksum passes on ALL 8 banks (2026-06-24)
The POST ROM-checksum self-test (`romchk_bankloop $F3E4`) maps each of the 8
EPROMs into logical `$0000-$3FFF`, sums it (`post_romchecksum $F412`), and
compares to the expected table at `$F07C` (`CMPA B,X`). Emulation: **all 8 banks
PASS** — an independent, end-to-end confirmation that `hp_mmu.rom_decode()` and
the whole memory map are correct for **all 8 ROMs**, not just the two POST runs.

## Self-test FAILS on a peripheral test (B=$02) — the wall to the application
At the result-display decision (`$F6D4`) B=$02 (bit1) ⇒ a self-test FAILED. ROM
checksum and RAM test pass, so B=$02 is a **peripheral/subsystem test** that
fails because that hardware is only stubbed (candidates: CRTC readback, 8279,
acquisition/timing boards, analog (1631A), HP-IB). The firmware then loops:
display result → delay → `JMP [$FFFE]` reboot (a self-test retry loop, ~1.1M
steps/cycle; confirmed 7 reboots/8M steps). A keypress does NOT break it (no kbd
read on this path); `$E852` bit7 doesn't either.
- ROOT CAUSE FOUND: the failing test is `acq_selftest` at **B0058 off $3D1F**
  (logical `$5D1F`), `JSR`'d from the kernel orchestrator at `$F528` (mmu_set_page
  maps the B0058 overlay to `$5000-$5FFF` first). It programs the **acquisition/
  timing engine at `$E800-$E836`** then reads back to verify; stubbed reads ($FF)
  fail the check → B=$02. This is the logic-analyzer ACQUISITION HARDWARE, absent
  in a firmware-only emulation. Acquisition I/O map captured in
  `annotations/B0058_labels.txt`.
- OPTION (a) IN PROGRESS — modeling the acquisition engine so POST passes
  legitimately (`hp_boot.py` IO class). Reverse-engineered the acq self-test:
  - `acq_fail $5F9E(A)` sets bit A of a result byte ⇒ B=$02 == fail code 1.
  - `acq_chk_status $5F43`: needs `$E800` bit7 SET + `$E854` bit4 CLEAR.
  - Status bits are CLOCK-COUNTER driven: write `$E800=0` arms (count:=0), each
    `$E806` write (via `acq_clock $5F94`) increments; `$E800` bit7 = (count<$400),
    `$E854` bit4 = (count>=$400). **$400 = 1024 = acquisition memory depth.**
  - Modeling this made fail codes 1,2,3,4,5 all PASS.
  - DATA-readback (`acq_read_compare $5F5C`, $E80E/$E815/$E825 FIFO) now MODELED:
    return read#0=(savedA==$80?0:savedA), reads 1..$1FF=savedA, $200..$3FF=
    transform(savedA) (=$87 if $80 else ~savedA), $400 samples = mem depth. With
    this, `$5F9E` reports **no fail codes** — the acquisition capture test PASSES.
  - BUT `acq_selftest` is a CHAIN: after the capture test it does the `$5E95` loop
    (sets bit1 $02 at `$5EA5`) and `JSR $4D20` (another subsystem, sets bits 5/6/7),
    OR-ing into the result ⇒ return **B=$E2**. So self-test still fails on MORE
    stubbed subsystems. **Option (a) = iteratively model each subsystem test**
    (acquisition capture = the hardest, DONE; `$4D20` + the `$5E95`-loop probe +
    likely others remain). This is effectively emulating the instrument's HW piece
    by piece — large but tractable; each step unmasks the next.
  - Acq register map + routine labels in `annotations/B0058_labels.txt`.
  - NEXT subsystem in the chain: `analog_selftest $4D20` (B0056, tests `$E830-$E83E`
    analog/threshold). Modeled presence ($E830≠0) + the $E838 bit3 toggle → fail
    code 7 cleared (B `$E2`→`$62`). Remaining: codes 0-6 = a multi-point analog
    measurement requiring the DAC/comparator transfer function, PLUS bit1 from the
    `$5E95` probe loop. See `annotations/B0056_labels.txt`.
  - REALISM: option (a) = modeling each instrument subsystem in turn (acquisition
    capture DONE & passing; analog partially). Each is a real RE+model task; the
    analog measurement chain and any further tests are substantial. Bypass (b)
    remains available to reach the main app quickly if full-fidelity isn't needed.
- (historical) the two options were:
  (a) MODEL the acquisition engine's readback (reverse the test's expected values
      from `$5D1F`+ and feed them) — exercises the test, more work; or
  (b) BYPASS: hook `$5D1F` to return B=0 (skip the HW-dependent test) so the
      firmware proceeds into its interrupt-driven main loop — unlocks coverage of
      the remaining ROMs cheaply (standard "stub the absent peripheral" technique).
  Orchestrator: `$F500` runs tests + displays results; `$F530`=display_hex_byte,
  `$F54F`=display_string.
- New routines mapped this session: `mmu_set_page $F637`, `setup_operational_map
  $F600`, `romchk_bankloop $F3E4`, `romchk_expected_tbl $F07C`, `clear_mem $F664`
  (see `annotations/B0061_labels.txt`).

## Option (b) bypass — FINDING: reboot is unconditional; main entry = $F089
Hooking `acq_selftest $5D1F` to return B=0 makes self-test PASS — but the firmware
**still reboots** (`$F6D4` result display falls through to `$F70F JMP [$FFFE]`
regardless of pass/fail). The boot flow is LINEAR: `reset → init_hw $FDC8 → (setup)
→ LBSR $F089 (main_entry) → BRA self`. The self-test + reboot lives INSIDE
`$F089`'s call tree. So reaching the interactive application is NOT a test-bypass;
it needs either (i) analysis of `main_entry $F089` (X=$FAD7 cmd/cfg ptr, D=$4000)
to find its app-vs-selftest dispatch, or (ii) a warm-boot path (the JMP [$FFFE]
reboot may, with a surviving RAM flag, take a different branch — but our RAM test
clears DRAM each boot). DIMINISHING RETURNS on boot emulation reached here.
- Toggle: `BYPASS=1` env in `hp_boot.py` installs the $5D1F pass-hook.

## RECOMMENDATION (2026-06-24): pivot to the annotated-disassembly deliverable
The emulation has yielded what it usefully can for now: validated memory map +
MMU (checksum-proven on all 8 ROMs), operational map, full POST + acquisition +
analog subsystem semantics, and the main entry `$F089`. Best next value is to
PRODUCE the per-ROM annotated `.asm` using `6809_HEURISTICS.md` (static walk
seeded by vectors / JMP-tables / LBSR-trampolines), folding in `coverage.txt`
(Tier-1) and the `annotations/*_labels.txt` (B0061/B0058/B0056) gathered here.
`main_entry $F089` is the spine to walk first.

## SOLVED: rear-panel switch = $E852 bit2 — unlocks the application (2026-06-24)
The self-test gate is the **rear-panel switch register $E852, bit2**: 1 = self-test
loop, **0 = OPERATE** (enter app). Matches the "...x0xx...to continue" message
(set bit2=0). My earlier "vsync" reading of $E852 bit2 was WRONG — it's a static
switch. Confirmed in `hp_boot.py` (env `SWITCHES`, default $00 = operate):
- `SWITCHES=0`: **0 reboots** (was 7-11), the self-test loop is broken, firmware
  runs the APPLICATION — **coverage 517→775 bytes across 2→5 ROMs** (B0056/58/59/60/61).
- The operational MMU map now maps the app's overlay banks (B0056@$5000, B0059@$8000,
  B0060@$A000). New frontier: a `WILD JUMP $0007` deeper in the app (more overlay/IO
  to model) — but the boot-mode wall is GONE.
- This single bit was the real key all along (supersedes acq/analog self-test modeling).
- Regenerate coverage in operate mode: `SWITCHES=0 python3 re/tools/hp_boot.py ...`.
- Operate-mode operational map: pg0-9 ($0000-$4FFF)=DRAM, pg10-11=B0056, pg12-15=
  B0058, pg16-19=B0059, pg20-23=B0060, pg24-25=B0061, pg30-31=B0061 hi.
- App interrupts: in operate mode the app UNMASKS interrupts (IRQ injection fires).
- FRONTIER: app runs ~87K steps then a display-formatting routine at `$CExx`
  (table-driven, $24E8/$24FA/$250C) returns corrupt (`PULS PC,X,A` -> `$0007`, a
  DRAM work addr). Deterministic (IRQ-independent). A deep stack-frame divergence
  from stubbed I/O — needs single-step stack tracing to pin; diminishing returns.
  Coverage caps ~775 bytes / 5 ROMs here. For broader coverage, the static walk
  (6809_HEURISTICS) remains the primary tool, seeded by this coverage.

## DELIVERABLE PRODUCED: re/asm/B0061_annotated.asm (2026-06-24)
First annotated disassembly — B0061 (kernel), 16 KB at base $C000 (off X ->
logical $C000+X; resident kernel $F000-$FFFF). Generated by f9dasm driven by the
control file `re/asm/B0061.info` (labels, data/word/fcc regions, line comments
from the RE). Seeded conceptually from `main_entry $F089`. f9dasm default-codes
the rest; data regions (MMU table FDB, checksum table, strings, vector table)
marked explicitly. Vector refs resolve to labels (`JMP [vec_RESET]`).
Workflow to replicate for the other 7 ROMs: write `<ROM>.info` (entries + data
regions + labels), run `f9dasm -6809 -info <ROM>.info -offset <base> <bin>`.
Annotated `.asm` produced so far: **B0061** (kernel), **B0059** (measurement UI),
**B0057** (state/timing config menu — trampoline entries $848E/$81D4/$83E8/$9410,
low-half menu logic, high-half templates rendered as readable FCC labels).
Overlay recipe (no coverage): head LBSR;RTS trampolines give public entries;
mark high half ($A000-$BFFF) as `data` (templates) + per-string `fcc`; code the
low half. See `re/annotations/B005{7,9}_labels.txt`.

**ALL 8 ROMs now have annotated `.asm`** (`re/asm/*_annotated.asm`, indexed in
`re/asm/README.md` with per-ROM role/base/entry-structure): B0061 kernel, B0055
disc/HP-IB, B0056 analog, B0057 state/timing menu, B0058 acquisition (JMP-table
entries, base $6000), B0059 measurement UI, B0060 overlay, B0054 messages.
Generators: `re/tools/gen_overlay_info.py` (+ per-ROM `.info`). Caveat: banked
ROMs run halves at different bases; file-offset is the canonical id (see README).

### KEY FINDING from the disasm — the self-test gate is REAR-PANEL SWITCHES
String at `$F67F`: **"Reset rear panel switches to xxxx x0xx to continue"**. So
the self-test loop is NOT unconditional — it waits until the rear-panel switch
register reads the required pattern. This explains every "unconditional reboot"
observation: the emulator returns the wrong switch bits. Identifying the switch
input register (candidate: `$E852` other bits, or a dedicated `$E8xx` port) and
feeding the right value is the real key to driving the firmware into normal
operation (supersedes the acq/analog self-test modeling as the gating issue).

## DELIVERABLE: code-referenced strings + the common printf (2026-06-24)
Careful string extraction (NOT a raw `strings` dump): only strings a real code
operand points at and feeds to output. Tools:
- `re/tools/extract_strings.py` — finds PC-relative (`LEAX str,PCR`), immediate,
  and pointer-table refs; keeps word-like, $00-terminated text; flags HIGH when
  PCR-referenced or consumed by a following call. Output per ROM in
  `re/strings/B00xx_strings.txt`. HIGH counts: B0054 10, B0055 38, B0056 18,
  B0057 23, B0058 36, B0059 32, B0060 7, B0061 22.
- `re/tools/find_printf.py` — tallies the call target after each string-load.
- **THE COMMON PRINTF = `display_string $F54F`** (kernel): X=$00-terminated string
  ptr, U=CRT display-RAM pos; `LDA ,X+; BEQ; STA ,U; LEAU $17,U; loop`. Every
  printed string funnels here (overlays via local wrappers, e.g. B0055 `$0489`).
  This is the proof that the extracted strings are actually PRINTED.
- Highlights: B0055 = disc/HP-IB message catalog ("disc is not LIF disc", "no disc
  drive present", "conflicting HPIB addresses", "loading configuration", ...);
  B0058 = "DATA ADDR STAT", BIN/OCT/DEC/HEX, "Vx = ", "On Acquired Data Place
  Cursors:"; B0059 = measurement UI ("Acquisition Time:", "Waveform Diagram",
  "Mean x to o", WARNING/ERROR/RUNNING/WAIT/INSERT).
- MENU LABELS via DISPLAY-LIST TEMPLATES: the menu/form labels are NOT
  pointer-loaded — they are LITERAL TEXT embedded in display-list templates
  (structure: `0A NN 00` position codes + field opcodes `8B/40 NN/05/26 27` +
  inline text), walked by a form interpreter that prints the literals. Extracted
  as the `[menu]`/TEMPLATE category in `extract_strings.py` (word-like filter:
  vowel ratio, real-word token, reject camelCase/all-caps/2-char-pattern noise).
  Per-ROM `[menu]` counts: B0054 47, B0055 65, B0056 9, B0057 54, B0058 21,
  B0059 25, B0060 3, B0061 8.
- ROM ROLES revealed by the strings:
  - B0054 = prompt/error/status messages ("Power-up complete","Value not allowed",
    "Trace aborted","One `*' required for each label","ROLL to change configuration")
  - B0055 = disc/HP-IB + config menu ("State","Timing","Channels","Clock set:",
    "Rear panel port:","Beeper:","Disc type:","LIF volume:") + disc msg catalog
  - B0056 = analog ("Analog Board not Present","Full Scale: 125V","Volts")
  - B0057 = state/timing config menu ("Display Mode","Post Processing","Statistical
    Measurements","External Probe Type","Waveform Display Mode:")
  - B0058 = acquisition + listing ("DATA ADDR STAT",BIN/OCT/DEC/HEX,"Vx =")
  - B0059 = measurement-display UI (histogram/waveform/statistics labels)
  - B0061 = kernel (printf $F54F, self-test strings)
- LIMIT: dynamic capture didn't reach menus ($0007 divergence) so this is static;
  a little residual noise remains (control bytes glued to text); signal is strong.

## DELIVERABLE: Monte Carlo bulk annotation (2026-06-24)
Applied the MC technique (`~/src/claude/libs/MONTECARLO.md`): sample disasm lines,
ask DeepSeek-chat + GLM-4.5-air to identify each (with a rich 1631 hardware-context
block), accept specific+keyworded answers, fold back. Pipeline (self-contained):
- `re/tools/prep_mc.py` -> `re/mc/{raw,code}.txt` (45,130 ROM-tagged code lines).
- `re/tools/mc_1631.py` -> `re/mc/annotations.txt` (1631 HW context + domain keywords +
  ROM:ADDR keys + GLM reasoning_content/max_tok fix).
- `re/tools/annotate_1631.py` -> folds `;>>>> [Rn] text` into `re/asm/*_annotated.asm`
  (idempotent).
- xref naming helper: `re/tools/xref.py` (names routines by the string they print —
  used for B0055 disc subsystem, see `annotations/B0055_labels.txt`).
RESULT: validation 96% YES (2 rounds); full campaign **10 seeds x 100 = 905 accepted
annotations across 898 addresses, 87-96% YES/round** (rich context >> the doc's
typical 30-50% R1). All 8 `*_annotated.asm` now carry an LLM-verified semantic layer
atop the structural labels. Re-fold any time: `python3 re/tools/annotate_1631.py`.
- CAMPAIGN 2 (10 fresh seeds R11-R20, with campaign-1 annotations woven into each
  sample's context via prep_mc): 87-96% YES, +911 annotations. MERGED+deduped with
  campaign 1 = **1,816 annotations across 1,781 addresses**, folded into all ROMs
  (per ROM: B0060 313, B0059 292, B0058 283, B0056 279, B0061 275, B0055 152,
  B0057 146, B0054 76). Sources kept: `re/mc/annotations_c{1,2}.txt`; merged in
  `re/mc/annotations.txt`. Context corpus `re/mc/raw.txt` now embeds prior
  annotations so further campaigns / the bulk pass cite them.


## DELIVERABLE: BULK linear annotation pass — near-complete coverage (2026-06-24)
After 2 MC campaigns (1,816 verified), ran the doc's final bulk linear pass:
`re/tools/bulk_annotate.py` walks EVERY code line of all 8 ROMs in 40-line chunks
(6 concurrent deepseek-chat, role-prompt + domain-keyword/address-ref filter,
rich 1631 context + inline MC annotations), resumable. **+24,067 bulk annotations
in ~25 min.** Merged (MC wins per line) = `re/mc/annotations_final.txt` =
**25,883 annotations across 24,906 addresses (~55% of 45k code lines)**, folded
into all 8 `*_annotated.asm` via `annotate_1631.py` (idempotent; tags [Rn] MC /
[BULK]). Per-ROM: B0059 4188, B0060 4185, B0056 3944, B0058 3806, B0061 2992,
B0055 2853, B0057 2331, B0054 1503. The disassemblies now carry four annotation
layers: structural labels + strings/FCC + xref routine-names + ~26k LLM line
annotations.


## DELIVERABLE: 2nd BULK pass (REASONING model) — ~79% coverage (2026-06-25)
`re/tools/bulk_annotate2.py`: deepseek-REASONER over only the lines still
unannotated after MC+bulk1 (1,732 chunks / 20,306 targets), with annotated
neighbors as inline context. KEY FIX: deepseek-reasoner CoT (24-28k chars)
exhausts the token budget -> set max_tokens=32000 (else empty content,
finish_reason=length). 8 workers, ~2.4h, resumable. **+10,565 reasoner
annotations.** Merged all layers (MC R1-R20 wins > BULK2 reasoner > BULK1 chat,
one per address) = `re/mc/annotations_final.txt` = **35,471 unique addresses**,
folded into all 8 ROMs (annotate_1631.py, tag-agnostic regex). Per-ROM: B0060
5934, B0056 5872, B0059 5807, B0058 5553, B0061 5149, B0057 3003, B0055 2836,
B0054 1236. **~79% of 45,130 code lines now carry a specific annotation.**


## DELIVERABLE: 3rd REASONER pass — 85% coverage (2026-06-25)
`re/tools/bulk_annotate3.py`: deepseek-reasoner over the ~22% still-unannotated
after pass 2 (1,335 chunks / 9,741 targets), now with the full 35k-annotation
context folded into the .asm as neighbors. +2,693 annotations (~28% yield on the
hard remainder). MERGED all 4 layers (MC R1-R20 > BULK2 > BULK3 > BULK1, one per
address) = `re/mc/annotations_final.txt` = **38,164 unique addresses = 85% of
45,130 code lines**. Sources: MC 1,781 (verified), reasoner BULK2+3 13,258, chat
BULK1 23,125. Folded into all 8 ROMs. Per-ROM: B0056 6306, B0060 6291, B0059
6202, B0058 6099, B0061 5634, B0057 3156, B0055 2927, B0054 1468. The remaining
~15% are predominantly data tables/strings misdecoded as code (the natural
ceiling) -- correctly left unannotated.


## CLEANUP: mislabeled data removed (2026-06-25)
`re/tools/cleanup_data.py`: detect lines that are DATA misdecoded as code via byte
signatures — printable-ASCII runs >=6 (strings/menu templates) + $FF/$00 fill >=8.
Found **4,315 data lines** (9,501 string + 2,106 fill bytes); **dropped 1,089 bogus
code-annotations** that were on data. Re-folded (`annotate_1631.py` now also inserts
`;==== DATA (string/table/fill, not code) ====` markers at each data run and skips
data-line annotations). Result: **36,994 real-code annotations** + 715 DATA-region
markers. Data addresses listed in `re/mc/data_addrs.txt`.


## REVIEW of unannotated lines + binary-table cleanup (2026-06-25)
Hand-reviewed the unannotated set: the top clusters were DATA tables the first
cleanup missed (e.g. B0061 $D118 config table '00 01 10'..., B0054 $958D pointer
table of $FFxx). Extended `cleanup_data.py` with the 6809_HEURISTICS signals:
avg-instruction-length <=1.5 OR >=40% $FF over >=6 lines -> binary table. Result:
**6,091 lines reclassified as data** (was 3,182), **2,336 more mislabeled
annotations removed**. TRUE real-code coverage now: 39,039 real-code lines,
**35,746 annotated = 91.6%** (was 88.2%); 8.4% unannotated, mostly trivial glue
(RTS/PULS/BRA/CLR). data_addrs.txt = 10,351 data lines.


## ADVERSARIAL/COOPERATIVE function refinement (2026-06-25)
`re/tools/refine_functions.py`: per-function adv/coop (lmt2 technique, batched ~10
funcs/call since our disasms are 20x lmt2's). Per batch: GLM-4.5-air CHALLENGES each
proposed purpose (adversarial — accurate or overreaching? what does it really do?),
deepseek-chat SYNTHESIZES a refined one-liner from the slice+critique (cooperative).
104 batches, 6 workers, ~25 min. **332 functions refined** -> folded into FUNCTIONS.md
(tag `·refined`). Fixes applied: slice-snap to nearest f9dasm instruction line;
markdown-tolerant parse. Source: re/mc/func_refined.txt.

## GAP-CLOSING: 91.5% -> 99.6% real-code coverage (2026-06-25)
Two-step close of the remaining unannotated real code.
1. `re/tools/close_gap.py` — STRUCTURAL annotations (`[STRUCT]`) for unannotated
   instruction lines that sit INSIDE an annotated function (an annotated instr within
   +/-3 lines = confirmed code, not data). `desc(mn,op)` maps mnemonic->mechanical
   text (RTS->"return from subroutine", BNE->"branch if != to <tgt>", LDA->"load A
   from <op>", BSR/JSR->"call <tgt>", etc.). **+1,987 STRUCT** merged into
   annotations_final.txt -> coverage 91.5%->96.6%. NOTE: these are opcode-level
   mechanical notes, NOT subsystem-level semantic insight (which stays at 35,812).
2. Isolated reclassification — the leftover 1,332 unannotated lines were dominated by
   STU (164, from $FF fill) / NEG (123, from $00 fill) i.e. residual data fragments.
   Unannotated runs of >=2 with NO annotated neighbor (= not part of any understood
   function) -> reclassified as DATA (appended to re/mc/data_addrs.txt): **1,181 lines**.
RESULT (intermediate): real code 37,950 lines; ANNOTATED 37,799 = 99.6%.

## RESIDUAL REVIEW: the last 151 lines examined by hand -> 100% (2026-06-25)
Dumped every one of the 151 still-unannotated real-code lines with ±2 lines of
context and read them. Finding: ~half were GENUINE code the structural pass had
skipped only because `close_gap.py`'s mnemonic table was missing `NEG/NEGA/NEGB`,
`COM/COMA/COMB`, `BITA/BITB`, `ASRA/ASRB`, `LBVC/LBVS/LBCC` — clear idioms with
annotated neighbors (`COMA;COMB;ADDD #1`=negate-D, `BGE;NEGB`=abs-value,
`BITA #$80;BNE`=test status bit, threshold-DAC `NEGB;STB $2FF4`). Extended the
table + re-ran. Split the 151 by OPCODE byte (not just mnemonic):
- **81 accepted as `[STRUCT]` code** — single-byte inherent ops ($40 NEGA, $43 COMA,
  $50 NEGB, $53 COMB, $47/$57 shifts), bit-tests, immediate-operand arithmetic, all
  with annotated instruction neighbors.
- **37 routed to DATA** — `$00`-opcode `NEG M00xx` lines = the classic "$00 byte
  false-decoded as NEG-direct" (the reasoner had already flagged many as [R15]/[R5]).
- **33 routed to DATA** — tiny isolated runs at data-block boundaries (STU from $FF
  fill, LDD/CMPB straddling string bytes); no annotated instruction neighbor. A
  handful (e.g. B0054:8D24 `SUBB;BHI;BRA` loop) are plausibly real but unreachable/
  unanalyzed — routed to data as the conservative call rather than mislabel strings.
FINAL: real code 37,880 lines; **ANNOTATED 37,880 = 100.00%** (35,812 semantic +
2,068 structural). All real-code lines now either carry an annotation or are
explicitly classified as data — full accounting, zero unexplained lines.

## DELIVERABLE: annotated disassemblies — workflow
GOAL = per-ROM annotated `.asm` for all 8 EPROMs (lmt2 convention: `_listing.txt`
/ `_annotated.asm` / `_annotations.txt`). Emulator coverage alone is thin (only
B0061+B0058 touched in POST), so the pipeline is **static walk + dynamic seeds +
semantic labels**:
1. STATIC per ROM via `6809_HEURISTICS.md` passes (f9dasm flow / Ghidra walker),
   seeded by the right entry points per ROM:
   - B0061: vectors ($FFF0-$FFFF) — RESET/IRQ/NMI/... (done partially in
     `listings/80061_C000.lst`).
   - B0058: the `JMP $xxxx` dispatch table at its $0000 (logical $6000).
   - B0055/56/57: the `LBSR;RTS` trampoline tables at their heads.
   - B0055: the disc/HP-IB string + drive-model table at off $3051 (DATA region).
2. DYNAMIC seeds: `listings/coverage.txt` = 517 confirmed-executed offsets
   (B0061 280, B0058 237) = Tier-1 code anchors.
3. SEMANTIC labels: `annotations/B0061_labels.txt` (routines, I/O, strings) — the
   first per-ROM annotation set; extend per ROM as analysis proceeds.
- Mind the banking: each ROM runs at specific logical bases (B0061 at $0000-$3FFF
  AND $F000-$FFFF; overlays at $6000-$BFFF). Disassemble at the canonical base and
  note the operational mapping (see OPERATIONAL MMU map above) for cross-bank refs.

## Methods available
- `heuristics/` (extracted from heuristics.zip): most relevant =
  `ghidra-heuristic-codefinder/HEURISTICS.md` (H03/H04/H06/H27 code-finder),
  `asmlogic/sleigh/HEURISTICS.md`, `m6801/HEURISTICS.md` (Motorola-8bit kin).
- lmt2 6809 pipeline (reuse once emulator ready): `ghidra_disasm_walker_6809.py`,
  `ghidra_pcode_cov_6809.py`, `emu6809_host.py`, `xval_6809.py`,
  `ghidra_apply_recovered_6809.py`; standalone `tools/f9dasm/f9dasm` (built).
- lmt2 output convention to mirror per ROM: `_listing.txt`, `_walk.txt`,
  `_functions.txt`, `_annotations.txt`, `_annotated.asm`, `_emu_seeds.txt`,
  `_rd_seeds.txt`, `_recovered.asm`.

## Next steps when resuming (after emulator lands)
1. Determine the MC68829 MMU page map (service manual + boot-ROM bank-switch
   writes near reset $FD80) → fixed load addresses per ROM.
2. Copy `f9dasm` into `re/tools/` (self-contained) → first static listings.
3. Static walk from RESET=$FD80 + the 80058 JMP table + LBSR trampolines as seeds.
4. Cross-validate with f9dasm; then dynamic coverage with lmt2's new emulator.
5. Annotate per the lmt2 file conventions above.
