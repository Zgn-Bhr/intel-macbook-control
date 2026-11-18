#!/usr/bin/python3 -I
"""smcfan - fan control for Intel Macs on kernels that drive the SMC with macsmc (/dev/smc).

It never puts a fan into manual mode. It only raises the fan's minimum speed (FxMn)
as the CPU warms up, so the SMC's own automatic fan control keeps running underneath.
When it stops, it puts the original minimum speed back. While running, it keeps
/run/smcfan.status (temperature, fan speeds, mode) readable for everyone.

The fan mode lives in /etc/macbook-control/fan.json and is picked up within a second:
  stock                     leave it to the Mac's own controller
  quiet / balanced / cool   ramp between two temperatures (see PRESETS)
  custom                    ramp between your own two temperatures
  fixed                     hold a fixed minimum speed
In every mode the fan runs at full speed from SAFETY_TEMP, or if the temperature can't be read.

  smcfan status                                  read-only: show the fan and temperature keys
  smcfan test RPM [SECONDS]                      hold a minimum speed briefly, then restore it
  smcfan set MODE [--low C --high C | --rpm N]   change the fan mode (allowed through pkexec)
  smcfan run                                     control loop (what the systemd service runs)
"""
import argparse
import fcntl
import glob
import json
import os
import signal
import struct
import sys
import time

DEV = "/dev/smc"
STATE = "/run/smcfan.original-min"
STATUS = "/run/smcfan.status"
CONFIG = "/etc/macbook-control/fan.json"

PRESETS = {"quiet": (70, 92), "balanced": (65, 85), "cool": (55, 78)}   # mode: (ramp start C, full speed C)
MODES = ("stock", "quiet", "balanced", "cool", "custom", "fixed")
DEFAULT_CONFIG = {"mode": "balanced"}
SAFETY_TEMP = 92

# Layouts from include/linux/mfd/macsmc.h:
#   struct ioctl_smc_rw_payload   { smc_key key; size_t size; u8 buf[32]; };
#   struct ioctl_smc_info_payload { smc_key key; struct { u32 type_code; u8 size; u8 flags; } info; };
RW = struct.Struct("@IQ32s")
INFO = struct.Struct("@IIBB2x")


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("S") << 8) | nr


IOCTL_SMC_READ = _ioc(3, 1, RW.size)       # _IOWR
IOCTL_SMC_WRITE = _ioc(1, 2, RW.size)      # _IOW
IOCTL_SMC_GET_KEY_INFO = _ioc(3, 4, INFO.size)

SMC_READABLE = 1 << 7
SMC_WRITABLE = 1 << 6
SMC_KEY_NOT_FOUND = 0x84


def smc_key(name):
    return int.from_bytes(name.encode("ascii"), "big")


def fourcc(value):
    return value.to_bytes(4, "big").decode("ascii", "replace")


def decode(typ, raw):
    if typ == "fpe2":
        return int.from_bytes(raw[:2], "big") >> 2
    if typ == "sp78":
        return int.from_bytes(raw[:2], "big", signed=True) / 256
    if typ == "flt ":
        return round(struct.unpack("<f", raw[:4])[0], 2)
    if typ.startswith("ui"):
        return int.from_bytes(raw, "big")
    if typ.startswith("si"):
        return int.from_bytes(raw, "big", signed=True)
    if typ == "flag":
        return raw[0]
    return raw.hex(" ")


def encode(typ, value):
    if typ == "fpe2":
        return (int(value) << 2).to_bytes(2, "big")
    if typ == "flt ":
        return struct.pack("<f", float(value))
    raise ValueError(f"don't know how to write SMC type {typ!r}")


class SMC:
    def __init__(self, writable=False):
        self.fd = os.open(DEV, os.O_RDWR if writable else os.O_RDONLY)

    def info(self, name):
        buf = bytearray(INFO.pack(smc_key(name), 0, 0, 0))
        fcntl.ioctl(self.fd, IOCTL_SMC_GET_KEY_INFO, buf)
        _, type_code, size, flags = INFO.unpack(buf)
        return fourcc(type_code), size, flags

    def read_raw(self, name, size):
        buf = bytearray(RW.pack(smc_key(name), size, b""))
        fcntl.ioctl(self.fd, IOCTL_SMC_READ, buf)
        return bytes(RW.unpack(buf)[2][:size])

    def write_raw(self, name, data):
        buf = bytearray(RW.pack(smc_key(name), len(data), data))
        fcntl.ioctl(self.fd, IOCTL_SMC_WRITE, buf)

    def read(self, name):
        typ, size, _ = self.info(name)
        return decode(typ, self.read_raw(name, size))

    def fan_count(self):
        try:
            return int(self.read("FNum"))
        except OSError:
            return 1


