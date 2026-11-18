"""The app's pages: Summary, Sensors, Fan and Keyboard. Each gets refresh(snapshot) once a second."""
import json
import os
import shutil

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QPushButton, QRadioButton, QSlider, QSpinBox, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

import desktop
import sensors
from widgets import FanCurve, LiveChart, StatCard, muted

SMCFAN = "/usr/local/bin/smcfan"
SETTINGS_HELPER = "/usr/local/bin/macbook-settings"

# Keep in sync with smcfan.py
PRESETS = {"quiet": (70, 92), "balanced": (65, 85), "cool": (55, 78)}
SAFETY_TEMP = 92

MODE_NAMES = {"stock": "Mac default", "quiet": "Quiet", "balanced": "Balanced", "cool": "Cool",
              "custom": "Custom curve", "fixed": "Fixed speed"}


def fan_target(cfg, temp, floor, top):
    """Same rule as smcfan: the minimum fan speed it sets for a temperature."""
    if temp >= SAFETY_TEMP:
        return top
    mode = cfg.get("mode")
    if mode == "stock":
        return floor
    if mode == "fixed":
        return min(max(int(cfg.get("rpm", floor)), floor), top)
    low, high = (cfg["low"], cfg["high"]) if mode == "custom" else PRESETS.get(mode, PRESETS["balanced"])
    return floor + min(max((temp - low) / (high - low), 0), 1) * (top - floor)


def fmt_duration(seconds):
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def gib(n):
    return f"{n / 2**30:.1f} GiB"


def heading(text):
    label = QLabel(text)
    font = QFont(label.font())
    font.setPointSizeF(font.pointSizeF() * 1.35)
    font.setWeight(QFont.DemiBold)
    label.setFont(font)
    return label


class SummaryPage(QWidget):
    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.addWidget(heading("Summary"))
        self.header = muted(QLabel())
        outer.addWidget(self.header)
        outer.addSpacing(6)

        self.fan = StatCard("Fan")
        self.fan.add_bar()
        self.fan_chart = self.fan.add_widget(LiveChart("rpm", y_min=0, y_max=6200))
        self.cpu = StatCard("CPU temperature")
        self.cpu_chart = self.cpu.add_widget(LiveChart("°C", y_min=40, y_max=100))
        self.power = StatCard("Power draw")
        self.power_chart = self.power.add_widget(LiveChart("W", decimals=1, y_min=0, y_max=30))
        self.battery = StatCard("Battery")
        self.battery.add_bar()
        self.memory = StatCard("Memory")
        self.memory.add_bar()
        self.keyboard = StatCard("Keyboard light")
        self.keyboard.add_bar()
        self.surface = StatCard("Palm rest")
        self.charger = StatCard("Charger")
        self.light = StatCard("Ambient light")

        grid = QGridLayout()
        grid.setSpacing(10)
        cards = [self.fan, self.cpu, self.power, self.battery, self.memory, self.keyboard,
                 self.surface, self.charger, self.light]
        for i, card in enumerate(cards):
            grid.addWidget(card, i // 3, i % 3)
        for col in range(3):
            grid.setColumnStretch(col, 1)
        outer.addLayout(grid)
        outer.addStretch()

    def refresh(self, s):
        info = s["system"]
        self.header.setText(f"{info['model']}  ·  kernel {info['kernel']}  ·  up {fmt_duration(info['uptime'])}")

        fan = s["fan"]
        if fan:
            mode = MODE_NAMES.get(fan["mode"], "mbpfan" if fan["controller"] == "mbpfan" else "not controlled")
            self.fan.set(f"{fan['actual']} rpm", f"{mode} · of {fan['max']} rpm max", fan["actual"] / (fan["max"] or 1))
        else:
            self.fan.set("n/a", "No fan readings: smcfan isn't running and applesmc isn't loaded")
        self.fan_chart.add(fan["actual"] if fan else None)

        cpu = s["cpu"]
        ghz = f" · {cpu['mhz'] / 1000:.1f} GHz" if cpu["mhz"] else ""
        self.cpu.set(f"{cpu['temp']:.0f} °C" if cpu["temp"] is not None else "n/a", f"{cpu['usage']:.0f} % busy{ghz}")
        self.cpu_chart.add(cpu["temp"])

        bat, ac = s["battery"], s["charger"]
        watts = s["power"] if s["power"] is not None else (bat or {}).get("watts")
        source = "on charger" if ac and ac["online"] else "on battery"
        self.power.set(f"{watts:.1f} W" if watts is not None else "n/a", f"whole system · {source}")
        self.power_chart.add(watts)

        if bat:
            parts = [bat["status"]]
            if bat["seconds"]:
                parts.append(f"{fmt_duration(bat['seconds'])} {'to full' if bat['status'] == 'Charging' else 'left'}")
            if bat["health"]:
                parts.append(f"health {bat['health'].lower()}")
            self.battery.set(f"{bat['percent']} %", " · ".join(parts), (bat["percent"] or 0) / 100)
        else:
            self.battery.set("n/a", "No battery found")

        used, total = s["memory"]
        self.memory.set(gib(used), f"of {gib(total)} in use", used / total)

        kb = s["keyboard"]
        if kb:
            pct = round(100 * kb[0] / kb[1])
            state = (s["kbdlight"]["status"] or {}).get("state")
            how = {"auto": "automatic, follows the room", "paused": "automatic, paused after a manual change"}
            self.keyboard.set("Off" if pct == 0 else f"{pct} %", how.get(state, "F5 / F6, or the Keyboard page"),
                              kb[0] / kb[1])
        else:
            self.keyboard.set("n/a", "No keyboard backlight found")

        if s["palm_rest"] is not None:
            extra = [f"bottom {s['bottom']:.0f} °C" if s["bottom"] is not None else None,
                     f"battery {bat['temp']:.0f} °C" if bat and bat["temp"] is not None else None]
            self.surface.set(f"{s['palm_rest']:.0f} °C", " · ".join(e for e in extra if e))
        else:
            self.surface.set("n/a", "No case sensors on this kernel")

        if ac and ac["online"]:
            self.charger.set(f"{ac['watts']:.0f} W" if ac["watts"] is not None else "Connected",
                             "MagSafe connected" + (f" · charge limit {bat['limit']} %" if bat and bat["limit"] else ""))
        else:
            self.charger.set("Unplugged", "running on battery" if bat else "")

        light = s["light"]
        lux = light.get("Ambient (lux)")
        smc = [f"{k.split()[1]} {v}" for k, v in light.items() if k.startswith("SMC")]
        self.light.set(f"{lux} lux" if lux is not None else "n/a", ("SMC sensors: " + ", ".join(smc)) if smc else "")


class SensorsPage(QWidget):
    GROUPS = {"temp": "Temperatures", "power": "Power", "curr": "Current", "in": "Voltage"}
    DECIMALS = {"temp": 1, "power": 2, "curr": 3, "in": 3}

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.addWidget(heading("Sensors"))
        lay.addWidget(muted(QLabel("Everything the SMC and CPU report, updated every second. Hover a name for its SMC key.")))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Sensor", "SMC key", "Value"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setStretchLastSection(False)
        lay.addWidget(self.tree, 1)
        self.groups, self.items = {}, {}

    def _group(self, name):
        if name not in self.groups:
            item = QTreeWidgetItem(self.tree, [name])
            font = item.font(0)
            font.setWeight(QFont.DemiBold)
            item.setFont(0, font)
            item.setFirstColumnSpanned(True)
            item.setExpanded(True)
            self.groups[name] = item
        return self.groups[name]

    def _row(self, group, ident, name, key, value):
        item = self.items.get(ident)
        if item is None:
            item = QTreeWidgetItem(self._group(group), [name, key, value])
            item.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)
            if key:
                item.setToolTip(0, f"SMC key {key}")
            self.items[ident] = item
        else:
            item.setText(2, value)

    def refresh(self, s):
        fan = s["fan"]
        if fan:
            self._row("Fan", "fan.actual", "Fan speed", "F0Ac", f"{fan['actual']} rpm")
            self._row("Fan", "fan.target", "Minimum set by the controller", "F0Mn", f"{fan['target']} rpm")
            self._row("Fan", "fan.max", "Maximum", "F0Mx", f"{fan['max']} rpm")
        for label, temp in s["cores"].items():
            self._row("CPU (coretemp)", f"core.{label}", label, "", f"{temp:.0f} °C")
        for kind, name, key, value, unit in s["smc"]:
            self._row(self.GROUPS[kind], f"{kind}.{key}.{name}", name, key, f"{value:.{self.DECIMALS[kind]}f} {unit}")
        bat = s["battery"]
        if bat:
            rows = [("Charge", f"{bat['percent']} %"), ("Status", bat["status"]),
                    ("Power", f"{bat['watts']:.1f} W" if bat["watts"] is not None else None),
                    ("Capacity when full", f"{bat['full_wh']:.1f} Wh" if bat["full_wh"] else None),
                    ("Charge cycles", str(bat["cycles"]) if bat["cycles"] is not None else None),
                    ("Health", bat["health"]), ("Temperature", f"{bat['temp']:.1f} °C" if bat["temp"] is not None else None)]
            for name, value in rows:
                if value is not None:
                    self._row("Battery", f"bat.{name}", name, "", value)


