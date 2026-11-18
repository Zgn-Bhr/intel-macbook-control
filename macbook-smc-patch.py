#!/usr/bin/python3 -I
"""macbook-smc-patch - let the macsmc-acpi driver run on Macs without a T2 chip.

From linux 7.3.3 the macsmc-acpi driver refuses Macs that have no T2 chip: its probe checks
the kernel flag `has_apple_t2_chip` and returns -ENODEV, and linux-cachyos doesn't build the
old applesmc driver, so a pre-T2 Mac is left without fan control, sensors or keyboard light.

This takes each kernel's stock macsmc-acpi module, finds that one check by its relocation
against `has_apple_t2_chip`, and replaces the conditional jump after it with a NOP of the same
length. It only patches when the code around the relocation is exactly the expected
`cmpb $1, has_apple_t2_chip(%rip)` followed by `jne` (or `cmpb $0` followed by `je`); anything
else is left alone. The result goes to /usr/lib/modules/<kernel>/updates/, which modprobe
prefers over the stock file, with the (no longer matching) signature removed.

  macbook-smc-patch                     patch every installed kernel that needs it
  macbook-smc-patch --reload            ... and reload the driver now if it isn't running
  macbook-smc-patch --uninstall         remove all patched copies
  macbook-smc-patch --check FILE [OUT]  only analyse a module file (and write the patched copy to OUT)
  macbook-smc-patch --hook-install      pacman hook: kernels from the module paths on stdin
  macbook-smc-patch --hook-remove       pacman hook: drop patched copies of removed kernels
"""
import glob
import gzip
import os
import re
import struct
import subprocess
import sys

MODULES = "/usr/lib/modules"
STOCK = "kernel/drivers/platform/x86/macsmc-acpi.ko"
OVERRIDE = "updates/macsmc-acpi.ko"
STATUS = "/var/lib/macbook-control/smc-patch.status"
SIG_MAGIC = b"~Module signature appended~\n"
SYMBOL = b"has_apple_t2_chip"

R_X86_64_PC32 = 2
SHT_SYMTAB, SHT_RELA = 2, 4
SHF_EXECINSTR = 0x4
NOP = {6: bytes.fromhex("660f1f440000"), 2: bytes.fromhex("6690")}


class Refused(Exception):
    """The module doesn't look exactly as expected, so it is not touched."""


def decompress(path):
    with open(path, "rb") as f:
        raw = f.read()
    if path.endswith(".zst"):
        return subprocess.run(["zstd", "-dcq"], input=raw, capture_output=True, check=True).stdout
    if path.endswith(".xz"):
        return subprocess.run(["xz", "-dcq"], input=raw, capture_output=True, check=True).stdout
    if path.endswith(".gz"):
        return gzip.decompress(raw)
    return raw


def strip_signature(data):
    if not data.endswith(SIG_MAGIC):
        return data
    # struct module_signature (12 bytes, sig_len big-endian at its end) sits right before the magic
    (sig_len,) = struct.unpack(">I", data[-len(SIG_MAGIC) - 4:-len(SIG_MAGIC)])
    end = len(data) - len(SIG_MAGIC) - 12 - sig_len
    if end <= 0 or data[:4] != b"\x7fELF":
        raise Refused("unexpected signature layout")
    return data[:end]


def sections(data):
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise Refused("not a 64-bit little-endian ELF module")
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    out = []
    for i in range(shnum):
        name, typ, flags, _addr, offset, size, link, info, _align, entsize = \
            struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize)
        out.append({"name": name, "type": typ, "flags": flags, "offset": offset, "size": size,
                    "link": link, "info": info, "entsize": entsize})
    return out


def c_string(data, offset):
    return data[offset:data.index(b"\0", offset)]


