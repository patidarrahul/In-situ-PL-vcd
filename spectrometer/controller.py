"""Spectrometer session: device, acquisition thread, dark spectrum and saving.

No UI code lives here. The acquisition thread only queues frames; every other
method, including ``poll``, is meant to be called from the Tk main thread.
"""

from __future__ import annotations

import queue
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import storage
from .acquisition import AcquisitionWorker, Frame
from .device import DeviceInfo, open_device
from .processing import process

# Recent spectra are kept so a run whose t = 0 lies in the past (auto-trigger
# anchors t = 0 at the last reading before the drop) still gets PL from t = 0.
RECENT_HORIZON_S = 30.0
RECENT_MIN_SPACING_S = 0.05
FRAME_QUEUE_MAX = 5000


@dataclass
class PollResult:
    frame: Frame | None = None
    processed: np.ndarray | None = None
    notices: list[str] = field(default_factory=list)
    error: str | None = None
    series_finished: bool = False


class SpectrometerController:
    def __init__(self) -> None:
        self.device = None
        self._worker: AcquisitionWorker | None = None
        self._frames: queue.Queue = queue.Queue(maxsize=FRAME_QUEUE_MAX)
        self._errors: queue.Queue = queue.Queue()

        self.integration_us = 100_000
        self.scans = 1
        self.electric_dark = False
        self.nonlinearity = False
        self.subtract_dark = False
        self.boxcar_width = 0

        self.dark: np.ndarray | None = None
        self.dark_frame: Frame | None = None
        self._pending_dark_t: float | None = None
        self._pending_single: tuple[float, Path, str, dict] | None = None

        self.recorder: storage.TimeseriesRecorder | None = None
        self.linked = False
        self._recent: deque[Frame] = deque()

        self.last_frame: Frame | None = None
        self.last_processed: np.ndarray | None = None
        self.rate_hz: float | None = None
        self._last_end: float | None = None

    # ------------------------------------------------------------- device
    @property
    def connected(self) -> bool:
        return self.device is not None

    @property
    def recording(self) -> bool:
        return self.recorder is not None

    def connect(self, info: DeviceInfo) -> None:
        self.disconnect()
        dev = open_device(info)
        self.device = dev
        self.integration_us = self.clamp_integration_us(self.integration_us)
        self._drain_queues()
        self._recent.clear()
        self.clear_dark()
        worker = AcquisitionWorker(dev, self.integration_us, self._enqueue, self._errors.put)
        worker.set_scans_to_average(self.scans)
        worker.set_corrections(self.electric_dark, self.nonlinearity)
        self._worker = worker
        worker.start()

    def disconnect(self) -> list[str]:
        notices = self.stop_series() if self.recorder is not None else []
        self._pending_dark_t = None
        self._pending_single = None
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.stop()
            worker.join(timeout=self.integration_us * self.scans / 1e6 + 3.0)
        dev, self.device = self.device, None
        if dev is not None:
            try:
                dev.close()
            except Exception as exc:
                notices.append(f"Error closing spectrometer: {exc}")
        self._drain_queues()
        self._recent.clear()
        self.last_frame = None
        self.last_processed = None
        self._reset_rate()
        return notices

    def _enqueue(self, frame: Frame) -> None:
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            pass  # GUI stalled; dropping beats unbounded memory growth

    def _drain_queues(self) -> None:
        for q in (self._frames, self._errors):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    def _reset_rate(self) -> None:
        self.rate_hz = None
        self._last_end = None

    # ----------------------------------------------------------- settings
    def clamp_integration_us(self, micros: float) -> int:
        micros = int(round(micros))
        if self.device is not None:
            lo, hi = self.device.integration_limits_us
            micros = min(max(micros, lo), hi)
        return max(1, micros)

    def set_integration_time_us(self, micros: float) -> int:
        self.integration_us = self.clamp_integration_us(micros)
        if self._worker is not None:
            self._worker.set_integration_time_us(self.integration_us)
        self._reset_rate()
        return self.integration_us

    def set_scans(self, n: int) -> None:
        self.scans = max(1, int(n))
        if self._worker is not None:
            self._worker.set_scans_to_average(self.scans)
        self._reset_rate()

    def set_corrections(self, electric_dark: bool, nonlinearity: bool) -> None:
        self.electric_dark = bool(electric_dark)
        self.nonlinearity = bool(nonlinearity)
        if self._worker is not None:
            self._worker.set_corrections(self.electric_dark, self.nonlinearity)

    def set_processing(self, subtract_dark: bool, boxcar_width: int) -> None:
        self.subtract_dark = bool(subtract_dark)
        self.boxcar_width = max(0, int(boxcar_width))

    def effective_electric_dark(self) -> bool:
        return self.electric_dark and bool(self.device and self.device.supports_electric_dark)

    def effective_nonlinearity(self) -> bool:
        return self.nonlinearity and bool(self.device and self.device.supports_nonlinearity)

    # --------------------------------------------------------------- dark
    @property
    def dark_pending(self) -> bool:
        return self._pending_dark_t is not None

    def request_dark(self) -> None:
        self._pending_dark_t = time.time()

    def clear_dark(self) -> None:
        self.dark = None
        self.dark_frame = None
        self._pending_dark_t = None

    def active_dark(self) -> np.ndarray | None:
        return self.dark if self.subtract_dark else None

    def dark_mismatch(self) -> str:
        d = self.dark_frame
        if d is None:
            return ""
        problems = []
        if d.integration_time_us != self.integration_us:
            problems.append(f"taken at {d.integration_time_us / 1000:g} ms")
        if d.electric_dark != self.effective_electric_dark():
            problems.append("electric dark changed")
        if d.nonlinearity != self.effective_nonlinearity():
            problems.append("nonlinearity changed")
        return ", ".join(problems)

    def process(self, counts: np.ndarray) -> np.ndarray:
        return process(counts, self.active_dark(), self.boxcar_width)

    # ------------------------------------------------------------- saving
    def metadata(self) -> dict:
        dev = self.device
        dark = self.active_dark()
        return storage.common_metadata(
            model=dev.model,
            serial=dev.serial,
            wavelengths=dev.wavelengths,
            integration_time_us=self.integration_us,
            scans_to_average=self.scans,
            electric_dark=self.effective_electric_dark(),
            nonlinearity=self.effective_nonlinearity(),
            boxcar_width=self.boxcar_width,
            dark=dark,
            dark_integration_time_us=self.dark_frame.integration_time_us if dark is not None else None,
        )

    @property
    def single_pending(self) -> bool:
        return self._pending_single is not None

    def request_single(self, directory: str | Path, prefix: str = "pl", extra: dict | None = None) -> None:
        """Save the next spectrum that is exposed entirely after this call."""
        self._pending_single = (time.time(), Path(directory), prefix, dict(extra or {}))

    def _save_single(self, frame: Frame, processed: np.ndarray, directory: Path, prefix: str, extra: dict) -> str:
        path = storage.make_path(directory, prefix, "single")
        meta = self.metadata()
        meta.update(
            integration_time_us=np.int64(frame.integration_time_us),
            scans_to_average=np.int64(frame.scans_averaged),
            electric_dark_correction=np.bool_(frame.electric_dark),
            nonlinearity_correction=np.bool_(frame.nonlinearity),
        )
        storage.save_single(path, meta, frame, processed, extra)
        return f"Saved {path.name}"

    def start_series(
        self,
        path: Path,
        interval_s: float,
        n_target: int = 0,
        t0: float | None = None,
        linked: bool = False,
    ) -> None:
        if self.device is None:
            raise RuntimeError("Spectrometer is not connected")
        if self.recorder is not None:
            raise RuntimeError("A PL series is already recording")
        rec = storage.TimeseriesRecorder(path, self.metadata(), interval_s, n_target, t0=t0)
        self.recorder = rec
        self.linked = linked
        for frame in list(self._recent):
            if frame.t_start >= rec.t0 and self._matches_settings(frame):
                rec.offer(frame, self.process(frame.counts))

    def stop_series(self, until: float | None = None, extra: dict | None = None) -> list[str]:
        rec, self.recorder = self.recorder, None
        self.linked = False
        if rec is None:
            return []
        if until is not None:
            rec.trim_after(until)
        if rec.count == 0:
            return ["PL series stopped · no spectra recorded"]
        try:
            rec.save(complete=True, extra=extra)
        except Exception as exc:
            return [f"PL save failed: {exc}"]
        return [f"Saved {rec.path.name} · {rec.count} spectra"]

    def _matches_settings(self, frame: Frame) -> bool:
        return (
            frame.integration_time_us == self.integration_us
            and frame.electric_dark == self.effective_electric_dark()
            and frame.nonlinearity == self.effective_nonlinearity()
        )

    # ------------------------------------------------------------ polling
    def poll(self) -> PollResult:
        """Process every queued spectrum; call regularly from the main thread."""
        result = PollResult()
        try:
            message = self._errors.get_nowait()
        except queue.Empty:
            pass
        else:
            result.notices.extend(self.disconnect())
            result.error = f"Spectrometer stopped: {message}"
            return result

        while self.device is not None:
            try:
                frame = self._frames.get_nowait()
            except queue.Empty:
                break
            self._handle(frame, result)
        return result

    def _handle(self, frame: Frame, result: PollResult) -> None:
        processed = self.process(frame.counts)

        if self._last_end is not None and frame.t_end > self._last_end:
            rate = 1.0 / (frame.t_end - self._last_end)
            self.rate_hz = rate if self.rate_hz is None else 0.8 * self.rate_hz + 0.2 * rate
        self._last_end = frame.t_end

        if not self._recent or frame.t_end - self._recent[-1].t_end >= RECENT_MIN_SPACING_S:
            self._recent.append(frame)
        while self._recent and frame.t_end - self._recent[0].t_end > RECENT_HORIZON_S:
            self._recent.popleft()

        if self._pending_dark_t is not None and frame.t_start >= self._pending_dark_t:
            self._pending_dark_t = None
            self.dark = frame.counts.copy()
            self.dark_frame = frame
            result.notices.append("Dark spectrum captured")

        if self._pending_single is not None and frame.t_start >= self._pending_single[0]:
            _, directory, prefix, extra = self._pending_single
            self._pending_single = None
            try:
                result.notices.append(self._save_single(frame, processed, directory, prefix, extra))
            except Exception as exc:
                result.error = f"Could not save spectrum: {exc}"

        rec = self.recorder
        if rec is not None:
            try:
                rec.offer(frame, processed)
            except Exception as exc:
                result.error = f"PL autosave failed: {exc}"
            if rec.done:
                result.notices.extend(self.stop_series())
                result.series_finished = True

        self.last_frame = frame
        self.last_processed = processed
        result.frame = frame
        result.processed = processed
