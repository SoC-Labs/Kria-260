# KR260 / Certified Ubuntu 22.04 — Issue Report for AMD/Xilinx

**Reporter:** SoCLabs (University of Southampton)
**Date:** 2026-07-10
**Boards:** 2 × Kria KR260 Starter Kit, K26 SOM (SMK-K26-XCL2G), carrier SCK-KR-G, board `ZynqMP KR260 revB`
**OS image:** "Certified Ubuntu 22.04 LTS for Xilinx Devices", kernel `5.15.0-1027-xilinx-zynqmp #31-Ubuntu`, FIT dated 2024-03-04
**Boot firmware tested:** 1.02 (PMUFW 2023.2 / TF-A v2.8 / U-Boot 2023.01) **and** 1.07 (PMUFW 2026.1 / TF-A v2.14 / U-Boot 2026.01)
**Use case:** headless deployment (no DisplayPort monitor), serial console + Ethernet only.

---

## Issue 1 (PRIMARY) — Headless boot hangs in the initramfs: a secondary CPU never wakes from PSCI deep idle to service an IPI

### Symptom
Booted **with no DisplayPort monitor attached**, the board hangs in the initramfs and never reaches userspace. On the serial console (`ttyPS1`, 115200) the last progress is the USB/SD enumeration (~t=15 s), then a watchdog soft-lockup fires and repeats indefinitely; the board never recovers (observed stuck >2000 s). **Attaching a DisplayPort monitor makes it boot reliably every time.**

### Reproduction
1. Flash the stock Certified Ubuntu 22.04 image to the microSD.
2. Boot the KR260 headless (no DP cable), serial console attached.
3. Observe the hang. (With a DP monitor attached, the identical image boots to a login prompt.)

Deterministic: fails 100% headless, succeeds 100% with a monitor.

### Serial evidence (verbatim)
```
[   76.496491] watchdog: BUG: soft lockup - CPU#2 stuck for 26s! [wait-for-root:458]
[   76.503987] Modules linked in: raid10 ... da9121_regulator i2c_mux_pca954x ... zynqmp_dpsub aes_neon_bs ...
[   76.530627] CPU: 2 PID: 458 Comm: wait-for-root Not tainted 5.15.0-1027-xilinx-zynqmp #31-Ubuntu
[   76.539409] Hardware name: ZynqMP KR260 revB (DT)
[   76.551058] pc : smp_call_function_many_cond+0x180/0x37c
Call trace:
 smp_call_function_many_cond+0x180/0x37c
 kick_all_cpus_sync+0x38/0x44
 flush_icache_range+0x40/0x50
 bpf_int_jit_compile+0x1ac/0x4dc
 bpf_prog_select_runtime+0xe4/0x11c
 bpf_prepare_filter+0x1f0/0x220
 __get_filter+0xb8/0x13c
 sk_attach_filter+0x20/0xb0
 sock_setsockopt+0x27c/0xbb0
 __sys_setsockopt+0x160/0x1b0
 __arm64_sys_setsockopt+0x30/0x40
```
Kernel command line (stock image):
```
root=LABEL=writable rootwait earlycon console=ttyPS1,115200 console=tty1 clk_ignore_unused uio_pdrv_genirq.of_id=generic-uio xilinx_tsn_ep.st_pcp=4 cma=1000M
```
The stuck CPU's registers in `smp_call_function_many_cond` indicate **CPU#2 is waiting on CPU#3** (`x27=0x2` = calling CPU, `x28=0x3` = target CPU, `x26=0x4` = nr_cpu_ids). CPU#3 is the non-responsive core.

