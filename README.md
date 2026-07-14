# Kria KR260 — SoCLabs board platform

Bring-up, operation and lockup-recovery for the two AMD **Kria KR260** boards
(K26 SOM on an SCK-KR-G carrier) running headless Ubuntu 22.04 on the SoCLabs
bench.

This repo holds the **board knowledge**: how to bring one up, the two firmware
bugs we hit and root-caused, the AMD issue report, and the operator scripts.
The **recovery mechanism** itself lives in `fpgahub` — see
[Lockup recovery](#lockup-recovery) below for where the seam is and why.

## Inventory

| | kr260-01 | kr260-02 |
|---|---|---|
| Carrier serial (FTDI) | `XFL1MHS3ZB1P` | `XFL1EAUJ5SPO` |
| IP (reserved DHCP) | `10.22.24.159` | `10.22.24.153` |
| LAN MAC | `00:0a:35:29:1f:81` | `00:0a:35:29:1f:84` |
| Console (on mapstone-dev) | `/dev/ttyUSB9` | `/dev/ttyUSB12` |
| USB path | `1-1.2.2.3` | `1-1.2.2.4` |
| Boot FW | 1.07 (2026.1), slot A keeps 1.02 for rollback | same |

Both boards: Ubuntu 22.04, `cpuidle.off=1` persisted, MAC pinned, JTAG+UART on
the carrier's FT4232H, reachable from **mapstone-dev** (which hosts the shared
`hw_server` on `tcp::3121`). Login credentials are **not** in this repo — ask
david or the lab password store.

## The two bugs, both root-caused

**1. Headless boot hangs** (fixed). With no DisplayPort cable, the board hung in
the initramfs: a secondary CPU never woke from PSCI deep idle to service an IPI
(`kick_all_cpus_sync`). A monitor "fixed" it only because the display pipeline's
vblank interrupts kept cores out of deep idle — `zynqmp_dpsub` is innocent.
**Fix: `cpuidle.off=1`** via `/etc/default/flash-kernel`. Persisted on both.

**2. `sudo reboot` wedges the PMU** (no firmware fix; we have recovery). A plain
reboot issues a soft `PS_SRST` that resets the PS *without* the PMU-orchestrated
idle/isolation drain. Un-drained AXI transactions deadlock the LPD/FPD
interconnect, so boot-from-reset hangs before any firmware runs — no PMU banner,
console UART held in reset, board dark. **Only a true power-on reset clears it**,
and the carrier reset button is *also* `PS_SRST_B`, so it can't. This is AMD's
documented ZynqMP reset hazard; the real fix is theirs.

Full root-cause with JTAG register evidence, and the report we're filing with
AMD: [`docs/XILINX_ISSUE_REPORT.md`](docs/XILINX_ISSUE_REPORT.md).

## Lockup recovery

Because bug 2 has no firmware fix, a wedged board used to mean walking to the
bench and pulling power. It no longer does.

**Everyday reboots:** use `sudo kreboot` (kexec) on the board, **never**
`sudo reboot`. kexec never invokes the PMUFW reset path, so it dodges the wedge.

**When a board wedges anyway:** `fpgahub` PORs it over JTAG — automatically.

The mechanism deliberately lives in **fpgahub**, not here:

| Where | What | Why there |
|---|---|---|
| `fpgahub/src/fpgahub/guard.py` | `HealthGuard` — offline board → dispatch its recovery reset, with cooldown + attempt ceiling | Board-agnostic. Nothing in it mentions Kria; any MPS3/PYNQ opts in with a config block. |
| `fpgahub/src/fpgahub/reset_plugins/kr260_por.py` | `kr260_jtag_por` — `xsdb rst -por`, pinned to the board's FTDI serial | Sits with `mps3.py`/`zynq.py`/`pynq_uart.py`. fpgahub registers reset plugins by in-tree import; there is no out-of-tree plugin discovery, so it *must* live there. |
| `fpgahub/src/fpgahub/reset_plugins/kr260_reboot.py` | `kr260_kexec_reboot` — `kreboot` over SSH | same |

The serial pinning is not cosmetic: the `hw_server` is **shared** with the KU115
and four PYNQ-Z2s, and an unfiltered `rst -por` resets whatever board it lands
on. The plugin *refuses to run* without a serial.

**Proven on silicon, unattended (2026-07-14):**

```
13:49     kr260_02 wedged with a plain `sudo reboot`
13:55:24  guard: kr260_02 offline 10 sweeps -> recovery reset, attempt 1/3
13:55:24  kr260_jtag_por: rst -por, cable=XFL1EAUJ5SPO, target=*a53*#0*
13:55:29  reset dispatched: ok=True, dur=4.67s
13:56:59  guard: kr260_02 back online after 12 offline sweeps
```

kr260-01, on the same shared `hw_server`, was untouched. Since the wedge cannot
self-clear, the board coming back *is* the proof the POR fired.

Details: [`docs/LOCKUP_RECOVERY.md`](docs/LOCKUP_RECOVERY.md).

## Contents

```
docs/BRINGUP.md              Runbook: bring up a new KR260 headless, from console to pinned IP
docs/XILINX_ISSUE_REPORT.md  The AMD/Xilinx filing — 4 issues, root causes, asks
docs/LOCKUP_RECOVERY.md      The wedge, the recovery stack, how to operate it
scripts/kpor                 Manual one-shot JTAG POR (run on mapstone-dev)
site/fpgahub-kr260.toml      Reference copy of the fpgahub board config
```

## Quick reference

```bash
# Recover a wedged board (either of these)
fpgahub target reset kr260_02 --yes      # via the daemon (preferred)
~/bin/kpor kr260-02 --wait               # direct xsdb, if fpgahubd is down

# Watch the auto-recovery guard
journalctl -u fpgahubd -f | grep -i guard

# Console
sudo screen /dev/ttyUSB9 115200          # kr260-01
```

## Open tasks

- **No key-based SSH from mapstone-dev to the boards** (`Permission denied
  (publickey,password)`). This means `kr260_kexec_reboot` — the plugin that
  reboots a board *without* wedging it — cannot currently run. The POR path
  needs no SSH, which is why auto-recovery works regardless. Wire a key + a
  NOPASSWD sudo rule for `/usr/local/sbin/kreboot`.
- **`scripts/kreboot` is missing here** for the same reason: the copy installed
  at `/usr/local/sbin/kreboot` on both boards couldn't be read back. `cat` it
  and commit it once keys are wired.
- **File the AMD report** (`docs/XILINX_ISSUE_REPORT.md`) — ready to go.
- **Phase 3 (hardware):** a C232HM MPSSE FET driving `PS_POR_B`, giving a
  recovery path that survives even a dead `hw_server`.
