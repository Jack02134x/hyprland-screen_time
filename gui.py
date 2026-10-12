#!/usr/bin/env python3
"""Screen-time dashboard for the Hyprland tracker (PySide6)."""

import json
import os
import signal
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QGuiApplication, QKeySequence, QPainter,
                           QPainterPath, QPen, QShortcut)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow,
                               QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

import common

# Categorical slots validated (CVD + contrast) against a dark #101418 surface.
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9"]
OTHER = "Other"


# ------------------------------------------------------------------ theme


def mix(a, b, t):
    a, b = QColor(a), QColor(b)
    return QColor(round(a.red() + (b.red() - a.red()) * t),
                  round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t)).name()


class Theme:
    def __init__(self):
        bg, fg, accent = "#101418", "#e1e2e8", "#9fcafc"
        try:
            wal = json.loads((Path.home() / ".cache/wal/colors.json").read_text())["colors"]
            bg, fg, accent = wal["color0"], wal["color7"], wal["color2"]
        except (OSError, KeyError, ValueError):
            pass
        if QColor(bg).lightnessF() > 0.5:  # keep the validated dark palette readable
            bg, fg = "#101418", "#e1e2e8"
        self.bg = bg
        self.fg = fg
        self.accent = accent
        self.surface = mix(bg, fg, 0.045)
        self.surface2 = mix(bg, fg, 0.09)
        self.border = mix(bg, fg, 0.12)
        self.grid = mix(bg, fg, 0.08)
        self.baseline = mix(bg, fg, 0.2)
        self.secondary = mix(fg, bg, 0.25)
        self.muted = mix(fg, bg, 0.48)
        self.other = mix(bg, fg, 0.38)
        self.accent_bg = mix(bg, accent, 0.2)


T = None  # set in main()


def qss():
    return f"""
    QWidget#root {{ background: {T.bg}; }}
    QLabel {{ color: {T.fg}; background: transparent; }}
    QLabel[role="muted"] {{ color: {T.muted}; }}
    QLabel[role="secondary"] {{ color: {T.secondary}; }}
    QFrame#card {{ background: {T.surface}; border: 1px solid {T.border}; border-radius: 14px; }}
    QFrame#seg {{ background: {T.surface}; border: 1px solid {T.border}; border-radius: 10px; }}
    QFrame#seg QPushButton {{ background: transparent; color: {T.secondary}; border: none;
        padding: 6px 18px; border-radius: 7px; font-weight: 600; }}
    QFrame#seg QPushButton:hover {{ color: {T.fg}; }}
    QFrame#seg QPushButton:checked {{ background: {T.accent_bg}; color: {T.fg}; }}
    QPushButton#nav {{ background: {T.surface}; color: {T.fg}; border: 1px solid {T.border};
        border-radius: 8px; min-width: 30px; min-height: 30px; font-size: 15px; }}
    QPushButton#nav:hover {{ background: {T.surface2}; }}
    QPushButton#nav:disabled {{ color: {T.border}; }}
    QPushButton#pill {{ background: {T.surface}; color: {T.secondary}; border: 1px solid {T.border};
        border-radius: 14px; padding: 5px 14px; text-align: left; }}
    QPushButton#pill:hover {{ background: {T.surface2}; }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {T.border}; border-radius: 3px; min-height: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
        background: none; height: 0; }}
    """


def font(size, weight=QFont.Weight.Normal):
    f = QFont(QApplication.font())
    f.setPixelSize(size)
    f.setWeight(weight)
    return f


def label(text="", size=13, weight=QFont.Weight.Normal, role=None):
    w = QLabel(text)
    w.setFont(font(size, weight))
    if role:
        w.setProperty("role", role)
    return w


class ElidedLabel(QLabel):
    """QLabel that elides instead of growing the layout."""

    def __init__(self, text="", size=13, weight=QFont.Weight.Normal, role=None):
        super().__init__(text)
        self.setFont(font(size, weight))
        if role:
            self.setProperty("role", role)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(10)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        p.setFont(self.font())
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.width()))