def find_check_sites(data):
    """File offsets of every instruction that reads has_apple_t2_chip, with what follows it."""
    secs = sections(data)
    symtab = next((s for s in secs if s["type"] == SHT_SYMTAB), None)
    if symtab is None:
        raise Refused("module has no symbol table")
    strtab = secs[symtab["link"]]
    target = None
    for i in range(symtab["size"] // 24):
        st_name, = struct.unpack_from("<I", data, symtab["offset"] + i * 24)
        if c_string(data, strtab["offset"] + st_name) == SYMBOL:
            target = i
    if target is None:
        return []
    sites = []
    for rela in (s for s in secs if s["type"] == SHT_RELA):
        code = secs[rela["info"]]
        if not code["flags"] & SHF_EXECINSTR:
            continue
        for i in range(rela["size"] // 24):
            r_offset, r_info, _ = struct.unpack_from("<QQq", data, rela["offset"] + i * 24)
            if r_info >> 32 == target:
                if r_info & 0xFFFFFFFF != R_X86_64_PC32:
                    raise Refused("has_apple_t2_chip is referenced in an unexpected way")
                sites.append(code["offset"] + r_offset)
    return sites


def analyse(data):
    """Return (state, patches): state is 'needs-patch', 'already-patched' or 'no-check'."""
    sites = find_check_sites(data)
    if not sites:
        return "no-check", []
    patches, already = [], 0
    for at in sites:
        # cmpb $imm8, disp32(%rip): 80 3d <disp32> <imm8>; the relocation covers disp32
        if data[at - 2:at] != b"\x80\x3d":
            raise Refused(f"the check at 0x{at:x} isn't the expected cmpb instruction")
        imm, nxt = data[at + 4], at + 5
        jump = data[nxt:nxt + 2]
        # cmp $1 + jne, or cmp $0 + je: both jump away when the Mac has no T2 chip
        refusing = {1: (b"\x0f\x85", b"\x75"), 0: (b"\x0f\x84", b"\x74")}.get(imm)
        if refusing is None:
            raise Refused(f"unexpected comparison value {imm} at 0x{at:x}")
        if jump == refusing[0]:
            patches.append((nxt, NOP[6]))
        elif jump[:1] == refusing[1]:
            patches.append((nxt, NOP[2]))
        elif data[nxt:nxt + 6] == NOP[6] or data[nxt:nxt + 2] == NOP[2]:
            already += 1
        else:
            raise Refused(f"no conditional jump after the check at 0x{at:x} (found {jump.hex()})")
    if not patches:
        return "already-patched", []
    if already:
        raise Refused("module is only partly patched")
    return "needs-patch", patches


def patched(data):
    data = bytearray(strip_signature(data))
    state, patches = analyse(bytes(data))
    for offset, nop in patches:
        data[offset:offset + len(nop)] = nop
    return state, bytes(data), len(patches)


def stock_module(kver):
    for ext in (".zst", ".xz", ".gz", ""):
        path = f"{MODULES}/{kver}/{STOCK}{ext}"
        if os.path.exists(path):
            return path
    return None


def depmod(kver):
    subprocess.run(["depmod", kver], check=False)


def record(kver, result):
    lines = {}
    try:
        with open(STATUS) as f:
            lines = dict(line.rstrip("\n").split(": ", 1) for line in f if ": " in line)
    except OSError:
        pass
    if result is None:
        lines.pop(kver, None)
    else:
        lines[kver] = result
    os.makedirs(os.path.dirname(STATUS), exist_ok=True)
    with open(STATUS, "w") as f:
        f.write("".join(f"{k}: {v}\n" for k, v in sorted(lines.items())))


def patch_kernel(kver):
    stock = stock_module(kver)
    override = f"{MODULES}/{kver}/{OVERRIDE}"
    if not stock:
        return f"{kver}: no macsmc-acpi driver, nothing to do"
    try:
        state, data, count = patched(decompress(stock))
    except (Refused, subprocess.CalledProcessError, ValueError, struct.error) as e:
        if os.path.exists(override):          # a copy made from an older build no longer fits
            os.remove(override)
            depmod(kver)
        record(kver, f"refused: {e}")
        return f"{kver}: NOT patched, the stock driver stays ({e}). Fan control needs the LTS kernel."
    if state == "no-check":
        if os.path.exists(override):
            os.remove(override)
            depmod(kver)
        record(kver, "not needed")
        return f"{kver}: driver has no T2 check, stock driver used"
    if state == "already-patched":
        record(kver, "patched")
        return f"{kver}: stock driver already allows this Mac"
    current = open(override, "rb").read() if os.path.exists(override) else None
    if current != data:
        os.makedirs(os.path.dirname(override), exist_ok=True)
        tmp = override + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, override)
        depmod(kver)
    record(kver, "patched")
    return f"{kver}: patched ({count} check removed), installed as {override}"


def unpatch_kernel(kver):
    override = f"{MODULES}/{kver}/{OVERRIDE}"
    if os.path.exists(override):
        os.remove(override)
        if os.path.isdir(f"{MODULES}/{kver}/kernel"):
            depmod(kver)
    record(kver, None)


def kernels_from_stdin():
    versions = set()
    for line in sys.stdin:
        m = re.match(r"/?usr/lib/modules/([^/]+)/", line.strip())
        if m:
            versions.add(m.group(1))
    return sorted(versions)


def installed_kernels():
    return sorted(os.path.basename(d) for d in glob.glob(f"{MODULES}/*") if os.path.isdir(f"{d}/kernel"))


def reload_if_unbound():
    kver = os.uname().release
    bound = glob.glob("/sys/bus/platform/drivers/macsmc-acpi/APP0001:*")
    if bound or not os.path.exists(f"{MODULES}/{kver}/{OVERRIDE}"):
        return
    subprocess.run(["modprobe", "-r", "macsmc_acpi"], check=False, capture_output=True)
    subprocess.run(["modprobe", "macsmc_acpi"], check=False)
    print(f"{kver}: reloaded macsmc-acpi" + (" - the SMC is running" if os.path.exists("/dev/smc")
                                             or glob.glob("/sys/bus/platform/drivers/macsmc-acpi/APP0001:*")
                                             else " - but it still didn't attach, see: journalctl -k | grep macsmc"))


def main():
    args = sys.argv[1:]
    if args[:1] == ["--check"]:
        state, data, count = patched(decompress(args[1]))
        print(f"{args[1]}: {state}" + (f", {count} check(s) to remove" if count else ""))
        if len(args) > 2 and state == "needs-patch":
            with open(args[2], "wb") as f:
                f.write(data)
            print(f"patched copy written to {args[2]}")
        return
    if os.geteuid() != 0:
        sys.exit("macbook-smc-patch needs root (run it with sudo)")
    if args[:1] == ["--uninstall"]:
        for kver in installed_kernels():
            unpatch_kernel(kver)
        print("removed all patched macsmc-acpi copies")
        return
    if args[:1] == ["--hook-remove"]:
        for kver in kernels_from_stdin():
            unpatch_kernel(kver)
        return
    try:
        with open("/sys/module/module/parameters/sig_enforce") as f:
            if f.read().strip() == "Y":
                sys.exit("this kernel only loads signed modules, so a patched driver can't be used")
    except OSError:
        pass
    kernels = kernels_from_stdin() if args[:1] == ["--hook-install"] else installed_kernels()
    for kver in kernels:
        print("MacBook Control: " + patch_kernel(kver))
    if "--reload" in args:
        reload_if_unbound()


if __name__ == "__main__":
    main()
