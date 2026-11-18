#!/usr/bin/python3 -I
"""macbook-kbdlight - the keyboard light follows the room: on in the dark, off in daylight.

Runs as a user service in the desktop session (no root). It reads the ambient light sensor,
smooths it over a few seconds and sets the keyboard backlight through UPower, fading between
levels. If you change the keyboard light yourself (F5/F6 or KDE), it leaves it alone for
PAUSE seconds.

Settings (written by MacBook Control, re-read every few seconds):
  ~/.config/macbook-control/kbdlight.json   {"enabled": true, "below_lux": 30, "level": 60}
    below_lux  the light starts coming on below this; full level at a quarter of it
    level      keyboard brightness in the dark, in percent
Status for the app: $XDG_RUNTIME_DIR/macbook-kbdlight.status
"""
import glob
import json
import math
import os
import subprocess
import time

CONFIG = os.path.expanduser("~/.config/macbook-control/kbdlight.json")
STATUS = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "macbook-kbdlight.status")
DEFAULTS = {"enabled": True, "below_lux": 30, "level": 60}
INTERVAL = 2          # seconds between sensor readings
PAUSE = 600           # seconds to leave the light alone after a manual change
SMOOTHING = 0.35      # weight of each new reading; lower = calmer
MIN_CHANGE = 0.04     # ignore changes smaller than this share of full brightness


def read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError, TypeError):
        return None


def lux_sensor():
    for dev in sorted(glob.glob("/sys/bus/iio/devices/iio:device*")):
        try:
            with open(f"{dev}/name") as f:
                if f.read().strip() == "acpi-als":
                    return f"{dev}/in_illuminance_input"
        except OSError:
            pass
    return None


def keyboard_led():
    leds = sorted(glob.glob("/sys/class/leds/*kbd_backlight*"))
    return leds[0] if leds else None


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return cfg
    if isinstance(saved, dict):
        if isinstance(saved.get("enabled"), bool):
            cfg["enabled"] = saved["enabled"]
        if isinstance(saved.get("below_lux"), (int, float)) and 2 <= saved["below_lux"] <= 2000:
            cfg["below_lux"] = saved["below_lux"]
        if isinstance(saved.get("level"), (int, float)) and 0 <= saved["level"] <= 100:
            cfg["level"] = saved["level"]
    return cfg


def target_percent(lux, cfg):
    """Off at or above below_lux, full level at a quarter of it, a log-scale fade in between."""
    below, full_at = cfg["below_lux"], cfg["below_lux"] / 4
    if lux >= below:
        return 0
    if lux <= full_at:
        return cfg["level"]
    return cfg["level"] * (math.log(below) - math.log(lux)) / (math.log(below) - math.log(full_at))


def set_brightness(raw):
    subprocess.run(["busctl", "--system", "call", "org.freedesktop.UPower", "/org/freedesktop/UPower/KbdBacklight",
                    "org.freedesktop.UPower.KbdBacklight", "SetBrightness", "i", str(raw)],
                   capture_output=True, timeout=5)


def fade(start, end, steps=8):
    for i in range(1, steps + 1):
        set_brightness(round(start + (end - start) * i / steps))
        time.sleep(0.1)


def write_status(**fields):
    tmp = STATUS + ".tmp"
    with open(tmp, "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in fields.items()))
    os.replace(tmp, STATUS)


def step(state):
    """One reading and (maybe) one adjustment. `state` carries smoothing and override info between calls."""
    cfg = load_config()
    sensor, led = lux_sensor(), keyboard_led()
    lux = read_int(sensor) if sensor else None
    top = read_int(f"{led}/max_brightness") if led else None
    current = read_int(f"{led}/brightness") if led else None
    if lux is None or not top or current is None:
        write_status(state="no-hardware")
        state["last_set"] = None
        return
    state["lux"] = lux if state.get("lux") is None else state["lux"] + SMOOTHING * (lux - state["lux"])
    now = time.monotonic()

    # Someone else changed the light since we last set it: that's the user, so step back for a while
    if state.get("last_set") is not None and current != state["last_set"]:
        state["paused_until"] = now + PAUSE
        state["last_set"] = None

    if not cfg["enabled"]:
        state["last_set"] = None
        write_status(state="disabled", lux=round(state["lux"]))
        return
    if now < state.get("paused_until", 0):
        write_status(state="paused", lux=round(state["lux"]), seconds_left=round(state["paused_until"] - now))
        return

    want = round(target_percent(state["lux"], cfg) / 100 * top)
    if (want == 0) != (current == 0) or abs(want - current) >= MIN_CHANGE * top:
        fade(current, want)
        current = read_int(f"{led}/brightness")
    state["last_set"] = current
    write_status(state="auto", lux=round(state["lux"]), percent=round(100 * (current or 0) / top))


def main():
    state = {}
    while True:
        step(state)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