def card():
    f = QFrame()
    f.setObjectName("card")
    return f


def top_rounded(rect, r):
    r = min(r, rect.height(), rect.width() / 2)
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    p = QPainterPath()
    p.moveTo(x, y + h)
    p.lineTo(x, y + r)
    p.quadTo(x, y, x + r, y)
    p.lineTo(x + w - r, y)
    p.quadTo(x + w, y, x + w, y + r)
    p.lineTo(x + w, y + h)
    p.closeSubpath()
    return p


def nice_step(max_secs):
    for step in (60, 300, 600, 900, 1200, 1800, 3600, 7200, 10800, 14400, 21600):
        if max_secs / step <= 4:
            return step
    return 43200


# ------------------------------------------------------------------ chart


class StackedBars(QWidget):
    """Stacked columns, one per hour/day. Hover shows a breakdown, click emits the column key."""

    clicked = Signal(object)

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self.setMinimumHeight(260)
        self.columns = []      # [{"label", "title", "key", "values": {series: secs}, "emph"}]
        self.series = []       # [(name, color)] bottom -> top
        self.average = None
        self.hover = -1
        self.clickable = False
        self.plot = QRectF()

    def set_data(self, columns, series, average=None, clickable=False):
        self.columns, self.series, self.average, self.clickable = columns, series, average, clickable
        self.hover = -1
        self.update()

    def _slot(self):
        return self.plot.width() / max(1, len(self.columns))

    def _legend_height(self, paint=None):
        x, y, row_h = 0.0, 0.0, 20
        fm = QFontMetrics(font(12))
        for name, color in self.series:
            w = 14 + fm.horizontalAdvance(name) + 18
            if x + w > self.width() and x > 0:
                x, y = 0.0, y + row_h
            if paint:
                paint.setPen(Qt.PenStyle.NoPen)
                paint.setBrush(QColor(color))
                paint.drawRoundedRect(QRectF(x, y + 5, 10, 10), 3, 3)
                paint.setPen(QColor(T.secondary))
                paint.drawText(QRectF(x + 15, y, w, row_h), Qt.AlignmentFlag.AlignVCenter, name)
            x += w
        return y + row_h if self.series else 0

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(font(12))
        legend_h = self._legend_height(p)

        totals = [sum(c["values"].values()) for c in self.columns]
        peak = max(totals + [self.average or 0, 1])
        step = nice_step(peak)
        top = step * max(1, -(-peak // step))

        fm = QFontMetrics(font(11))
        y_w = fm.horizontalAdvance("12h 30m") + 8
        self.plot = QRectF(y_w, legend_h + 18, self.width() - y_w - 4, self.height() - legend_h - 18 - 40)
        pl = self.plot

        if not any(totals):
            p.setPen(QColor(T.muted))
            p.setFont(font(13))
            p.drawText(pl, Qt.AlignmentFlag.AlignCenter, "No activity recorded for this period")
            return

        def y_of(v):
            return pl.bottom() - v / top * pl.height()

        # gridlines + y ticks
        p.setFont(font(11))
        v = 0
        while v <= top:
            y = y_of(v)
            p.setPen(QPen(QColor(T.baseline if v == 0 else T.grid), 1))
            p.drawLine(QPointF(pl.left(), y), QPointF(pl.right(), y))
            p.setPen(QColor(T.muted))
            p.drawText(QRectF(0, y - 8, y_w - 10, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       common.fmt_duration(v, short=True) if v else "0")
            v += step

        slot = self._slot()
        bar_w = min(24.0, slot * 0.62)
        label_every = max(1, -(-int(fm.horizontalAdvance("Wed 30") + 10) // max(1, int(slot))))
        if len(self.columns) == 24:
            label_every = max(label_every, 3)

        for i, col in enumerate(self.columns):
            cx = pl.left() + slot * (i + 0.5)
            if i == self.hover:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(T.surface2))
                p.drawRoundedRect(QRectF(cx - slot / 2 + 1, pl.top(), slot - 2, pl.height()), 6, 6)

            stacks = [(name, color, col["values"].get(name, 0)) for name, color in self.series]
            stacks = [s for s in stacks if s[2] > 0]
            base = 0.0
            for j, (name, color, val) in enumerate(stacks):
                y0, y1 = y_of(base), y_of(base + val)
                base += val
                last = j == len(stacks) - 1
                gap = 0 if last else 2  # surface gap between stacked segments
                h = y0 - y1 - gap
                if h <= 0.4:
                    continue
                rect = QRectF(cx - bar_w / 2, y1 + gap, bar_w, h)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(color))
                if last:
                    p.drawPath(top_rounded(rect, 4))
                else:
                    p.drawRect(rect)

            if i % label_every == 0 or col.get("emph"):
                p.setFont(font(11, QFont.Weight.DemiBold if col.get("emph") else QFont.Weight.Normal))
                p.setPen(QColor(T.fg if col.get("emph") else T.muted))
                p.drawText(QRectF(cx - 40, pl.bottom() + 6, 80, 18), Qt.AlignmentFlag.AlignCenter, col["label"])

        if self.average:
            y = y_of(self.average)
            p.setPen(QPen(QColor(T.accent), 1))
            p.drawLine(QPointF(pl.left(), y), QPointF(pl.right(), y))
            txt = f"avg {common.fmt_duration(self.average)}"
            p.setFont(font(11, QFont.Weight.DemiBold))
            tw = QFontMetrics(p.font()).horizontalAdvance(txt) + 12
            box = QRectF(pl.right() - tw, y - 20, tw, 17)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(T.surface))
            p.drawRoundedRect(box, 4, 4)
            p.setPen(QColor(T.fg))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, txt)

        if 0 <= self.hover < len(self.columns):
            self._tooltip(p, self.hover)

    def _tooltip(self, p, i):
        col = self.columns[i]
        total = sum(col["values"].values())
        rows = [(n, c, col["values"].get(n, 0)) for n, c in self.series]
        rows = sorted([r for r in rows if r[2] > 0], key=lambda r: -r[2])
        f_title, f_row = font(12, QFont.Weight.DemiBold), font(12)
        fm, fmt = QFontMetrics(f_row), QFontMetrics(f_title)
        w = max([fmt.horizontalAdvance(col["title"]) + 90] +
                [fm.horizontalAdvance(n) + fm.horizontalAdvance(common.fmt_duration(v)) + 50 for n, _, v in rows])
        w = min(max(w, 190), 320)
        h = 34 + 20 * len(rows) + (22 if self.clickable else 6)

        cx = self.plot.left() + self._slot() * (i + 0.5)
        x = cx + self._slot() / 2 + 8
        if x + w > self.width():
            x = cx - self._slot() / 2 - 8 - w
        y = max(0, min(self.plot.top() + 10, self.height() - h))
        box = QRectF(max(0, x), y, w, h)

        p.setPen(QPen(QColor(T.border), 1))
        p.setBrush(QColor(T.surface2))
        p.drawRoundedRect(box, 8, 8)
        inner = box.adjusted(12, 8, -12, 0)
        p.setFont(f_title)
        p.setPen(QColor(T.fg))
        p.drawText(QRectF(inner.left(), inner.top(), inner.width(), 20), Qt.AlignmentFlag.AlignVCenter, col["title"])
        p.drawText(QRectF(inner.left(), inner.top(), inner.width(), 20),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, common.fmt_duration(total))
        p.setFont(f_row)
        yy = inner.top() + 26
        for name, color, val in rows:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(color))
            p.drawRoundedRect(QRectF(inner.left(), yy + 5, 9, 9), 2.5, 2.5)
            p.setPen(QColor(T.secondary))
            p.drawText(QRectF(inner.left() + 16, yy, inner.width() - 16, 20), Qt.AlignmentFlag.AlignVCenter,
                       fm.elidedText(name, Qt.TextElideMode.ElideRight, int(inner.width() - 80)))
            p.setPen(QColor(T.fg))
            p.drawText(QRectF(inner.left(), yy, inner.width(), 20),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, common.fmt_duration(val))
            yy += 20
        if self.clickable:
            p.setPen(QColor(T.muted))
            p.setFont(font(11))
            p.drawText(QRectF(inner.left(), yy + 2, inner.width(), 16), Qt.AlignmentFlag.AlignVCenter,
                       "Click to open this day")

    def _index_at(self, pos):
        if not self.plot.contains(QPointF(pos.x(), self.plot.center().y())) or not (0 <= pos.y() <= self.height()):
            return -1
        i = int((pos.x() - self.plot.left()) // self._slot())
        return i if 0 <= i < len(self.columns) else -1

    def mouseMoveEvent(self, e):
        i = self._index_at(e.position())
        if i != self.hover:
            self.hover = i
            self.setCursor(Qt.CursorShape.PointingHandCursor if self.clickable and i >= 0 else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, _):
        self.hover = -1
        self.update()

    def mousePressEvent(self, e):
        i = self._index_at(e.position())
        if self.clickable and i >= 0 and sum(self.columns[i]["values"].values()) > 0:
            self.clicked.emit(self.columns[i]["key"])


# ------------------------------------------------------------------ ranked lists


class Row(QWidget):
    """One ranked line: [dot|chevron] name  sub ..... time, with a proportional bar below."""

    toggled = Signal()

    def __init__(self, name, seconds, frac, color, sub="", indent=0, expandable=False, compact=False):
        super().__init__()
        self.name, self.seconds, self.frac, self.color, self.sub = name, seconds, frac, color, sub
        self.indent, self.expandable, self.expanded, self.compact = indent, expandable, False, compact
        self._hover = False
        self.setFixedHeight(38 if compact else 46)
        if expandable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, _):
        self._hover = True
        self.update()

    def leaveEvent(self, _):
        self._hover = False
        self.update()

    def mousePressEvent(self, _):
        if self.expandable:
            self.expanded = not self.expanded
            self.toggled.emit()
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(4, 1, -4, -1)
        if self._hover and self.expandable:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(T.surface2))
            p.drawRoundedRect(r, 8, 8)

        x = r.left() + 10 + self.indent
        text_h = 22
        ty = r.top() + (5 if not self.compact else 3)
        if not self.compact:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(self.color))
            p.drawRoundedRect(QRectF(x, ty + 6, 10, 10), 3, 3)
            x += 18

        time_txt = common.fmt_duration(self.seconds)
        f_name = font(13 if not self.compact else 12, QFont.Weight.Medium if not self.compact else QFont.Weight.Normal)
        f_time = font(12)
        right = r.right() - 10
        if self.expandable:
            p.setPen(QColor(T.muted))
            p.setFont(font(11))
            p.drawText(QRectF(right - 12, ty, 12, text_h), Qt.AlignmentFlag.AlignCenter, "▾" if self.expanded else "▸")
            right -= 20
        tw = QFontMetrics(f_time).horizontalAdvance(time_txt)
        p.setFont(f_time)
        p.setPen(QColor(T.fg if not self.compact else T.secondary))
        p.drawText(QRectF(right - tw, ty, tw, text_h), Qt.AlignmentFlag.AlignVCenter, time_txt)

        avail = right - tw - 12 - x
        p.setFont(f_name)
        fm = QFontMetrics(f_name)
        name = fm.elidedText(self.name, Qt.TextElideMode.ElideRight, int(avail * (0.7 if self.sub else 1)))
        p.setPen(QColor(T.fg if not self.compact else T.secondary))
        p.drawText(QRectF(x, ty, avail, text_h), Qt.AlignmentFlag.AlignVCenter, name)
        if self.sub:
            nx = x + fm.horizontalAdvance(name) + 8
            p.setFont(font(12))
            p.setPen(QColor(T.muted))
            p.drawText(QRectF(nx, ty, max(0, x + avail - nx), text_h), Qt.AlignmentFlag.AlignVCenter,
                       QFontMetrics(font(12)).elidedText(self.sub, Qt.TextElideMode.ElideRight, int(max(0, x + avail - nx))))

        bar_y = ty + text_h + 3
        bar = QRectF(x, bar_y, right - x, 4 if not self.compact else 3)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(T.grid))
        p.drawRoundedRect(bar, 2, 2)
        fill = QColor(self.color)
        if self.compact:
            fill.setAlphaF(0.7)
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(bar.left(), bar.top(), max(bar.height(), bar.width() * self.frac), bar.height()), 2, 2)