class Fan:
    def __init__(self, smc, index):
        self.smc = smc
        self.key = f"F{index}"
        self.typ, _, flags = smc.info(f"{self.key}Mn")
        if not flags & SMC_WRITABLE:
            sys.exit(f"{self.key}Mn is not writable on this Mac")
        self.max = int(smc.read(f"{self.key}Mx"))
        lo = int(self.min())
        # Refuse to write anything if the speeds don't decode to sane values
        if not 500 <= lo < self.max <= 10000:
            sys.exit(f"{self.key} limits look wrong (min {lo}, max {self.max}); not touching the fan")

    def actual(self):
        return int(self.smc.read(f"{self.key}Ac"))

    def min(self):
        return int(self.smc.read(f"{self.key}Mn"))

    def set_min(self, rpm):
        self.smc.write_raw(f"{self.key}Mn", encode(self.typ, rpm))


def cpu_temp():
    temps = []
    for hwmon in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            with open(f"{hwmon}/name") as f:
                if f.read().strip() != "coretemp":
                    continue
            for path in glob.glob(f"{hwmon}/temp*_input"):
                with open(path) as f:
                    temps.append(int(f.read()) / 1000)
        except OSError:
            pass
    if not temps:
        raise RuntimeError("no coretemp readings")
    return max(temps)


def check_config(cfg):
    """Raise ValueError unless cfg is a complete, sane fan setting."""
    mode = cfg.get("mode")
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r} (choose from {', '.join(MODES)})")
    try:
        if mode == "custom":
            low, high = float(cfg["low"]), float(cfg["high"])
            if not (40 <= low and high <= 100 and high - low >= 5):
                raise ValueError("custom needs --low >= 40, --high <= 100 and high at least 5 above low")
        if mode == "fixed" and not 1000 <= int(cfg["rpm"]) <= 7000:
            raise ValueError("fixed needs --rpm between 1000 and 7000")
    except (KeyError, TypeError):
        raise ValueError(f"{mode} is missing its settings (custom: --low and --high, fixed: --rpm)")


def load_config():
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
        check_config(cfg)
        return cfg
    except (OSError, ValueError):
        return dict(DEFAULT_CONFIG)


def config_mtime():
    try:
        return os.stat(CONFIG).st_mtime_ns
    except OSError:
        return None


def describe(cfg):
    mode = cfg["mode"]
    if mode == "custom":
        return f"custom ({cfg['low']:g}-{cfg['high']:g} C)"
    if mode == "fixed":
        return f"fixed ({cfg['rpm']} rpm)"
    if mode in PRESETS:
        return f"{mode} ({PRESETS[mode][0]}-{PRESETS[mode][1]} C)"
    return mode


def fan_target(cfg, temp, floor, top):
    if temp is None or temp >= SAFETY_TEMP:
        return top
    mode = cfg["mode"]
    if mode == "stock":
        return floor
    if mode == "fixed":
        return min(max(int(cfg["rpm"]), floor), top)
    low, high = (cfg["low"], cfg["high"]) if mode == "custom" else PRESETS[mode]
    frac = min(max((temp - low) / (high - low), 0), 1)
    return floor + frac * (top - floor)


def write_status(temp, fans, current, floors, mode):
    lines = [f"temp={temp}", f"mode={mode}"]
    for fan, rpm, floor in zip(fans, current, floors):
        lines += [f"{fan.key}_min={rpm}", f"{fan.key}_actual={fan.actual()}",
                  f"{fan.key}_floor={floor}", f"{fan.key}_max={fan.max}"]
    tmp = STATUS + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, STATUS)


def cmd_status(args):
    smc = SMC(writable=False)
    names = ["#KEY", "FNum", "FS! "]
    for i in range(smc.fan_count()):
        names += [f"F{i}Ac", f"F{i}Mn", f"F{i}Mx", f"F{i}Tg", f"F{i}Sf"]
    names += ["TC0P", "TC0D", "TC0E", "TC0F", "TB0T", "Th1H"]

    print(f"{'key':5} {'type':5} {'size':>4} {'flags':5} {'raw':12} value")
    for name in names:
        try:
            typ, size, flags = smc.info(name)
            raw = smc.read_raw(name, size)
        except OSError as e:
            # The SMC's "key not found" status (0x84) comes back as errno 132
            print(f"{name:5} -- {'not on this Mac' if e.errno == SMC_KEY_NOT_FOUND else e.strerror}")
            continue
        rw = ("r" if flags & SMC_READABLE else "-") + ("w" if flags & SMC_WRITABLE else "-")
        print(f"{name:5} {typ:5} {size:>4} {rw:5} {raw.hex(' '):12} {decode(typ, raw)}")
    print(f"\nCPU (coretemp): {cpu_temp():.0f} C")
    print(f"Fan mode: {describe(load_config())}")


