"""Stored pump-down runs: in-memory model, CSV loading, curve colors."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence

# Characters Windows rejects in a file name.
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
DEFAULT_SAVE_NAME = "pressure"

# Distinguishable on white, and none of them clash with the teal live curve.
CURVE_COLORS = [
    "#2563eb",
    "#c2410c",
    "#7c3aed",
    "#0891b2",
    "#4d7c0f",
    "#be123c",
    "#a16207",
    "#0f766e",
]
REFERENCE_COLOR = "#475569"


def color_for_index(index: int) -> str:
    return CURVE_COLORS[index % len(CURVE_COLORS)]


def sanitize_save_name(name: str) -> str:
    """A file-name stem the user typed, or ``pressure`` when it is blank."""
    cleaned = _UNSAFE_NAME.sub("_", str(name or "").strip()).strip(" .")
    return cleaned or DEFAULT_SAVE_NAME


def save_basename(
    name: str, stamp: str | None = None, run: int | None = None, *, both: bool = False
) -> str:
    """``{name}_{timestamp}``, or ``{name}_PL_and_Pressure_{timestamp}`` when both are recorded.

    ``stamp`` is shared by every cycle of one arming, so the run number is
    what distinguishes ``_run1`` from ``_run2``.
    """
    cleaned = sanitize_save_name(name)
    when = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    if both:
        cleaned = f"{cleaned}_PL_and_Pressure"
    if run is None:
        return f"{cleaned}_{when}"
    return f"{cleaned}_{when}_run{int(run)}"


def unique_save_path(directory: str | Path, stem: str, suffix: str) -> Path:
    """``stem + suffix`` in ``directory``, adding ``_2`` and so on if it exists."""
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}{suffix}"
    n = 2
    while path.exists():
        path = folder / f"{stem}_{n}{suffix}"
        n += 1
    return path


def thin_points(
    points: Sequence[tuple[float, float]], max_points: int
) -> list[tuple[float, float]]:
    """Evenly sample a curve down for display, always keeping the last point.

    A fast run can hold far more samples than the plot has pixels; the CSV and
    the stored run keep every one of them.
    """
    if max_points <= 1 or len(points) <= max_points:
        return list(points)
    stride = len(points) // max_points + 1
    thinned = list(points[::stride])
    if thinned[-1] != points[-1]:
        thinned.append(points[-1])
    return thinned


@dataclass
class Run:
    label: str
    times: list[float] = field(default_factory=list)
    pressures: list[float] = field(default_factory=list)
    units: str = "mbar"
    source: str = ""

    def points(self, positive_only: bool = False) -> list[tuple[float, float]]:
        pts = list(zip(self.times, self.pressures))
        if positive_only:
            pts = [(t, p) for t, p in pts if p > 0]
        return pts

    def __len__(self) -> int:
        return len(self.times)


def units_from_header(header: Sequence[str]) -> str:
    for column in header:
        if column.startswith("pressure_"):
            return column.split("_", 1)[1]
    return "mbar"


def load_run_csv(path: str | Path) -> Run:
    """Read a CSV written by this app back into a Run.

    Rows that cannot be parsed as numbers are skipped, so a file from an
    interrupted run still loads.
    """
    csv_path = Path(path)
    with csv_path.open(newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        raise ValueError(f"{csv_path.name} is empty")

    header, *data = rows
    units = units_from_header(header)
    time_idx = header.index("elapsed_s") if "elapsed_s" in header else 1
    pressure_idx = next(
        (i for i, c in enumerate(header) if c.startswith("pressure_")), 2
    )

    times: list[float] = []
    pressures: list[float] = []
    for row in data:
        if len(row) <= max(time_idx, pressure_idx):
            continue
        try:
            t = float(row[time_idx])
            p = float(row[pressure_idx])
        except ValueError:
            continue
        times.append(t)
        pressures.append(p)

    if not times:
        raise ValueError(f"{csv_path.name} has no usable samples")

    return Run(
        label=csv_path.stem,
        times=times,
        pressures=pressures,
        units=units,
        source=str(csv_path),
    )
