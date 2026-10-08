"""Unit tests for comparison-chart specs and rendering."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
from matplotlib.figure import Figure

from charts import (
    ChartSpec,
    add_csv_paths,
    convert_pressure,
    load_chart,
    load_runs_for_spec,
    normalize_linestyle,
    render_chart,
    save_chart,
    unique_label,
)
from runs import CURVE_COLORS

HEADER = "iso_time,elapsed_s,pressure_Pa,gas,status_hex,flags\n"
SAMPLE = (
    HEADER
    + "2026-09-02T15:00:00,0.00,1.000000e+05,N2/air,0020,ok\n"
    + "2026-09-02T15:00:01,1.00,1.000000e+03,N2/air,0020,ok\n"
    + "2026-09-02T15:00:02,2.00,1.000000e+02,N2/air,0020,ok\n"
)
MBAR = (
    "iso_time,elapsed_s,pressure_mbar,gas,status_hex,flags\n"
    "2026-09-02T15:00:00,0.00,1.000000e+03,N2/air,0020,ok\n"
    "2026-09-02T15:00:01,1.00,1.000000e+01,N2/air,0020,ok\n"
)


class TestHelpers(unittest.TestCase):
    def test_unique_label_suffixes(self):
        self.assertEqual(unique_label("run", []), "run")
        self.assertEqual(unique_label("run", ["run"]), "run (2)")
        self.assertEqual(unique_label("run", ["run", "run (2)"]), "run (3)")

    def test_pressure_conversion(self):
        self.assertAlmostEqual(convert_pressure(1.0, "mbar", "Pa"), 100.0)
        self.assertAlmostEqual(convert_pressure(100.0, "Pa", "mbar"), 1.0)
        self.assertAlmostEqual(convert_pressure(760.0, "Torr", "Pa"), 760.0 * 133.322368)

    def test_linestyle_normalize(self):
        self.assertEqual(normalize_linestyle("Solid"), "-")
        self.assertEqual(normalize_linestyle("Dotted"), ":")
        self.assertEqual(normalize_linestyle(":"), ":")
        self.assertEqual(normalize_linestyle("nope"), "-")


class TestChartSpec(unittest.TestCase):
    def test_default_ylabel_follows_units(self):
        spec = ChartSpec(units="Torr")
        self.assertEqual(spec.ylabel_text(), "Pressure (Torr)")
        spec.ylabel = "Chamber P"
        self.assertEqual(spec.ylabel_text(), "Chamber P")

    def test_from_dict_clamps_bad_values(self):
        spec = ChartSpec.from_dict(
            {
                "units": "psi",
                "yscale": "nope",
                "legend_loc": "somewhere",
                "line_width": 99,
                "xmin": "abc",
                "curves": [
                    {"path": "/tmp/a.csv", "label": "A", "linestyle": "zigzag"}
                ],
            }
        )
        self.assertEqual(spec.units, "Pa")
        self.assertEqual(spec.yscale, "log")
        self.assertEqual(spec.legend_loc, "best")
        self.assertEqual(spec.line_width, 6.0)
        self.assertIsNone(spec.xmin)
        self.assertEqual(spec.curves[0].color, CURVE_COLORS[0])
        self.assertEqual(spec.curves[0].linestyle, "-")


class TestCsvAndJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text)
        return path

    def test_add_csv_paths_and_skip_duplicates(self):
        a = self._write("run_a.csv", SAMPLE)
        spec = ChartSpec()
        errors = add_csv_paths(spec, [a, a])
        self.assertEqual(errors, [])
        self.assertEqual(len(spec.curves), 1)
        self.assertEqual(spec.curves[0].label, "run_a")

    def test_add_csv_paths_reports_bad_files(self):
        spec = ChartSpec()
        missing = self.dir / "nope.csv"
        errors = add_csv_paths(spec, [missing])
        self.assertTrue(errors)
        self.assertEqual(spec.curves, [])

    def test_json_round_trip_uses_relative_paths(self):
        csv_path = self._write("run.csv", SAMPLE)
        spec = ChartSpec(title="Batch 23", units="Pa")
        add_csv_paths(spec, [csv_path])
        spec.curves[0].label = "0.6 M, 10 s vent"
        spec.xmin = 0.0
        spec.ymax = 1e5
        json_path = self.dir / "chart.json"
        save_chart(spec, json_path)
        payload = json.loads(json_path.read_text())
        self.assertEqual(payload["curves"][0]["path"], "run.csv")
        loaded = load_chart(json_path)
        self.assertEqual(loaded.title, "Batch 23")
        self.assertEqual(loaded.curves[0].label, "0.6 M, 10 s vent")
        self.assertEqual(Path(loaded.curves[0].path).resolve(), csv_path.resolve())
        self.assertEqual(loaded.xmin, 0.0)
        self.assertEqual(loaded.ymax, 1e5)

    def test_json_rejects_non_object(self):
        path = self._write("bad.json", "[1, 2]")
        with self.assertRaises(ValueError):
            load_chart(path)


class TestRender(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_draws_visible_curves_only(self):
        a = self.dir / "a.csv"
        b = self.dir / "b.csv"
        a.write_text(SAMPLE)
        b.write_text(SAMPLE)
        spec = ChartSpec()
        add_csv_paths(spec, [a, b])
        spec.curves[0].linestyle = ":"
        spec.curves[1].visible = False
        spec.curves[1].label = "hidden"
        runs, errors = load_runs_for_spec(spec)
        self.assertEqual(errors, [])
        fig = Figure()
        ax = fig.add_subplot(111)
        render_chart(ax, spec, runs)
        labels = [line.get_label() for line in ax.get_lines()]
        self.assertEqual(labels, [spec.curves[0].label])
        self.assertEqual(ax.get_lines()[0].get_linestyle(), ":")
        self.assertEqual(ax.get_yscale(), "log")
        self.assertIn("Pa", ax.get_ylabel())

    def test_converts_units_onto_the_chart(self):
        path = self.dir / "mbar.csv"
        path.write_text(MBAR)
        spec = ChartSpec(units="Pa", yscale="linear")
        add_csv_paths(spec, [path])
        runs, _ = load_runs_for_spec(spec)
        fig = Figure()
        ax = fig.add_subplot(111)
        render_chart(ax, spec, runs)
        ys = ax.get_lines()[0].get_ydata()
        self.assertAlmostEqual(ys[0], 1.0e5)

    def test_manual_limits(self):
        path = self.dir / "run.csv"
        path.write_text(SAMPLE)
        spec = ChartSpec(xmin=0.0, xmax=1.5, ymin=50.0, ymax=2e5, yscale="linear")
        add_csv_paths(spec, [path])
        runs, _ = load_runs_for_spec(spec)
        fig = Figure()
        ax = fig.add_subplot(111)
        render_chart(ax, spec, runs)
        self.assertEqual(ax.get_xlim(), (0.0, 1.5))
        self.assertEqual(ax.get_ylim(), (50.0, 2e5))


if __name__ == "__main__":
    unittest.main()
