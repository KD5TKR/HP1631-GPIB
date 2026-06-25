# HP 1630/1631A — memory-mapped peripheral map

Identification and protocol documentation for the I/O window **$E000–$EFFF** of the
HP 1631A logic analyzer (MC68B09E CPU, MC68829 MMU). Produced by a three-voice
campaign over two rounds: register-access evidence mined from the annotated
disassemblies (`re/tools/io_evidence.py` → `re/mc/io_evidence.json`), independent
analysis by **deepseek-reasoner** and **GLM (4.5-air round 1, 5.2 round 2)**, and
**Claude's verification of every disputed claim against the ROM**. Hardware ground
truth from `PARTS.md` (service manual 01630-90917 parts list) adjudicates where
available.

> **The decisive method — read this.** The single most reliable discriminator was
> not register-layout pattern-matching (which misled both models) but **which ROM,
> by its role, accesses a register**. The disc/HP-IB ROM (B0055), the acquisition
> overlay (B0058), and the analog ROM (B0056) each only touch *their own*
> peripheral. That test corrected a confident two-model error (see `$E8A0` below).
> Config-string correlation ("Bus address", "Trigger Level", "Sample Period" → the
> register the field programs) was the second key technique.

## Summary

| Window | Peripheral | Chip identification | Confidence | Basis |
|---|---|---|---|---|
| `$E025–$E043` | CRT controller + display ctrl | **MC68A45** CRTC | **High** | HW-confirmed (A3U6D 1820-2853) + firmware |
| `$E800–$E80E` | Acquisition capture engine | Custom HP acquisition ASIC | **High** | both models; behavioral |
| `$E810–$E82B` | Acquisition trigger / threshold / capture | Custom HP acq/trigger ASIC | **High** | both models; ROM |
| `$E830–$E83F` | Analog threshold DAC array (1631A) | Custom HP threshold/comparator ASIC | Med-High | behavioral |
| `$E840–$E847` | Misc control / mode-muxed latches | Custom HP gate array | Medium | behavioral |
| `$E848–$E84F` | Keyboard / display controller | **Intel 8279** core (in HP ASIC) | Med-High | command-byte map fits 8279 |
| `$E850–$E867` | System status + **HP-IB command/data port** | discrete inputs + HP-IB interface (`$E860-$E863`) | **High** (HP-IB) | GPIB commands + handshake, ROM-confirmed |
| `$E8A0–$E8AF` | **Acquisition/analog front-end control** | Custom HP acquisition state machine | **High** | driven by analog/acq ROMs, NOT disc ROM |

**Verified corrections (read these — they overturned model output):**
1. **`$E8A0–$E8AF` is acquisition/analog control, NOT HP-IB.** GLM-5.2 argued HP-IB
   (8-write/8-read layout like a 9914, `$E8A4 ← #$1E` = "address 30"), and it was
   seductive — but **wrong**. The ROM-role test refutes it: `$E8A3` is written by the
   **analog ROM B0056 (45×)** and **acquisition overlay B0058 (13×)**, while the
   **disc/HP-IB ROM B0055 touches it 0×**. An HP-IB chip would be driven by the
   HP-IB ROM, not the analog/acquisition ROMs. The `#$1E`/layout were coincidences.
2. **HP-IB is at `$E860–$E863`**, not `$E8A0`. B0061 writes real GPIB bus commands
   there — `$5F` (UNT), `$3F`/`$1F`, `$BF`, secondary `$40` — via `$E861`=command,
   `$E862`=data, with a per-byte software handshake (`ZC480`) and a 25-byte "HP-IB
   message buffer." The disc ROM B0055 reaches it only through kernel TX `Z603C`
   (it touches no `$E8xx` directly). My extractor's `imm` table under-reported this
   (the `$5F`/`$BF` loads sit >3 instructions from the store), which is why round-1
   first flagged it "unverified" — the full code listing confirms it.
3. **`$E8A0` is not an 8254 timer** (an earlier hypothesis): the `#$36` "control
   word" was an extractor false-positive (`LDA #$36` = ASCII `'6'` for display, never
   stored to `$E8A0`); `$E8A0`/`$E8A1` are `CLR`-initialized.
4. **Confirmed:** MC68A45 CRTC (`$E032` addr / `$E034` data); 8279 command bytes at
   `$E84F`; analog DAC array polled via `$E838`.

