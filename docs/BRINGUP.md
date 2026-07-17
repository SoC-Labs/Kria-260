# KR260 Headless Bring-Up Runbook

How to bring up an AMD/Xilinx **Kria KR260** Starter Kit (revB, K26 SOM) running
**Certified Ubuntu 22.04** as a **headless** board with a working serial console — and
how to reproduce the fixes on a second board.

Written from the bring-up of `kr260-01` on 2026-07-10. The board attaches to a host
(`mapstone-dev`) over USB (FTDI serial + JTAG) and to the LAN over Ethernet.

---

## TL;DR

A stock KR260 + Certified Ubuntu 22.04 (kernel `5.15.0-1027-xilinx-zynqmp`, boot FW 1.02)
has **two independent defects** that bite a headless deployment:

1. **Headless boot hang.** With no DisplayPort monitor attached, Linux hangs in the
   initramfs — `wait-for-root` soft-locks in `kick_all_cpus_sync()` /
   `smp_call_function_many_cond`: an IPI broadcast that a secondary core never acknowledges
   because it entered the PSCI power-down idle state (`CPU_SLEEP_0`,
   `arm,psci-suspend-param = <0x40000000>`) and did not wake. A monitor "fixes" boot only
   because the DisplayPort pipeline's vblank interrupts keep the cores out of deep idle —
   `zynqmp_dpsub` itself is innocent (it probes fine headless).
   **Fix:** boot with `cpuidle.off=1`.

2. **Warm-reboot PMU wedge.** `sudo reboot` prints `reboot: Restarting system` and the board
   goes dark — no PMU banner, no U-Boot. Only a full **power-cycle** recovers it. Matches the
   documented ZynqMP PMUFW reboot hang. May be fixed by a boot-FW update (see §5).

Plus a nuisance: on warm-reset boots U-Boot fails to read the MAC from EEPROM, so Linux logs
`macb ... invalid hw address, using random` and the DHCP lease (hence IP) moves. Cold boots
get the real Xilinx-OUI MAC.

Note: **PYNQ is irrelevant** to any of this — it is a userspace overlay framework, not a boot
or console component. The serial `login:` prompt already works out of the box (systemd's
getty generator enables `serial-getty@ttyPS1` on the active console); the board just never
reached userspace before fix #1.

---

## Hardware facts (KR260 revB / K26 SOM)

| Thing | Value |
|---|---|
| Console UART | `ttyPS1` @ `0xff010000`, **115200 8N1** |
| Console on the FTDI | interface **if01** → `/dev/serial/by-id/usb-Xilinx_KR_Carrier_Card_<SERIAL>-if01-port0`. if00 = JTAG (no tty); if02/if03 emit **nothing** |
| Root filesystem | microSD **behind a USB card reader** → `/dev/sda2`, `LABEL=writable`. U-Boot `boot_targets` are USB-only (no `mmc`) |
| U-Boot env | **none writable** — `Loading Environment from nowhere`. So nothing typed at `ZynqMP>` persists, and `saveenv` has nothing to write to. Persistence lives on the SD card |
| Kernel cmdline owner | `flash-kernel` (its db has a `ZynqMP *KR260*` entry). It regenerates `/boot/firmware/boot.scr.uimg` + `image.fit`; the runtime `boot.scr` **overwrites** `bootargs`, so text-editing it by hand is futile |
| LAN interface | `eth1` = `ff0b0000.ethernet` (driver `macb`, path `platform-ff0b0000.ethernet`) |
| Boot firmware (as shipped) | 1.02 = 2023.2 (PMUFW 2023.2, TF-A v2.8, U-Boot 2023.01) |

---

## Standing operational rules

