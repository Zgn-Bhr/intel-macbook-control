"""KDE settings for the built-in screen: brightness, automatic brightness, night light and color profile.

Everything goes through KDE's own interfaces as the logged-in user; nothing here needs root.
Reads are cheap (two small config files and D-Bus properties); changes run small helper
commands (busctl, kscreen-doctor, kwriteconfig6) without blocking the window.
"""
import configparser
import json
import os
import subprocess

from PySide6.QtCore import QProcess
from PySide6.QtDBus import QDBusConnection, QDBusInterface

KWINRC = os.path.expanduser("~/.config/kwinrc")
OUTPUTS = os.path.expanduser("~/.config/kwinoutputconfig.json")
NIGHT_MODES = ("Constant", "DarkLight")      # always on / from sunset to sunrise
NIGHT_MIN, NIGHT_MAX = 1000, 6500


def output_config():
    """KWin's saved settings for the built-in screen (LVDS/eDP), or the first screen it knows."""
    try:
        with open(OUTPUTS) as f:
            sections = json.load(f)
        outputs = next(s["data"] for s in sections if s.get("name") == "outputs")
    except (OSError, ValueError, StopIteration, KeyError, TypeError):
        return None
    internal = [o for o in outputs if str(o.get("connectorName", "")).startswith(("LVDS", "eDP"))]
    return (internal or outputs or [None])[0]


def screen_info(output):
    """(width, height, refresh Hz, diagonal inches) of an output, from KDE's own screen data, or None."""
    # kscreen-doctor is a Qt program too: it must talk to the real session even if we run offscreen
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    try:
        data = json.loads(subprocess.run(["kscreen-doctor", "-j"], capture_output=True, text=True, timeout=5,
                                         env=env).stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    for o in data.get("outputs", []):
        if o.get("name") == output:
            mode = next((m for m in o.get("modes", []) if m.get("id") == o.get("currentModeId")), {})
            size, mm = mode.get("size") or o.get("size") or {}, o.get("sizeMM") or {}
            diagonal = (mm.get("width", 0) ** 2 + mm.get("height", 0) ** 2) ** 0.5 / 25.4
            return size.get("width"), size.get("height"), mode.get("refreshRate"), diagonal or None
    return None


def night_light_config():
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    try:
        parser.read(KWINRC)
    except configparser.Error:
        pass
    sec = parser["NightColor"] if parser.has_section("NightColor") else {}
    try:
        temp = int(sec.get("NightTemperature", 4500))
    except ValueError:
        temp = 4500
    mode = sec.get("Mode", "DarkLight")
    return {"active": sec.get("Active", "false").lower() == "true",
            "mode": mode if mode in NIGHT_MODES else "DarkLight",
            "temperature": min(max(temp, NIGHT_MIN), NIGHT_MAX)}


class Desktop:
    """Live state over D-Bus. Create it after the QApplication exists."""

    def __init__(self):
        bus = QDBusConnection.sessionBus()
        self.night = QDBusInterface("org.kde.KWin", "/org/kde/KWin/NightLight", "org.kde.KWin.NightLight", bus)
        self.brightness_path = None
        root = QDBusInterface("org.kde.ScreenBrightness", "/org/kde/ScreenBrightness", "org.kde.ScreenBrightness", bus)
        for name in (root.property("DisplaysDBusNames") or []) if root.isValid() else []:
            path = f"/org/kde/ScreenBrightness/{name}"
            display = QDBusInterface("org.kde.ScreenBrightness", path, "org.kde.ScreenBrightness.Display", bus)
            if display.isValid() and (display.property("IsInternal") or self.brightness_path is None):
                self.brightness_path, self.display = path, display

    def brightness(self):
        """(brightness, maximum) on KDE's scale, or None."""
        if not self.brightness_path:
            return None
        value, top = self.display.property("Brightness"), self.display.property("MaxBrightness")
        return (value, top) if isinstance(value, int) and isinstance(top, int) and top > 0 else None

    def night_state(self):
        if not self.night.isValid():
            return None
        return {"available": bool(self.night.property("available")), "running": bool(self.night.property("running")),
                "current": self.night.property("currentTemperature")}

    # Changes. D-Bus methods with unsigned arguments go through busctl, which types them exactly.

    def set_brightness(self, value):
        if self.brightness_path:
            # flags 1 = don't show KDE's on-screen indicator while dragging our slider
            QProcess.startDetached("busctl", ["--user", "call", "org.kde.ScreenBrightness", self.brightness_path,
                                              "org.kde.ScreenBrightness.Display", "SetBrightness", "iu", str(value), "1"])

    def preview_night(self, kelvin):
        QProcess.startDetached("busctl", ["--user", "call", "org.kde.KWin", "/org/kde/KWin/NightLight",
                                          "org.kde.KWin.NightLight", "preview", "u", str(kelvin)])

    def stop_preview(self):
        QProcess.startDetached("busctl", ["--user", "call", "org.kde.KWin", "/org/kde/KWin/NightLight",
                                          "org.kde.KWin.NightLight", "stopPreview"])


def night_light_commands(active, mode, temperature):
    """kwriteconfig6 calls that save the night light settings and tell KWin about them."""
    base = ["--notify", "--file", "kwinrc", "--group", "NightColor"]
    return [("kwriteconfig6", [*base, "--key", "Active", "true" if active else "false"]),
            ("kwriteconfig6", [*base, "--key", "Mode", mode]),
            ("kwriteconfig6", [*base, "--key", "NightTemperature", str(temperature)])]


def screen_commands(output, **settings):
    """kscreen-doctor call for the given output: auto_brightness=bool, profile=("sRGB"|"EDID"|"ICC", path)."""
    args = []
    if "auto_brightness" in settings:
        args.append(f"output.{output}.autoBrightness.{'enable' if settings['auto_brightness'] else 'disable'}")
    if "profile" in settings:
        source, path = settings["profile"]
        if source == "ICC":
            args.append(f"output.{output}.iccprofile.{path}")
        args.append(f"output.{output}.colorProfileSource.{source}")
    return [("kscreen-doctor", args)]


class Runner:
    """Runs a list of (program, args) one after another and reports (ok, error text) at the end."""

    def __init__(self, parent):
        self.parent, self.queue, self.proc, self.done = parent, [], None, None

    def busy(self):
        return self.proc is not None

    def run(self, commands, done):
        self.queue, self.done = list(commands), done
        self._next()

    def _next(self):
        if not self.queue:
            self.proc = None
            self.done(True, "")
            return
        program, args = self.queue.pop(0)
        self.proc = QProcess(self.parent)
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(self._error)
        self.proc.start(program, args)

    def _finished(self, code, _status):
        if code == 0:
            self._next()
            return
        text = (bytes(self.proc.readAllStandardError()) + bytes(self.proc.readAllStandardOutput())).decode().strip()
        program = self.proc.program()
        self.proc, self.queue = None, []
        self.done(False, text.splitlines()[-1] if text else f"{program} failed (exit code {code})")

    def _error(self, error):
        if error == QProcess.FailedToStart:
            program = self.proc.program()
            self.proc, self.queue = None, []
            self.done(False, f"{program} isn't installed")
