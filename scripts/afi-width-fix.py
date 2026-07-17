#!/usr/bin/env python3
"""Set the ZynqMP PS<->PL AFI port widths to 32-bit to match the TideLink block design.

WHY THIS EXISTS (2026-07-17): on Kria (K26/KR260) the stock boot firmware configures
the PS -- our psu_init NEVER runs -- and it leaves the AFI port widths at 128-bit.
The TideLink BD is 32-bit. 128 bits = 16 bytes = one beat, so ONLY the first 32-bit
word of every 16-byte beat reaches the PL: every APB register whose address is not
16-byte aligned silently reads 0 and ignores writes. That killed 3/4 of the control
plane -- including 0x8403_0208 (the FC/LL bootstrap) and 0x8403_0214 (the lane mask)
-- so the link could never be brought up, on either the control OR the data plane.

Pynq-Z2 is immune: its PS7 init DOES run and sets 32-bit.

CANARY (cheapest possible check on any KR260, before debugging anything else):
    0x8403_0204 must read 0x00000001   (a hardwired constant in Wlink)
    0x8403_0214 must read 0x0000e4e4   (the lane mask reset value)
  If either reads 0, the AFI is wrong and EVERY other register reading is a lie.
"""
import mmap, os, struct, sys

AFI = ((0xFF419000, "LPD (HPM0_LPD - control plane)"),
       (0xFD615000, "FPD (HPM0/1_FPD - data plane)"))

def _acc(f, a):
    return mmap.mmap(f, 0x1000, offset=a & ~0xFFF), (a & 0xFFF)

def rd(f, a):
    m, o = _acc(f, a); m.seek(o); v = struct.unpack("<I", m.read(4))[0]; m.close(); return v

def wr(f, a, v):
    m, o = _acc(f, a); m.seek(o); m.write(struct.pack("<I", v)); m.close()

def main():
    f = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    rc = 0
    for addr, tag in AFI:
        before = rd(f, addr)
        wr(f, addr, before & ~0x300)          # [9:8] = 00 => 32-bit
        after = rd(f, addr)
        width = {0: "32-bit", 1: "64-bit", 2: "128-bit", 3: "reserved"}[(after >> 8) & 3]
        print("afi %-34s 0x%08x -> 0x%08x  [9:8] => %s" % (tag, before, after, width))
        if (after >> 8) & 3 != 0:
            print("  ERROR: AFI did not take", file=sys.stderr); rc = 1
    os.close(f)
    return rc

if __name__ == "__main__":
    sys.exit(main())
