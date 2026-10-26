"""Multi-run figure used by Compare plots.

Each PL time series is one row: the colormap, pressure curve, and peak trace
on the left, and the spectra colored by time on the right. A summary row plots
onset time and the last spectrum's peak wavelength against run number.
"""

from __future__ import annotations

import re
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import MultipleLocator

from charts import PlSeries


def run_number(label: str, source: str = "") -> int | None:
    """The ``run12`` number in a file name, if it has one."""
    for text in (source, label):
        match = re.search(r"run(\d+)", Path(text).stem, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _ordered(items: list[tuple[str, PlSeries]]) -> list[tuple[str, PlSeries, int | None]]:
    numbered = [(label, series, run_number(label, series.source)) for label, series in items]
    numbered.sort(key=lambda item: (item[2] is None, item[2] or 0, item[0].lower()))
    return numbered


def _display_limits(items, t_min, t_max, w_min, w_max):
    times = np.concatenate([np.asarray(series.elapsed_s, dtype=float) for _label, series in items])
    waves = np.concatenate([np.asarray(series.wavelengths, dtype=float) for _label, series in items])
    finite_t = times[np.isfinite(times)]
    finite_w = waves[np.isfinite(waves)]
    left = float(np.min(finite_t)) if t_min is None else float(t_min)
    right = float(np.max(finite_t)) if t_max is None else float(t_max)
    bottom = float(np.min(finite_w)) if w_min is None else float(w_min)
    top = float(np.max(finite_w)) if w_max is None else float(w_max)
    if right <= left:
        right = left + 1.0
    if top <= bottom:
        top = bottom + 1.0
    return left, right, bottom, top


def _time_ticks(t_min: float, t_max: float) -> np.ndarray:
    span = max(t_max - t_min, 1.0)
    step = 5.0
    while span / step > 8:
        step *= 2
    start = np.ceil(t_min / step) * step
    return np.arange(start, t_max + 1e-9, step)


def _plot_image(series: PlSeries, ax, *, t_min, t_max, w_min, w_max, min_peak_intensity):
    """Colormap with the pressure curve and the peak-wavelength trace."""
    wavelengths = np.asarray(series.wavelengths, dtype=float)
    time = np.asarray(series.elapsed_s, dtype=float)
    intensities = np.asarray(series.intensities, dtype=float)
    image = intensities.T

    in_time = (time >= t_min) & (time <= t_max)
    in_wave = (wavelengths >= w_min) & (wavelengths <= w_max)
    if not np.any(in_time) or not np.any(in_wave):
        ax.set_axis_off()
        ax.set_title("No data in the time/wavelength window")
        return None, np.nan

    window = image[np.ix_(in_wave, in_time)]
    wavelengths_roi = wavelengths[in_wave]
    time_roi = time[in_time]
    im = ax.imshow(
        window,
        aspect="auto",
        origin="lower",
        extent=(time_roi[0], time_roi[-1], wavelengths_roi[0], wavelengths_roi[-1]),
        cmap="turbo",
    )

    pressure_line = None
    if series.pressure is not None and series.pressure_elapsed_s is not None:
        pump_down_time = np.asarray(series.pressure_elapsed_s, dtype=float)
        pump_down_pressure = np.asarray(series.pressure, dtype=float)
        kept = (
            (pump_down_time >= t_min)
            & (pump_down_time <= t_max)
            & (pump_down_pressure > 0)
        )
        if np.any(kept):
            ax_pressure = ax.twinx()
            pressure_line, = ax_pressure.plot(
                pump_down_time[kept],
                pump_down_pressure[kept],
                color="#ffd166",
                lw=1.6,
            )
            ax_pressure.set_yscale("log")
            ax_pressure.yaxis.tick_left()
            ax_pressure.yaxis.set_label_position("left")
            ax_pressure.set_ylabel(f"Pressure ({series.pressure_units})")

    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")
    ax.set_ylabel("Wavelength (nm)")
    ax.yaxis.set_major_locator(MultipleLocator(50))
    ax.yaxis.set_minor_locator(MultipleLocator(10))
    ax.set_xlabel("Time (s)")
    ax.xaxis.set_major_locator(MultipleLocator(5))
    ax.xaxis.set_minor_locator(MultipleLocator(1))
    ax.set_xlim(t_min, t_max)

    intensity_roi = intensities[in_time][:, in_wave]
    index_max_intensity = np.argmax(intensity_roi, axis=1)
    peak_counts = intensity_roi[np.arange(intensity_roi.shape[0]), index_max_intensity]
    peak_wavelengths = np.where(
        peak_counts > min_peak_intensity,
        wavelengths_roi[index_max_intensity],
        wavelengths_roi[0],
    )
    peak_line, = ax.plot(time_roi, peak_wavelengths, color="white", lw=1.4, zorder=4)

    onset_s = np.nan
    if len(peak_wavelengths) > 1:
        change_at = int(np.argmax(np.diff(peak_wavelengths))) + 1
        onset_s = float(time_roi[change_at])
        ax.plot(
            time_roi[change_at],
            peak_wavelengths[change_at],
            marker="o",
            color="white",
            markeredgecolor="black",
            zorder=5,
        )
        ax.text(
            time_roi[change_at] + 1,
            peak_wavelengths[change_at],
            f"{onset_s:.2f} s",
            color="white",
            size=8,
        )

    handles = [peak_line]
    labels = ["Peak position"]
    if pressure_line is not None:
        handles.append(pressure_line)
        labels.append("Pump-down pressure")
    ax.legend(
        handles,
        labels,
        loc="upper center",
        facecolor="none",
        edgecolor="none",
        labelcolor="white",
        fontsize=8,
    )
    return im, onset_s


def _plot_spectra(series: PlSeries, ax, *, t_min, t_max, w_min, w_max, min_peak_intensity):
    """Every spectrum in the window, colored by time."""
    wavelengths = np.asarray(series.wavelengths, dtype=float)
    time = np.asarray(series.elapsed_s, dtype=float)
    intensities = np.asarray(series.intensities, dtype=float)
    in_time = (time >= t_min) & (time <= t_max)
    in_wave = (wavelengths >= w_min) & (wavelengths <= w_max)
    if not np.any(in_time) or not np.any(in_wave):
        ax.set_axis_off()
        return None, np.nan

    waves = wavelengths[in_wave]
    times = time[in_time]
    spectra = intensities[in_time][:, in_wave]
    cmap = plt.cm.turbo
    norm = plt.Normalize(vmin=float(times.min()), vmax=float(times.max()))
    for spectrum, t in zip(spectra, times):
        ax.plot(waves, spectrum, color=cmap(norm(t)), lw=0.8)

    last = spectra[-1]
    peak = int(np.argmax(last))
    if float(last[peak]) > min_peak_intensity:
        last_peak_nm = float(waves[peak])
        ax.text(
            waves[peak] + 50,
            last[peak],
            f"{last_peak_nm:.1f} nm",
            color="brown",
            ha="center",
            va="bottom",
            fontsize=8,
        )
    else:
        last_peak_nm = 0.0

    ax.set_xlim(w_min, w_max)
    top = float(spectra.max())
    ax.set_ylim(0, top * 1.08 if top > 0 else 1)
    ax.set_yticks([])
    ax.set_xlabel("Wavelength (nm)")
    ax.set_ylabel("Intensity (a.u.)")
    return plt.cm.ScalarMappable(norm=norm, cmap=cmap), last_peak_nm


def _plot_summary(ax, labels, onset_times, last_peaks) -> None:
    x = np.arange(len(labels))
    time_line, = ax.plot(x, onset_times, marker="o", color="#1d4e89", lw=1.6)
    ax.set_ylabel("Onset time (s)")
    ax.set_xlabel("Run")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=40, ha="right")

    ax_wave = ax.twinx()
    wave_line, = ax_wave.plot(x, last_peaks, marker="s", color="#9a3412", lw=1.6)
    ax_wave.set_ylabel("Last spectrum peak (nm)")
    ax.legend(
        [time_line, wave_line],
        ["Onset time", "Last spectrum peak"],
        loc="best",
        edgecolor="none",
        facecolor="none",
    )


