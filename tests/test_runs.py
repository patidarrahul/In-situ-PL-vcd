"""Unit tests for stored-run loading and settings persistence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from config import (
    load_settings,
    remember_browse_dir,
    resolve_browse_dir,
    resolve_data_dir,
    save_settings,
)
from runs import (
    CURVE_COLORS,
    Run,
    color_for_index,
    load_run_csv,
    thin_points,
    units_from_header,
)

HEADER = "iso_time,elapsed_s,pressure_mbar,gas,status_hex,flags\n"
SAMPLE = (
    HEADER
    + "2026-09-02T15:00:00,0.00,1.000000e+03,N2/air,0020,ok\n"
    + "2026-09-02T15:00:01,1.00,5.000000e+02,N2/air,0020,ok\n"
    + "2026-09-02T15:00:02,2.00,1.000000e+02,N2/air,0020,ok\n"
)


class TestThinPoints(unittest.TestCase):
    def test_short_curves_are_untouched(self):
        pts = [(float(i), 1.0) for i in range(10)]
        self.assertEqual(thin_points(pts, 100), pts)

    def test_long_curves_are_capped(self):
        pts = [(float(i), 1.0) for i in range(10_000)]
        thinned = thin_points(pts, 1000)
        self.assertLessEqual(len(thinned), 1001)

    def test_keeps_first_and_latest_sample(self):
        pts = [(float(i), float(i)) for i in range(101)]
        thinned = thin_points(pts, 10)
        self.assertEqual(thinned[0], pts[0])
        self.assertEqual(thinned[-1], pts[-1])

    def test_degenerate_limits(self):
        pts = [(0.0, 1.0), (1.0, 2.0)]
        self.assertEqual(thin_points(pts, 0), pts)
        self.assertEqual(thin_points([], 10), [])


class TestRunModel(unittest.TestCase):
    def test_points_filtering(self):
        run = Run("r", [0.0, 1.0, 2.0], [1e3, 0.0, 1e2])
        self.assertEqual(len(run.points()), 3)
        self.assertEqual(run.points(positive_only=True), [(0.0, 1e3), (2.0, 1e2)])

    def test_len(self):
        self.assertEqual(len(Run("r", [0.0, 1.0], [1.0, 2.0])), 2)

    def test_palette_cycles(self):
        self.assertEqual(color_for_index(0), CURVE_COLORS[0])
        self.assertEqual(color_for_index(len(CURVE_COLORS)), CURVE_COLORS[0])

    def test_units_from_header(self):
        self.assertEqual(units_from_header(HEADER.strip().split(",")), "mbar")
        self.assertEqual(units_from_header(["a", "b"]), "mbar")
        self.assertEqual(units_from_header(["pressure_Torr"]), "Torr")


class TestLoadRunCsv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text)
        return path

    def test_round_trip(self):
        run = load_run_csv(self._write("pressure_run.csv", SAMPLE))
        self.assertEqual(run.label, "pressure_run")
        self.assertEqual(run.units, "mbar")
        self.assertEqual(run.times, [0.0, 1.0, 2.0])
        self.assertAlmostEqual(run.pressures[-1], 100.0)

    def test_skips_unparsable_rows(self):
        text = SAMPLE + "2026-09-02T15:00:03,bad,also-bad,N2/air,0020,ok\n"
        run = load_run_csv(self._write("partial.csv", text))
        self.assertEqual(len(run), 3)

    def test_empty_file_raises(self):
        with self.assertRaises(ValueError):
            load_run_csv(self._write("empty.csv", ""))

    def test_header_only_raises(self):
        with self.assertRaises(ValueError):
            load_run_csv(self._write("header.csv", HEADER))

    def test_missing_file_raises(self):
        with self.assertRaises(OSError):
            load_run_csv(self.dir / "nope.csv")


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        save_settings({"data_dir": "/tmp/data"}, self.path)
        self.assertEqual(load_settings(self.path), {"data_dir": "/tmp/data"})

    def test_missing_file_gives_empty(self):
        self.assertEqual(load_settings(self.path), {})

    def test_corrupt_file_gives_empty(self):
        self.path.write_text("{not json")
        self.assertEqual(load_settings(self.path), {})

    def test_resolve_existing_dir(self):
        self.assertEqual(resolve_data_dir(self.tmp.name), Path(self.tmp.name))

    def test_resolve_falls_back_to_project_data(self):
        from config import default_data_dir

        fallback = default_data_dir()
        self.assertEqual(resolve_data_dir(None), fallback)
        self.assertEqual(resolve_data_dir(self.path / "missing"), fallback)

    def test_remember_browse_dir_from_file(self):
        folder = Path(self.tmp.name)
        file_path = folder / "run.csv"
        file_path.write_text("x")
        settings: dict = {}
        remembered = remember_browse_dir(settings, file_path)
        self.assertEqual(remembered, folder)
        self.assertEqual(settings["last_browse_dir"], str(folder))
        self.assertEqual(
            resolve_browse_dir(settings["last_browse_dir"]), folder
        )


if __name__ == "__main__":
    unittest.main()
