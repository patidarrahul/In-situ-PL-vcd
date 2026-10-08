"""Saving PL spectra to .npz files.

Every file contains ``wavelengths`` and ``intensities`` (dark-subtracted and
boxcar-smoothed, exactly as shown on screen) as well as ``raw_intensities``
(as delivered by the spectrometer, before software dark subtraction and
boxcar), the dark spectrum used, and all acquisition settings, so data can be
reprocessed later. All entries load with ``np.load(path)`` (no pickling).
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path

import numpy as np

SOFTWARE = "pump-down + PL monitor"


def iso_time(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds")


def make_path(directory: str | Path, prefix: str, kind: str) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    prefix = re.sub(r'[<>:"/\\|?*]+', "_", prefix.strip()) or "pl"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = directory / f"{prefix}_{stamp}_{kind}.npz"
    i = 1
    while path.exists():
        path = directory / f"{prefix}_{stamp}_{kind}_{i}.npz"
        i += 1
    return path


def write_npz(path: Path, data: dict) -> None:
    """Write atomically so a crash never leaves a half-written file behind."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        np.savez(f, **data)
    os.replace(tmp, path)


def common_metadata(
    *,
    model: str,
    serial: str,
    wavelengths: np.ndarray,
    integration_time_us: int,
    scans_to_average: int,
    electric_dark: bool,
    nonlinearity: bool,
    boxcar_width: int,
    dark: np.ndarray | None,
    dark_integration_time_us: int | None,
) -> dict:
    return {
        "wavelengths": np.asarray(wavelengths, dtype=float),
        "model": np.array(model),
        "serial_number": np.array(serial),
        "integration_time_us": np.int64(integration_time_us),
        "scans_to_average": np.int64(scans_to_average),
        "electric_dark_correction": np.bool_(electric_dark),
        "nonlinearity_correction": np.bool_(nonlinearity),
        "boxcar_width": np.int64(boxcar_width),
        "dark_subtracted": np.bool_(dark is not None),
        "dark_spectrum": np.asarray(dark if dark is not None else [], dtype=float),
        "dark_integration_time_us": np.int64(dark_integration_time_us or 0),
        "software": np.array(SOFTWARE),
    }


def save_single(path: Path, meta: dict, frame, processed: np.ndarray, extra: dict | None = None) -> None:
    data = dict(meta)
    data.update(
        kind=np.array("single"),
        intensities=np.asarray(processed, dtype=float),
        raw_intensities=np.asarray(frame.counts, dtype=float),
        timestamp=np.float64(frame.timestamp),
        timestamp_iso=np.array(iso_time(frame.timestamp)),
        t_start=np.float64(frame.t_start),
        t_end=np.float64(frame.t_end),
        peak_counts=np.float64(frame.peak_counts),
        saturated=np.bool_(frame.saturated),
    )
    data.update(extra or {})
    write_npz(path, data)


class TimeseriesRecorder:
    """Collects spectra at a fixed polling interval and saves them to one file.

    ``t0`` is the zero of ``elapsed_s``; a run linked to a pump-down passes the
    pump-down's own t = 0 so both share one time axis. The file is rewritten
    periodically while recording (``autosave_s``) so that at most that much
    data is lost if the program is interrupted.
    """

    def __init__(
        self,
        path: Path,
        meta: dict,
        interval_s: float,
        n_target: int,
        t0: float | None = None,
        autosave_s: float = 60.0,
    ):
        self.path = path
        self.meta = meta
        self.interval_s = max(0.0, float(interval_s))
        self.n_target = int(n_target)  # 0 means "until stopped"
        self.autosave_s = autosave_s
        self.t0 = time.time() if t0 is None else float(t0)
        self._next_due = self.t0
        self._last_save = time.monotonic()
        self.processed: list[np.ndarray] = []
        self.raw: list[np.ndarray] = []
        self.t_start: list[float] = []
        self.t_end: list[float] = []
        self.peak: list[float] = []
        self.saturated: list[bool] = []

    @property
    def count(self) -> int:
        return len(self.raw)

    @property
    def done(self) -> bool:
        return self.n_target > 0 and self.count >= self.n_target

    def seconds_until_next(self) -> float:
        return max(0.0, self._next_due - time.time())

    def offer(self, frame, processed: np.ndarray) -> bool:
        """Record ``frame`` if it is due. Returns True if it was recorded."""
        if self.done:
            return False
        if self.count == 0:
            # The first spectrum must be exposed entirely after t0.
            if frame.t_start < self.t0:
                return False
        elif frame.t_end < self._next_due or frame.t_start < self.t_end[-1]:
            return False

        self.processed.append(np.asarray(processed, dtype=float))
        self.raw.append(np.asarray(frame.counts, dtype=float))
        self.t_start.append(frame.t_start)
        self.t_end.append(frame.t_end)
        self.peak.append(frame.peak_counts)
        self.saturated.append(frame.saturated)

        # Keep a regular time grid; if a slot is missed, skip to the next one.
        if self.interval_s > 0:
            k = int((frame.t_end - self.t0) // self.interval_s) + 1
            self._next_due = self.t0 + k * self.interval_s
        else:
            self._next_due = frame.t_end

        if time.monotonic() - self._last_save >= self.autosave_s:
            self.save(complete=False)
        return True

    def trim_after(self, t: float) -> None:
        """Drop spectra that started after ``t`` (e.g. after a run was closed)."""
        keep = [i for i, ts in enumerate(self.t_start) if ts <= t]
        if len(keep) == self.count:
            return
        for name in ("processed", "raw", "t_start", "t_end", "peak", "saturated"):
            values = getattr(self, name)
            setattr(self, name, [values[i] for i in keep])

    def save(self, complete: bool, extra: dict | None = None) -> None:
        n_pix = len(self.meta["wavelengths"])
        t_start = np.asarray(self.t_start, dtype=float)
        t_end = np.asarray(self.t_end, dtype=float)
        timestamps = 0.5 * (t_start + t_end)
        data = dict(self.meta)
        data.update(
            kind=np.array("timeseries"),
            intensities=np.asarray(self.processed, dtype=float).reshape(-1, n_pix),
            raw_intensities=np.asarray(self.raw, dtype=float).reshape(-1, n_pix),
            timestamps=timestamps,
            elapsed_s=timestamps - self.t0,
            t_start=t_start,
            t_end=t_end,
            start_time=np.float64(self.t0),
            start_time_iso=np.array(iso_time(self.t0)),
            interval_s=np.float64(self.interval_s),
            n_requested=np.int64(self.n_target),
            peak_counts=np.asarray(self.peak, dtype=float),
            saturated=np.asarray(self.saturated, dtype=bool),
            complete=np.bool_(complete),
        )
        data.update(extra or {})
        write_npz(self.path, data)
        self._last_save = time.monotonic()
