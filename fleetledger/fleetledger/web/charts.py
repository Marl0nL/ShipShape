"""Charts drawn as SVG on the server: no script, no external library.

The page's Content-Security-Policy allows no inline styles or scripts, so a
chart is plain SVG markup whose geometry is computed here and whose colours
come from app.css (classes such as `s1`, `s2`, `s3` for series and `crit`,
`warn` for thresholds). Each function returns a plain dict the templates draw
(templates/_charts.html); nothing here renders HTML, so every label goes
through Jinja's escaping.

Hover readouts are SVG <title> elements on invisible columns (the browser's own
tooltip, no script), and every chart has a table twin for reading exact values.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

# Plot box inside the SVG viewBox (the SVG scales to its container's width).
WIDTH, HEIGHT = 360, 150
LEFT, RIGHT, TOP, BOTTOM = 34, 10, 8, 22


@dataclass(frozen=True)
class Series:
    key: str  # css class: s1, s2, s3 ...
    label: str
    values: Sequence[float | None]  # one per bucket; None: no data


def _fmt_time(at: float, span: float) -> str:
    when = datetime.fromtimestamp(at).astimezone()
    return when.strftime("%a %H:%M") if span > 2 * 86400 else when.strftime("%H:%M")


def nice_max(value: float) -> float:
    """A round axis top at or above value (1, 2, 2.5, 5 x 10^n)."""
    if value <= 0:
        return 1.0
    power = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * power:
            return step * power
    return 10 * power  # pragma: no cover


def _num(value: float, unit: str) -> str:
    if value == 0:
        return "0"
    if unit == "%":
        return f"{value:.0f}%"
    if unit == "MB":
        if value >= 1024:
            return f"{value / 1024:.1f} GB"
        return f"{value:.0f} MB" if value >= 10 else f"{value:.1f} MB"
    return f"{value:g}" if value == int(value) else f"{value:.1f}"


def _box(width: int, height: int) -> tuple[float, float, float, float]:
    return LEFT, TOP, width - LEFT - RIGHT, height - TOP - BOTTOM


def _yticks(ymax: float, unit: str, top: float, plot_h: float, n: int = 2) -> list[dict]:
    return [{"y": round(top + plot_h * (1 - i / n), 1), "label": _num(ymax * i / n, unit)}
            for i in range(n + 1)]


def _xticks(starts: Sequence[float], step: float, left: float, plot_w: float,
            n: int = 4) -> list[dict]:
    count = len(starts)
    if not count:
        return []
    span = step * count
    picks = sorted({round(i * (count - 1) / n) for i in range(n + 1)})
    return [{"x": round(left + plot_w * (i + 0.5) / count, 1),
             "label": _fmt_time(starts[i], span)} for i in picks]


def line_chart(starts: Sequence[float], step: float, series: Sequence[Series], *,
               unit: str = "%", ymax: float | None = None,
               thresholds: Sequence[tuple[float, str, str]] = (),
               width: int = WIDTH, height: int = HEIGHT) -> dict:
    """Lines over time buckets (`starts`, each `step` seconds long). A None value
    breaks the line: no data then (the UI was closed or the Ship did not answer).
    thresholds: (value, label, css class) drawn as labelled rules."""
    left, top, plot_w, plot_h = _box(width, height)
    count = len(starts)
    seen = [v for s in series for v in s.values if v is not None]
    top_value = ymax if ymax is not None else nice_max(max(seen, default=0) * 1.1)

    def x(i: int) -> float:
        return round(left + plot_w * (i + 0.5) / max(1, count), 1)

    def y(v: float) -> float:
        return round(top + plot_h * (1 - min(v, top_value) / top_value), 1)

    lines = []
    for s in series:
        parts, pen_down, last = [], False, None
        for i, v in enumerate(s.values):
            if v is None:
                pen_down = False
                continue
            parts.append(f"{'L' if pen_down else 'M'}{x(i)} {y(v)}")
            pen_down, last = True, (i, v)
        # Lone points (no neighbour on either side) get a dot so they show.
        dots = [{"x": x(i), "y": y(v)} for i, v in enumerate(s.values) if v is not None
                and (i == 0 or s.values[i - 1] is None)
                and (i == count - 1 or s.values[i + 1] is None)]
        lines.append({
            "key": s.key, "label": s.label, "d": " ".join(parts), "dots": dots,
            "end": ({"x": x(last[0]), "y": y(last[1]), "value": _num(last[1], unit)}
                    if last else None),
        })
    columns = []
    for i, at in enumerate(starts):
        readings = [f"{s.label} {_num(s.values[i], unit)}" for s in series
                    if s.values[i] is not None]
        columns.append({
            "x": round(left + plot_w * i / max(1, count), 1),
            "w": round(plot_w / max(1, count), 2),
            "title": _fmt_time(at, step * count) + ": "
            + ("; ".join(readings) if readings else "no data"),
        })
    return {
        "width": width, "height": height, "left": left, "top": top,
        "right": left + plot_w, "bottom": top + plot_h,
        "yticks": _yticks(top_value, unit, top, plot_h),
        "xticks": _xticks(starts, step, left, plot_w),
        "lines": lines, "columns": columns,
        "thresholds": [{"y": y(v), "label": label, "cls": cls}
                       for v, label, cls in thresholds if v <= top_value],
        "empty": not seen,
        "table": _table(starts, step, series, unit),
    }


def stacked_columns(starts: Sequence[float], step: float, series: Sequence[Series], *,
                    unit: str = "", width: int = WIDTH, height: int = HEIGHT) -> dict:
    """Stacked columns, one per bucket, with a 2px surface gap between segments."""
    left, top, plot_w, plot_h = _box(width, height)
    count = len(starts)
    totals = [sum(s.values[i] or 0 for s in series) for i in range(count)]
    top_value = nice_max(max(totals, default=0) * 1.1)
    if not unit:  # counts: whole numbers on the axis
        top_value = float(max(2, 2 * math.ceil(top_value / 2)))
    slot = plot_w / max(1, count)
    bar_w = max(1.0, min(24.0, slot - 2))
    columns = []
    for i, at in enumerate(starts):
        base = top + plot_h
        segments = []
        for s in series:
            v = s.values[i]
            if not v:
                continue
            h = plot_h * v / top_value
            gap = 2 if segments else 0  # the surface gap between stacked segments
            segments.append({"key": s.key, "y": round(base - h, 1),
                             "h": round(max(0.0, h - gap), 1)})
            base -= h
        readings = [f"{s.label} {_num(s.values[i], unit)}" for s in series
                    if s.values[i] is not None]
        columns.append({
            "x": round(left + slot * i + (slot - bar_w) / 2, 1), "w": round(bar_w, 1),
            "slot_x": round(left + slot * i, 1), "slot_w": round(slot, 2),
            "segments": segments,
            "title": _fmt_time(at, step * count) + ": "
            + ("; ".join(readings) if readings else "no data"),
        })
    return {
        "width": width, "height": height, "left": left, "top": top,
        "right": left + plot_w, "bottom": top + plot_h,
        "yticks": _yticks(top_value, unit, top, plot_h),
        "xticks": _xticks(starts, step, left, plot_w),
        "columns": columns,
        "legend": [{"key": s.key, "label": s.label} for s in series],
        "empty": not any(totals),
        "table": _table(starts, step, series, unit),
    }


def bars(rows: Sequence[tuple[str, float, str]], *, unit: str = "%", ymax: float = 100,
         thresholds: Sequence[tuple[float, str, str]] = (), width: int = WIDTH) -> dict:
    """Horizontal bars, one per row: (label, value, css class)."""
    row_h, label_w, value_w = 22, 70, 44
    height = TOP + row_h * len(rows) + 4
    plot_w = width - label_w - value_w

    def x(v: float) -> float:
        return round(label_w + plot_w * min(v, ymax) / ymax, 1)

    return {
        "width": width, "height": height, "label_w": label_w, "plot_right": label_w + plot_w,
        "rows": [{"label": label, "value": _num(value, unit), "cls": cls,
                  "y": TOP + row_h * i, "bar_y": TOP + row_h * i + 5, "bar_h": 12,
                  "w": max(2.0, x(value) - label_w), "value_x": width - 2}
                 for i, (label, value, cls) in enumerate(rows)],
        "thresholds": [{"x": x(v), "label": label, "cls": cls} for v, label, cls in thresholds],
        "top": TOP, "bottom": height - 4,
    }


def sparkline(values: Sequence[float | None], *, ymax: float = 100,
              width: int = 120, height: int = 28) -> dict:
    """A tiny line, no axes: the shape of the last hours. None values break it."""
    count = len(values)
    pad = 3
    parts, pen_down, last = [], False, None
    for i, v in enumerate(values):
        if v is None:
            pen_down = False
            continue
        px = round(pad + (width - 2 * pad) * i / max(1, count - 1), 1)
        py = round(pad + (height - 2 * pad) * (1 - min(v, ymax) / ymax), 1)
        parts.append(f"{'L' if pen_down else 'M'}{px} {py}")
        pen_down, last = True, (px, py)
    return {"width": width, "height": height, "d": " ".join(parts),
            "end": {"x": last[0], "y": last[1]} if last else None,
            "empty": last is None}


def _table(starts: Sequence[float], step: float, series: Sequence[Series],
           unit: str) -> dict:
    """The chart's values as a table, newest first, skipping buckets with no data."""
    rows = []
    for i in range(len(starts) - 1, -1, -1):
        values = [s.values[i] for s in series]
        if all(v is None for v in values):
            continue
        rows.append({"when": _fmt_time(starts[i], step * len(starts)),
                     "cells": ["-" if v is None else _num(v, unit) for v in values]})
    return {"head": [s.label for s in series], "rows": rows}


def buckets(span: float, step: float, now: float | None = None) -> list[float]:
    """Start times of the `span // step` buckets ending at the bucket holding now."""
    now = time.time() if now is None else now
    last = now - now % step
    count = int(span // step)
    return [last - step * (count - 1 - i) for i in range(count)]