class RankList(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.box = QVBoxLayout(self.body)
        self.box.setContentsMargins(0, 0, 4, 0)
        self.box.setSpacing(0)
        self.setWidget(self.body)
        self.expanded = set()

    def clear(self):
        while self.box.count():
            item = self.box.takeAt(0)
            if w := item.widget():
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def empty(self, text):
        self.clear()
        w = label(text, 13, role="muted")
        w.setAlignment(Qt.AlignmentFlag.AlignCenter)
        w.setMinimumHeight(120)
        self.box.addWidget(w)
        self.box.addStretch()

    def set_apps(self, apps, color_of):
        """apps: [(app, seconds, [(detail, seconds), ...])] sorted desc."""
        if not apps:
            return self.empty("Nothing tracked yet")
        self.clear()
        top = apps[0][1] or 1
        for app, secs, details in apps:
            details = [d for d in details if d[0]]
            row = Row(app, secs, secs / top, color_of(app), expandable=bool(details))
            self.box.addWidget(row)
            kids = []
            for name, dsecs in details:
                kid = Row(name, dsecs, dsecs / (secs or 1), color_of(app), indent=28, compact=True)
                kid.setVisible(app in self.expanded)
                kids.append(kid)
                self.box.addWidget(kid)
            row.expanded = app in self.expanded

            def toggle(app=app, row=row, kids=kids):
                (self.expanded.add if row.expanded else self.expanded.discard)(app)
                for k in kids:
                    k.setVisible(row.expanded)
            row.toggled.connect(toggle)
        self.box.addStretch()

    def set_activities(self, items, color_of):
        """items: [(app, detail, seconds)] sorted desc."""
        if not items:
            return self.empty("No sites or processes recorded")
        self.clear()
        top = items[0][2] or 1
        for app, detail, secs in items:
            self.box.addWidget(Row(detail, secs, secs / top, color_of(app), sub=app))
        self.box.addStretch()


# ------------------------------------------------------------------ main window


class Tile(QFrame):
    def __init__(self, title, hero=False):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(2)
        self.title = label(title, 12, QFont.Weight.Medium, role="muted")
        self.value = ElidedLabel("—", 34 if hero else 20, QFont.Weight.DemiBold)
        self.sub = ElidedLabel("", 12, role="secondary")
        lay.addWidget(self.title)
        lay.addStretch()
        lay.addWidget(self.value)
        lay.addWidget(self.sub)

    def set(self, value, sub=""):
        self.value.setText(value)
        self.sub.setText(sub)
        self.value.update()
        self.sub.update()


class Window(QMainWindow):
    MODES = {"day": 1, "week": 7, "month": 30}

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Screen Time")
        self.resize(1180, 860)
        self.cfg = common.load_config()
        self.retention = int(self.cfg["retention_days"])
        self.data = common.empty_data()
        self.mtime = None
        self.mode = "day"
        self.anchor = date.today()
        self.status_pid = None

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(14)

        # header
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(label("Screen Time", 24, QFont.Weight.Bold))
        self.range_label = label("", 13, role="secondary")
        titles.addWidget(self.range_label)
        head.addLayout(titles)
        head.addSpacing(16)
        self.prev_btn, self.next_btn = QPushButton("‹"), QPushButton("›")
        for b, d in ((self.prev_btn, -1), (self.next_btn, 1)):
            b.setObjectName("nav")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, d=d: self.shift(d))
            head.addWidget(b)
        self.today_btn = QPushButton("Today")
        self.today_btn.setObjectName("nav")
        self.today_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.today_btn.clicked.connect(self.go_today)
        head.addWidget(self.today_btn)
        head.addStretch()
        self.pill = QPushButton()
        self.pill.setObjectName("pill")
        self.pill.clicked.connect(self.start_tracker)
        head.addWidget(self.pill)
        head.addSpacing(10)
        seg = QFrame()
        seg.setObjectName("seg")
        seg_l = QHBoxLayout(seg)
        seg_l.setContentsMargins(3, 3, 3, 3)
        seg_l.setSpacing(2)
        self.seg_group = QButtonGroup(self)
        for key, text in (("day", "Day"), ("week", "7 Days"), ("month", "30 Days")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setChecked(key == self.mode)
            b.clicked.connect(lambda _=False, k=key: self.set_mode(k))
            self.seg_group.addButton(b)
            seg_l.addWidget(b)
            setattr(self, f"seg_{key}", b)
        head.addWidget(seg)
        outer.addLayout(head)

        # tiles
        tiles = QHBoxLayout()
        tiles.setSpacing(14)
        self.t_total = Tile("Total screen time", hero=True)
        self.t_second = Tile("Daily average")
        self.t_app = Tile("Most used app")
        self.t_act = Tile("Top activity")
        for t, s in ((self.t_total, 5), (self.t_second, 4), (self.t_app, 4), (self.t_act, 4)):
            t.setFixedHeight(118)
            tiles.addWidget(t, s)
        outer.addLayout(tiles)

        # chart
        chart_card = card()
        cl = QVBoxLayout(chart_card)
        cl.setContentsMargins(20, 16, 20, 10)
        cl.setSpacing(10)
        self.chart_title = label("", 15, QFont.Weight.DemiBold)
        cl.addWidget(self.chart_title)
        self.chart = StackedBars()
        self.chart.clicked.connect(self.open_day)
        cl.addWidget(self.chart)
        outer.addWidget(chart_card, 5)

        # lists
        bottom = QHBoxLayout()
        bottom.setSpacing(14)
        self.apps = RankList()
        self.acts = RankList()
        for title, sub, lst, stretch in (("Apps", "click an app for its sites / processes", self.apps, 1),
                                         ("Top activities", "sites, programs and projects", self.acts, 1)):
            c = card()
            lay = QVBoxLayout(c)
            lay.setContentsMargins(14, 14, 10, 10)
            lay.setSpacing(6)
            hl = QHBoxLayout()
            hl.setContentsMargins(6, 0, 0, 0)
            hl.addWidget(label(title, 15, QFont.Weight.DemiBold))
            hl.addWidget(label(sub, 12, role="muted"))
            hl.addStretch()
            lay.addLayout(hl)
            lay.addWidget(lst)
            bottom.addWidget(c, stretch)
        outer.addLayout(bottom, 5)

        for key, mode in (("1", "day"), ("2", "week"), ("3", "month")):
            QShortcut(QKeySequence(key), self, activated=lambda m=mode: self.set_mode(m))
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, activated=lambda: self.shift(-1))
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, activated=lambda: self.shift(1))
        QShortcut(QKeySequence("q"), self, activated=self.close)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.close)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(5000)
        self.update_status()
        self.poke_tracker()
        QTimer.singleShot(400, self.tick)
        self.reload(force=True)

    # ---------------------------------------------------------------- data

    def poke_tracker(self):
        """Ask the daemon to flush so we see up-to-the-second numbers."""
        status = self.read_status()
        if status and status.get("pid"):
            try:
                os.kill(status["pid"], signal.SIGUSR1)
            except OSError:
                pass

    def read_status(self):
        try:
            st = json.loads(common.STATUS_FILE.read_text())
            os.kill(st["pid"], 0)
            if datetime.now().timestamp() - st.get("ts", 0) > 20:
                return None
            return st
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def tick(self):
        self.update_status()
        self.reload()
        self.poke_tracker()

    def reload(self, force=False):
        try:
            mtime = common.DATA_FILE.stat().st_mtime
        except OSError:
            mtime = None
        if not force and mtime == self.mtime and self.anchor_ok():
            return
        self.mtime = mtime
        self.data = common.load_data()
        if self.anchor > date.today() or not self.anchor_ok():
            self.anchor = date.today()
        self.render()

    def anchor_ok(self):
        return self.oldest() <= self.anchor <= date.today()

    def oldest(self):
        return date.today() - timedelta(days=self.retention - 1)

    def update_status(self):
        st = self.read_status()
        self.status_pid = st and st.get("pid")
        if not st:
            self.pill.setText("○  Tracker not running — click to start")
            self.pill.setCursor(Qt.CursorShape.PointingHandCursor)
            self.pill.setEnabled(True)
            return
        self.pill.setCursor(Qt.CursorShape.ArrowCursor)
        state = st.get("state")
        if state == "active":
            what = st.get("app") or ""
            if st.get("detail"):
                what += f"  ›  {st['detail']}"
            text = f"●  {what}"
        else:
            text = f"❙❙  Paused — {state}"
        if st.get("audio"):
            text += "   ♪  " + ", ".join(st["audio"])
        self.pill.setText(text if len(text) <= 70 else text[:69] + "…")

    def start_tracker(self):
        if self.read_status():
            return
        subprocess.Popen([sys.executable, str(common.SCRIPT_DIR / "tracker.py")], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.pill.setText("…  starting tracker")
        QTimer.singleShot(2000, self.tick)

    # ---------------------------------------------------------------- navigation

    def set_mode(self, mode):
        self.mode = mode
        getattr(self, f"seg_{mode}").setChecked(True)
        if mode != "day":
            self.anchor = date.today()
        self.render()

    def shift(self, direction):
        step = {"day": 1, "week": 7, "month": 0}[self.mode]
        new = self.anchor + timedelta(days=direction * step)
        if step and self.oldest() <= new <= date.today():
            self.anchor = new
            self.render()

    def go_today(self):
        self.anchor = date.today()
        self.render()

    def open_day(self, key):
        self.mode = "day"
        self.seg_day.setChecked(True)
        self.anchor = date.fromisoformat(key)
        self.render()

    # ---------------------------------------------------------------- render

    def days(self, end=None, span=None):
        end = end or self.anchor
        span = span or self.MODES[self.mode]
        return [(end - timedelta(days=i)).isoformat() for i in range(span - 1, -1, -1)]

    def color_map(self):
        """Stable colors: rank apps over all stored history, so a filter never repaints an app."""
        ranked = sorted(common.app_totals(self.data, self.data["days"].keys()).items(), key=lambda kv: -kv[1])
        return {app: SERIES[i] for i, (app, _) in enumerate(ranked[:len(SERIES)])}

    def render(self):
        today = date.today()
        cmap = self.color_map()

        def color_of(app):
            return cmap.get(app, T.other)

        def series_of(app):
            return app if app in cmap else OTHER

        days = self.days()
        span = len(days)
        totals = common.app_totals(self.data, days)
        grand = sum(totals.values())

        # header
        if self.mode == "day":
            names = {today: "Today · ", today - timedelta(days=1): "Yesterday · "}
            self.range_label.setText(names.get(self.anchor, "") + self.anchor.strftime("%A, %-d %B %Y"))
        else:
            a, b = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
            self.range_label.setText(f"{a.strftime('%-d %b')} – {b.strftime('%-d %b %Y')}")
        step = {"day": 1, "week": 7, "month": 0}[self.mode]
        self.prev_btn.setEnabled(bool(step) and self.anchor - timedelta(days=step) >= self.oldest())
        self.next_btn.setEnabled(bool(step) and self.anchor + timedelta(days=step) <= today)
        self.today_btn.setVisible(self.anchor != today)

        # chart
        series_used = set()
        columns = []
        if self.mode == "day":
            for h, slot in enumerate(common.hourly(self.data, days[0])):
                vals = {}
                for app, s in slot.items():
                    vals[series_of(app)] = vals.get(series_of(app), 0) + s
                series_used |= set(vals)
                columns.append({"label": f"{h:02d}", "title": f"{h:02d}:00 – {(h + 1) % 24:02d}:00", "key": h,
                                "values": vals, "emph": self.anchor == today and h == datetime.now().hour})
            self.chart_title.setText("Usage by hour")
            average = None
        else:
            for d in days:
                vals = {}
                for app, s in common.app_totals(self.data, [d]).items():
                    vals[series_of(app)] = vals.get(series_of(app), 0) + s
                series_used |= set(vals)
                dd = date.fromisoformat(d)
                columns.append({"label": dd.strftime("%a %-d") if span <= 7 else dd.strftime("%-d"),
                                "title": dd.strftime("%A, %-d %b"), "key": d, "values": vals, "emph": dd == today})
            self.chart_title.setText("Usage by day")
            active = [sum(c["values"].values()) for c in columns if c["values"]]
            average = sum(active) / len(active) if active else None
        series = [(app, color) for app, color in sorted(cmap.items(), key=lambda kv: SERIES.index(kv[1]))
                  if app in series_used]
        if OTHER in series_used:
            series.append((OTHER, T.other))
        self.chart.set_data(columns, series, average, clickable=self.mode != "day")

        # tiles
        sub = [t for t in (self.delta_text(grand, days),) if t]
        if audio := common.audio_total(self.data, days):
            sub.append(f"incl. {common.fmt_duration(audio)} background audio")
        self.t_total.set(common.fmt_duration(grand) if grand else "0m", " · ".join(sub))
        if self.mode == "day":
            self.t_second.title.setText("Peak hour")
            hours = [sum(c["values"].values()) for c in columns]
            if any(hours):
                h = max(range(24), key=lambda i: hours[i])
                self.t_second.set(f"{h:02d}:00 – {(h + 1) % 24:02d}:00", f"{common.fmt_duration(hours[h])} on screen")
            else:
                self.t_second.set("—", "")
        else:
            self.t_second.title.setText("Daily average")
            active_days = sum(1 for c in columns if c["values"])
            self.t_second.set(common.fmt_duration(average) if average else "—",
                              f"over {active_days} tracked day{'s' * (active_days != 1)}" if active_days else "")
        ranked = sorted(totals.items(), key=lambda kv: -kv[1])
        if ranked:
            app, s = ranked[0]
            self.t_app.set(app, f"{common.fmt_duration(s)} · {s / grand:.0%} of total")
        else:
            self.t_app.set("—", "")
        acts = sorted(common.detail_totals(self.data, days).items(), key=lambda kv: -kv[1])
        if acts:
            (app, detail), s = acts[0]
            self.t_act.set(detail, f"{app} · {common.fmt_duration(s)}")
        else:
            self.t_act.set("—", "")

        # lists
        per_app = {}
        for (app, detail), s in common.detail_totals(self.data, days).items():
            per_app.setdefault(app, []).append((detail, s))
        self.apps.set_apps([(app, s, sorted(per_app.get(app, []), key=lambda d: -d[1])) for app, s in ranked],
                           color_of)
        self.acts.set_activities([(app, detail, s) for (app, detail), s in acts[:40]], color_of)

    def delta_text(self, grand, days):
        if self.mode == "month":
            return "last 30 days"
        span = len(days)
        prev_end = date.fromisoformat(days[0]) - timedelta(days=1)
        if prev_end < self.oldest():
            return ""
        prev = sum(common.app_totals(self.data, self.days(prev_end, span)).values())
        if not prev:
            return ""
        what = "yesterday" if self.mode == "day" and self.anchor == date.today() else \
            "previous day" if self.mode == "day" else "previous 7 days"
        diff = grand - prev
        if abs(diff) < 60:
            return f"about the same as {what}"
        arrow = "▲" if diff > 0 else "▼"
        return f"{arrow} {common.fmt_duration(abs(diff))} vs {what}"


def main():
    global T
    QGuiApplication.setDesktopFileName(common.APP_ID)  # Wayland app_id / Hyprland class
    app = QApplication(sys.argv)
    app.setApplicationName("Screen Time")
    T = Theme()
    app.setStyleSheet(qss())
    w = Window()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
