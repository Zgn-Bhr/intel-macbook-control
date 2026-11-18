"""Small display widgets: summary cards, live line charts and the fan-curve preview.

Colours come from the active Qt palette, so light and dark KDE themes both work:
the data line uses the accent (Highlight) colour, text keeps the normal text colours.
"""
from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QFrame, QLabel, QProgressBar, QSizePolicy, QToolTip, QVBoxLayout, QWidget


def muted(widget):
    widget.setForegroundRole(QPalette.PlaceholderText)
    return widget


def grid_color(palette):
    c = QColor(palette.color(QPalette.Text))
    c.setAlphaF(0.12)
    return c


def card_fill(palette):
    """A surface just off the window colour: lighter in dark themes, darker in light ones."""
    window = palette.color(QPalette.Window)
    return window.lighter(112) if window.lightness() < 128 else window.darker(104)


class StatCard(QFrame):
    """A titled tile with one headline value, a detail line and optional bar/chart underneath."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(2)
        self.title = muted(QLabel(title))
        self.value = QLabel("–")
        font = QFont(self.value.font())
        font.setPointSizeF(font.pointSizeF() * 1.9)
        font.setWeight(QFont.DemiBold)
        self.value.setFont(font)
        self.detail = muted(QLabel(""))
        self.detail.setWordWrap(True)
        for w in (self.title, self.value, self.detail):
            lay.addWidget(w)
        self.bar = None
        self.layout_ = lay

    def add_bar(self):
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.setRange(0, 1000)
        self.layout_.insertWidget(2, self.bar)
        return self.bar

    def add_widget(self, widget):
        self.layout_.addSpacing(4)
        self.layout_.addWidget(widget)
        return widget

    def set(self, value, detail="", fraction=None):
        self.value.setText(value)
        self.detail.setText(detail)
        if self.bar is not None:
            self.bar.setValue(int(1000 * min(max(fraction or 0, 0), 1)))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(grid_color(self.palette()), 1))
        p.setBrush(card_fill(self.palette()))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        p.end()


class LiveChart(QWidget):
    """One measure over the last few minutes: 2px line, faint gridlines, crosshair and tooltip on hover."""

    def __init__(self, unit, decimals=0, y_min=None, y_max=None, seconds=180, parent=None):
        super().__init__(parent)
        self.values = deque(maxlen=seconds)
        self.unit, self.decimals = unit, decimals
        self.y_min, self.y_max = y_min, y_max
        self.hover_index = None
        self.setMouseTracking(True)
        self.setMinimumHeight(84)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def add(self, value):
        self.values.append(value)
        self.update()

    def fmt(self, v, decimals=None):
        return f"{v:.{self.decimals if decimals is None else decimals}f}"

    def _range(self):
        vals = [v for v in self.values if v is not None]
        lo = self.y_min if self.y_min is not None else min(vals, default=0)
        hi = self.y_max if self.y_max is not None else max(vals, default=1)
        if vals:   # grow a fixed range if the data leaves it
            lo, hi = min(lo, min(vals)), max(hi, max(vals))
        if hi - lo < 1e-9:
            hi = lo + 1
        return lo, hi

    def _plot_rect(self):
        return QRectF(36, 6, self.width() - 36 - 10, self.height() - 6 - 22)

    def _x(self, i, area):
        step = area.width() / max(self.values.maxlen - 1, 1)
        return area.right() - (len(self.values) - 1 - i) * step

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        area = self._plot_rect()
        lo, hi = self._range()
        y = lambda v: area.bottom() - (v - lo) / (hi - lo) * area.height()

        small = QFont(self.font())
        small.setPointSizeF(small.pointSizeF() * 0.8)
        p.setFont(small)
        for v in (lo, (lo + hi) / 2, hi):
            p.setPen(QPen(grid_color(pal), 1))
            p.drawLine(QPointF(area.left(), y(v)), QPointF(area.right(), y(v)))
            p.setPen(pal.color(QPalette.PlaceholderText))
            p.drawText(QRectF(0, y(v) - 8, area.left() - 6, 16), Qt.AlignRight | Qt.AlignVCenter, self.fmt(v, 0))
        span = self.values.maxlen
        ago = f"{span // 60} min ago" if span % 60 == 0 else f"{span} s ago"
        p.drawText(QRectF(area.left(), area.bottom() + 4, area.width(), 14), Qt.AlignLeft | Qt.AlignVCenter, ago)
        p.drawText(QRectF(area.left(), area.bottom() + 4, area.width(), 14), Qt.AlignRight | Qt.AlignVCenter, "now")

        points = [(self._x(i, area), y(v)) for i, v in enumerate(self.values) if v is not None]
        if len(points) >= 2:
            path = QPainterPath(QPointF(*points[0]))
            for pt in points[1:]:
                path.lineTo(QPointF(*pt))
            p.setPen(QPen(pal.color(QPalette.Highlight), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)

        index = self.hover_index if self.hover_index is not None else (len(self.values) - 1 if self.values else None)
        if index is not None and self.values[index] is not None:
            x, yy = self._x(index, area), y(self.values[index])
            if self.hover_index is not None:
                p.setPen(QPen(pal.color(QPalette.PlaceholderText), 1, Qt.DashLine))
                p.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
            # 8px marker with a 2px ring in the card colour so it stays clear of the line
            p.setPen(QPen(card_fill(pal), 2))
            p.setBrush(pal.color(QPalette.Highlight))
            p.drawEllipse(QPointF(x, yy), 5, 5)
        p.end()

    def mouseMoveEvent(self, event):
        if not self.values:
            return
        area = self._plot_rect()
        step = area.width() / max(self.values.maxlen - 1, 1)
        back = round((area.right() - event.position().x()) / step)
        index = len(self.values) - 1 - back
        if 0 <= index < len(self.values) and self.values[index] is not None:
            self.hover_index = index
            ago = len(self.values) - 1 - index
            when = "now" if ago == 0 else f"{ago} s ago"
            QToolTip.showText(event.globalPosition().toPoint(), f"{self.fmt(self.values[index])} {self.unit} · {when}", self)
        else:
            self.hover_index = None
            QToolTip.hideText()
        self.update()

    def leaveEvent(self, event):
        self.hover_index = None
        self.update()


class FanCurve(QWidget):
    """Preview of fan speed against CPU temperature for a mode, with the current reading marked."""

    T_MIN, T_MAX = 40, 100

    def __init__(self, parent=None):
        super().__init__(parent)
        self.target = None          # function temp -> rpm
        self.floor, self.top = 2000, 6200
        self.now = None             # (temp, rpm)
        self.setMinimumSize(320, 200)

    def set_curve(self, target, floor, top):
        self.target, self.floor, self.top = target, floor, top
        self.update()

    def set_now(self, temp, rpm):
        self.now = (temp, rpm) if temp is not None and rpm is not None else None
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        area = QRectF(52, 10, self.width() - 52 - 14, self.height() - 10 - 34)
        x = lambda t: area.left() + (t - self.T_MIN) / (self.T_MAX - self.T_MIN) * area.width()
        y = lambda r: area.bottom() - r / self.top * area.height()

        small = QFont(self.font())
        small.setPointSizeF(small.pointSizeF() * 0.85)
        p.setFont(small)
        for r in range(0, self.top + 1, 2000):
            p.setPen(QPen(grid_color(pal), 1))
            p.drawLine(QPointF(area.left(), y(r)), QPointF(area.right(), y(r)))
            p.setPen(pal.color(QPalette.PlaceholderText))
            p.drawText(QRectF(0, y(r) - 8, area.left() - 6, 16), Qt.AlignRight | Qt.AlignVCenter, f"{r}")
        for t in range(self.T_MIN, self.T_MAX + 1, 10):
            p.drawText(QRectF(x(t) - 20, area.bottom() + 4, 40, 14), Qt.AlignCenter, f"{t}°")
        p.drawText(QRectF(area.left(), area.bottom() + 18, area.width(), 14), Qt.AlignCenter, "CPU temperature (°C)")
        p.save()
        p.translate(10, area.center().y())
        p.rotate(-90)
        p.drawText(QRectF(-60, -8, 120, 16), Qt.AlignCenter, "rpm")
        p.restore()

        if self.target:
            path = QPainterPath()
            for i, t in enumerate(range(self.T_MIN, self.T_MAX + 1)):
                pt = QPointF(x(t), y(self.target(t)))
                path.moveTo(pt) if i == 0 else path.lineTo(pt)
            p.setPen(QPen(pal.color(QPalette.Highlight), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

        if self.now:
            t, r = self.now
            t = min(max(t, self.T_MIN), self.T_MAX)
            p.setPen(QPen(pal.color(QPalette.Window), 2))
            p.setBrush(pal.color(QPalette.Text))
            p.drawEllipse(QPointF(x(t), y(min(r, self.top))), 5, 5)
            p.setPen(pal.color(QPalette.Text))
            p.drawText(QRectF(x(t) + 8, y(min(r, self.top)) - 18, 140, 16), Qt.AlignLeft | Qt.AlignVCenter,
                       f"now: {self.now[0]:.0f}°, {r} rpm")
        p.end()
