"""Unit tests for comparison-chart specs and rendering."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
from matplotlib.figure import Figure

import numpy as np

from charts import (
    ChartSpec,
    add_csv_paths,
    add_pl_paths,
    convert_pressure,
    load_chart,
    load_pl_for_spec,
    load_runs_for_spec,
    normalize_linestyle,
    render_chart,
    render_recording,
    save_chart,
    unique_label,
    wavelength_indices,
    wavelength_limits,
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


def _data_lines(ax):
    return [line for line in ax.get_lines() if not str(line.get_label()).startswith("_")]


class TestPlSeries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.wavelengths = np.linspace(400, 800, 5)
        self.intensities = np.array(
            [
                [0, 1, 5, 1, 0],
                [0, 2, 9, 2, 0],
                [0, 1, 4, 1, 0],
            ],
            dtype=float,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _write_series(self, name: str, with_pressure: bool = True) -> Path:
        data = {
            "kind": np.array("timeseries"),
            "wavelengths": self.wavelengths,
            "intensities": self.intensities,
            "elapsed_s": np.array([0.0, 1.0, 2.0]),
        }
        if with_pressure:
            data["pressure_elapsed_s"] = np.array([0.0, 1.0, 2.0])
            data["pressure"] = np.array([1.0e5, 1.0e3, 1.0e1])
            data["pressure_units"] = np.array("Pa")
        path = self.dir / name
        np.savez(path, **data)
        return path

    def test_single_spectrum_plots_with_a_pressure_csv(self):
        path = self.dir / "one.npz"
        np.savez(
            path,
            kind=np.array("single"),
            wavelengths=self.wavelengths,
            intensities=self.intensities[0],
        )
        csv_path = self.dir / "run.csv"
        csv_path.write_text(SAMPLE)
        spec = ChartSpec()
        add_pl_paths(spec, [path])
        add_csv_paths(spec, [csv_path])
        series, spectra, errors = load_pl_for_spec(spec)
        self.assertEqual(errors, [])
        self.assertEqual(series, {})
        runs, _ = load_runs_for_spec(spec)
        fig = Figure()
        render_recording(fig, spec, runs, series, spectra)
        pressure = fig.axes[0]
        spectrum = next(ax for ax in fig.axes if ax.get_xlabel() == "Wavelength (nm)")
        self.assertEqual(list(_data_lines(pressure)[0].get_ydata()), [1.0e5, 1.0e3, 100.0])
        self.assertEqual(list(_data_lines(spectrum)[0].get_ydata()), [0, 1, 5, 1, 0])
        self.assertFalse(any(ax.images for ax in fig.axes))

    def test_recording_stacks_pressure_pl_and_spectrum(self):
        path = self._write_series("linked.npz")
        spec = ChartSpec(wavelength_nm=600, xmin=0.0, xmax=2.0, ymin=1.0, ymax=1.0e6)
        errors = add_pl_paths(spec, [path, path])
        self.assertEqual(errors, [])
        self.assertEqual(len(spec.pl_series), 1)
        series, _spectra, load_errors = load_pl_for_spec(spec)
        self.assertEqual(load_errors, [])
        fig = Figure()
        render_recording(fig, spec, {}, series)
        self.assertEqual(len(fig.axes), 3)
        pressure = fig.axes[0]
        heat = next(ax for ax in fig.axes if ax.images)
        image = np.asarray(heat.images[0].get_array())
        self.assertEqual(pressure.get_yscale(), "log")
        self.assertEqual(list(_data_lines(pressure)[0].get_ydata()), [1.0e5, 1.0e3, 10.0])
        self.assertEqual(image.shape, (5, 3))
        self.assertEqual(float(image[2, 1]), 9.0)
        cmap = heat.images[0].get_cmap()
        self.assertEqual(cmap.name, "blue_red")
        low = cmap(0.0)
        high = cmap(1.0)
        self.assertGreater(low[2], low[0])
        self.assertGreater(high[0], high[2])
        self.assertEqual(tuple(heat.images[0].get_extent()), (0.0, 2.0, 400.0, 800.0))
        self.assertEqual(heat.get_ylabel(), "Wavelength (nm)")
        self.assertEqual(heat.get_xlabel(), "Time (s)")
        self.assertEqual(heat.get_xlim(), (0.0, 2.0))
        self.assertEqual(pressure.get_xlim(), (0.0, 2.0))
        self.assertEqual(pressure.get_ylim(), (1.0, 1.0e6))

    def test_wavelength_window_limits_the_colormap(self):
        self.assertEqual(wavelength_limits(700, 400), (400, 700))
        indexes = wavelength_indices(self.wavelengths, 450, 650)
        self.assertEqual(list(self.wavelengths[indexes]), [500, 600])
        self.assertEqual(list(wavelength_indices(self.wavelengths, 10, 20)), list(range(5)))
        path = self._write_series("linked.npz")
        spec = ChartSpec(wl_min=450, wl_max=650)
        add_pl_paths(spec, [path])
        series, _spectra, _errors = load_pl_for_spec(spec)
        fig = Figure()
        render_recording(fig, spec, {}, series)
        heat = next(ax for ax in fig.axes if ax.images)
        image = np.asarray(heat.images[0].get_array())
        self.assertEqual(image.shape, (2, 3))
        self.assertEqual(float(image[1, 1]), 9.0)
        self.assertEqual(heat.get_ylim(), (450, 650))

    def test_csv_replaces_pressure_stored_in_the_pl_file(self):
        npz = self._write_series("batch.csv".replace(".csv", ".npz"))
        csv_path = self.dir / "batch.csv"
        csv_path.write_text(SAMPLE)
        spec = ChartSpec(yscale="linear")
        add_csv_paths(spec, [csv_path])
        add_pl_paths(spec, [npz])
        runs, _ = load_runs_for_spec(spec)
        series, _spectra, _ = load_pl_for_spec(spec)
        fig = Figure()
        render_recording(fig, spec, runs, series)
        pressure = fig.axes[0]
        self.assertEqual(len(_data_lines(pressure)), 1)
        self.assertEqual(pressure.get_lines()[0].get_label(), "batch")

    def test_pl_without_pressure_skips_that_panel(self):
        path = self._write_series("only_pl.npz", with_pressure=False)
        spec = ChartSpec()
        add_pl_paths(spec, [path])
        series, _spectra, _ = load_pl_for_spec(spec)
        fig = Figure()
        render_recording(fig, spec, {}, series)
        heat = next(ax for ax in fig.axes if ax.images)
        self.assertEqual(len(fig.axes), 2)
        self.assertEqual(heat.get_ylabel(), "Wavelength (nm)")
        self.assertEqual(np.asarray(heat.images[0].get_array()).shape, (5, 3))

    def test_chart_json_keeps_the_pl_series(self):
        npz = self._write_series("run.npz")
        spec = ChartSpec(wavelength_nm=642, cursor_s=1.5)
        add_pl_paths(spec, [npz])
        json_path = self.dir / "chart.json"
        save_chart(spec, json_path)
        payload = json.loads(json_path.read_text())
        self.assertEqual(payload["pl_series"][0]["path"], "run.npz")
        self.assertEqual(payload["wavelength_nm"], 642)
        loaded = load_chart(json_path)
        self.assertEqual(Path(loaded.pl_series[0].path).resolve(), npz.resolve())
        self.assertEqual(loaded.wavelength_nm, 642)
        self.assertEqual(loaded.cursor_s, 1.5)


if __name__ == "__main__":
    unittest.main()