### Root cause (analysis)
- `wait-for-root` (initramfs-tools) opens a libudev monitor, which calls `setsockopt(SO_ATTACH_FILTER)`. On arm64 this JITs a BPF filter (`bpf_int_jit_compile`), which calls `flush_icache_range()` → `kick_all_cpus_sync()` → an **IPI broadcast to all CPUs that spins until every core acknowledges** (`smp_call_function_many_cond`). This is an entirely ordinary code path — it is the **victim, not the cause**.
- The proximate fault is that **a secondary CPU does not answer the function-call IPI.** The device tree enables a PSCI power-down idle state — `CPU_SLEEP_0`, `arm,psci-suspend-param = <0x40000000>` (bit 30 = power-down), `entry-method = "psci"` — referenced by every CPU node. A core power-gated in this state is not waking to service the IPI.
- **DisplayPort dependency explained:** with a monitor attached, `zynqmp_dpsub` brings up a real display pipeline whose periodic **vblank interrupts keep the cores out of the deepest idle state** during the critical window, so the wakeup race never arms. Headless, there is no such activity and the cores reach the power-down state.
- **`zynqmp_dpsub` is NOT the cause.** Headless it probes cleanly (`[drm] Cannot find any crtc or sizes` then `ZynqMP DisplayPort Subsystem driver probed`) and the system continues; it is loaded but not blocking.

### Confirmed fix / workaround
Adding **`cpuidle.off=1`** to the kernel command line resolves it completely: the board boots headless to a login prompt, verified across multiple boots on two boards. (Cores fall back to plain WFI, which always wakes on an IPI.)

### Notable: the boot-firmware update does NOT fix it
We updated one board from boot FW 1.02 (2023.2) to 1.07 (2026.1) — the hang persists identically. The defect is in the PSCI `CPU_SUSPEND` / secondary-CPU-wakeup path and is not addressed by the 2026.1 PMUFW/TF-A. We could find **no changelog entry** in TF-A (v2.8→v2.14) or PMUFW referencing a ZynqMP `CPU_SUSPEND`/IPI-wakeup fix.

### Ask
Please advise whether this is a known ZynqMP cpuidle/PSCI erratum, whether the Certified Ubuntu image should ship with the deepest idle state disabled (or `cpuidle.off=1`) for headless configurations, and/or whether a PMUFW/TF-A fix is planned. We could find no prior public report of this specific headless-boot hang.

---

## Issue 2 (SECONDARY) — Warm reboot wedges the PMU/SoC; only a power-cycle recovers

### Symptom
`sudo systemctl reboot` (or `reboot`) prints `reboot: Restarting system` on the serial console and the board then goes **permanently dark** — no PMU firmware banner, no U-Boot, nothing. It never restarts. A hardware **power-cycle is the only recovery**; the soft reset is insufficient.

### Reproduction
From a booted session: `sudo reboot`. Observe `reboot: Restarting system` followed by indefinite silence on serial.

### Scope
Reproduces on **both** boot FW 1.02 (PMUFW 2023.2 / TF-A v2.8) **and** 1.07 (PMUFW 2026.1 / TF-A v2.14). Rootfs is on USB (the KR260 microSD is behind a USB card-reader). The last kernel messages before silence are the DRM/DisplayPort bridge teardown, then `reboot: Restarting system`.

### Evidence and an honest caveat on "permanent wedge vs. very slow recovery"
The proof of the stall is the **real-time reachability timeline**, not the serial log alone: after a `reboot`, SSH was unreachable across a 250 s timeout and the (still-running) serial capture sat frozen at `reboot: Restarting system` from the reboot until a manual power-cycle ~29 minutes later — the capture reader was alive throughout and recorded zero bytes. A serial *log* cannot by itself prove this, because a dark board emits nothing, so `Restarting system` sits directly against the next boot's FSBL banner with the multi-minute gap invisible in the file. Two open points we have **not** yet nailed and that a controlled test should settle: (a) whether it is a *hard* wedge (POR-only, forever) vs. an extremely slow eventual reset — a power-cycle was always applied before natural recovery could be ruled out; and (b) every recovery boot reports `Reset Mode: System Reset` / `Reset reason: SOFT`, **not** a power-on reset, which is unexpected after pulling power (likely unreliable reset-reason reporting on the K26, but unconfirmed). **Recommended confirming experiment:** timestamp the serial capture (`ts`), issue one `reboot`, and measure the exact silence duration with **no** intervention until either self-recovery is observed or a long timeout elapses.