class FanPage(QWidget):
    MODES = [
        ("stock", "Leave the fan to the Mac's own controller. On Linux it mostly stays at its minimum, even when hot."),
        ("quiet", "Starts speeding up at 70 °C, full speed at 92 °C. Quietest, runs warmer."),
        ("balanced", "Starts at 65 °C, full speed at 85 °C. Recommended."),
        ("cool", "Starts at 55 °C, full speed at 78 °C. Coolest, louder."),
        ("custom", "Choose where the fan starts speeding up and where it reaches full speed."),
        ("fixed", "Keep the fan at least at this speed, whatever the temperature."),
    ]

    def __init__(self):
        super().__init__()
        self.floor, self.top, self.temp = 2000, 6200, None
        self.dirty = False        # the user changed something that isn't applied yet
        self.loaded = False
        self.showing = False      # _show_config is moving the controls, not the user
        self.result = ""          # outcome of the last Apply, shown until the user changes something

        outer = QVBoxLayout(self)
        outer.addWidget(heading("Fan"))
        self.live = QLabel("–")
        font = QFont(self.live.font())
        font.setPointSizeF(font.pointSizeF() * 1.6)
        font.setWeight(QFont.DemiBold)
        self.live.setFont(font)
        outer.addWidget(self.live)
        self.live_detail = muted(QLabel())
        outer.addWidget(self.live_detail)
        outer.addSpacing(6)

        body = QHBoxLayout()
        outer.addLayout(body, 1)

        box = QGroupBox("Mode")
        modes = QVBoxLayout(box)
        self.group = QButtonGroup(self)
        self.buttons = {}
        for mode, text in self.MODES:
            rb = QRadioButton(MODE_NAMES[mode])
            self.group.addButton(rb)
            self.buttons[mode] = rb
            modes.addWidget(rb)
            desc = muted(QLabel(text))
            desc.setWordWrap(True)
            desc.setContentsMargins(24, 0, 0, 4)
            modes.addWidget(desc)
            if mode == "custom":
                row = QHBoxLayout()
                row.setContentsMargins(24, 0, 0, 6)
                self.low, self.high = QSpinBox(), QSpinBox()
                for spin, value in ((self.low, 60), (self.high, 85)):
                    spin.setRange(40, 100)
                    spin.setSuffix(" °C")
                    spin.setValue(value)
                    spin.valueChanged.connect(self._changed)
                row.addWidget(QLabel("Start at"))
                row.addWidget(self.low)
                row.addWidget(QLabel("full speed at"))
                row.addWidget(self.high)
                row.addStretch()
                modes.addLayout(row)
            if mode == "fixed":
                row = QHBoxLayout()
                row.setContentsMargins(24, 0, 0, 6)
                self.rpm_slider = QSlider(Qt.Horizontal)
                self.rpm_slider.setRange(2000, 6200)
                self.rpm_slider.setSingleStep(100)
                self.rpm_slider.setPageStep(500)
                self.rpm_spin = QSpinBox()
                self.rpm_spin.setRange(2000, 6200)
                self.rpm_spin.setSingleStep(100)
                self.rpm_spin.setSuffix(" rpm")
                self.rpm_slider.valueChanged.connect(self.rpm_spin.setValue)
                self.rpm_spin.valueChanged.connect(self.rpm_slider.setValue)
                self.rpm_spin.setValue(3500)
                self.rpm_spin.valueChanged.connect(self._changed)
                row.addWidget(self.rpm_slider, 1)
                row.addWidget(self.rpm_spin)
                modes.addLayout(row)
        modes.addStretch()
        self.group.buttonToggled.connect(lambda *_: self._changed())
        body.addWidget(box, 1)

        right = QVBoxLayout()
        self.curve = FanCurve()
        right.addWidget(self.curve, 1)
        note = muted(QLabel(f"In every mode the fan runs at full speed from {SAFETY_TEMP} °C, and the Mac's own "
                            "controller can always speed it up further."))
        note.setWordWrap(True)
        right.addWidget(note)
        body.addLayout(right, 1)

        bottom = QHBoxLayout()
        self.message = muted(QLabel())
        self.message.setWordWrap(True)
        bottom.addWidget(self.message, 1)
        self.apply = QPushButton("Apply")
        self.apply.setEnabled(False)
        self.apply.clicked.connect(self._apply)
        bottom.addWidget(self.apply)
        outer.addLayout(bottom)

        self.proc = None
        self.mode_box = box

    def selected(self):
        mode = next((m for m, b in self.buttons.items() if b.isChecked()), "balanced")
        cfg = {"mode": mode}
        if mode == "custom":
            cfg.update(low=self.low.value(), high=self.high.value())
        elif mode == "fixed":
            cfg["rpm"] = self.rpm_spin.value()
        return cfg

    def _show_config(self, cfg):
        self.showing = True
        self.buttons.get(cfg.get("mode"), self.buttons["balanced"]).setChecked(True)
        if cfg.get("mode") == "custom":
            self.low.setValue(int(cfg["low"]))
            self.high.setValue(int(cfg["high"]))
        if cfg.get("mode") == "fixed":
            self.rpm_spin.setValue(int(cfg["rpm"]))
        self.showing = False
        self.dirty = False
        self._update_curve()

    def _changed(self):
        if self.loaded and not self.showing:
            self.dirty = True
            self.result = ""
        self._update_curve()

    def _update_curve(self):
        cfg = self.selected()
        valid = not (cfg["mode"] == "custom" and cfg["high"] - cfg["low"] < 5)
        if valid:
            self.curve.set_curve(lambda t: fan_target(cfg, t, self.floor, self.top), self.floor, self.top)
        self.apply.setEnabled(self.dirty and valid and self.proc is None and self.mode_box.isEnabled())
        if not valid:
            self.message.setText("Full speed must be at least 5 °C above the start temperature.")
        else:
            self.message.setText(self.result or ("Not applied yet." if self.dirty else ""))

    def refresh(self, s):
        fan, cpu = s["fan"], s["cpu"]
        self.temp = cpu["temp"]
        if fan:
            self.floor, self.top = fan.get("floor") or 2000, fan["max"] or 6200
            self.live.setText(f"{fan['actual']} rpm")
            parts = [MODE_NAMES[fan["mode"]]] if fan["mode"] in MODE_NAMES else []
            parts.append(f"controller minimum {fan['target']} rpm")
            if cpu["temp"] is not None:
                parts.append(f"CPU {cpu['temp']:.0f} °C")
            self.live_detail.setText(" · ".join(parts))
            self.curve.set_now(cpu["temp"], fan["actual"])
        else:
            self.live.setText("n/a")

        controllable = bool(fan and fan["controller"] == "smcfan" and os.path.exists(SMCFAN) and shutil.which("pkexec"))
        self.mode_box.setEnabled(controllable)
        if not controllable:
            patch = sensors.smc_patch_status() or ""
            if fan and fan["controller"] == "mbpfan":
                why = "This kernel uses the old applesmc driver, so mbpfan controls the fan. Modes here need smcfan (kernels with the macsmc driver)."
            elif patch.startswith("refused"):
                why = ("This kernel's SMC driver refuses Macs without a T2 chip, and the automatic fix couldn't be "
                       f"applied ({patch.split(': ', 1)[-1]}). Boot the LTS kernel for fan control until it's updated.")
            elif patch == "patched" and not os.path.exists("/dev/smc"):
                why = ("The SMC driver is fixed for this kernel but isn't loaded yet. It starts after a restart, "
                       "or right now with: sudo macbook-smc-patch --reload")
            else:
                why = "smcfan isn't running, so the fan mode can't be changed. Start it with: sudo systemctl start smcfan"
            self.message.setText(why)
            self.apply.setEnabled(False)
            return
        if not self.loaded:
            self._show_config(s["fan_config"])
            self.loaded = True
        elif not self.dirty and self.proc is None and s["fan_config"] != self.selected():
            self._show_config(s["fan_config"])   # changed elsewhere, e.g. with smcfan set
        self._update_curve()

    def _apply(self):
        cfg = self.selected()
        args = ["--low", str(cfg["low"]), "--high", str(cfg["high"])] if cfg["mode"] == "custom" else \
               ["--rpm", str(cfg["rpm"])] if cfg["mode"] == "fixed" else []
        self.proc = QProcess(self)
        self.proc.finished.connect(self._applied)
        self.result = "Applying…"
        self._update_curve()
        self.proc.start("pkexec", [SMCFAN, "set", cfg["mode"], *args])

    def _applied(self, code, status):
        out = bytes(self.proc.readAllStandardOutput()).decode().strip()
        err = bytes(self.proc.readAllStandardError()).decode().strip()
        self.proc = None
        if code == 0:
            self.dirty = False
            self.result = out or "Applied."
        elif code in (126, 127):
            self.result = "Not changed: permission was refused or cancelled."
        else:
            self.result = f"Couldn't change the mode: {err or out or f'exit code {code}'}"
        self._update_curve()