def cmd_test(args):
    if os.path.exists(STATE):
        sys.exit("The smcfan service seems to be running; stop it first: sudo systemctl stop smcfan")
    fan = Fan(SMC(writable=True), 0)
    original = fan.min()
    target = max(original, min(args.rpm, fan.max))
    print(f"Fan limits {original}-{fan.max} rpm. Holding minimum at {target} rpm for {args.seconds} s...")
    try:
        fan.set_min(target)
        for s in range(1, args.seconds + 1):
            time.sleep(1)
            print(f"  {s:2d} s: actual {fan.actual()} rpm (minimum now {fan.min()})")
    finally:
        fan.set_min(original)
        print(f"Restored minimum to {fan.min()} rpm.")


def cmd_set(args):
    cfg = {"mode": args.mode}
    if args.mode == "custom":
        cfg.update(low=args.low, high=args.high)
    elif args.mode == "fixed":
        cfg["rpm"] = args.rpm
    try:
        check_config(cfg)
    except ValueError as e:
        sys.exit(f"smcfan set: {e}")
    try:
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        tmp = CONFIG + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, CONFIG)
    except PermissionError:
        sys.exit("smcfan set: needs root (run it with sudo or pkexec)")
    print(f"Fan mode set to {describe(cfg)}")


def cmd_run(args):
    smc = SMC(writable=True)
    fans = [Fan(smc, i) for i in range(smc.fan_count())]

    # Keep the true original minimums across restarts, so a crash can't make a raised value stick
    try:
        with open(STATE) as f:
            floors = [int(x) for x in f.read().split()]
    except (OSError, ValueError):
        floors = [fan.min() for fan in fans]
        with open(STATE, "w") as f:
            f.write(" ".join(map(str, floors)))

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    cfg, cfg_seen = load_config(), config_mtime()
    current = list(floors)
    logged = list(floors)
    print(f"smcfan: {len(fans)} fan(s), floors {floors}, mode {describe(cfg)}", flush=True)
    try:
        while True:
            if config_mtime() != cfg_seen:
                cfg, cfg_seen = load_config(), config_mtime()
                print(f"smcfan: mode changed to {describe(cfg)}", flush=True)
            try:
                temp = cpu_temp()
            except RuntimeError:
                temp = None
            for i, fan in enumerate(fans):
                want = fan_target(cfg, temp, floors[i], fan.max)
                # Speed up at once, slow down gently so the fan doesn't hunt
                want = max(want, current[i] - args.step_down)
                want = int(round(want / 50) * 50)
                # Also re-apply if something (sleep/resume) reset the SMC's value
                if want != current[i] or fan.min() != want:
                    fan.set_min(want)
                    current[i] = want
                    if abs(want - logged[i]) >= 500 or want in (floors[i], fan.max):
                        if want != logged[i]:
                            print(f"{fan.key}: {temp} C -> minimum {want} rpm", flush=True)
                            logged[i] = want
            write_status(temp, fans, current, floors, cfg["mode"])
            time.sleep(1)
    finally:
        for fan, floor in zip(fans, floors):
            fan.set_min(floor)
        for path in (STATE, STATUS):
            try:
                os.remove(path)
            except OSError:
                pass
        print(f"smcfan: stopped, minimums restored to {floors}", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(func=cmd_status)
    t = sub.add_parser("test")
    t.add_argument("rpm", type=int)
    t.add_argument("seconds", type=int, nargs="?", default=10)
    t.set_defaults(func=cmd_test)
    s = sub.add_parser("set")
    s.add_argument("mode", choices=MODES)
    s.add_argument("--low", type=float, help="custom: temperature where the fan starts speeding up (C)")
    s.add_argument("--high", type=float, help="custom: temperature for full speed (C)")
    s.add_argument("--rpm", type=int, help="fixed: minimum fan speed")
    s.set_defaults(func=cmd_set)
    r = sub.add_parser("run")
    r.add_argument("--step-down", type=int, default=100, help="slow down by at most this many rpm per second")
    r.set_defaults(func=cmd_run)
    args = p.parse_args()

    # The polkit rule lets the desktop user run this without a password, so only allow harmless commands
    if "PKEXEC_UID" in os.environ and args.cmd not in ("set", "status"):
        sys.exit("Only 'smcfan set' and 'smcfan status' may be run through pkexec")
    try:
        args.func(args)
    except PermissionError:
        sys.exit(f"Need root to open {DEV}: run with sudo")
    except FileNotFoundError:
        sys.exit(f"{DEV} not found: this kernel doesn't use the macsmc driver")


if __name__ == "__main__":
    main()
