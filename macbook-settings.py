#!/usr/bin/python3 -I
"""macbook-settings - battery charge limit and Apple keyboard options, kept across reboots.

  macbook-settings show                         current values and the saved settings
  macbook-settings set charge-limit PERCENT     stop charging at 50-100 %
  macbook-settings set fn-keys media|function   what the top row does without Fn
  macbook-settings set swap-opt-cmd on|off      swap the Option (Alt) and Command keys
  macbook-settings set swap-fn-ctrl on|off      swap Fn and the left Control key
  macbook-settings apply                        re-apply the saved settings (run at boot)

Settings are saved in /etc/macbook-control/settings.json. The desktop user may run this
through pkexec without a password, so every value is checked before it is written.
"""
import argparse
import glob
import json
import os
import sys
import time

SETTINGS = "/etc/macbook-control/settings.json"
HID_APPLE = "/sys/module/hid_apple/parameters"

# setting: (hid_apple parameter, {word: kernel value})
KEY_OPTIONS = {
    "fn-keys": ("fnmode", {"media": 3, "function": 2}),
    "swap-opt-cmd": ("swap_opt_cmd", {"off": 0, "on": 1}),
    "swap-fn-ctrl": ("swap_fn_leftctrl", {"off": 0, "on": 1}),
}


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def write(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def limit_file():
    """The system battery's charge_control_end_threshold, or None if the driver has none."""
    for ps in sorted(glob.glob("/sys/class/power_supply/*")):
        path = f"{ps}/charge_control_end_threshold"
        if read(f"{ps}/type") == "Battery" and read(f"{ps}/scope", "System") == "System" and os.path.exists(path):
            return path
    return None


def load():
    try:
        with open(SETTINGS) as f:
            settings = json.load(f)
        return settings if isinstance(settings, dict) else {}
    except (OSError, ValueError):
        return {}


def save(settings):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=1)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, SETTINGS)


def set_charge_limit(percent):
    """Write the limit and return what the SMC actually kept (some Macs only do 80 or 100)."""
    path = limit_file()
    if not path:
        raise RuntimeError("this kernel's battery driver has no charge limit")
    write(path, percent)
    return int(read(path))


def set_key(name, word):
    param = f"{HID_APPLE}/{KEY_OPTIONS[name][0]}"
    if not os.path.exists(param):
        raise RuntimeError("the Apple keyboard driver (hid_apple) isn't loaded")
    write(param, KEY_OPTIONS[name][1][word])


def apply_one(name, value):
    if name == "charge-limit":
        effective = set_charge_limit(int(value))
        return f"charge limit {effective} %"
    set_key(name, value)
    return f"{name} {value}"


def cmd_show(args):
    current = {"charge-limit": int(read(limit_file())) if limit_file() else None}
    for name, (param, words) in KEY_OPTIONS.items():
        raw = read(f"{HID_APPLE}/{param}")
        current[name] = next((w for w, v in words.items() if str(v) == raw), raw)
    print(json.dumps({"current": current, "saved": load()}, indent=1))


def cmd_set(args):
    settings = load()
    try:
        if args.name == "charge-limit":
            try:
                percent = int(args.value)
            except ValueError:
                sys.exit("charge-limit needs a whole number of percent")
            if not 50 <= percent <= 100:
                sys.exit("charge-limit must be between 50 and 100")
            effective = set_charge_limit(percent)
            settings["charge-limit"] = effective   # remember what the SMC accepted
            note = "" if effective == percent else f" (this Mac doesn't support {percent} %)"
            message = f"Charge limit set to {effective} %{note}"
        else:
            words = KEY_OPTIONS[args.name][1]
            if args.value not in words:
                sys.exit(f"{args.name} must be one of: {', '.join(words)}")
            set_key(args.name, args.value)
            settings[args.name] = args.value
            message = f"{args.name} set to {args.value}"
        save(settings)
    except PermissionError:
        sys.exit("macbook-settings set: needs root (run it with sudo or pkexec)")
    except (OSError, RuntimeError) as e:
        sys.exit(f"Couldn't change {args.name}: {e}")
    print(message)


def cmd_apply(args):
    settings = load()
    if not settings:
        print("macbook-settings: nothing saved, nothing to apply")
        return
    # At boot the battery and keyboard drivers may still be loading
    for _ in range(15):
        if (("charge-limit" not in settings or limit_file())
                and all(name not in settings or os.path.exists(HID_APPLE) for name in KEY_OPTIONS)):
            break
        time.sleep(1)
    failed = False
    for name, value in settings.items():
        if name != "charge-limit" and name not in KEY_OPTIONS:
            continue
        try:
            print(f"macbook-settings: applied {apply_one(name, value)}")
        except (OSError, RuntimeError, ValueError, KeyError) as e:
            print(f"macbook-settings: couldn't apply {name}={value}: {e}")
            failed = True
    sys.exit(1 if failed else 0)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show").set_defaults(func=cmd_show)
    s = sub.add_parser("set")
    s.add_argument("name", choices=["charge-limit", *KEY_OPTIONS])
    s.add_argument("value")
    s.set_defaults(func=cmd_set)
    sub.add_parser("apply").set_defaults(func=cmd_apply)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