#### UPDATE — confirmed by controlled remote test (2026-07-10)
We ran that experiment (with a JTAG POR as the recovery net — see workarounds):
- **The wedge is real and hard, not a slow recovery.** After `sudo reboot`, kr260-02 stayed `ping`-DOWN with the serial frozen at exactly `reboot: Restarting system` (immediately preceded by the DP bridge teardown `unregister bridge display which is owned by other component`) for **~4 minutes with zero output and zero reachability** — versus a normal ~40–60 s reboot. No self-recovery.
- **A power-on reset does rescue it.** Issuing a **JTAG `rst -por`** (true POR over the carrier FTDI JTAG via `hw_server`/`xsdb`) recovered the wedged board in **~66 s** — fresh FSBL → PMU → Linux → login — **with no physical power-cycle.** This is direct confirmation that the wedged state is cleared by a POR but **not** by the SRST that `reboot` issues, matching the mechanism above.
- **Reset-reason ambiguity resolved.** The post-POR FSBL still prints `Reset Mode: System Reset`, so the K26 FSBL/U-Boot reset-reason field does **not** distinguish POR from SRST; the earlier `Reset reason: SOFT` on recovery boots is therefore unreliable reporting, **not** evidence against a power-cycle.

#### Fix campaign & working workaround (2026-07-10)
We attempted to make the PSCI/soft-SRST path *complete* by quiescing suspected masters before reboot — each tested on kr260-02 via a deliberate wedge + JTAG-POR rescue:
- **`shutdown_scope=ps_only`** → still wedged (no effect).
- **Unbind the secondary USB3 controller `fe300000`** (the marginal PS-GTR lane) → still wedged. Note the **rootfs USB controller `fe200000` cannot be unbound** (the OS needs it until the last moment), and on the KR260 the microSD is *fundamentally* behind USB — so the one master we cannot quiesce is exactly the rootfs-USB path.
- **Live-unbind of DisplayPort** → invalid experiment: the board runs an Xorg session on DP (default `graphical.target`), and unbinding DRM under it hung the *shutdown* on a zombie Xorg — a separate artifact. In the genuine wedge the DP teardown (`unregister bridge display`) completes cleanly *before* `reboot: Restarting system`, so DP is not the hang point.

None of the quiesce/scope levers made the soft-SRST complete, consistent with the culprit being the **un-quiescible rootfs-USB master or a firmware-level interconnect/PS-GTR issue** rather than anything addressable from Linux userspace.

**Working workaround — reboot via `kexec` (VERIFIED on both boards):** `kexec` reloads the OS by long-jumping into a pre-loaded kernel and **never invokes PSCI `SYSTEM_RESET` / PMUFW**, so it bypasses the wedge entirely. We installed `kexec-tools` and a wrapper `/usr/local/sbin/kreboot` = `kexec -l <kernel> --initrd=<initrd> --reuse-cmdline; systemctl kexec`. **`sudo kreboot` reboots cleanly in ~65 s with no wedge** — confirmed on kr260-01 and kr260-02 (each rebooted, came back with fresh uptime).

**Honest status — this is a workaround, not a root-cause fix.** It avoids the broken reset rather than repairing it. A true fix lives in **PMUFW/ATF firmware** (or in whatever leaves the interconnect/PS-GTR master un-quiesced across the soft-SRST). Two operational caveats: (1) `kexec` is **not** a hardware reset — PL/peripheral state persists; for a full POR use JTAG `rst -por` or a power-cycle; (2) the kexec image must be (re)loaded each boot — the `kreboot` wrapper does this via `--reuse-cmdline`.

#### Deep JTAG root-cause — PINNED (register-level, 2026-07-11)
Using JTAG (`hw_server`/`xsdb`) to halt cores and read physical registers *during* the wedge (each cycle recovered by `rst -por`), the mechanism is pinned, and it matches AMD's own documented ZynqMP reset hazard.

