"""Spectrometer processing, PL recording and the controller (simulated device)."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from spectrometer import storage
from spectrometer.controller import SpectrometerController
from spectrometer.device import SIMULATED_SERIAL, DeviceInfo
from spectrometer.processing import boxcar, process


def fake_frame(t_start: float, t_end: float, n: int = 4, value: float = 1.0):
    return SimpleNamespace(
        counts=np.full(n, value),
        t_start=t_start,
        t_end=t_end,
        peak_counts=value,
        saturated=False,
        timestamp=0.5 * (t_start + t_end),
    )


def meta(n: int = 4) -> dict:
    return storage.common_metadata(
        model="test",
        serial="0",
        wavelengths=np.arange(n, dtype=float),
        integration_time_us=1000,
        scans_to_average=1,
        electric_dark=False,
        nonlinearity=False,
        boxcar_width=0,
        dark=None,
        dark_integration_time_us=None,
    )


class TestProcessing(unittest.TestCase):
    def test_boxcar_zero_is_identity(self):
        y = np.array([1.0, 5.0, 2.0])
        np.testing.assert_array_equal(boxcar(y, 0), y)

    def test_boxcar_averages_2n_plus_1(self):
        y = np.array([0.0, 0.0, 3.0, 0.0, 0.0])
        np.testing.assert_allclose(boxcar(y, 1), [0.0, 1.0, 1.0, 1.0, 0.0])

    def test_boxcar_edges_use_existing_pixels(self):
        np.testing.assert_allclose(boxcar(np.ones(6), 2), np.ones(6))

    def test_dark_then_boxcar(self):
        np.testing.assert_allclose(process(np.full(3, 5.0), np.full(3, 2.0), 0), [3.0, 3.0, 3.0])


class TestRecorder(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_interval_grid_and_t0(self):
        t0 = 1_760_000_000.0  # Windows cannot format timestamps near 1970
        rec = storage.TimeseriesRecorder(self.dir / "a.npz", meta(), interval_s=1.0, n_target=0, t0=t0)
        self.assertFalse(rec.offer(fake_frame(t0 - 0.1, t0 + 0.1), np.ones(4)))  # started before t0
        for k in range(30):
            t = t0 + 0.2 * k
            rec.offer(fake_frame(t, t + 0.2), np.ones(4))
        # One spectrum per 1 s slot: t_end at 100.2, then the first at/after 101 ... 106
        self.assertEqual(rec.count, 7)
        rec.save(complete=True, extra={"pressure": np.array([1.0, 2.0])})
        d = np.load(rec.path)
        self.assertEqual(d["intensities"].shape, (7, 4))
        self.assertAlmostEqual(float(d["elapsed_s"][0]), 0.1, places=5)
        self.assertTrue(bool(d["complete"]))
        np.testing.assert_array_equal(d["pressure"], [1.0, 2.0])

    def test_target_count(self):
        rec = storage.TimeseriesRecorder(self.dir / "b.npz", meta(), interval_s=0, n_target=3, t0=0.0)
        for k in range(10):
            rec.offer(fake_frame(k, k + 0.5), np.ones(4))
        self.assertEqual(rec.count, 3)
        self.assertTrue(rec.done)

    def test_trim_after(self):
        rec = storage.TimeseriesRecorder(self.dir / "c.npz", meta(), interval_s=0, n_target=0, t0=0.0)
        for k in range(5):
            rec.offer(fake_frame(k, k + 0.5), np.ones(4))
        rec.trim_after(2.0)
        self.assertEqual(rec.t_start, [0, 1, 2])


class TestController(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.sc = SpectrometerController()
        self.sc.integration_us = 10_000
        self.sc.connect(DeviceInfo("USB4000", SIMULATED_SERIAL))

    def tearDown(self):
        self.sc.disconnect()

    def pump(self, seconds: float) -> list[str]:
        notices = []
        end = time.time() + seconds
        while time.time() < end:
            notices += self.sc.poll().notices
            time.sleep(0.01)
        return notices

    def test_live_frames(self):
        self.pump(0.3)
        self.assertIsNotNone(self.sc.last_frame)
        self.assertEqual(self.sc.last_processed.shape, (self.sc.device.pixels,))

    def test_dark_and_single_spectrum(self):
        self.sc.request_dark()
        self.pump(0.2)
        self.assertIsNotNone(self.sc.dark)
        self.sc.set_processing(True, 2)
        self.sc.request_single(self.dir, "pl", {"pressure": np.float64(1.5)})
        notices = self.pump(0.2)
        self.assertTrue(any(n.startswith("Saved") for n in notices))
        (path,) = self.dir.glob("pl_*_single.npz")
        d = np.load(path)
        self.assertTrue(bool(d["dark_subtracted"]))
        self.assertEqual(int(d["boxcar_width"]), 2)
        self.assertEqual(float(d["pressure"]), 1.5)

    def test_linked_series_backfills_from_t0(self):
        self.pump(0.5)
        t0 = time.time() - 0.3  # auto-trigger puts t = 0 slightly in the past
        path = self.dir / "pl_run01.npz"
        self.sc.start_series(path, interval_s=0.1, t0=t0, linked=True)
        self.assertGreater(self.sc.recorder.count, 0)
        self.pump(0.3)
        notices = self.sc.stop_series(extra={"pressure": np.array([1e5, 1e3])})
        self.assertTrue(notices[0].startswith("Saved pl_run01.npz"))
        d = np.load(path)
        self.assertGreaterEqual(float(d["elapsed_s"][0]), 0.0)
        self.assertLess(float(d["elapsed_s"][0]), 0.15)
        self.assertEqual(d["intensities"].shape[1], 3648)


if __name__ == "__main__":
    unittest.main()
