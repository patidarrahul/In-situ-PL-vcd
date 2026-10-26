"""The Compare plots Runs figure: one image/spectra row per file, then onset."""

import unittest

import numpy as np
from matplotlib.figure import Figure

from charts import PlSeries
from gallery import render_run_gallery, render_run_pair, run_number


def _series(name, intensities, times, pressure=True):
    data = dict(
        label=name,
        wavelengths=np.array([500.0, 600.0, 700.0, 800.0]),
        elapsed_s=np.asarray(times, dtype=float),
        intensities=np.asarray(intensities, dtype=float),
        source=name,
    )
    if pressure:
        data["pressure_elapsed_s"] = np.asarray(times, dtype=float)
        data["pressure"] = np.array([1.0e5, 1.0e3, 10.0][: len(times)], dtype=float)
        data["pressure_units"] = "Pa"
    return PlSeries(**data)


class TestRunGallery(unittest.TestCase):
    def test_run_number_reads_the_file_name(self):
        self.assertEqual(run_number("notes", r"C:\data\batch_run10.npz"), 10)
        self.assertIsNone(run_number("dark"))

    def test_rows_follow_run_number_and_mark_the_peak_jump(self):
        early = _series(
            "batch_run10.npz",
            [[0, 20, 0, 0], [0, 0, 0, 300]],
            [0.0, 8.0],
        )
        later_name_first = _series(
            "batch_run2.npz",
            [[40, 0, 0, 0], [0, 0, 250, 0], [0, 0, 0, 400]],
            [0.0, 5.0, 10.0],
        )
        fig = Figure()
        render_run_gallery(
            fig,
            [("run 10", early), ("run 2", later_name_first)],
            t_min=0,
            t_max=10,
            w_min=500,
            w_max=800,
            min_peak_intensity=100,
        )
        titles = [ax.get_title(loc="left") for ax in fig.axes if ax.get_title(loc="left")]
        self.assertEqual(titles, ["Run 2", "Run 10"])

        images = [ax for ax in fig.axes if ax.images]
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0].images[0].get_cmap().name, "turbo")
        # Run 2 is first. Its low-count frame stays at 500 nm, then jumps to 700.
        peak = [line for line in images[0].get_lines() if line.get_color() == "white"][0]
        self.assertEqual(list(peak.get_ydata()), [500.0, 700.0, 800.0])
        pressure = next(ax for ax in fig.axes if ax.get_ylabel().startswith("Pressure"))
        self.assertEqual(pressure.get_yscale(), "log")

        spectra = next(ax for ax in fig.axes if ax.get_xlabel() == "Wavelength (nm)")
        self.assertEqual(len(spectra.get_lines()), 3)

        summary = next(ax for ax in fig.axes if ax.get_ylabel() == "Onset time (s)")
        self.assertEqual([tick.get_text() for tick in summary.get_xticklabels()], ["Run 2", "Run 10"])
        self.assertEqual(list(summary.get_lines()[0].get_ydata()), [5.0, 8.0])
        final_peak = next(ax for ax in fig.axes if ax.get_ylabel() == "Last spectrum peak (nm)")
        self.assertEqual(list(final_peak.get_lines()[0].get_ydata()), [800.0, 800.0])

    def test_one_pair_is_its_own_figure(self):
        series = _series(
            "batch_run2.npz",
            [[40, 0, 0, 0], [0, 0, 250, 0], [0, 0, 0, 400]],
            [0.0, 5.0, 10.0],
        )
        fig = Figure()
        onset, last_peak = render_run_pair(
            fig,
            "Run 2",
            series,
            t_min=0,
            t_max=10,
            w_min=500,
            w_max=800,
            min_peak_intensity=100,
        )
        self.assertEqual(onset, 5.0)
        self.assertEqual(last_peak, 800.0)
        self.assertEqual(len([ax for ax in fig.axes if ax.images]), 1)
        self.assertEqual(fig.axes[0].get_title(loc="left"), "Run 2")

    def test_empty_gallery_asks_for_files(self):
        fig = Figure()
        render_run_gallery(fig, [])
        self.assertIn("Add PL time series", fig.axes[0].texts[0].get_text())


if __name__ == "__main__":
    unittest.main()