> Caveat on `imm_written` columns in the evidence JSON: a 3-instruction backward scan
> for `LD #imm` feeding a store — it under-reports values loaded earlier (see #2) and
> occasionally grabs an unrelated load (see #3). Constants corroborate; they aren't gospel.

---

## $E025–$E043 — MC68A45 CRT controller + display hardware  *(HW-confirmed)*

**Chip:** Motorola **MC68A45** CRTC, A3U6D / HP 1820-2853 (parts list). The block is
the CRTC register port plus surrounding 74LS373 display-control latches.

| Addr | Dir | Function |
|---|---|---|
| `$E025–E02C` | R/W | 16-bit display pointers (cursor / start address / aux) — heavily polled |
| `$E02E` | W | display mode select (`#$12`/`#$14`/`#$15` = column count) |
| `$E030` | W | video timing (`#$08`/`#$07` — scan-line/cursor params) |
| `$E032/33` | W | **CRTC register port** — `STD #$rrdd` (index `rr`, data `dd`; init `#$9669`) |
| `$E034` | R/W | video control; `#$80` = video enable |
| `$E03B` `$E03F` `$E043` | R | status (vsync / diagnostics) |

Both models agree 6845; deepseek noted the 18-register span matches the 6845 set.
**Protocol:** boot programs the registers (via `$E032` or direct), enables video
(`#$80`→`$E034`), then updates pointers and polls status during raster ops. Polled.

---

## $E800–$E80E — acquisition capture engine  *(both models agree)*

**Chip:** custom HP acquisition ASIC. **Confidence: high.**

| Addr | Dir | Function (constants) |
|---|---|---|
| `$E800` | W | acquisition control / mode (`#$03,07,0B,0D,0E,FE`) |
| `$E801` | W | reset (`#$80`, `#$FF`) |
| `$E802` | R | status |
| `$E803` | W | clock/mode config (`#$80,$60,$D0`) |
| `$E804` | R/W | channel select / mask (low nibble) |
| `$E806` | W | **sample-clock strobe / word load** — see refinement |
| `$E808` | W | sample count / depth (`#$08`) |
| `$E80C` | W | trigger command: `#$80` arm, `#$01` start, `#$05` start+cfg, `#$00` stop |
| `$E80E` | R | **capture FIFO** data (read-only, polled) |

> **Round-2 refinement:** only 4 of 16 `$E806` writes are immediates — the rest are
> *variable data*, so `$E806` is a sample-clock strobe / pattern-word load (consistent
> with the emulator finding that **each write advances the capture counter**), and the
> clock-divider config is more likely `$E803`.

**Protocol:** reset → configure mode/channel (`$E803`/`$E804`) → clock/strobe
(`$E806`) → depth (`$E808`) → arm+start (`$E80C`) → poll → read FIFO (`$E80E`). Polled.

---

## $E810–$E82B — acquisition trigger / threshold / capture  *(both models agree)*

**Chip:** custom HP acquisition/trigger ASIC. **Confidence: high.**

| Addr | Dir | Function |
|---|---|---|
| `$E810` | R/W | capture-data read window A + control/status |
| `$E812` | W | threshold-DAC data (`#$80,$20,$12`) |
| `$E813` | W | **trigger/arm command**: `#$80` = ARM, `#$81` = DAC setup |
| `$E814` | W | trigger reset (`CLR`) |
| `$E815` | R | status (polled ×9) |
| `$E816` | W | clock/qualifier threshold (`#$10`,`#$20`) |
| `$E817` | W | comparator/qualifier (`#$01,$0D,$81`) |
| `$E81B` | R | capture/FIFO status |
| `$E820` | R | capture-data read window B |
| `$E822` | W | clock-qualifier mask (`#$20`) |
| `$E824` `$E82A` | W | pattern/external trigger mask (`#$FF` = care-all) |
| `$E826` | W | trigger delay count (`#$10`) |
| `$E82B` | R/W | capture control — `#$FF` flush/reset |

> **Round-2 refinement:** `$E810`/`$E820` are two *selectable* capture-data read
> windows (B0060 picks the base on a mode byte — ROM-confirmed), with `$E810` also a
> control/status port — not a symmetric complementary FIFO pair.

**Protocol:** set thresholds & masks → arm (`#$80`→`$E813`) → poll `$E81B`/`$E815` →
select read window (`$E810`/`$E820`) → burst-read → flush `$E82B`.

---

## $E830–$E83F — analog threshold DAC array (1631A)  *(both models agree)*

**Chip:** custom HP threshold/comparator ASIC — a bank of 8-bit threshold DACs
(per-channel-group threshold voltages, the 1631A's analog front end). The **"Trigger
Level" / "in voltage range" / "in voltage offset"** config fields drive it.
**Confidence: med-high.**

| Addr | Dir | Function |
|---|---|---|
| `$E830–$E833` | R/W | channel/group select & DAC update strobe (`#$F7,$F1,$FE,$01`) |
| `$E834` | R | comparator status |
| `$E835`,`$E837`,`$E839`,`$E83D–$E83F` | W | threshold-DAC data (`#$FF` full-scale, `#$0F`, `#$80` polarity) |
| `$E836` | R/W | command register (`#$0D,$12,$0F,$13` — channel/calibrate) |
| `$E838` | R/W | **primary status** — polled ×23 (`BITB #$80` done, `#$01` data-ready) |
| `$E83A` | W | DAC update latch (all `#$80` strobes) |
| `$E83B` | W | reference / calibration control |
| `$E83C` | W | threshold+control (24 writes; low nibble = level, high nibble = ctrl) |

**Protocol:** select channel → write 8-bit threshold byte (from RAM `M271x`) → strobe
(`$E83A`/`$E833`) → poll `$E838` → repeat per channel; calibration re-issues `$E836`.

---

## $E840–$E847 — misc control / mode-multiplexed latches  *(both models agree)*

**Chip:** custom HP gate array. `$E843` selects the role of the shared latch pair
`$E844`/`$E845`. **Confidence: medium.**

| Addr | Dir | Function |
|---|---|---|
| `$E840` | R | status input (busy/ready) |
| `$E842` | W | start/arm strobe (B0058) |
| `$E843` | W | mode select: `#$04`=display addr/data, `#$2C`=acquisition data (also `#$01,$50,$21`) |
| `$E844` | W | address-low / data-low (role per `$E843`) |
| `$E845` | W | data / data-high |
| `$E846` | W | measurement strobe (B0060) |

**Protocol (display):** `#$04`→`$E843`, addr→`$E844`, char→`$E845`.
**Protocol (acq):** `#$2C`→`$E843`, low/high bytes to `$E844`/`$E845`.

---

## $E848–$E84F — keyboard / display controller (Intel 8279 core)  *(both models agree; cmd bits ROM-checked)*

**Chip:** **Intel 8279**-style keyboard/display controller (likely the 8279 core in an
HP ASIC — 3 addresses vs the bare 8279's 2). Prior RE notes: U9E (1820-2150).
**Confidence: med-high.** deepseek decoded the `$E84F` command-bit fields exactly
(clear=`110x`, write-display=`100x`, read-FIFO=`010x`, mode=`000x`).

| Addr | Dir | Function |
|---|---|---|
| `$E848` | W | reset/enable (`#$80`) |
| `$E84A` | W | clock/timer divider (`#$0B`,`#$03`) |
| `$E84C` | R | keyboard FIFO read (two reads = one key event) |
| `$E84E` | W | display-RAM data |
| `$E84F` | W | **command** — `#$D0` clear-all, `#$86` write-display+autoinc, `#$C8` clear, `#$48` |

**Protocol:** reset (`$E848`), set scan clock (`$E84A`), issue commands to `$E84F`,
write display chars to `$E84E`; poll keyboard via `$E84C` (empty FIFO ⇒ 0). The boot
"reboot loop" is a timeout waiting for a keypress from this controller.

---

## $E850–$E867 — system status + HP-IB command/data port

A **mixed region**: discrete status inputs at the low end (ROM-confirmed) and the
**HP-IB / IEEE-488 interface** at `$E860–$E863` (correction #2). The HP-IB driver is
in the kernel (B0061); the disc/LIF ROM B0055 uses it only via kernel TX `Z603C`.

| Addr | Dir | Function | Verification |
|---|---|---|---|
| `$E852` | R | **rear-panel switch**, bit2: 0=operate, 1=self-test loop | ✅ ROM-confirmed (boot gate) |
| `$E854` | R | **acquisition-done** flag | ✅ ROM-confirmed |
| `$E856` | W | control latch (`#$08`) | behavioral |
| `$E860` | R/W | HP-IB control / handshake | behavioral |
| `$E861` | W | **HP-IB command register** — GPIB bus commands `#$5F`(UNT), `#$3F`/`#$1F`(UNL), `#$BF`, byte-count | ✅ GPIB-command-confirmed |
| `$E862` | R/W | **HP-IB data register** — data bytes + secondary address (`#$40`) | ✅ ROM-confirmed |
| `$E863` | R/W | HP-IB/trigger enable: read, `ANDB #$DF` (clear bit5), write back | behavioral |
| `$E864–$E867` | W | control/clear (`#$01`/`#$00`) | behavioral |

**HP-IB protocol (kernel, polled):** to address the bus — write the command byte
(UNL `$3F` → UNT `$5F` → talk/listen/secondary) to `$E861`, the data/address byte to
`$E862`, then call the handshake-wait `ZC480` (poll until byte accepted). Block
transfers loop this over a 25-byte message buffer. The **"Bus address" / "External
Controller" / "conflicting HPIB addresses"** config fields set the device's HP-IB
address used here. *(Chip family — likely a TMS9914-class GPIB controller or HP
custom — not confirmed from the schematic; the byte/command interface is confirmed.)*

---

## $E8A0–$E8AF — acquisition / analog front-end control  *(round-1 conclusion; round-2 HP-IB hypothesis tested and rejected)*

**Chip:** custom HP acquisition/analog-control ASIC (a command-sequenced state
machine). **Confidence: high.** The HP-IB hypothesis (GLM-5.2 round-2) was tested and
**rejected**: `$E8A3` is driven by the **analog ROM B0056 (45×)** and **acquisition
overlay B0058 (13×)** — never by the disc/HP-IB ROM B0055. HP-IB lives at `$E860–$E863`
(above). The 8-write/8-read layout and `$E8A4 ← #$1E` that suggested a GPIB chip were
coincidental.

| Addr | Dir | Function |
|---|---|---|
| `$E8A0`,`$E8A1` | W | mode/control (CLR-initialized at reset) |
| `$E8A3` | W | **main command register** — sequenced writes `#$8F,$0F,$0B,$90,$0C` with inter-write delays (configures the acquisition/analog timing state machine) |
| `$E8A4–$E8A6` | W | parameter / mask registers |
| `$E8A7` | W | channel/scan index (from a loop counter) |
| `$E8A8`,`$E8A9`,`$E8AB` | R | status bytes |
| `$E8AA` | R | **primary status** (read ×11; 2-bit codes table-translated for display) |
| `$E8AE`,`$E8AF` | R | per-channel status |

**Protocol:** init clears `$E8A0`/`$E8A1`, writes a command sequence to `$E8A3` with
settling delays to set up the front-end state machine; per channel, write the index to
`$E8A7` and read status from `$E8AA`/`$E8AE`/`$E8AF`. Polled, no DMA/interrupt.

---

## Config-string → register correlation
| On-screen config field | ROM (string) | Drives | Register block |
|---|---|---|---|
| "Bus address" / "External Controller" / "conflicting HPIB addresses" | B0055 `$A235`,`$A0F7`,`$87CF` | HP-IB device address & bus commands | `$E860–$E863` (kernel) |
| "Trigger Level" / "in voltage range" / "in voltage offset" | B0057 `$A2C4`, B0054 `$BE37` | analog comparator thresholds | `$E830–$E83F` |
| "Sample Period" / "After Trigger" / "Trigger" | B0057 `$A0F8`,`$A263` | acquisition clock & trigger | `$E800–$E82B` |
| "Invalid clock setting" / "Delay too big for sample period" | B0054 `$BDCA`,`$BAEB` | acquisition timing | `$E800–$E82B` |

## Campaign artifacts & method
- `re/tools/io_evidence.py` → `re/mc/io_evidence.json` — 137 I/O registers, 30 clusters.
- **Round 1** `re/tools/periph_campaign.py` → `re/mc/periph_<name>.{ds,glm}.txt` —
  deepseek-reasoner ×8 + GLM-4.5-air ×2 (rest rate-limited).
- **Round 2** `re/tools/periph_resolve.py` (+ `periph_resolve_glm.py` serial) →
  `re/mc/periph2_<name>.{ds,glm}.txt` — adversarial cross-examination, deepseek-reasoner
  ×8 + **GLM-5.2** (run serially: z.ai 429-throttles GLM above ~4 concurrent).
- **Lessons:** (a) register-layout pattern-matching fooled both models on `$E8A0`;
  the **access-by-ROM-role** test (does the *disc* ROM touch it?) was decisive.
  (b) Config strings name their hardware — trace the field to its register.
  (c) When models agree confidently, still verify against the ROM: here the agreeing
  answer was wrong twice (8254, then HP-IB) before the role test settled it.