- **Power-cycle, never `sudo reboot`,** until the boot firmware is updated past 1.02 (defect #2).
- Keep `image.fit.bak` / `boot.scr.uimg.bak` on the FAT boot partition — they are loadable from
  the `ZynqMP>` prompt over serial alone if a regenerated `image.fit` ever fails to boot.
- To inject a **one-shot** kernel arg at the U-Boot prompt, bypass `boot.scr` (it overwrites
  `bootargs`):
  ```
  usb start
  load usb 0:1 0x10000000 image.fit
  setenv bootargs root=LABEL=writable rootwait earlycon console=ttyPS1,115200 console=tty1 clk_ignore_unused uio_pdrv_genirq.of_id=generic-uio xilinx_tsn_ep.st_pcp=4 cma=1000M <YOUR_ARG>
  bootm 0x10000000#conf-smk-k26-revA-sck-kr-g-revB
  ```
  Autoboot delay is 2 s; spam Enter to reach the prompt.

---

## Bring-up procedure for a new board (`kr260-0N`)

### 0. Find its console
Each board's FTDI has a unique serial, so don't assume a `/dev/ttyUSBn` number:
```
ls -l /dev/serial/by-id/ | grep KR_Carrier      # note this board's serial; if01 is the console
```
Open it at 115200 8N1 (`sudo screen /dev/ttyUSB<if01> 115200`, or `picocom -b 115200`).

### 1. Get it booted the first time (headless)
Out of the box it hangs in the initramfs with no monitor. Either:
- **Easy:** plug in a DisplayPort monitor, boot, log in, pull the monitor after step 2; or
- **No monitor:** interrupt U-Boot and boot once with the fix injected (see the one-shot recipe
  under *Standing operational rules*, with `<YOUR_ARG>` = `cpuidle.off=1`).

Default login on the stock image is `ubuntu` / `ubuntu` (forced change on first login).

### 2. Make the headless fix permanent — AND fix CMA, or the PL can never be programmed
```
sudo cp -n /boot/firmware/image.fit     /boot/firmware/image.fit.bak
sudo cp -n /boot/firmware/boot.scr.uimg /boot/firmware/boot.scr.uimg.bak
sudo sed -i 's|^LINUX_KERNEL_CMDLINE=.*|LINUX_KERNEL_CMDLINE="cpuidle.off=1 cma=512M"|' /etc/default/flash-kernel
sudo flash-kernel                                              # rebuilds boot.scr.uimg + image.fit
strings /boot/firmware/boot.scr.uimg | grep -o cpuidle.off=1   # MUST print cpuidle.off=1 before you trust it
strings /boot/firmware/boot.scr.uimg | grep -o cma=512M        # MUST print cma=512M    before you trust it
```
`flash-kernel` appends `LINUX_KERNEL_CMDLINE` after the built-in args, so the result is the
stock cmdline plus these. A harmless `Couldn't find DTB` warning is expected — the
device trees come from `image-kria.its`, not a standalone DTB.

**Why `cma=512M` is not optional (2026-07-16 — see XILINX_ISSUE_REPORT.md Issue 5).**
The stock image ships `cma=1000M`, and **that reservation fails** (`cma: Failed to reserve
1000 MiB` → `CmaTotal: 0 kB`). The ZynqMP FPGA manager DMA-allocates a contiguous buffer the
size of the bitstream, so with no CMA it falls back to the buddy allocator, whose largest
block is 4 MB — a ~7.8 MB KR260 bitstream **can never** be loaded. Every `fpgautil` attempt
dies as `write error: 0xfffffff4` (`-ENOMEM`), *identically for a known-good .bin*, which
makes it look like a bitstream-format problem. It is not. `512M` is the vendor's own KD240
value and reserves cleanly (~8 MB would do).

The `cma=` parser takes the **last** occurrence, so the appended `cma=512M` beats the
built-in `cma=1000M`. Do **not** try to hand-edit `boot.scr` — flash-kernel regenerates it.

Verify after the next boot:
```
grep -i cma /proc/meminfo        # CmaTotal must be 524288 kB, NOT 0
```

**Applying a cmdline change needs a real early-boot, not a `reboot`** (which wedges — Issue 2).
Use `kexec` with the new args, which re-runs early init in ~65 s without the PMUFW reset:
```
NEW=$(sed 's/cma=1000M/cma=512M/' /proc/cmdline)
sudo kexec -l "/boot/vmlinuz-$(uname -r)" --initrd="/boot/initrd.img-$(uname -r)" --command-line="$NEW"
sudo systemctl kexec
```
(Plain `sudo kreboot` uses `--reuse-cmdline` and will **not** pick up the change.)

### 2b. Load a bitstream (the PL path — no PYNQ on this image)
The board runs **plain Ubuntu 22.04 with no `pynq` installed** — `import pynq` throws. Any
tooling that assumes `pynq.Overlay` will not work here. The PL path is the Kria/DFX one:
```
# .bit -> .bin is a 127-byte header strip. Do NOT byte-swap: that is the zynq-7000
# fpga-manager format; ZynqMP wants the raw payload.
python3 -c "open('d.bit.bin','wb').write(open('d.bit','rb').read()[127:])"
scp d.bit.bin ubuntu@<board>:~/
sudo fpgautil -b ~/d.bit.bin -f Full        # ~135 ms; expect "loaded ... successfully"
cat /sys/class/fpga_manager/fpga0/state     # expect: operating
```

### 3. Pin the MAC → stable IP (read *this* board's own MAC first)
```
IFACE=$(ip -o -4 addr show | awk '$4 ~ /^10\.22\./ {print $2; exit}')     # the LAN interface
MAC=$(cat /sys/class/net/$IFACE/address)                                  # must be a real 00:0a:35:* Xilinx MAC
PATHID=$(udevadm info -q property /sys/class/net/$IFACE | sed -n 's/^ID_PATH=//p')
printf '[Match]\nPath=%s\n\n[Link]\nMACAddress=%s\nNamePolicy=keep kernel\n' "$PATHID" "$MAC" \
  | sudo tee /etc/systemd/network/10-$IFACE-mac.link
```
If the MAC is **not** `00:0a:35:*` you captured a warm-reset boot (random MAC) — cold-boot the
board and re-read it. A stable MAC gives a stable DHCP lease, which is the real fix for the
wandering IP. Validate without rebooting: `sudo udevadm test-builtin net_setup_link /sys/class/net/$IFACE`.

### 4. Rename
```
sudo hostnamectl set-hostname kr260-0N
grep -q 127.0.1.1 /etc/hosts || sudo sed -i '1a 127.0.1.1 kr260-0N' /etc/hosts
```
The static hostname is what the board sends over DHCP, so on the next boot its DNS name becomes
`kr260-0N.<domain>` and the old name stops resolving. Find it by the new name, by IP, or over serial.

### 5. Verify
**Power-cycle** (not `reboot`), monitor unplugged. On serial you should reach `kr260-0N login:`.
Over SSH confirm: `cat /proc/cmdline` ends in `cpuidle.off=1`; `cat /sys/class/net/$IFACE/address`
is the real MAC; `cat /sys/devices/system/cpu/cpuidle/current_driver` prints nothing (idle off);
`hostname` is `kr260-0N`.

---

## 5. Optional: boot firmware update 1.02 → 1.07 (2026.1)

**Why:** no TF-A/PMUFW changelog between 2023.2 and 2026.1 names a PSCI `CPU_SUSPEND`/IPI-wakeup
fix, so this is *speculative* for defect #1 — but defect #2 (warm-reboot PMU wedge) is exactly
the class a PMUFW update tends to fix.

**Cost / caveat:** boot FW ≥1.04 breaks **DisplayPort** on Ubuntu 22.04 until the kernel is
≥ `5.15.0-1052` (Launchpad #2114250). Irrelevant to headless boot (that runs off `cpuidle.off=1`
on the SD card, independent of firmware), but it removes the monitor as a fallback. Upgrade the
kernel first if you want DP back.

**Get the file:** the **`-kr-`** per-kit image (the `-kv-` one carries the KV260 device tree).
AMD's download is account/EULA-gated (browser only). Verify inside with
`strings BOOT-*.bin | grep -iE 'kr260|2026.1'`.

**Procedure** (needs a physical power-cycle midway — cannot be done fully remotely):
```
# 1. copy the bin to the board, verify checksum
scp BOOT-k26-smk-kr-sdt-*.bin ubuntu@<board>:/tmp/boot.bin
ssh ubuntu@<board> sha256sum /tmp/boot.bin          # compare to the source

# 2. inspect current slots (A/B), then stage into the INACTIVE slot
sudo xmutil bootfw_status                            # note which slot is active
sudo xmutil bootfw_update -i /tmp/boot.bin           # writes the inactive slot, marks it "requested" + trial

# 3. POWER-CYCLE (not reboot). ImgSel boots the new slot ONCE as a trial.
#    After it reaches Linux, CONFIRM within that same boot session:
sudo xmutil bootfw_update -v                          # marks the new slot permanently bootable
sudo xmutil bootfw_status                             # verify new revision is active + both slots Bootable
```
**Rollback / safety:** `bootfw_update -i` only writes the *inactive* slot, so the running
firmware is never at risk and is untouched by an interrupted write. After staging, the new slot
shows **`Non Bootable` + `Requested`** — that is the trial state: it boots once, and **if you do
not run `-v`, the next power-cycle auto-reverts** to the old slot. There is **no `-r` flag** — the
auto-revert *is* the rollback. Worst case, hold **FWUEN** at power-on for the golden Ethernet
recovery tool at `http://192.168.0.111` (set your host to `192.168.0.x`).

---

## Record: what was done on `kr260-01` (2026-07-10)

- **Headless fix:** `LINUX_KERNEL_CMDLINE="cpuidle.off=1"` in `/etc/default/flash-kernel` +
  `flash-kernel`. Verified 2-for-2 headless to `login:`. Originals saved as `*.bak` on the FAT partition.
- **MAC pinned:** `/etc/systemd/network/10-eth1-mac.link` → `00:0a:35:29:1f:81` (and eth0 →
  `00:0a:35:28:c0:f1`). Held across a cold boot.
- **Renamed:** hostname `kr260-01`; DNS `kr260-01.ecs.soton.ac.uk` → `10.22.24.159`.
- **Boot FW:** updated to **1.07** (2026.1) in Image B and confirmed with `-v` (2026-07-10);
  Image A retains 1.02 as rollback. Booted headless on 1.07 with `cpuidle.off=1`, no hang.

### Open tasks
- **Reboot wedge (defect #2) — INVESTIGATED & CHARACTERISED (2026-07-10).** `sudo reboot` wedges
  the PMU on **both** FW 1.02 and 1.07: `reboot: Restarting system` → dark, **no self-recovery
  (measured ~4 min)**, recovered only by a POR. Root cause (leading, ~55%): `reboot` → PSCI
  `SYSTEM_RESET` → TF-A → PMUFW **soft-SRST**, which does not clear an un-quiesced PS-GTR/USB3 (or
  DisplayPort) master; only a POR does — and the carrier reset button is also `PS_SRST_B`, so it
  can't recover it either. Bootlin PMUFW TCM-ECC bug ruled out. Full analysis + AMD asks:
  `docs/KR260_XILINX_ISSUE_REPORT.md` Issue 2. **Operational rule: never `reboot` these boards**
  (either firmware) — do a POR instead.
  - **Remote reboot / wedge-recovery via JTAG (VERIFIED 2026-07-10).** mapstone-dev runs
    `hw_server` (tcp::3121) and the carrier FTDI JTAG (interface 0) is free, so a true POR can be
    issued remotely — it both reboots a healthy board and **rescues a wedged one** (rescued a
    4-min-wedged kr260-02 in ~66 s, no physical access):
    ```
    source /tools/Xilinx/2025.2/Vivado/settings64.sh
    printf 'connect\ntargets -set -nocase -filter {jtag_cable_name =~ "*%s*" && name =~ "*a53*#0*"}\nrst -por\nafter 3000\ndisconnect\n' <CARRIER-SERIAL> | xsdb
    ```
    Carrier serials: kr260-01 = `XFL1MHS3ZB1P`, kr260-02 = `XFL1EAUJ5SPO`. **ALWAYS filter by
    `jtag_cable_name`** — the hw_server is shared (also a KU115 + four PYNQ-Z2 boards); an
    unfiltered `rst` could reset someone else's board.
  - **Software reboot that avoids the wedge — `sudo kreboot` (INSTALLED + VERIFIED on both boards,
    2026-07-10).** `kexec` reloads the OS without ever calling the PSCI/PMUFW reset that hangs.
    Installed: `kexec-tools` + `/usr/local/sbin/kreboot` (`kexec -l <kernel> --reuse-cmdline;
    systemctl kexec`). **Use `sudo kreboot` instead of `sudo reboot`** — clean reboot in ~65 s, no
    JTAG needed. Caveats: it is NOT a hardware reset (PL/peripherals keep state — use JTAG
    `rst -por` for a true POR); and it is a **workaround**, not a firmware fix (the underlying
    PMUFW soft-SRST wedge is untouched). Root cause narrowed by experiment: `shutdown_scope=ps_only`
    and unbinding the secondary USB3 controller did NOT help; the un-quiescible rootfs-USB master or
    a firmware-level PS-GTR/interconnect issue is the leading suspect. Deeper root-cause needs a
    JTAG PMU-halt during the wedge (see `KR260_XILINX_ISSUE_REPORT.md`).
- **Is defect #1 (cpuidle hang) fixed on 1.07?** Still untested (a bring-up catcher missed the
  autoboot window on the retry). To check, boot headless once *without* `cpuidle.off=1` (U-Boot
  one-shot). Keep `cpuidle.off=1` regardless.
- **Evidence boot + bug report** (defect #1 on 1.02): `modprobe.blacklist=zynqmp_dpsub
  softlockup_all_cpu_backtrace=1 csdlock_debug=1`, cpuidle on — dumps the deaf core, exonerates
  dpsub. Then file against `linux-xilinx-zynqmp` (Launchpad) + AMD Kria forum.

### Firmware-variant gotcha (learned on kr260-02, 2026-07-10)
The 1.07 boot firmware ships as **per-kit** images: the **`-kr-`** bin carries the KR260 device
tree; the **`-kv-`** bin carries the KV260 tree. Flashing the `-kv-` bin onto a KR260 makes the
firmware mis-detect the board as `Model: ZynqMP KV260 revB`, its boot script fails
(`JTAG: SCRIPT FAILED`), and it does not reach Linux. Always verify the bin with
`strings BOOT-*.bin | grep -iE 'kr260|kv260'` before `bootfw_update`, and re-check
`Model:` on the next boot. Recovery from a mis-flash: if the bad image was staged with
`bootfw_update -i` but never confirmed (`-v`), a power-cycle auto-reverts to the previous slot.
- **Evidence boot** for a bug report: one boot with `modprobe.blacklist=zynqmp_dpsub
  softlockup_all_cpu_backtrace=1 csdlock_debug=1` and cpuidle *on* — exonerates dpsub
  independently and dumps the deaf core's backtrace. Turns "cpuidle.off=1 fixed it" into a
  filable Launchpad / AMD report.
- **File the report** against `linux-xilinx-zynqmp` (Launchpad) + AMD Kria forum — no prior
  report of this headless hang exists.


## Step 3 — Fix the AFI port widths (MANDATORY before any PL register access)

The stock boot firmware leaves the PS↔PL AFI ports at **128-bit**. If your block design uses
**32-bit** PS master ports, only the first 32-bit word of every 16-byte beat reaches the PL:
**every register that is not 16-byte aligned silently reads 0 and ignores writes.** No bus error,
no warning. See `docs/XILINX_ISSUE_REPORT.md` Issue 6.

```
sudo ./scripts/afi-width-fix.py          # clears [9:8] on 0xFF419000 (LPD) and 0xFD615000 (FPD)
```

Installed permanently as a systemd oneshot:

```
sudo install -m 0755 scripts/afi-width-fix.py /usr/local/sbin/tidelink-afi-fix.py
sudo systemctl enable --now tidelink-afi.service
```

**Canary — do this before debugging anything else on a Kria.** Pick two PL registers with known
non-zero values, at least one of them *not* 16-byte aligned, and read them. For TideLink:

```
0x8403_0204  must read 0x00000001    # a hardwired constant
0x8403_0214  must read 0x0000e4e4    # a known reset value
```

If either reads `0x00000000`, the AFI is wrong and **every other register reading you take is a lie**.

