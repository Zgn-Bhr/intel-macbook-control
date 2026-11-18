#!/usr/bin/python3 -I
"""MacBook Control: sensors, fan, battery, display and keyboard for this MacBook Pro.

  macbook-control                    open the window
  macbook-control --screenshot DIR [SECONDS]   render every page to DIR/*.png after SECONDS (default 6.5) and quit
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QSize, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QListWidget, QListWidgetItem, QMainWindow, \
    QScrollArea, QStackedWidget, QWidget

import sensors
from pages import BatteryPage, DisplayPage, FanPage, KeyboardPage, SensorsPage, SummaryPage

PAGES = [("Summary", "dashboard-show", SummaryPage), ("Sensors", "temperature-normal", SensorsPage),
         ("Fan", "speedometer", FanPage), ("Battery", "battery-good", BatteryPage),
         ("Display", "video-display", DisplayPage), ("Keyboard", "input-keyboard", KeyboardPage)]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MacBook Control")
        self.setWindowIcon(QIcon.fromTheme("computer-laptop"))
        self.resize(1000, 680)

        self.nav = QListWidget()
        self.nav.setIconSize(QSize(22, 22))
        self.nav.setFixedWidth(160)
        self.nav.setFrameShape(QFrame.NoFrame)
        self.stack = QStackedWidget()
        self.pages = []
        for name, icon, cls in PAGES:
            QListWidgetItem(QIcon.fromTheme(icon), name, self.nav)
            page = cls()
            scroll = QScrollArea()
            scroll.setWidget(page)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            self.stack.addWidget(scroll)
            self.pages.append(page)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.nav)
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.cpu = sensors.CpuMeter()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        QTimer.singleShot(200, self.tick)

    def tick(self):
        snap = sensors.snapshot(self.cpu)
        for page in self.pages:
            page.refresh(snap)


def screenshot(window, folder):
    os.makedirs(folder, exist_ok=True)
    for i, (name, _, _) in enumerate(PAGES):
        window.nav.setCurrentRow(i)
        QApplication.processEvents()
        window.grab().save(os.path.join(folder, f"{i + 1}-{name.lower()}.png"))
    QApplication.quit()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("macbook-control")
    app.setApplicationDisplayName("MacBook Control")
    app.setDesktopFileName("macbook-control")
    window = MainWindow()
    window.show()
    if "--screenshot" in sys.argv:
        i = sys.argv.index("--screenshot")
        folder = sys.argv[i + 1]
        wait = float(sys.argv[i + 2]) if len(sys.argv) > i + 2 and sys.argv[i + 2].replace(".", "").isdigit() else 6.5
        QTimer.singleShot(int(wait * 1000), lambda: screenshot(window, folder))   # let the charts collect data
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
