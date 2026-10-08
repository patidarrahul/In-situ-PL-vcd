"""Background thread that continuously reads spectra from the device."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

SATURATION_FRACTION = 0.95


@dataclass
class Frame:
    """One (possibly scan-averaged) spectrum straight from the device."""

    counts: np.ndarray
    t_start: float
    t_end: float
    integration_time_us: int
    scans_averaged: int
    electric_dark: bool
    nonlinearity: bool
    peak_counts: float
    saturated: bool

    @property
    def timestamp(self) -> float:
        return 0.5 * (self.t_start + self.t_end)


class AcquisitionWorker(threading.Thread):
    """Reads spectra until stopped, handing each one to ``on_frame``.

    The callbacks run on this thread; they must only hand data over (e.g. put
    it on a queue), never touch widgets.
    """

    def __init__(
        self,
        device,
        integration_time_us: int,
        on_frame: Callable[[Frame], None],
        on_error: Callable[[str], None],
    ):
        super().__init__(daemon=True)
        self._device = device
        self._on_frame = on_frame
        self._on_error = on_error
        self._lock = threading.Lock()
        self._running = True
        self._pending_integration_us: int | None = int(integration_time_us)
        self._integration_us = int(integration_time_us)
        self._scans = 1
        self._electric_dark = False
        self._nonlinearity = False

    # --- settings (safe to call from any thread) -------------------------
    def set_integration_time_us(self, micros: int) -> None:
        with self._lock:
            self._pending_integration_us = int(micros)

    def set_scans_to_average(self, n: int) -> None:
        with self._lock:
            self._scans = max(1, int(n))

    def set_corrections(self, electric_dark: bool, nonlinearity: bool) -> None:
        with self._lock:
            self._electric_dark = bool(electric_dark)
            self._nonlinearity = bool(nonlinearity)

    def stop(self) -> None:
        with self._lock:
            self._running = False

    # --- thread body -----------------------------------------------------
    def run(self) -> None:
        try:
            self._loop()
        except Exception as exc:
            self._on_error(f"{type(exc).__name__}: {exc}")

    def _loop(self) -> None:
        dev = self._device
        while True:
            with self._lock:
                if not self._running:
                    return
                pending = self._pending_integration_us
                self._pending_integration_us = None
                scans = self._scans
                edark = self._electric_dark
                nonlin = self._nonlinearity

            if pending is not None:
                dev.set_integration_time_us(pending)
                self._integration_us = pending
                # The first spectrum after changing the integration time may
                # have been exposed with the old setting; discard it.
                dev.read(edark, nonlin)
                continue

            t_start = time.time()
            total = np.zeros(dev.pixels, dtype=float)
            peak = -np.inf
            n = 0
            for _ in range(scans):
                counts = dev.read(edark, nonlin)
                total += counts
                peak = max(peak, float(counts.max()))
                n += 1
                with self._lock:
                    if not self._running or self._pending_integration_us is not None:
                        break
            t_end = time.time()

            with self._lock:
                if not self._running:
                    return
                if self._pending_integration_us is not None:
                    continue

            self._on_frame(
                Frame(
                    counts=total / n,
                    t_start=t_start,
                    t_end=t_end,
                    integration_time_us=self._integration_us,
                    scans_averaged=n,
                    electric_dark=edark and dev.supports_electric_dark,
                    nonlinearity=nonlin and dev.supports_nonlinearity,
                    peak_counts=peak,
                    saturated=peak >= SATURATION_FRACTION * dev.max_intensity,
                )
            )