Observed during the wedge (kr260-02), healthy → wedged:
- **A53#0 halts in `Reset Catch` at PC `0xFFFF0000`** (the APU reset vector). So the soft-SRST *did* fire and reset the A53 — it is **not** parked in BL31 `wfi()` (correcting the earlier literature-based assumption). The re-boot never executes (no PMU/CSU/FSBL → no banner).
- **The console UART1 is held in reset** — `CRL_APB.RST_LPD_IOU2` (0xFF5E0238) bit 2 = 0 (running) → 1 (reset); the register reverts to `0x0017FFFE` (reset default, only QSPI released). FSBL is what re-releases these, so **FSBL never ran — this is the concrete reason there is no serial banner.**
- **USB is in a *torn, partial* reset** — `CRL_APB.RST_LPD_TOP` (0xFF5E023C, `0x13`→`0x188617`): USB0 core+hiber released/APB held, USB1 core+APB released/hiber held (neither cleanly quiesced), plus RPU_AMBA, LPD APM, the **AFI_FM6 PS↔PL AXI bridge**, and LPD_SWDT freshly re-asserted. A reset applied to controllers *mid-transfer*.
- **The PMU is non-functional** — the PMU MicroBlaze is undebuggable (`Invalid context`), and `PMU_GLOBAL.ERROR_STATUS_2` (0xFFD80534) reads `0xFFFF4FFF`. **Correction/caveat:** that value is a **corrupted/undriven read, NOT latched errors** — its reserved bits [23:16] read all-1 (no latch source exists), and `ERROR_STATUS_1` reads clean `0x0`. Do **not** cite specific error bits; the diagnostic fact is that PMU_GLOBAL returns garbage → the PMU block is wedged/inaccessible.

**Root cause (pinned, = documented AMD hazard):** a plain `sudo reboot` issues a **raw CRL soft-SRST** (PMUFW `XPfw_ResetSystem`) that resets the PS components **without the PMU-orchestrated idle/isolation that drains outstanding AXI transactions.** Per AMD's ZynqMP Restart-Solution guidance (wiki 18841820): *"if PMU firmware resets all components in a subsystem while leaving unfinished transactions in the interconnect … the unfinished AXI transactions will remain in the interconnect, thus blocking all subsequent traffic … [and] will hang the system."* The un-drained transactions **deadlock the shared LPD/FPD interconnect**; the boot-from-reset (PMU ROM → CSU ROM → FSBL) then hangs on its own interconnect accesses before it can re-release clocks/resets (UART1 stays down → no banner). Only a **POR** — which resets the PMU power domain and re-initialises the whole interconnect — clears it. That USB's reset bit is (partly) set does **not** refute this: asserting a master's reset does not retract a transaction already in the fabric, and the interconnect has no per-peripheral reset (cleared only at LPD-level / POR).

**Fix implication:** the true fix is in **firmware** — the reset must run through the PMU-orchestrated idle/isolation/drain (or PMUFW error-management + WDT recovery, `ZYNQMP_WDT_RESTART`), not a bare CRL SRST. That is AMD's to fix. Our `kexec`/`kreboot` workaround sidesteps it by never invoking the reset; JTAG `rst -por` is the remote hardware recovery.

**Capture gaps for a follow-up:** 0xFF5E0100/0104 are *clock* regs (GEM_TSU/DLL, not RST_FPD_TOP — GEM-TSU clock is gated off in the wedge); the real FPD reset register is **CRF_APB `0xFD1A0100`** / APU resets `0xFD1A0104` — worth grabbing for the FPD-interconnect side.

### Reset handoff chain — where it dies (from source)
`reboot: Restarting system` is printed by Linux immediately *before* the reboot handler runs; everything after is firmware. On ZynqMP:
- Linux PSCI restart handler → **`SYSTEM_RESET` via SMC** (DT `psci { method = "smc" }`, firmware PSCIv1.1, SMCCC v1.5) → TF-A/BL31.
- BL31 `zynqmp_system_reset()` (`plat/xilinx/zynqmp/plat_psci.c`) does **not** reset the chip — it calls `pm_system_shutdown(RESET, scope)` and parks the A53s in `wfi()`.
- Scope defaults to **SYSTEM** (`drivers/firmware/xilinx/zynqmp.c`), so PMUFW `PmSystemShutdown()` → **`XPfw_ResetSystem()`**, which quiesces slaves (`PmResetSlaveStates()`) then writes `CRL_APB_RESET_CTRL[SOFT_RESET]` — a **soft system reset (SRST), not a POR** (`embeddedsw .../pm_core.c`, `xpfw_resets.c`).

