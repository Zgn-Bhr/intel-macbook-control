"""Read-only views of this MacBook's sensors, battery, fan and keyboard light.

Nothing here needs root: everything comes from sysfs, /proc and the status file
that the smcfan service keeps in /run.
"""
import glob
import json
import os
import re
import time

FAN_STATUS = "/run/smcfan.status"
FAN_CONFIG = "/etc/macbook-control/fan.json"
APPLESMC = "/sys/devices/platform/applesmc.768"
HID_APPLE = "/sys/module/hid_apple/parameters"

KBDLIGHT_CONFIG = os.path.expanduser("~/.config/macbook-control/kbdlight.json")
KBDLIGHT_STATUS = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "macbook-kbdlight.status")
KBDLIGHT_DEFAULTS = {"enabled": True, "below_lux": 30, "level": 60}   # keep in sync with macbook-kbdlight.py

MODELS = {"MacBookPro9,2": "MacBook Pro 13″ Mid 2012 (A1278)"}
# Design capacity of the original battery (the driver doesn't report it): (mAh, battery model)
DESIGN_CAPACITY = {"MacBookPro9,2": (5800, "A1322")}

# hwmon kind: (divisor to get the unit, unit)
KINDS = {"temp": (1000, "°C"), "power": (1e6, "W"), "curr": (1000, "A"), "in": (1000, "V")}
# Some SMC "temperatures" are filter settings, not readings
NOT_A_READING = ("Coeff", "Max Error", "Trend")


def read_text(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def read_int(path, default=None):
    try:
        return int(read_text(path))
    except (TypeError, ValueError):
        return default


def find_hwmon(*names):
    for hwmon in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if read_text(f"{hwmon}/name") in names:
            return hwmon
    return None


def smc_sensors():
    """Every SMC sensor the kernel exposes, as (kind, name, smc_key, value, unit), sorted by kind and name."""
    hwmon = find_hwmon("macsmc_hwmon", "applesmc")
    if not hwmon:
        return []
    out = []
    for path in glob.glob(f"{hwmon}/*_input"):
        base = path[: -len("_input")]
        m = re.match(r"(temp|power|curr|in)\d+$", os.path.basename(base))
        raw = read_int(path)
        if not m or raw is None:
            continue
        kind = m.group(1)
        label = read_text(f"{base}_label", os.path.basename(base))
        if any(s in label for s in NOT_A_READING):
            continue
        value = raw / KINDS[kind][0]
        if kind == "temp" and not -40 < value < 150:
            continue
        key = re.search(r"\(([^()]{4})\)$", label)
        key = key.group(1) if key else label
        name = re.sub(r"\s*\([^()]{4}\)$", "", label).replace(" Temperature", "")
        out.append((kind, name, key, value, KINDS[kind][1]))
    order = list(KINDS)
    return sorted(out, key=lambda s: (order.index(s[0]), s[1].lower()))


def coretemp():
    """{label: C} for the CPU package and each core."""
    hwmon = find_hwmon("coretemp")
    temps = {}
    for path in sorted(glob.glob(f"{hwmon}/temp*_input")) if hwmon else []:
        value = read_int(path)
        if value is not None:
            temps[read_text(path.replace("_input", "_label"), "CPU")] = value / 1000
    return temps


def fan_status():
    """Fan speeds and who controls them (smcfan on macsmc kernels, mbpfan on applesmc ones), or None."""
    text = read_text(FAN_STATUS)
    if text:
        try:
            st = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
            return {"controller": "smcfan", "mode": st.get("mode", "balanced"), "actual": int(st["F0_actual"]),
                    "target": int(st["F0_min"]), "floor": int(st.get("F0_floor", 2000)), "max": int(st["F0_max"])}
        except (KeyError, ValueError):
            pass
    actual = read_int(f"{APPLESMC}/fan1_input")
    if actual is not None:
        return {"controller": "mbpfan" if os.path.exists("/run/mbpfan.pid") else None, "mode": None,
                "actual": actual, "target": read_int(f"{APPLESMC}/fan1_min"), "floor": 2000,
                "max": read_int(f"{APPLESMC}/fan1_max")}
    return None


def smc_patch_status():
    """What macbook-smc-patch did for the running kernel: 'patched', 'not needed', 'refused: ...' or None."""
    text = read_text("/var/lib/macbook-control/smc-patch.status", "")
    lines = dict(line.split(": ", 1) for line in text.splitlines() if ": " in line)
    return lines.get(os.uname().release)


def fan_config():
    try:
        with open(FAN_CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"mode": "balanced"}


class CpuMeter:
    """CPU usage since the previous call, from /proc/stat."""

    def __init__(self):
        self.prev = self._times()

    @staticmethod
    def _times():
        with open("/proc/stat") as f:
            fields = [int(x) for x in f.readline().split()[1:]]
        return sum(fields), fields[3] + fields[4]   # total, idle + iowait

    def usage(self):
        total, idle = self._times()
        dt, di = total - self.prev[0], idle - self.prev[1]
        self.prev = (total, idle)
        return 100 * (1 - di / dt) if dt else 0.0


def cpu_mhz():
    freqs = [read_int(p) for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")]
    freqs = [f for f in freqs if f]
    return sum(freqs) / len(freqs) / 1000 if freqs else None


def memory():
    """(used, total) in bytes."""
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0]) * 1024
    return info["MemTotal"] - info["MemAvailable"], info["MemTotal"]