def render_run_pair(
    fig,
    title: str,
    series: PlSeries,
    *,
    t_min: float,
    t_max: float,
    w_min: float,
    w_max: float,
    min_peak_intensity: float = 100.0,
) -> tuple[float, float]:
    """One figure: colormap and pressure on the left, spectra on the right."""
    fig.clear()
    fig.set_layout_engine("constrained")
    ax_image = fig.add_subplot(1, 2, 1)
    ax_spectra = fig.add_subplot(1, 2, 2)
    image, onset_s = _plot_image(
        series,
        ax_image,
        t_min=t_min,
        t_max=t_max,
        w_min=w_min,
        w_max=w_max,
        min_peak_intensity=min_peak_intensity,
    )
    spectra, last_peak = _plot_spectra(
        series,
        ax_spectra,
        t_min=t_min,
        t_max=t_max,
        w_min=w_min,
        w_max=w_max,
        min_peak_intensity=min_peak_intensity,
    )
    ax_image.set_title(title, loc="left", fontsize=10)
    if image is not None:
        fig.colorbar(image, ax=ax_image, location="bottom", label="Intensity", pad=0.02)
    if spectra is not None:
        fig.colorbar(spectra, ax=ax_spectra, label="Time (s)", ticks=_time_ticks(t_min, t_max))
    return float(onset_s), float(last_peak)