So the component that actually resets the SoC is the **PMU (PMUFW `XPfw_ResetSystem`) via a CRL_APB soft-SRST**. "Dark, no PMU banner" means the re-boot never reached PMUFW's first UART write — the A53s are already parked in BL31 `wfi`, Linux is gone, and the SRST either didn't propagate to a fresh boot or the earliest boot stage hung.

### Leading root cause (hypothesis, ~55% confidence)
**An un-quiesced PS-GTR / USB3 (and/or DisplayPort/DPDMA) master prevents the soft-SRST from completing; only a POR clears it.** The SYSTEM-scope reboot fires `XPfw_ResetSystem()`; either the PMU hangs in its pre-reset AXI-drain waiting on a peripheral on the marginal USB3 PS-GTR lane or on the DP/DPDMA (whose teardown, `unregister bridge display...`, is the *last* kernel message before silence), **or** the SRST asserts but a stuck transaction / un-reset PS-GTR lane hangs the interconnect before any UART output. AMD's own restart guidance warns the PMU "must first ensure all on-going AXI transactions are terminated… otherwise it may lead to hanging of the interconnect and eventually hanging of the entire system" (Restart-solution wiki). A soft SRST is documented as "less hard than POR — several registers are unaffected," so a PS-GTR lane or its DWC3/DP master can hold state across it that only a POR clears. **This one mechanism explains every observed fact:** dark after `Restarting system`, no banner, POR-only recovery, reset-button-useless, headless/USB-rootfs/marginal-lane specificity, and reproduction on both firmwares.

### Why only a power-cycle recovers (and the reset button doesn't)
A plain `sudo reboot` takes the **SRST** path, not POR (AMD ties "`sudo reboot` → `POR_B`" specifically to the `xmutil bootfw_update` utility arming it; without that, reboot = SRST). The **KR260 carrier reset button is wired to `PS_SRST_B`** — i.e. it is *also* an SRST — which is exactly why pressing it does not recover the board. The wedged block lives in the set that SRST does not clear, so **only removing power (POR) resets it.** This is internally consistent with everything observed.