def power_supplies():
    """(battery, charger) sysfs paths. The same battery can show up twice (ACPI BAT0 next to the SMC's
    macsmc-battery when the SMC driver loads late), so prefer the entry that reports the most."""
    def richness(ps, files):
        return sum(os.path.exists(f"{ps}/{f}") for f in files)
    batteries, chargers = [], []
    for ps in sorted(glob.glob("/sys/class/power_supply/*")):
        kind = read_text(f"{ps}/type")
        # Skip batteries of mice and keyboards, which report scope "Device"
        if kind == "Battery" and read_text(f"{ps}/scope", "System") == "System":
            batteries.append(ps)
        elif kind == "Mains":
            chargers.append(ps)
    battery = max(batteries, key=lambda p: richness(p, ("charge_control_end_threshold", "energy_full", "cycle_count")),
                  default=None)
    charger = max(chargers, key=lambda p: richness(p, ("power_now",)), default=None)
    return battery, charger


def battery():
    path, _ = power_supplies()
    if not path:
        return None
    current, voltage = read_int(f"{path}/current_now"), read_int(f"{path}/voltage_now")
    status = read_text(f"{path}/status", "Unknown")
    seconds = read_int(f"{path}/time_to_full_now" if status == "Charging" else f"{path}/time_to_empty_now")
    temp = read_int(f"{path}/temp")
    full, design = read_int(f"{path}/energy_full"), read_int(f"{path}/energy_full_design")
    full_mah = read_int(f"{path}/charge_full")
    return {
        "percent": read_int(f"{path}/capacity"),
        "status": status,
        "seconds": seconds if seconds and seconds > 0 else None,
        "health": read_text(f"{path}/health"),
        "cycles": read_int(f"{path}/cycle_count"),
        "temp": temp / 10 if temp is not None else None,
        "watts": abs(current * voltage) / 1e12 if current is not None and voltage is not None else None,
        "volts": voltage / 1e6 if voltage is not None else None,
        "full_wh": full / 1e6 if full else None,
        "design_wh": design / 1e6 if design else None,
        "full_mah": full_mah / 1000 if full_mah else None,
        "limit": read_int(f"{path}/charge_control_end_threshold"),
    }


def apple_keys():
    """{parameter: value} of the Apple keyboard driver (fnmode, swap_opt_cmd, ...), or {} if it isn't loaded."""
    keys = {}
    for path in glob.glob(f"{HID_APPLE}/*"):
        value = read_int(path)
        if value is not None:
            keys[os.path.basename(path)] = value
    return keys


def charger():
    _, path = power_supplies()
    if not path:
        return None
    watts = read_int(f"{path}/power_now")
    return {"online": read_int(f"{path}/online") == 1, "watts": watts / 1e6 if watts is not None else None}


def keyboard_light():
    """(brightness, max) of the keyboard backlight, or None."""
    for led in sorted(glob.glob("/sys/class/leds/*kbd_backlight*")):
        level, top = read_int(f"{led}/brightness"), read_int(f"{led}/max_brightness")
        if level is not None and top:
            return level, top
    return None


def kbdlight():
    """Settings and live state of the automatic keyboard light helper (status is None if it isn't running)."""
    cfg = dict(KBDLIGHT_DEFAULTS)
    try:
        with open(KBDLIGHT_CONFIG) as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg.update({k: v for k, v in saved.items() if k in cfg})
    except (OSError, ValueError):
        pass
    status = None
    try:
        if time.time() - os.stat(KBDLIGHT_STATUS).st_mtime < 15:     # it writes every 2 s
            with open(KBDLIGHT_STATUS) as f:
                status = dict(line.split("=", 1) for line in f.read().split() if "=" in line)
    except OSError:
        pass
    return {"config": cfg, "status": status}


def ambient_light():
    """{sensor: reading} from the light sensors (lux for acpi-als, raw counts for the SMC ones)."""
    out = {}
    for dev in sorted(glob.glob("/sys/bus/iio/devices/iio:device*")):
        name = read_text(f"{dev}/name")
        if name == "acpi-als":
            value = read_int(f"{dev}/in_illuminance_input")
            if value is not None:
                out["Ambient (lux)"] = value
        elif name == "macsmc-light":
            for side in ("left", "right"):
                value = read_int(f"{dev}/in_illuminance_{side}_raw")
                if value is not None:
                    out[f"SMC {side}"] = value
    return out


def system_info():
    product = read_text("/sys/class/dmi/id/product_name", "Mac")
    uptime = float(read_text("/proc/uptime", "0").split()[0])
    return {"model": MODELS.get(product, product), "product": product,
            "kernel": os.uname().release, "uptime": uptime}


def snapshot(cpu_meter):
    """Everything the pages show, read once per refresh."""
    smc = smc_sensors()
    by_key = {key: value for _, _, key, value, _ in smc}
    cores = coretemp()
    info = system_info()
    return {
        "time": time.time(),
        "system": info,
        "fan": fan_status(),
        "fan_config": fan_config(),
        "cpu": {"usage": cpu_meter.usage(), "mhz": cpu_mhz(),
                "temp": cores.get("Package id 0", max(cores.values(), default=None))},
        "cores": cores,
        "memory": memory(),
        "battery": battery(),
        "charger": charger(),
        "keyboard": keyboard_light(),
        "kbdlight": kbdlight(),
        "keys": apple_keys(),
        "design": DESIGN_CAPACITY.get(info["product"]),
        "light": ambient_light(),
        "smc": smc,
        "power": by_key.get("PSTR"),          # System Total, 1 s delayed
        "palm_rest": by_key.get("Ts0P"),
        "bottom": by_key.get("Ts0S"),
    }