class Privileged:
    """Runs one `pkexec PROGRAM ARGS` at a time and hands (ok, message) to a callback."""

    def __init__(self, parent):
        self.parent, self.proc = parent, None

    def busy(self):
        return self.proc is not None

    def run(self, program, args, done):
        self.proc = QProcess(self.parent)
        self.proc.finished.connect(lambda code, _status: self._finished(code, done))
        self.proc.errorOccurred.connect(lambda error: self._failed(error, done))
        self.proc.start("pkexec", [program, *args])

    def _finished(self, code, done):
        out = bytes(self.proc.readAllStandardOutput()).decode().strip()
        err = bytes(self.proc.readAllStandardError()).decode().strip()
        self.proc = None
        if code == 0:
            done(True, out or "Done.")
        elif code in (126, 127):
            done(False, "Not changed: permission was refused or cancelled.")
        else:
            done(False, err or out or f"Failed (exit code {code}).")

    def _failed(self, error, done):
        if error == QProcess.FailedToStart:
            self.proc = None
            done(False, "Not changed: pkexec isn't installed.")


class BatteryPage(QWidget):
    INFO = ["Capacity when full", "Original capacity", "Health", "Charge cycles", "Temperature", "Voltage", "Charger"]

    def __init__(self):
        super().__init__()
        self.priv = Privileged(self)
        self.loaded = self.dirty = self.showing = False
        self.result = ""
        self.blocker = ""       # why the limit can't be changed right now, if it can't

        outer = QVBoxLayout(self)
        outer.addWidget(heading("Battery"))
        self.value = QLabel("–")
        font = QFont(self.value.font())
        font.setPointSizeF(font.pointSizeF() * 1.6)
        font.setWeight(QFont.DemiBold)
        self.value.setFont(font)
        outer.addWidget(self.value)
        self.detail = muted(QLabel())
        outer.addWidget(self.detail)
        outer.addSpacing(6)

        row = QHBoxLayout()
        box = QGroupBox("Health")
        grid = QGridLayout(box)
        self.info = {}
        for i, name in enumerate(self.INFO):
            grid.addWidget(muted(QLabel(name)), i, 0)
            self.info[name] = QLabel("–")
            grid.addWidget(self.info[name], i, 1, Qt.AlignRight)
        row.addWidget(box, 1)
        self.level = StatCard("Charge level")
        self.level_chart = self.level.add_widget(LiveChart("%", y_min=0, y_max=100, seconds=1800))
        row.addWidget(self.level, 2)
        outer.addLayout(row)
        outer.addSpacing(6)

        self.limit_box = QGroupBox("Charge limit")
        lay = QVBoxLayout(self.limit_box)
        intro = muted(QLabel("A battery that sits at 100 % all day ages faster. If the MacBook is plugged in most "
                             "of the time, stopping earlier helps the battery last longer."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.group = QButtonGroup(self)
        self.full = QRadioButton("Charge to 100 %")
        self.eighty = QRadioButton("Stop at 80 %  (recommended if it's mostly plugged in)")
        self.custom = QRadioButton("Stop at")
        self.spin = QSpinBox()
        self.spin.setRange(50, 95)
        self.spin.setSingleStep(5)
        self.spin.setSuffix(" %")
        self.spin.setValue(70)
        for rb in (self.full, self.eighty):
            self.group.addButton(rb)
            lay.addWidget(rb)
        self.group.addButton(self.custom)
        custom_row = QHBoxLayout()
        custom_row.addWidget(self.custom)
        custom_row.addWidget(self.spin)
        custom_row.addStretch()
        lay.addLayout(custom_row)
        self.group.buttonToggled.connect(lambda *_: self._changed())
        self.spin.valueChanged.connect(self._changed)
        bottom = QHBoxLayout()
        self.message = muted(QLabel())
        self.message.setWordWrap(True)
        bottom.addWidget(self.message, 1)
        self.apply = QPushButton("Apply")
        self.apply.setEnabled(False)
        self.apply.clicked.connect(self._apply)
        bottom.addWidget(self.apply)
        lay.addLayout(bottom)
        note = muted(QLabel("KDE's Power Management settings can set the same limit. Use only one of the two, "
                            "or they will overwrite each other."))
        note.setWordWrap(True)
        lay.addWidget(note)
        outer.addWidget(self.limit_box)
        outer.addStretch()

    def selected(self):
        if self.full.isChecked():
            return 100
        if self.eighty.isChecked():
            return 80
        return self.spin.value()

    def _show_limit(self, limit):
        self.showing = True
        if limit >= 100:
            self.full.setChecked(True)
        elif limit == 80:
            self.eighty.setChecked(True)
        else:
            self.custom.setChecked(True)
            self.spin.setValue(limit)
        self.showing = False
        self.dirty = False
        self._update()

    def _changed(self):
        if self.loaded and not self.showing:
            self.dirty = True
            self.result = ""
        self._update()

    def _update(self):
        self.limit_box.setEnabled(not self.blocker)
        self.apply.setEnabled(self.dirty and not self.priv.busy() and not self.blocker)
        self.message.setText(self.blocker or self.result or ("Not applied yet." if self.dirty else ""))

    def _apply(self):
        self.result = "Applying…"
        self._update()
        self.priv.run(SETTINGS_HELPER, ["set", "charge-limit", str(self.selected())], self._applied)

    def _applied(self, ok, message):
        if ok:
            self.dirty = False
        self.result = message
        self._update()

    def refresh(self, s):
        bat, ac, design = s["battery"], s["charger"], s["design"]
        if not bat:
            self.value.setText("No battery found")
            self.limit_box.setEnabled(False)
            return
        self.value.setText(f"{bat['percent']} %")
        parts = [bat["status"]]
        if bat["seconds"]:
            parts.append(f"{fmt_duration(bat['seconds'])} {'to full' if bat['status'] == 'Charging' else 'left'}")
        if bat["watts"]:
            parts.append(f"{bat['watts']:.1f} W {'in' if bat['status'] == 'Charging' else 'out'}")
        self.detail.setText(" · ".join(parts))

        full = bat["full_mah"]
        values = {
            "Capacity when full": f"{full:.0f} mAh  ({bat['full_wh']:.1f} Wh)" if full and bat["full_wh"] else
                                  f"{bat['full_wh']:.1f} Wh" if bat["full_wh"] else None,
            "Original capacity": f"{design[0]} mAh  ({design[1]} battery)" if design else None,
            "Health": (f"{100 * full / design[0]:.0f} % of original" if full and design else bat["health"]),
            "Charge cycles": str(bat["cycles"]) if bat["cycles"] is not None else None,
            "Temperature": f"{bat['temp']:.1f} °C" if bat["temp"] is not None else None,
            "Voltage": f"{bat['volts']:.2f} V" if bat["volts"] is not None else None,
            "Charger": (f"connected, {ac['watts']:.0f} W" if ac["watts"] is not None else "connected")
                       if ac and ac["online"] else "unplugged",
        }
        for name, value in values.items():
            self.info[name].setText(value or "–")
        self.level.set(f"{bat['percent']} %", "last 30 minutes")
        self.level_chart.add(bat["percent"])

        if bat["limit"] is None:
            self.blocker = "This kernel's battery driver doesn't support a charge limit."
        elif not os.path.exists(SETTINGS_HELPER) or not shutil.which("pkexec"):
            self.blocker = "Install the latest MacBook Control (sudo bash install.sh) to change the limit."
        else:
            self.blocker = ""
        if bat["limit"] is not None and (not self.loaded or (not self.dirty and not self.priv.busy()
                                                            and bat["limit"] != self.selected())):
            self._show_limit(bat["limit"])
            self.loaded = True
        self._update()


class DisplayPage(QWidget):
    """Brightness, automatic brightness, night light and color profile of the built-in screen, all through KDE."""

    PANELS = {"APP": "Apple"}

    def __init__(self):
        super().__init__()
        self.desk = desktop.Desktop()
        self.auto_runner, self.night_runner, self.profile_runner = (desktop.Runner(self) for _ in range(3))
        self.output = None
        self.top = 10000            # KDE's brightness scale, refreshed from D-Bus
        self.bright_hold = 0        # refresh ticks to wait after the user moved the slider
        self.profile_dirty = False
        self.icc_path = ""

        outer = QVBoxLayout(self)
        outer.addWidget(heading("Display"))
        self.info = muted(QLabel())
        outer.addWidget(self.info)
        outer.addSpacing(6)

        box = QGroupBox("Brightness")
        lay = QVBoxLayout(box)
        row = QHBoxLayout()
        self.bright = QSlider(Qt.Horizontal)
        self.bright.setRange(1, 100)
        self.bright.valueChanged.connect(self._bright_moved)
        self.bright_label = QLabel("–")
        self.bright_label.setMinimumWidth(48)
        self.bright_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.bright, 1)
        row.addWidget(self.bright_label)
        lay.addLayout(row)
        arow = QHBoxLayout()
        self.auto = QCheckBox("Adjust automatically to the light in the room")
        self.auto.clicked.connect(self._auto_clicked)
        arow.addWidget(self.auto)
        arow.addStretch()
        self.lux = muted(QLabel())
        arow.addWidget(self.lux)
        lay.addLayout(arow)
        self.bright_message = muted(QLabel())
        lay.addWidget(self.bright_message)
        outer.addWidget(box)
        self.bright_send = QTimer(self)
        self.bright_send.setSingleShot(True)
        self.bright_send.setInterval(60)
        self.bright_send.timeout.connect(lambda: self.desk.set_brightness(round(self.bright.value() / 100 * self.top)))

        nbox = QGroupBox("Night light")
        nl = QVBoxLayout(nbox)
        intro = muted(QLabel("Warmer colors in the evening are easier on the eyes and make it easier to fall asleep."))
        intro.setWordWrap(True)
        nl.addWidget(intro)
        self.night_group = QButtonGroup(self)
        self.night_off = QRadioButton("Off")
        self.night_always = QRadioButton("Always on")
        self.night_schedule = QRadioButton("From sunset to sunrise")
        radios = QHBoxLayout()
        for rb in (self.night_off, self.night_always, self.night_schedule):
            self.night_group.addButton(rb)
            radios.addWidget(rb)
            rb.clicked.connect(self._save_night)
        radios.addStretch()
        nl.addLayout(radios)
        trow = QHBoxLayout()
        trow.addWidget(muted(QLabel("warmer")))
        self.warmth = QSlider(Qt.Horizontal)
        self.warmth.setRange(desktop.NIGHT_MIN, desktop.NIGHT_MAX)
        self.warmth.setSingleStep(100)
        self.warmth.setPageStep(500)
        self.warmth.sliderMoved.connect(self._warmth_dragged)
        self.warmth.sliderReleased.connect(self._warmth_released)
        self.warmth.valueChanged.connect(self._warmth_changed)
        trow.addWidget(self.warmth, 1)
        trow.addWidget(muted(QLabel("cooler")))
        self.warmth_label = QLabel("–")
        self.warmth_label.setMinimumWidth(64)
        self.warmth_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        trow.addWidget(self.warmth_label)
        nl.addLayout(trow)
        brow = QHBoxLayout()
        self.night_message = muted(QLabel())
        self.night_message.setWordWrap(True)
        brow.addWidget(self.night_message, 1)
        times = QPushButton("Sunset times…")
        times.setToolTip("Location or fixed times are set in KDE's Night Light settings")
        times.clicked.connect(lambda: QProcess.startDetached("systemsettings", ["kcm_nightlight"]))
        brow.addWidget(times)
        nl.addLayout(brow)
        outer.addWidget(nbox)
        self.night_save = QTimer(self)
        self.night_save.setSingleShot(True)
        self.night_save.setInterval(400)
        self.night_save.timeout.connect(self._save_night)
        self.preview_end = QTimer(self)
        self.preview_end.setSingleShot(True)
        self.preview_end.setInterval(2000)
        self.preview_end.timeout.connect(self.desk.stop_preview)

        cbox = QGroupBox("Color profile")
        cl = QVBoxLayout(cbox)
        intro = muted(QLabel("A color profile corrects how the screen shows colors. The most accurate one comes from "
                             "measuring this screen with a colorimeter (for example with DisplayCAL). Without one, "
                             "sRGB is a sensible default."))
        intro.setWordWrap(True)
        cl.addWidget(intro)
        self.profile_group = QButtonGroup(self)
        self.srgb = QRadioButton("sRGB (standard)")
        self.edid = QRadioButton("The panel's own color data (EDID)")
        self.icc = QRadioButton("ICC profile file:")
        for rb in (self.srgb, self.edid):
            self.profile_group.addButton(rb)
            cl.addWidget(rb)
        self.profile_group.addButton(self.icc)
        irow = QHBoxLayout()
        irow.addWidget(self.icc)
        self.icc_label = muted(QLabel("none chosen"))
        irow.addWidget(self.icc_label, 1)
        choose = QPushButton("Choose…")
        choose.clicked.connect(self._choose_icc)
        irow.addWidget(choose)
        cl.addLayout(irow)
        for rb in (self.srgb, self.edid, self.icc):
            rb.clicked.connect(self._profile_changed)
        prow = QHBoxLayout()
        self.profile_message = muted(QLabel())
        self.profile_message.setWordWrap(True)
        prow.addWidget(self.profile_message, 1)
        self.profile_apply = QPushButton("Apply")
        self.profile_apply.setEnabled(False)
        self.profile_apply.clicked.connect(self._apply_profile)
        prow.addWidget(self.profile_apply)
        cl.addLayout(prow)
        outer.addWidget(cbox)
        outer.addStretch()

    # Brightness

    def _bright_moved(self, pct):
        self.bright_label.setText(f"{pct} %")
        self.bright_hold = 3
        self.bright_send.start()

    def _auto_clicked(self, on):
        self.bright_message.setText("Switching…")
        self.auto_runner.run(desktop.screen_commands(self.output, auto_brightness=on),
                             lambda ok, err: self.bright_message.setText(
                                 ("Brightness now follows the room's light." if on else "Automatic brightness is off.")
                                 if ok else f"Couldn't change it: {err}"))

    # Night light

    def _warmth_dragged(self, kelvin):
        self.warmth_label.setText(f"{kelvin} K")
        self.preview_end.stop()
        self.desk.preview_night(kelvin)

    def _warmth_released(self):
        self.preview_end.start()
        self._save_night()

    def _warmth_changed(self, kelvin):
        self.warmth_label.setText(f"{kelvin} K")
        if not self.warmth.isSliderDown() and not self.warmth.signalsBlocked():
            self.night_save.start()     # keyboard or wheel changes

    def _save_night(self):
        active = not self.night_off.isChecked()
        mode = "Constant" if self.night_always.isChecked() else "DarkLight"
        kelvin = round(self.warmth.value() / 100) * 100
        self.night_message.setText("Saving…")
        self.night_runner.run(desktop.night_light_commands(active, mode, kelvin),
                              lambda ok, err: self.night_message.setText("" if ok else f"Couldn't save: {err}"))

    # Color profile

    def _choose_icc(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a color profile", os.path.expanduser("~"),
                                              "Color profiles (*.icc *.icm)")
        if path:
            self.icc_path = path
            self.icc_label.setText(os.path.basename(path))
            self.icc.setChecked(True)
            self._profile_changed()

    def _profile_changed(self):
        self.profile_dirty = True
        ready = not (self.icc.isChecked() and not self.icc_path)
        self.profile_apply.setEnabled(ready and not self.profile_runner.busy())
        self.profile_message.setText("Not applied yet." if ready else "Choose an ICC file first.")

    def _apply_profile(self):
        source = "ICC" if self.icc.isChecked() else "EDID" if self.edid.isChecked() else "sRGB"
        self.profile_apply.setEnabled(False)
        self.profile_message.setText("Applying…")
        self.profile_runner.run(desktop.screen_commands(self.output, profile=(source, self.icc_path)), self._profile_done)

    def _profile_done(self, ok, err):
        if ok:
            self.profile_dirty = False
            self.profile_message.setText("Applied.")
        else:
            self.profile_apply.setEnabled(True)
            self.profile_message.setText(f"Couldn't apply it: {err}")

    def refresh(self, s):
        out = desktop.output_config() or {}
        self.output = out.get("connectorName", "LVDS-1")
        parts = ["Built-in screen" if self.output.startswith(("LVDS", "eDP")) else self.output]
        if getattr(self, "screen_cache", (None,))[0] != self.output:
            self.screen_cache = (self.output, desktop.screen_info(self.output))
        info = self.screen_cache[1]
        if info and info[0]:
            width, height, hz, diagonal = info
            parts.append(f"{width} × {height}" + (f" at {hz:.0f} Hz" if hz else ""))
            if diagonal:
                parts.append(f"{diagonal:.1f}″")
        ident = str(out.get("edidIdentifier", "")).split()
        if ident:
            vendor = self.PANELS.get(ident[0], ident[0])
            parts.append(f"{vendor} panel {int(ident[1]):04X}" if len(ident) > 1 and ident[1].isdigit() else f"{vendor} panel")
        self.info.setText("  ·  ".join(parts))

        b = self.desk.brightness()
        self.bright.setEnabled(b is not None)
        if b is None:
            self.bright_label.setText("n/a")
            self.bright_message.setText("KDE's brightness service isn't reachable.")
        else:
            self.top = b[1]
            if self.bright_message.text().startswith("KDE's brightness"):
                self.bright_message.setText("")
            if self.bright_hold > 0:
                self.bright_hold -= 1
            elif not self.bright.isSliderDown():
                self.bright.blockSignals(True)
                self.bright.setValue(max(1, round(100 * b[0] / b[1])))
                self.bright.blockSignals(False)
                self.bright_label.setText(f"{self.bright.value()} %")
        if not self.auto_runner.busy():
            self.auto.setChecked(bool(out.get("automaticBrightness")))
        lux = s["light"].get("Ambient (lux)")
        self.lux.setText(f"light sensor: {lux} lux" if lux is not None else "")

        night = desktop.night_light_config()
        if not self.night_runner.busy() and not self.night_save.isActive() and not self.warmth.isSliderDown():
            (self.night_off if not night["active"] else
             self.night_always if night["mode"] == "Constant" else self.night_schedule).setChecked(True)
            self.warmth.blockSignals(True)
            self.warmth.setValue(night["temperature"])
            self.warmth.blockSignals(False)
            self.warmth_label.setText(f"{night['temperature']} K")
            state = self.desk.night_state()
            if state and not state["available"]:
                self.night_message.setText("Night light isn't available on this session.")
            elif state:
                kelvin = state["current"]
                tinting = state["running"] and isinstance(kelvin, int) and kelvin < desktop.NIGHT_MAX
                self.night_message.setText(f"Tinting the screen now: {kelvin} K" if tinting
                                           else "Not tinting the screen right now.")

        if not self.profile_dirty and not self.profile_runner.busy():
            source = out.get("colorProfileSource", "sRGB")
            path = out.get("iccProfilePath", "")
            if path:
                self.icc_path = path
                self.icc_label.setText(os.path.basename(path))
            (self.icc if source == "ICC" else self.edid if source == "EDID" else self.srgb).setChecked(True)
        self.edid.setEnabled(bool(out.get("edidHash") or out.get("edidIdentifier")))


class KeyboardPage(QWidget):
    PRESETS = [("Off", 0), ("Low", 25), ("Medium", 50), ("Full", 100)]

    def __init__(self):
        super().__init__()
        self.max = 255
        self.hold_until = 0     # ignore refreshes briefly after the user moves the slider
        lay = QVBoxLayout(self)
        lay.addWidget(heading("Keyboard"))
        self.value = QLabel("–")
        font = QFont(self.value.font())
        font.setPointSizeF(font.pointSizeF() * 1.6)
        font.setWeight(QFont.DemiBold)
        self.value.setFont(font)
        lay.addWidget(self.value)
        lay.addWidget(muted(QLabel("The F5 / F6 keys and KDE's battery panel change the same light.")))
        lay.addSpacing(8)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.valueChanged.connect(self._moved)
        lay.addWidget(self.slider)
        row = QHBoxLayout()
        self.preset_buttons = []
        for name, pct in self.PRESETS:
            b = QPushButton(name)
            b.clicked.connect(lambda _=False, p=pct: self.slider.setValue(p))
            row.addWidget(b)
            self.preset_buttons.append(b)
        row.addStretch()
        lay.addLayout(row)

        lay.addSpacing(14)
        self.keys_box = QGroupBox("Keys")
        keys = QVBoxLayout(self.keys_box)
        keys.addWidget(QLabel("The top row of keys (F1–F12) does:"))
        self.fn_group = QButtonGroup(self)
        self.fn_media = QRadioButton("Brightness, volume and media; F1–F12 when you hold Fn  (Mac default)")
        self.fn_function = QRadioButton("F1–F12; brightness, volume and media when you hold Fn")
        for rb in (self.fn_media, self.fn_function):
            self.fn_group.addButton(rb)
            keys.addWidget(rb)
            rb.clicked.connect(self._fn_clicked)
        keys.addSpacing(6)
        self.swap_opt = QCheckBox("Swap Option and Command, so Alt sits next to the space bar like on a PC keyboard")
        self.swap_fn = QCheckBox("Swap Fn and the left Control key, so Ctrl is in the bottom-left corner like on a PC keyboard")
        self.swap_opt.clicked.connect(lambda on: self._set_key("swap-opt-cmd", "on" if on else "off"))
        self.swap_fn.clicked.connect(lambda on: self._set_key("swap-fn-ctrl", "on" if on else "off"))
        keys.addWidget(self.swap_opt)
        keys.addWidget(self.swap_fn)
        self.keys_message = muted(QLabel("Changes take effect immediately and are kept after a restart."))
        self.keys_message.setWordWrap(True)
        keys.addWidget(self.keys_message)
        lay.addWidget(self.keys_box)
        self.priv = Privileged(self)

        self.auto_box = QGroupBox("Automatic")
        al = QVBoxLayout(self.auto_box)
        self.auto_on = QCheckBox("Turn the keyboard light on by itself when the room gets dark")
        self.auto_on.clicked.connect(self._auto_edited)
        al.addWidget(self.auto_on)
        grid = QGridLayout()
        grid.setContentsMargins(24, 0, 0, 0)
        grid.addWidget(QLabel("Starts coming on below"), 0, 0)
        self.below = QSpinBox()
        self.below.setRange(5, 1000)
        self.below.setSingleStep(5)
        self.below.setSuffix(" lux")
        self.below.valueChanged.connect(self._auto_edited)
        grid.addWidget(self.below, 0, 1)
        self.room_now = muted(QLabel())
        grid.addWidget(self.room_now, 0, 2)
        grid.addWidget(QLabel("Brightness in the dark"), 1, 0)
        self.dark_level = QSlider(Qt.Horizontal)
        self.dark_level.setRange(10, 100)
        self.dark_level.setSingleStep(5)
        self.dark_level.setMinimumWidth(240)
        self.dark_level.valueChanged.connect(self._auto_edited)
        grid.addWidget(self.dark_level, 1, 1)
        self.dark_label = QLabel()
        grid.addWidget(self.dark_label, 1, 2)
        grid.setColumnStretch(2, 1)
        al.addLayout(grid)
        self.auto_status = muted(QLabel())
        self.auto_status.setWordWrap(True)
        al.addWidget(self.auto_status)
        self.sensor_line = muted(QLabel())
        al.addWidget(self.sensor_line)
        lay.insertSpacing(lay.indexOf(self.keys_box), 14)
        lay.insertWidget(lay.indexOf(self.keys_box), self.auto_box)
        lay.insertSpacing(lay.indexOf(self.keys_box), 14)
        lay.addStretch()
        self.auto_save = QTimer(self)
        self.auto_save.setSingleShot(True)
        self.auto_save.setInterval(300)
        self.auto_save.timeout.connect(self._save_auto)
        self.auto_loaded = False

        self.send = QTimer(self)
        self.send.setSingleShot(True)
        self.send.setInterval(80)
        self.send.timeout.connect(self._send)

    def _show(self, pct):
        self.value.setText("Light off" if pct == 0 else f"Light {pct} %")

    def _moved(self, pct):
        """The user moved the slider or pressed a preset."""
        self._show(pct)
        self.hold_until = 3       # refresh ticks to wait before trusting sysfs again
        self.send.start()

    def _send(self):
        raw = round(self.slider.value() / 100 * self.max)
        QProcess.startDetached("busctl", ["--system", "call", "org.freedesktop.UPower",
                                          "/org/freedesktop/UPower/KbdBacklight",
                                          "org.freedesktop.UPower.KbdBacklight", "SetBrightness", "i", str(raw)])

    def _fn_clicked(self):
        self._set_key("fn-keys", "function" if self.fn_function.isChecked() else "media")

    def _set_key(self, name, value):
        self.keys_box.setEnabled(False)
        self.keys_message.setText("Applying…")
        self.priv.run(SETTINGS_HELPER, ["set", name, value], self._key_set)

    def _key_set(self, ok, message):
        self.keys_box.setEnabled(True)
        texts = {"fn-keys set to function": "Top row now gives F1–F12; hold Fn for brightness, volume and media.",
                 "fn-keys set to media": "Top row now controls brightness, volume and media; hold Fn for F1–F12.",
                 "swap-opt-cmd set to on": "Option and Command are swapped.",
                 "swap-opt-cmd set to off": "Option and Command are back to the Mac layout.",
                 "swap-fn-ctrl set to on": "Fn and left Control are swapped.",
                 "swap-fn-ctrl set to off": "Fn and left Control are back to the Mac layout."}
        self.keys_message.setText(texts.get(message, message) if ok else message)

    def refresh(self, s):
        keys = s["keys"]
        install = "Install the latest MacBook Control (sudo bash install.sh) to change the keys."
        if not keys:
            self.keys_box.setEnabled(False)
            self.keys_message.setText("The Apple keyboard driver (hid_apple) isn't loaded.")
        elif not self.priv.busy():
            (self.fn_function if keys.get("fnmode") == 2 else self.fn_media).setChecked(True)
            self.swap_opt.setChecked(keys.get("swap_opt_cmd", 0) != 0)
            self.swap_fn.setChecked(keys.get("swap_fn_leftctrl", 0) != 0)
            helper_ok = os.path.exists(SETTINGS_HELPER) and shutil.which("pkexec") is not None
            self.keys_box.setEnabled(helper_ok)
            if not helper_ok:
                self.keys_message.setText(install)
            elif self.keys_message.text() == install:
                self.keys_message.setText("Changes take effect immediately and are kept after a restart.")

        kb = s["keyboard"]
        for w in (self.slider, *self.preset_buttons):
            w.setEnabled(kb is not None)
        if kb is None:
            self.value.setText("No keyboard backlight found")
        else:
            self.max = kb[1]
            if self.hold_until > 0:
                self.hold_until -= 1
            elif not self.slider.isSliderDown():
                self.slider.blockSignals(True)
                self.slider.setValue(round(100 * kb[0] / kb[1]))
                self.slider.blockSignals(False)
                self._show(self.slider.value())
        self._refresh_auto(s)

    # Automatic keyboard light: settings live in a user file the helper re-reads, so no root is needed

    def _auto_edited(self):
        self.dark_label.setText(f"{self.dark_level.value()} %")
        for w in (self.below, self.dark_level):
            w.setEnabled(self.auto_on.isChecked())
        if self.auto_loaded:
            self.auto_save.start()

    def _save_auto(self):
        cfg = {"enabled": self.auto_on.isChecked(), "below_lux": self.below.value(), "level": self.dark_level.value()}
        path = sensors.KBDLIGHT_CONFIG
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump(cfg, f)
            f.write("\n")
        os.replace(path + ".tmp", path)

    def _refresh_auto(self, s):
        auto, light = s["kbdlight"], s["light"]
        lux = light.get("Ambient (lux)")
        self.room_now.setText(f"the room is {lux} lux now" if lux is not None else "")
        self.sensor_line.setText("Light sensors: " + ", ".join(f"{k} {v}" for k, v in light.items()) if light else "")
        if not self.auto_save.isActive() and not self.below.hasFocus() and not self.dark_level.isSliderDown():
            cfg = auto["config"]
            self.auto_loaded = False      # moving the controls here isn't an edit
            self.auto_on.setChecked(bool(cfg["enabled"]))
            self.below.setValue(int(cfg["below_lux"]))
            self.dark_level.setValue(int(cfg["level"]))
            self._auto_edited()
            self.auto_loaded = True
        st = auto["status"]
        state = st.get("state") if st else None
        if state is None:
            text = ("The automatic helper isn't running. Install the latest MacBook Control "
                    "(sudo bash install.sh), then run: systemctl --user start macbook-kbdlight")
        elif state == "no-hardware":
            text = "No light sensor or keyboard light found."
        elif state == "disabled":
            text = "Off: the light only changes when you change it."
        elif state == "paused":
            minutes = max(1, round(int(st.get("seconds_left", 0)) / 60))
            text = f"Paused for {minutes} more min because you changed the light yourself."
        else:
            pct = int(st.get("percent", 0))
            text = f"Room {st.get('lux')} lux → keyboard light {'off' if pct == 0 else f'{pct} %'}. " \
                   "Changing it with F5 / F6 pauses this for 10 minutes."
        self.auto_status.setText(text)
