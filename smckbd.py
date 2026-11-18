#!/usr/bin/env python3
"""smckbd - make the macsmc keyboard backlight visible to UPower and KDE.

UPower only treats an LED as a keyboard backlight if its name contains "kbd_backlight",
but the macsmc driver calls it "macsmc-led". This creates a userspace LED named
smc::kbd_backlight (the old applesmc name) through /dev/uleds and copies every
brightness change made to it onto the real LED.
"""
import os
import struct
import sys

ULEDS = "/dev/uleds"
NAME = "smc::kbd_backlight"
REAL = "/sys/class/leds/macsmc-led"
VIRTUAL = f"/sys/class/leds/{NAME}"

# struct uleds_user_dev { char name[LED_MAX_NAME_SIZE]; int max_brightness; }   (uapi/linux/uleds.h)
USER_DEV = struct.Struct("@64si")
BRIGHTNESS = struct.Struct("@i")


def read_int(path):
    with open(path) as f:
        return int(f.read())


def write_int(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def bridge(fd, real):
    """Copy each brightness the kernel reports for our LED onto the real one, forever."""
    while True:
        data = os.read(fd, BRIGHTNESS.size)
        if len(data) < BRIGHTNESS.size:
            raise RuntimeError("uleds closed")
        write_int(f"{real}/brightness", BRIGHTNESS.unpack(data)[0])


def main():
    try:
        max_brightness = read_int(f"{REAL}/max_brightness")
        fd = os.open(ULEDS, os.O_RDWR)
    except FileNotFoundError as e:
        sys.exit(f"{e.filename} not found (is this a macsmc kernel, and is the uleds module loaded?)")
    os.write(fd, USER_DEV.pack(NAME.encode(), max_brightness))
    # Start the new LED at the real LED's current level so nothing jumps
    write_int(f"{VIRTUAL}/brightness", read_int(f"{REAL}/brightness"))
    print(f"smckbd: {NAME} (0-{max_brightness}) -> {REAL}", flush=True)
    bridge(fd, REAL)


if __name__ == "__main__":
    main()
