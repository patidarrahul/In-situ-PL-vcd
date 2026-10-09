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
    sanitize_save_name,
    save_basename,
    thin_points,
    unique_save_path,
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


class TestSaveNames(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_blank_name_falls_back(self):
        self.assertEqual(sanitize_save_name("   "), "pressure")
        self.assertEqual(sanitize_save_name("a/b:c"), "a_b_c")

    def test_single_run_is_name_and_timestamp(self):
        self.assertEqual(save_basename("wafer A", "20261009_094600"), "wafer A_20261009_094600")

    def test_cycles_share_the_timestamp(self):
        stamp = "20261009_094600"
        self.assertEqual(save_basename("wafer A", stamp, run=1), "wafer A_20261009_094600_run1")
        self.assertEqual(save_basename("wafer A", stamp, run=2), "wafer A_20261009_094600_run2")

    def test_both_pressure_and_pl_marks_the_name(self):
        stamp = "20261009_094600"
        self.assertEqual(
            save_basename("wafer A", stamp, both=True),
            "wafer A_PL_and_Pressure_20261009_094600",
        )
        self.assertEqual(
            save_basename("wafer A", stamp, run=2, both=True),
            "wafer A_PL_and_Pressure_20261009_094600_run2",
        )

    def test_unique_path_adds_a_suffix(self):
        folder = Path(self.tmp.name)
        first = unique_save_path(folder, "wafer_20261009_094600_run1", ".csv")
        first.write_text("x")
        second = unique_save_path(folder, "wafer_20261009_094600_run1", ".csv")
        self.assertEqual(first.name, "wafer_20261009_094600_run1.csv")
        self.assertEqual(second.name, "wafer_20261009_094600_run1_2.csv")


if __name__ == "__main__":
    unittest.main()
