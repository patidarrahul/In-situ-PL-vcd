"""Derived pump-down statistics (pure functions, no UI dependencies)."""

from __future__ import annotations

import math
from typing import Optional, Sequence


def format_elapsed(seconds: Optional[float]) -> str:
    """Seconds as h:mm:ss (or m:ss under an hour)."""
    if seconds is None or seconds < 0:
        return "--:--"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_pressure(value: Optional[float], units: str) -> str:
    if value is None:
        return "--"
    return f"{value:.3e} {units}"


def lowest_pressure(pressures: Sequence[float]) -> Optional[float]:
    positives = [p for p in pressures if p > 0]
    return min(positives) if positives else None


def decades_per_min(
    times: Sequence[float],
    pressures: Sequence[float],
    window_s: float = 30.0,
) -> Optional[float]:
    """Pump-down speed over the trailing window.

    Positive means pressure is falling. Needs two usable points spanning
    a non-zero time interval, so early samples return None.
    """
    pts = [(t, p) for t, p in zip(times, pressures) if p > 0]
    if len(pts) < 2:
        return None

    t_end = pts[-1][0]
    recent = [(t, p) for t, p in pts if t >= t_end - window_s]
    if len(recent) < 2:
        recent = pts[-2:]

    (t0, p0), (t1, p1) = recent[0], recent[-1]
    span = t1 - t0
    if span <= 0:
        return None
    return (math.log10(p0) - math.log10(p1)) / (span / 60.0)


def format_rate(rate: Optional[float]) -> str:
    if rate is None:
        return "--"
    return f"{rate:+.2f} dec/min"


def sample_rate_hz(times: Sequence[float], window_s: float = 3.0) -> Optional[float]:
    """Measured sampling rate over the trailing window.

    This is what the link actually delivers, which can be slower than the
    requested interval when the gauge or the bridge is the bottleneck.
    """
    if len(times) < 2:
        return None
    recent = [t for t in times if t >= times[-1] - window_s]
    if len(recent) < 2:
        recent = list(times[-2:])
    span = recent[-1] - recent[0]
    if span <= 0:
        return None
    return (len(recent) - 1) / span


def format_sample_count(count: int, rate: Optional[float]) -> str:
    if rate is None:
        return str(count)
    return f"{count}  ·  {rate:.1f} Hz"
