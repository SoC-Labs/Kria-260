# KR260 lockup recovery

How a KR260 wedges, why only a power-on reset clears it, and the recovery stack
that now handles it without anyone walking to the bench.

## The wedge

A plain `sudo reboot` on a KR260 goes:

```
reboot → PSCI SYSTEM_RESET → TF-A → PMUFW XPfw_ResetSystem()
       → raw CRL soft-SRST  (NOT a power-on reset)
```

That resets the PS components **without** the PMU-orchestrated idle/isolation
drain. Un-drained AXI transactions deadlock the shared LPD/FPD interconnect, and
the next boot-from-reset (PMU/CSU ROM → FSBL) hangs on its own interconnect
access. The board goes dark: no PMU banner, no U-Boot, console UART held in
reset (`RST_LPD_IOU2` bit 2).

Pinned at register level over JTAG (2026-07-11): during the wedge the A53 sits
halted in Reset Catch at `0xFFFF0000` — the A53 *was* reset, it is not parked in
a BL31 `wfi` — with USB in a torn partial reset and the PMU MicroBlaze
undebuggable. This is AMD's documented ZynqMP reset hazard (Restart-Solution
wiki 18841820).

Two consequences that shape everything below:

- **Only a true POR clears it.** A soft SRST does not.
- **The carrier reset button is also `PS_SRST_B`** — so the button cannot
  recover a wedged board either. Neither can a watchdog.

> A caution from the investigation: `ERROR_STATUS_2 = 0xFFFF4FFF`, which looks
> like a wall of error bits, is a **corrupted/undriven read** (reserved bits
> set), not real errors. Don't cite it. Read physical registers with `mrd -force`
> on the *halted* A53 (MMU off) during the wedge; never `stop` a healthy A53 —
> it freezes Linux (resume with `con`).

## The two-layer answer

**Layer 1 — don't wedge in the first place: `sudo kreboot`.**
`kexec-tools` + `/usr/local/sbin/kreboot` (`kexec -l <kernel> --reuse-cmdline;
systemctl kexec`) reboots in ~65 s **without** invoking the PMUFW reset, so the
wedge never happens. Installed and verified on both boards.
**Use `sudo kreboot`, never `sudo reboot`.** It is a workaround, not a fix: it's
not a hardware reset, so PL state persists.

**Layer 2 — when a board wedges anyway: an automatic JTAG POR.**
`fpgahub` watches both boards and PORs one that stops answering.

```
reachability probe (icmp)  →  board reads `offline`
        ↓  10 consecutive guard sweeps × 30 s  ≈ 5 min
HealthGuard  →  dispatch_reset(board, method="default")
        ↓
kr260_jtag_por  →  xsdb: connect; targets -filter {cable =~ *SERIAL*}; rst -por
        ↓
board POWER-ON resets and boots  (~95 s)
```

Safety rails, all per-board configurable:

| Rail | Default | Why |
|---|---|---|
| `offline_ticks` | 10 (≈5 min) | Layered *on top of* the reachability tracker's own debounce, so a brief ICMP blip never triggers a POR. |
| `cooldown_ticks` | 20 (≈10 min) | Space out retries. |
| `max_attempts` | 3 | Then emit `guard.recovery_exhausted` and back off. **A genuinely dead board must not be POR-looped forever** — it should escalate to a human. |
| `skip_when_leased` | true | Never POR a board a client is actively holding. |

**The serial pin is the safety-critical part.** mapstone-dev's `hw_server` is
shared with the KU115 and four PYNQ-Z2s. A POR is chip-wide, so an unfiltered
`rst -por` resets whatever board it happens to select. `kr260_jtag_por`
therefore **refuses to run without a JTAG serial** rather than guessing.

## Where the code lives, and why it isn't in this repo

| Component | Repo | Reason |
|---|---|---|
| `HealthGuard` (`guard.py`) | fpgahub | Board-agnostic infrastructure. Nothing in it is Kria-specific — any MPS3 or PYNQ that can wedge gets auto-recovery by adding a `[boards.<n>.guard]` block. |
| `kr260_jtag_por`, `kr260_kexec_reboot` | fpgahub `reset_plugins/` | fpgahub registers reset plugins **by in-tree import side-effect** — there is no entry-point/out-of-tree plugin discovery. A plugin in this repo could not be registered at all. They sit alongside `mps3.py`, `zynq.py`, `pynq_uart.py`. |
| Board config, runbooks, AMD report, `kpor` | **here** | Board knowledge and site config. |

## Operating it

```bash
# Is the guard running and watching?
journalctl -u fpgahubd | grep -i "health guard started"
#   -> health guard started (interval=30s, watching=['kr260_01', 'kr260_02'])

# Watch a recovery happen
journalctl -u fpgahubd -f | grep -i guard

# Recover a board by hand, right now
fpgahub target reset kr260_02 --yes     # through the daemon (takes the board lock)
~/bin/kpor kr260-02 --wait              # direct xsdb — use if fpgahubd is down

# What reset methods does a board have?
fpgahub target reset kr260_01 --list
```

**Before you deliberately power a board down**, disable its guard
(`[boards.<n>.guard] enabled = false`, restart `fpgahubd`) — otherwise the guard
will see an unreachable board and POR it back to life.

## Proof (2026-07-14, unattended)

kr260_02 was wedged with a plain `sudo reboot` and left alone:

```
13:49     wedged (PMU deadlock — cannot self-recover)
13:55:24  guard: kr260_02 offline 10 sweeps -> recovery reset method=default attempt=1/3
13:55:24  kr260_jtag_por: issuing rst -por (cable=XFL1EAUJ5SPO glob=*a53*#0*)
13:55:29  reset dispatched: plugin=kr260_jtag_por ok=True dur=4.67s
13:56:59  guard: kr260_02 back online after 12 offline sweeps
```

~7 min detect-to-recovered; the POR itself took 4.7 s and the board's boot ~95 s.
kr260-01, on the same shared `hw_server`, logged **zero** events — the serial pin
held. And because the wedge cannot self-clear, the board returning *is* the proof
that the POR fired: there is no benign alternative explanation.

## Known gaps

- **`kr260_kexec_reboot` can't run yet.** It SSHes to the board, and there is no
  key-based SSH from mapstone-dev to the KR260s
  (`Permission denied (publickey,password)`). Needs a key plus a NOPASSWD sudo
  rule for `/usr/local/sbin/kreboot`. The POR path needs **none** of that, which
  is why auto-recovery works regardless.
- **`hw_server` is a single point of failure** for the POR path. It is at least
  supervised (`hw_server.service`, `Restart=always`), and earlyoom killing it is
  survivable — tested: killing the real server makes its bash wrapper exit, so
  systemd restarts it in ~100 ms, costing at most one failed POR attempt before
  the guard's cooldown retry succeeds. But if it were truly gone, recovery would
  be gone with it. Hence Phase 3.
- **Phase 3:** a C232HM MPSSE FET driving `PS_POR_B` directly — a hard-reset
  backend that needs neither JTAG nor `hw_server`.