### Ruled out
- **Bootlin PMUFW TCM-ECC `memset` reboot hang** (the well-known ZynqMP reboot-hang): the fix (Neal Frager, AMD, "use 32-bit writes for tcm ecc init," Feb 2023, embeddedsw PR #250) **predates both** PMUFW 2023.2 and 2026.1, and the bug only affected crosstool-NG-built community PMUFW, never AMD's Vitis toolchain. **Reproduction on 2026.1 definitively excludes it.**
- **A firmware-version regression:** the SYSTEM_RESET→soft-SRST structure is unchanged across 2023.2→2026.1, and the fault reproduces on both — pointing to a hardware/peripheral-state/platform-config interaction, not a code regression.

### Likely shared root cause with Issue 4
The leading hypothesis for this wedge (an un-quiesced/un-reset **PS-GTR USB3** master) is the *same subsystem* as Issue 4 (a marginal PS-GTR USB3 lane on one board). These two reports may share one underlying PS-GTR/USB3 root cause on ZynqMP + soft-SRST + USB-hosted rootfs.

### Workarounds (for our headless deployment; each needs board confirmation)
- **kexec** — restarts the OS **without** invoking PSCI `SYSTEM_RESET`, so it never enters the hanging PMUFW path. Best "reboot without a power-cycle." Not a hardware reset (peripherals keep state).
- **Quiesce before reboot** — `sync; mount -o remount,ro /`, stop DMA users, and **unbind the DWC3 USB and DP/DPDMA drivers** just before `reboot`. If a quiesced reboot succeeds where a busy one hangs, that both mitigates and confirms H1.
- **`shutdown_scope`** — try `echo ps_only > /sys/devices/platform/firmware:zynqmp-firmware/shutdown_scope` vs `system` and compare (low confidence; default is already `system`).
- **`reboot=warm/cold` does NOT help** — ZynqMP TF-A implements no `SYSTEM_RESET2`, so all modes collapse to the same SYSTEM_RESET path.
- **JTAG `rst -por` (remote POR) — VERIFIED.** Over the carrier FTDI JTAG channel via a running `hw_server` + `xsdb`, `rst -por` performs a true power-on reset that both (a) serves as a wedge-free remote *reboot* and (b) *rescues* an already-wedged board — no physical access. Confirmed 2026-07-10 (rescued a 4-minute-wedged board in ~66 s). Requires JTAG access and correct per-board targeting via `targets -set -nocase -filter {jtag_cable_name =~ "*<carrier-serial>*" && name =~ "*a53*#0*"}` on a shared server.
- **Operational fallback** — a network-controlled power relay / smart PDU gating the 12 V input gives reliable remote reboot (the KR260 has no soft power button; POR is the only guaranteed reset).

### Disambiguating experiments (need hardware + a power-cycle to recover)
Boot rootfs from **SD/eMMC instead of USB**, or force the marginal port to **USB2** (no SuperSpeed), and retry reboot → if it then works, implicates the USB3 PS-GTR lane. Unbind DWC3 + DP just before reboot → if the hang disappears, confirms an un-quiesced master.

### Ask to AMD/Xilinx
1. Is a `reboot`-triggered SYSTEM-scope soft-SRST expected to complete on a KR260 running headless with the rootfs on USB, or is there a required quiescing / `shutdown_scope` / PMUFW config for it? 2. Is the PS-GTR/USB3 interaction across a soft-SRST a known hazard with a recommended mitigation? 3. Is there a supported way to make `sudo reboot` perform a `POR_B` (as `xmutil bootfw_update` reportedly does)? We found no public report of this specific KR260 warm-reboot wedge.

### Could not determine (flagged)
Whether a *successful* ZynqMP SYSTEM reset re-emits the PMU banner (sources conflict — if it does, its absence confirms the re-boot never fired); an explicit TRM statement that PS-GTR/SIOU survives SRST (inferred, not quoted); the exact `bootfw_update` mechanism that arms `POR_B`; and whether other KR260 + USB-rootfs + headless users reproduce this (no matching public report found).

---

## Issue 3 (MINOR) — Random MAC address on warm-reset boots

On warm-reset boots, U-Boot fails to read the SOM EEPROM MAC and Linux logs `macb ff0b0000.ethernet eth1: invalid hw address, using random`, so the interface gets a fresh random MAC and its DHCP lease/IP changes. Cold (power-on) boots get the correct Xilinx-OUI MAC (`00:0a:35:xx:xx:xx`). Likely related to the same warm-reset EEPROM/I²C access path as Issue 2. Worked around with a systemd `.link` file pinning the MAC.

---

## Issue 4 (INFORMATIONAL) — Boot FW 1.07 exposes a marginal PS-GTR USB3 lane on one board

On **one** of our two boards, boot FW 1.07 reports at U-Boot:
```
psgtr_phy phy@fd400000: lane 3 (type 1, protocol 3): PLL lock timeout
Bus usb@fe300000: probe failed, error -110
```
i.e. the PS-GTR PLL for the lane carrying the **second** USB3 controller (`usb@fe300000`) fails to lock, so that controller does not initialise in U-Boot. The SD reader is on the other controller (`usb@fe200000`) so boot is unaffected; in Linux both `dwc3` controllers bind. The **other** board, running the identical 1.07 image, shows **zero** PS-GTR errors across multiple boots, and neither board showed this on 1.02. This reads as a board-marginal USB3 lane 3 (SerDes / 26 MHz reference) exposed by 1.07's PS-GTR bring-up being stricter than 1.02's — not a firmware defect per se, but flagged in case 1.07 tightened a PLL-lock timeout. Not a functional blocker for headless use.

---

## Issue 5 (PRIMARY) — The stock image's own `cma=1000M` fails to reserve, so the PL can never be programmed

### Symptom
On the stock Certified Ubuntu 22.04 image, **loading any bitstream fails**, always, for every file and every format:

```
$ sudo fpgautil -b design.bit.bin -f Full
BIN FILE loading through FPGA manager failed
$ cat /sys/class/fpga_manager/fpga0/state
write error: 0xfffffff4                     # 0xfffffff4 = -12 = -ENOMEM
```

This is not a bitstream-format problem: a known-good `.bin` and a freshly generated one fail **identically**.

### Root cause
The image ships `cma=1000M` on its own kernel command line (see Issue 1 for the full cmdline), and **that reservation fails at boot**:

```
[    0.000000] cma: Failed to reserve 1000 MiB
[    0.000000] Memory: 3947032K/4194304K available (... 247272K reserved, 0K cma-reserved)
$ grep -i cma /proc/meminfo
CmaTotal:              0 kB
CmaFree:               0 kB
```

The ZynqMP FPGA manager DMA-allocates a contiguous buffer the size of the bitstream before writing it:

```
__alloc_pages+0x210/0x240
__dma_direct_alloc_pages.constprop.0+0x1bc/0x274
dma_direct_alloc+0x84/0x350
dma_alloc_attrs+0x84/0xec
zynqmp_fpga_ops_write+0x78/0x1a0
fpga_mgr_buf_load+0x70/0x160
fpga_mgr_firmware_load+0xdc/0x120
fpga_manager fpga0: Error while writing image data to FPGA
```

With `CmaTotal=0` that allocation falls back to the buddy allocator, whose largest block is `MAX_ORDER-1` (order-10 = **4 MB** — confirmed via `/proc/buddyinfo`). A KR260 full bitstream is **~7.8 MB**, so the allocation can never succeed. **The failure is arithmetic, not marginal.**

`cma=1000M` cannot be placed under the arm64 DMA-zone limit alongside the ~247 MB of existing low reservations. Notably the vendor `boot.scr` **already uses the safe `512M` for the KD240 branch** and only assigns `1000M` to the K26/KR260 branch:

```
if test $kria = "KD"; then
        cma="512M"
elif  test -n $kria; then
        cma="1000M"          # <-- KR260 takes this; it fails to reserve
```

### Impact
**Out of the box, a KR260 on this image cannot program its PL at all** — the headline use case of the product. Silent, too: nothing at boot warns that CMA is absent; the failure only surfaces later as an opaque `-ENOMEM` from `fpgautil`.

### Confirmed fix / workaround
Reduce the CMA reservation to a size that actually reserves (512 MiB — the vendor's own KD240 value; ~8 MB would suffice):

```
sudo sed -i 's|^LINUX_KERNEL_CMDLINE=.*|LINUX_KERNEL_CMDLINE="cpuidle.off=1 cma=512M"|' /etc/default/flash-kernel
sudo flash-kernel
strings /boot/firmware/boot.scr.uimg | grep -o cma=512M     # must print before you trust it
```

`flash-kernel` appends `LINUX_KERNEL_CMDLINE` **after** the built-in args, and the kernel's `cma=` parser takes the **last** occurrence — so the appended `cma=512M` wins over the built-in `cma=1000M` without touching `boot.scr` (which flash-kernel regenerates anyway).

Verified on kr260-02 (2026-07-16):

```
[    0.000000] cma: Reserved 512 MiB at 0x000000005fc00000
CmaTotal:         524288 kB
$ sudo fpgautil -b die_b.bit.bin -f Full
Time taken to load BIN is 135.000000 Milli Seconds
BIN FILE loaded through FPGA manager successfully
$ cat /sys/class/fpga_manager/fpga0/state
operating
```

Applying the new cmdline needs a real early-boot; `kexec` (see `LOCKUP_RECOVERY.md`, Layer 1) does that in ~65 s without triggering the Issue-2 reboot wedge.

### Suggested vendor action
Either lower the K26/KR260 branch to a reservation that fits (e.g. `512M`, matching KD240), or make a failed CMA reservation **loud** — a boot-time error rather than a silent `CmaTotal=0` that surfaces much later as `-ENOMEM` from the FPGA manager.

---

## Issue 6 (PRIMARY) — Stock boot firmware leaves the PS→PL AFI ports at 128-bit, silently discarding 3 of every 4 words to a 32-bit PL design

### Symptom
Every AXI/APB register in the PL whose address is **not 16-byte aligned** reads `0x00000000` and
ignores writes. 16-byte-aligned registers work perfectly. There is no bus error, no timeout and no
warning — the accesses simply evaporate, so the PL looks alive but subtly, catastrophically wrong.

### Root cause
On Kria (K26/KR260) the **stock boot firmware configures the PS; a user `psu_init` never runs**.
The firmware leaves the PS↔PL AFI port widths at **128-bit**:

```
LPD  afi_fs  0xFF419000  = 0x00000200   -> [9:8] = 2 = 128-bit    (M_AXI_HPM0_LPD)
FPD  afi_fs  0xFD615000  = 0x00000a00   -> [9:8] = 2 = 128-bit    (M_AXI_HPM0/1_FPD)
```

A block design built for **32-bit** PS master ports then sees one 128-bit (= 16-byte) beat per
transaction, and **only the first 32-bit word of each beat reaches the PL**. Address bits [3:2] are
effectively dead. Both the control plane (LPD) and the data plane (FPD) are affected.

Zynq-7000 boards are immune: their PS7 init *does* run and sets the port width to match the design.

### Impact
Total, silent, and easy to misdiagnose as an RTL, timing or hardware fault. On our two boards this
made an inter-board link impossible to bring up **for weeks**: the register that enables the
link-layer (`0x8403_0208`) and the one holding the lane mask (`0x8403_0214`) are both non-16B-aligned,
so they read 0 and ignored writes, while the 16B-aligned status registers reported a plausible-looking
"dead link". We chased cabling, pinout, bitstream staleness, clocking and reset for a full night
before finding it. Nothing in the tools reports a mismatch between the AFI width and the BD width.

### Reproduction / proof (turn the bug on and off at will)
```
# a hardwired constant in our PL and a register with a known reset value:
#   0x8403_0204 must read 0x00000001   (hardwired 32'h1)
#   0x8403_0214 must read 0x0000e4e4   (reset value)

devmem 0xFF419000 32 0x200      # force 128-bit (the stock state)
  -> 0x8403_0204 reads 0x00000000     # constant reads ZERO
  -> 0x8403_0214 reads 0x00000000

devmem 0xFF419000 32 0x000      # 32-bit, matching the BD
  -> 0x8403_0204 reads 0x00000001     # correct
  -> 0x8403_0214 reads 0x0000e4e4     # correct
```

### Workaround
Clear `[9:8]` on both AFI registers after boot — see `scripts/afi-width-fix.py`, installed as a
`systemd` oneshot (`tidelink-afi.service`) before `basic.target`. No rebuild is required; the PL
design was correct all along.

```
0xFF419000 &= ~0x300     # LPD / control plane -> 32-bit
0xFD615000 &= ~0x300     # FPD / data plane    -> 32-bit
```

### Suggested vendor action
Make the mismatch **loud**, not silent. Either (a) have the tools emit a warning when a bitstream's
PS master-port widths disagree with the AFI width the firmware will program, (b) have the FPGA
manager / DFX flow program the AFI widths from the bitstream's own metadata at load time, or
(c) document prominently that on Kria the stock firmware owns the AFI configuration and a 32-bit BD
must reprogram it. Silently dropping 3 of every 4 words is the worst possible failure mode: it looks
exactly like a hardware fault.

---

## Cross-reference (already-known, not re-reporting)
- **DisplayPort dead on Ubuntu 22.04 with boot FW ≥1.04 until kernel ≥ 5.15.0-1052** — Launchpad #2114250. Relevant because updating boot firmware for a headless board silently removes DP as a fallback.

---

## Environment capture commands (for AMD triage)
```
cat /proc/cmdline
cat /proc/version
dmesg | grep -iE 'psci|cpuidle|ttyPS|zynqmp|dpsub'
xmutil bootfw_status                 # boot FW version in QSPI A/B slots
cat /sys/firmware/devicetree/base/cpus/cpu@0/cpu-idle-states   # CPU_SLEEP_0 phandle
```