def render_run_summary(fig, labels, onset_times, last_peaks) -> None:
    """Onset time and the last spectrum peak against run number."""
    fig.clear()
    fig.set_layout_engine("constrained")
    _plot_summary(fig.add_subplot(111), labels, onset_times, last_peaks)


def resolve_limits(items, t_min, t_max, w_min, w_max):
    """Shared time and wavelength window. Blank limits use the data range."""
    return _display_limits(items, t_min, t_max, w_min, w_max)


def render_run_gallery(
    fig,
    items: list[tuple[str, PlSeries]],
    *,
    t_min: float | None = None,
    t_max: float | None = None,
    w_min: float | None = None,
    w_max: float | None = None,
    min_peak_intensity: float = 100.0,
) -> None:
    """Draw one notebook-style row per series, then the onset summary."""
    fig.clear()
    if not items:
        ax = fig.add_subplot(111)
        ax.set_axis_off()
        ax.text(
            0.5,
            0.5,
            "Add PL time series, then choose Runs",
            ha="center",
            va="center",
            transform=ax.transAxes,
            color="#6b7684",
        )
        return

    t_min, t_max, w_min, w_max = _display_limits(items, t_min, t_max, w_min, w_max)
    ordered = _ordered(items)
    counts: dict[int, int] = {}
    for _label, _series, number in ordered:
        if number is not None:
            counts[number] = counts.get(number, 0) + 1

    fig.set_layout_engine("constrained")
    grid = fig.add_gridspec(len(ordered) + 1, 2, height_ratios=[1.0] * len(ordered) + [0.85])
    onset_times = []
    last_peaks = []
    tick_labels = []
    for row, (label, series, number) in enumerate(ordered):
        ax_image = fig.add_subplot(grid[row, 0])
        ax_spectra = fig.add_subplot(grid[row, 1])
        image, onset_s = _plot_image(
            series,
            ax_image,
            t_min=t_min,
            t_max=t_max,
            w_min=w_min,
            w_max=w_max,
            min_peak_intensity=min_peak_intensity,
        )
        spectra, last_peak = _plot_spectra(
            series,
            ax_spectra,
            t_min=t_min,
            t_max=t_max,
            w_min=w_min,
            w_max=w_max,
            min_peak_intensity=min_peak_intensity,
        )
        title = f"Run {number}" if number is not None and counts.get(number) == 1 else label
        ax_image.set_title(title, loc="left", fontsize=10)
        if image is not None:
            fig.colorbar(image, ax=ax_image, location="bottom", label="Intensity", pad=0.02)
        if spectra is not None:
            fig.colorbar(
                spectra,
                ax=ax_spectra,
                label="Time (s)",
                ticks=_time_ticks(t_min, t_max),
            )
        onset_times.append(onset_s)
        last_peaks.append(last_peak)
        tick_labels.append(title)

    _plot_summary(fig.add_subplot(grid[-1, :]), tick_labels, onset_times, last_peaks)
