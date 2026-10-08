"""Unit tests for derived pump-down metrics."""

from __future__ import annotations

import unittest

from metrics import (
    decades_per_min,
    format_elapsed,
    format_pressure,
    format_rate,
    format_sample_count,
    lowest_pressure,
    sample_rate_hz,
)


class TestFormatting(unittest.TestCase):
    def test_elapsed_under_hour(self):
        self.assertEqual(format_elapsed(0), "0:00")
        self.assertEqual(format_elapsed(65.4), "1:05")

    def test_elapsed_over_hour(self):
        self.assertEqual(format_elapsed(3725), "1:02:05")

    def test_elapsed_missing(self):
        self.assertEqual(format_elapsed(None), "--:--")

    def test_pressure(self):
        self.assertEqual(format_pressure(1e5, "Pa"), "1.000e+05 Pa")
        self.assertEqual(format_pressure(None, "Pa"), "--")

    def test_rate(self):
        self.assertEqual(format_rate(None), "--")
        self.assertEqual(format_rate(1.5), "+1.50 dec/min")


class TestLowest(unittest.TestCase):
    def test_ignores_non_positive(self):
        self.assertEqual(lowest_pressure([1e3, 0.0, 5e2, -1.0]), 5e2)

    def test_all_invalid(self):
        self.assertIsNone(lowest_pressure([0.0, -2.0]))

    def test_empty(self):
        self.assertIsNone(lowest_pressure([]))


class TestRate(unittest.TestCase):
    def test_one_decade_per_minute(self):
        rate = decades_per_min([0.0, 60.0], [1e5, 1e4], window_s=120)
        self.assertAlmostEqual(rate, 1.0)

    def test_uses_trailing_window(self):
        # Fast early drop then flat: the window should report the flat part
        times = [0.0, 10.0, 100.0, 130.0]
        pressures = [1e5, 1e2, 1e2, 1e2]
        self.assertAlmostEqual(decades_per_min(times, pressures, window_s=30.0), 0.0)

    def test_rising_pressure_is_negative(self):
        rate = decades_per_min([0.0, 60.0], [1e2, 1e3], window_s=120)
        self.assertAlmostEqual(rate, -1.0)

    def test_needs_two_points(self):
        self.assertIsNone(decades_per_min([0.0], [1e5]))
        self.assertIsNone(decades_per_min([], []))

    def test_zero_span_returns_none(self):
        self.assertIsNone(decades_per_min([5.0, 5.0], [1e5, 1e4]))

    def test_skips_non_positive_samples(self):
        rate = decades_per_min([0.0, 30.0, 60.0], [1e5, 0.0, 1e4], window_s=120)
        self.assertAlmostEqual(rate, 1.0)


class TestSampleRate(unittest.TestCase):
    def test_ten_hz(self):
        times = [i * 0.1 for i in range(31)]
        self.assertAlmostEqual(sample_rate_hz(times), 10.0, places=6)

    def test_uses_trailing_window(self):
        """A slow start should not drag down the currently achieved rate."""
        times = [0.0, 5.0] + [5.0 + i * 0.05 for i in range(1, 41)]
        self.assertAlmostEqual(sample_rate_hz(times, window_s=1.0), 20.0, places=6)

    def test_needs_two_points(self):
        self.assertIsNone(sample_rate_hz([]))
        self.assertIsNone(sample_rate_hz([1.0]))

    def test_zero_span_returns_none(self):
        self.assertIsNone(sample_rate_hz([2.0, 2.0]))

    def test_format(self):
        self.assertEqual(format_sample_count(120, 9.83), "120  ·  9.8 Hz")
        self.assertEqual(format_sample_count(1, None), "1")


if __name__ == "__main__":
    unittest.main()
