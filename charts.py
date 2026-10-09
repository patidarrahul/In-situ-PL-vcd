"""Saved comparison charts: curve lists, axis details, JSON round-trip."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.ticker import LogFormatterSciNotation, LogLocator

# Blue through green and yellow to red. The violet end of rainbow is left out.
BLUE_RED = LinearSegmentedColormap.from_list(
    "blue_red",
    ["#1d4ed8", "#0891b2", "#16a34a", "#eab308", "#dc2626"],
)

from runs import CURVE_COLORS, Run, color_for_index, load_run_csv, thin_points

TO_PA = {"Pa": 1.0, "mbar": 100.0, "Torr": 133.322368}
UNITS = ("Pa", "mbar", "Torr")
LEGEND_LOCS = (
    "best",
    "upper right",
    "upper left",
    "lower right",
    "lower left",
    "center",
)
# Display label → matplotlib linestyle code.
LINESTYLES = {"Solid": "-", "Dotted": ":"}
LINESTYLE_LABELS = {code: label for label, code in LINESTYLES.items()}
MAX_PLOT_POINTS = 4000


def normalize_linestyle(value: str | None) -> str:
    if not value:
        return "-"
    if value in LINESTYLES:
        return LINESTYLES[value]
    if value in LINESTYLE_LABELS:
        return value
    return "-"


def convert_pressure(value: float, from_units: str, to_units: str) -> float:
    src = TO_PA.get(from_units, TO_PA["Pa"])
    dst = TO_PA.get(to_units, TO_PA["Pa"])
    return value * src / dst


def unique_label(base: str, existing: Sequence[str]) -> str:
    if base not in existing:
        return base
    n = 2
    while f"{base} ({n})" in existing:
        n += 1
    return f"{base} ({n})"


def _opt_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_curve_path(path: str | Path, relative_to: str | Path | None = None) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_file():
        return candidate
    if relative_to is not None:
        anchored = (Path(relative_to).expanduser().resolve().parent / path).resolve()
        if anchored.is_file():
            return anchored
    return candidate


def path_for_save(path: str | Path, json_path: str | Path) -> str:
    """Prefer a path relative to the chart file so the JSON travels with the CSVs."""
    try:
        return str(
            Path(path).expanduser().resolve().relative_to(
                Path(json_path).expanduser().resolve().parent
            )
        )
    except ValueError:
        return str(Path(path).expanduser().resolve())


@dataclass
class CurveSpec:
    path: str
    label: str = ""
    color: str = CURVE_COLORS[0]
    visible: bool = True
    linestyle: str = "-"

    def with_defaults(self, index: int) -> "CurveSpec":
        label = self.label or Path(self.path).stem
        color = self.color or color_for_index(index)
        return CurveSpec(
            path=self.path,
            label=label,
            color=color,
            visible=self.visible,
            linestyle=normalize_linestyle(self.linestyle),
        )


@dataclass
class ChartSpec:
    title: str = "Pump-down curves"
    xlabel: str = "Time (s)"
    ylabel: str = ""
    units: str = "Pa"
    yscale: str = "log"
    xscale: str = "linear"
    xmin: Optional[float] = None
    xmax: Optional[float] = None
    ymin: Optional[float] = None
    ymax: Optional[float] = None
    legend: bool = True
    legend_loc: str = "best"
    grid: bool = True
    line_width: float = 1.8
    curves: list[CurveSpec] = field(default_factory=list)
    pl_series: list[CurveSpec] = field(default_factory=list)
    # Older chart files may still carry these. The PL view is a colormap.
    wavelength_nm: Optional[float] = None
    cursor_s: Optional[float] = None
    # Display window only. Saved spectra still contain every pixel.
    wl_min: Optional[float] = None
    wl_max: Optional[float] = None

    def ylabel_text(self) -> str:
        if self.ylabel.strip():
            return self.ylabel
        return f"Pressure ({self.units})"

    def to_dict(self, json_path: str | Path | None = None) -> dict:
        payload = asdict(self)
        if json_path is not None:
            for key in ("curves", "pl_series"):
                for curve in payload[key]:
                    curve["path"] = path_for_save(curve["path"], json_path)
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> "ChartSpec":
        curves = _curves_from(data.get("curves"))
        pl_series = _curves_from(data.get("pl_series"))
        units = str(data.get("units") or "Pa")
        if units not in UNITS:
            units = "Pa"
        yscale = str(data.get("yscale") or "log")
        if yscale not in ("log", "linear"):
            yscale = "log"
        loc = str(data.get("legend_loc") or "best")
        if loc not in LEGEND_LOCS:
            loc = "best"
        try:
            width = float(data.get("line_width", 1.8))
        except (TypeError, ValueError):
            width = 1.8
        return cls(
            title=str(data.get("title", "Pump-down curves")),
            xlabel=str(data.get("xlabel", "Time (s)")),
            ylabel=str(data.get("ylabel", "")),
            units=units,
            yscale=yscale,
            xscale="log" if data.get("xscale") == "log" else "linear",
            xmin=_opt_float(data.get("xmin")),
            xmax=_opt_float(data.get("xmax")),
            ymin=_opt_float(data.get("ymin")),
            ymax=_opt_float(data.get("ymax")),
            legend=bool(data.get("legend", True)),
            legend_loc=loc,
            grid=bool(data.get("grid", True)),
            line_width=max(0.4, min(6.0, width)),
            curves=curves,
            pl_series=pl_series,
            wavelength_nm=_opt_float(data.get("wavelength_nm")),
            cursor_s=_opt_float(data.get("cursor_s")),
            wl_min=_opt_float(data.get("wl_min")),
            wl_max=_opt_float(data.get("wl_max")),
        )


def save_chart(spec: ChartSpec, path: str | Path) -> None:
    json_path = Path(path)
    json_path.write_text(json.dumps(spec.to_dict(json_path), indent=2))


def load_chart(path: str | Path) -> ChartSpec:
    json_path = Path(path)
    data = json.loads(json_path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{json_path.name} is not a chart file")
    spec = ChartSpec.from_dict(data)
    for curve in [*spec.curves, *spec.pl_series]:
        curve.path = str(resolve_curve_path(curve.path, json_path))
    return spec


def _curves_from(raw) -> list[CurveSpec]:
    curves = []
    for index, item in enumerate(raw or []):
        if not isinstance(item, dict):
            continue
        curves.append(
            CurveSpec(
                path=str(item.get("path", "")),
                label=str(item.get("label", "")),
                color=str(item.get("color") or color_for_index(index)),
                visible=bool(item.get("visible", True)),
                linestyle=normalize_linestyle(str(item.get("linestyle") or "-")),
            ).with_defaults(index)
        )
    return curves


def add_csv_paths(spec: ChartSpec, paths: Sequence[str | Path]) -> list[str]:
    """Append CSVs to the spec. Returns error messages for files that fail to load."""
    errors: list[str] = []
    existing_labels = [curve.label for curve in spec.curves]
    existing_paths = {str(Path(curve.path).expanduser().resolve()) for curve in spec.curves}
    for raw in paths:
        path = Path(raw).expanduser()
        key = str(path.resolve()) if path.exists() else str(path)
        if key in existing_paths:
            continue
        try:
            run = load_run_csv(path)
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
            continue
        index = len(spec.curves)
        label = unique_label(run.label, existing_labels)
        spec.curves.append(
            CurveSpec(
                path=str(path.resolve()),
                label=label,
                color=color_for_index(index),
            )
        )
        existing_labels.append(label)
        existing_paths.add(key)
    return errors


def load_runs_for_spec(spec: ChartSpec) -> tuple[dict[str, Run], list[str]]:
    runs: dict[str, Run] = {}
    errors: list[str] = []
    for curve in spec.curves:
        try:
            runs[curve.path] = load_run_csv(curve.path)
        except (OSError, ValueError) as exc:
            errors.append(f"{Path(curve.path).name}: {exc}")
    return runs, errors


def render_chart(ax, spec: ChartSpec, runs: dict[str, Run]) -> None:
    """Draw every visible curve onto an existing Axes and apply chart details."""
    ax.clear()
    ax.set_title(spec.title, fontsize=13, pad=10, color="#111a27")
    ax.set_xlabel(spec.xlabel, fontsize=11, labelpad=8, color="#6b7684")
    ax.set_ylabel(spec.ylabel_text(), fontsize=11, labelpad=8, color="#6b7684")
    ax.set_xscale(spec.xscale)
    ax.set_yscale(spec.yscale)
    if spec.yscale == "log":
        ax.yaxis.set_major_locator(LogLocator(base=10.0))
        ax.yaxis.set_major_formatter(LogFormatterSciNotation(base=10.0))
        ax.yaxis.set_minor_locator(
            LogLocator(base=10.0, subs=tuple(range(2, 10)), numticks=100)
        )
    if spec.grid:
        ax.grid(True, which="major", color="#e6ebf2", linewidth=0.9)
        ax.grid(True, which="minor", color="#f0f3f8", linewidth=0.7)
    else:
        ax.grid(False)

    log_y = spec.yscale == "log"
    for curve in spec.curves:
        if not curve.visible:
            continue
        run = runs.get(curve.path)
        if run is None:
            continue
        pairs = []
        for t, p in zip(run.times, run.pressures):
            pressure = convert_pressure(p, run.units, spec.units)
            if log_y and pressure <= 0:
                continue
            pairs.append((t, pressure))
        pairs = thin_points(pairs, MAX_PLOT_POINTS)
        if not pairs:
            continue
        xs, ys = zip(*pairs)
        ax.plot(
            xs,
            ys,
            color=curve.color,
            lw=spec.line_width,
            linestyle=curve.linestyle,
            solid_capstyle="round",
            label=curve.label,
        )

    ax.tick_params(colors="#6b7684", labelsize=10)
    for side, spine in ax.spines.items():
        spine.set_visible(side in ("left", "bottom"))
        spine.set_color("#dfe6ef")
    ax.relim()
    ax.autoscale_view()
    if spec.xmin is not None or spec.xmax is not None:
        ax.set_xlim(left=spec.xmin, right=spec.xmax)
    if spec.ymin is not None or spec.ymax is not None:
        ax.set_ylim(bottom=spec.ymin, top=spec.ymax)
    legend = ax.get_legend()
    if legend is not None:
        legend.remove()
    if spec.legend and ax.get_lines():
        ax.legend(frameon=False, fontsize=9, loc=spec.legend_loc, labelcolor="#6b7684")


@dataclass
class PlSeries:
    """One saved PL time series, plus the pump-down trace when the file has one."""

    label: str
    wavelengths: np.ndarray
    elapsed_s: np.ndarray
    intensities: np.ndarray
    pressure_elapsed_s: Optional[np.ndarray] = None
    pressure: Optional[np.ndarray] = None
    pressure_units: str = "Pa"
    source: str = ""


@dataclass
class PlSpectrum:
    """One saved spectrum from Record PL. Pressure for that moment stays in the CSV."""

    label: str
    wavelengths: np.ndarray
    intensities: np.ndarray
    source: str = ""


def wavelength_limits(
    lo: Optional[float], hi: Optional[float]
) -> tuple[Optional[float], Optional[float]]:
    """Ordered display bounds. ``None`` means that side is open."""
    if lo is not None and hi is not None and lo > hi:
        return hi, lo
    return lo, hi


def wavelength_indices(
    wavelengths, lo: Optional[float] = None, hi: Optional[float] = None
) -> np.ndarray:
    """Pixel indexes inside ``lo``–``hi``. An empty window falls back to every pixel."""
    wl = np.asarray(wavelengths, dtype=float).reshape(-1)
    lo, hi = wavelength_limits(lo, hi)
    if lo is None and hi is None:
        return np.arange(wl.size)
    mask = np.ones(wl.size, dtype=bool)
    if lo is not None:
        mask &= wl >= lo
    if hi is not None:
        mask &= wl <= hi
    chosen = np.flatnonzero(mask)
    if chosen.size == 0:
        return np.arange(wl.size)
    return chosen


def _text_field(value: Any) -> str:
    if value is None:
        return ""
    arr = np.asarray(value)
    if arr.shape == ():
        return str(arr.item())
    if arr.size == 0:
        return ""
    return str(arr.reshape(-1)[0])


def _vector(archive, key: str) -> Optional[np.ndarray]:
    if key not in archive:
        return None
    arr = np.asarray(archive[key], dtype=float).reshape(-1)
    if arr.size == 0 or not np.any(np.isfinite(arr)):
        return None
    return arr


def load_pl_recording(path: str | Path) -> "PlSeries | PlSpectrum":
    """Load a Record PL spectrum or a PL time series."""
    npz_path = Path(path)
    try:
        with np.load(npz_path) as archive:
            kind = _text_field(archive["kind"]) if "kind" in archive else ""
            wavelengths = np.asarray(archive["wavelengths"], dtype=float).reshape(-1)
            intensities = np.asarray(archive["intensities"], dtype=float)
            elapsed = (
                np.asarray(archive["elapsed_s"], dtype=float).reshape(-1)
                if "elapsed_s" in archive
                else None
            )
            pressure = _vector(archive, "pressure")
            pressure_elapsed = _vector(archive, "pressure_elapsed_s")
            units = _text_field(archive["pressure_units"]) if "pressure_units" in archive else "Pa"
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError(f"{npz_path.name} is not a PL file ({exc})") from exc

    if units not in UNITS:
        units = "Pa"
    single = kind == "single" or intensities.ndim == 1
    if single:
        spectrum = np.asarray(intensities, dtype=float).reshape(-1)
        if spectrum.size != wavelengths.size or spectrum.size == 0:
            raise ValueError(f"{npz_path.name} has mismatched wavelengths")
        return PlSpectrum(
            label=npz_path.stem,
            wavelengths=wavelengths,
            intensities=spectrum,
            source=str(npz_path),
        )

    if intensities.ndim != 2 or intensities.shape[0] == 0:
        raise ValueError(f"{npz_path.name} is not a PL file")
    if elapsed is None:
        raise ValueError(f"{npz_path.name} has no elapsed_s")
    if intensities.shape[1] != wavelengths.size or elapsed.size != intensities.shape[0]:
        raise ValueError(f"{npz_path.name} has mismatched wavelengths or times")
    if (
        pressure is None
        or pressure_elapsed is None
        or pressure.size != pressure_elapsed.size
        or pressure.size < 1
    ):
        pressure = None
        pressure_elapsed = None
    return PlSeries(
        label=npz_path.stem,
        wavelengths=wavelengths,
        elapsed_s=elapsed,
        intensities=intensities,
        pressure_elapsed_s=pressure_elapsed,
        pressure=pressure,
        pressure_units=units,
        source=str(npz_path),
    )


def load_pl_series(path: str | Path) -> PlSeries:
    """Load a time-series ``.npz``. A single spectrum is rejected."""
    loaded = load_pl_recording(path)
    if not isinstance(loaded, PlSeries):
        raise ValueError(f"{Path(path).name} is one spectrum, not a time series")
    return loaded


def add_pl_paths(spec: ChartSpec, paths: Sequence[str | Path]) -> list[str]:
    """Append PL time series to the spec. Returns error messages for bad files."""
    errors: list[str] = []
    existing_labels = [curve.label for curve in spec.curves] + [
        curve.label for curve in spec.pl_series
    ]
    existing_paths = {
        str(Path(curve.path).expanduser().resolve())
        for curve in [*spec.curves, *spec.pl_series]
        if Path(curve.path).expanduser().exists()
    }
    for raw in paths:
        path = Path(raw).expanduser()
        key = str(path.resolve()) if path.exists() else str(path)
        if key in existing_paths:
            continue
        try:
            loaded = load_pl_recording(path)
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
            continue
        index = len(spec.curves) + len(spec.pl_series)
        label = unique_label(loaded.label, existing_labels)
        spec.pl_series.append(
            CurveSpec(
                path=str(path.resolve()),
                label=label,
                color=color_for_index(index),
            )
        )
        existing_labels.append(label)
        existing_paths.add(key)
    return errors


def load_pl_for_spec(
    spec: ChartSpec,
) -> tuple[dict[str, PlSeries], dict[str, PlSpectrum], list[str]]:
    series: dict[str, PlSeries] = {}
    spectra: dict[str, PlSpectrum] = {}
    errors: list[str] = []
    for curve in spec.pl_series:
        try:
            loaded = load_pl_recording(curve.path)
        except (OSError, ValueError) as exc:
            errors.append(f"{Path(curve.path).name}: {exc}")
            continue
        if isinstance(loaded, PlSeries):
            series[curve.path] = loaded
        else:
            spectra[curve.path] = loaded
    return series, spectra, errors


def _plot_pairs(ax, pairs, curve: CurveSpec, spec: ChartSpec) -> None:
    pairs = thin_points(pairs, MAX_PLOT_POINTS)
    if not pairs:
        return
    xs, ys = zip(*pairs)
    ax.plot(
        xs,
        ys,
        color=curve.color,
        lw=spec.line_width,
        linestyle=curve.linestyle,
        solid_capstyle="round",
        label=curve.label,
    )


def _style_panel(ax, *, title: str, xlabel: str, ylabel: str, yscale: str, grid: bool) -> None:
    ax.set_title(title, fontsize=12, pad=8, color="#111a27")
    ax.set_xlabel(xlabel, fontsize=11, labelpad=6, color="#6b7684")
    ax.set_ylabel(ylabel, fontsize=11, labelpad=8, color="#6b7684")
    ax.set_yscale(yscale)
    if yscale == "log":
        ax.yaxis.set_major_locator(LogLocator(base=10.0))
        ax.yaxis.set_major_formatter(LogFormatterSciNotation(base=10.0))
        ax.yaxis.set_minor_locator(
            LogLocator(base=10.0, subs=tuple(range(2, 10)), numticks=100)
        )
    if grid:
        ax.grid(True, which="major", color="#e6ebf2", linewidth=0.9)
        ax.grid(True, which="minor", color="#f0f3f8", linewidth=0.7)
    else:
        ax.grid(False)
    ax.tick_params(colors="#6b7684", labelsize=10)
    for side, spine in ax.spines.items():
        spine.set_visible(side in ("left", "bottom"))
        spine.set_color("#dfe6ef")


def _legend(ax, spec: ChartSpec) -> None:
    legend = ax.get_legend()
    if legend is not None:
        legend.remove()
    lines = [line for line in ax.get_lines() if not str(line.get_label()).startswith("_")]
    if spec.legend and lines:
        ax.legend(handles=lines, frameon=False, fontsize=9, loc=spec.legend_loc, labelcolor="#6b7684")


def _spectrogram(ax, series: PlSeries, wl_min: Optional[float] = None, wl_max: Optional[float] = None):
    """Time on x, wavelength on y, intensity as color. Returns the image."""
    elapsed = np.asarray(series.elapsed_s, dtype=float)
    wavelengths = np.asarray(series.wavelengths, dtype=float)
    values = np.asarray(series.intensities, dtype=float)
    time_order = np.argsort(elapsed)
    wave_order = np.argsort(wavelengths)
    elapsed = elapsed[time_order]
    wavelengths = wavelengths[wave_order]
    image = values[time_order][:, wave_order].T
    keep = wavelength_indices(wavelengths, wl_min, wl_max)
    wavelengths = wavelengths[keep]
    image = image[keep]
    t0, t1 = float(elapsed[0]), float(elapsed[-1])
    w0, w1 = float(wavelengths[0]), float(wavelengths[-1])
    if t1 <= t0:
        t1 = t0 + 1.0
    if w1 <= w0:
        w1 = w0 + 1.0
    return ax.imshow(
        image,
        aspect="auto",
        origin="lower",
        extent=(t0, t1, w0, w1),
        cmap=BLUE_RED,
        interpolation="nearest",
    )


def render_recording(
    fig,
    spec: ChartSpec,
    runs: dict[str, Run],
    series: dict[str, PlSeries],
    spectra: dict[str, PlSpectrum] | None = None,
) -> None:
    """Pressure CSV above PL.

    A time series is a blue-to-red colormap: time on x, wavelength on y, intensity
    in color. Record PL is one spectrum, intensity versus wavelength. Pressure
    stored in a time-series file is skipped when that file's CSV is already plotted.
    """
    spectra = spectra or {}
    visible_pl = [curve for curve in spec.pl_series if curve.visible and curve.path in series]
    visible_spectra = [
        curve for curve in spec.pl_series if curve.visible and curve.path in spectra
    ]
    if not visible_pl and not visible_spectra:
        return

    csv_stems = {Path(curve.path).stem for curve in spec.curves if curve.visible and curve.path in runs}
    has_pressure = any(curve.visible and curve.path in runs for curve in spec.curves) or any(
        series[curve.path].pressure is not None and Path(curve.path).stem not in csv_stems
        for curve in visible_pl
    )

    from matplotlib.gridspec import GridSpec

    heights = ([1.0] if has_pressure else []) + [2.4] * len(visible_pl)
    if visible_spectra:
        heights.append(1.6)
    n_cols = 2 if visible_pl else 1
    grid = GridSpec(
        len(heights),
        n_cols,
        figure=fig,
        height_ratios=heights,
        width_ratios=[24, 1] if n_cols == 2 else None,
    )

    ax_pressure = None
    shared = None
    row = 0
    if has_pressure:
        ax_pressure = fig.add_subplot(grid[0, 0])
        shared = ax_pressure
        row = 1

    heat_axes = []
    for offset, curve in enumerate(visible_pl):
        ax = fig.add_subplot(grid[row + offset, 0], sharex=shared)
        if shared is None:
            shared = ax
        last = offset == len(visible_pl) - 1
        _style_panel(
            ax,
            title=(
                spec.title
                if ax_pressure is None and len(visible_pl) == 1 and not visible_spectra
                else curve.label
            ),
            xlabel=spec.xlabel if last else "",
            ylabel="Wavelength (nm)",
            yscale="linear",
            grid=False,
        )
        image = _spectrogram(ax, series[curve.path], spec.wl_min, spec.wl_max)
        colorbar = fig.colorbar(image, cax=fig.add_subplot(grid[row + offset, 1]))
        colorbar.set_label("Intensity", color="#6b7684", fontsize=10)
        colorbar.ax.tick_params(colors="#6b7684", labelsize=9)
        if not last:
            ax.tick_params(labelbottom=False)
        heat_axes.append(ax)

    if visible_spectra:
        ax_spectrum = fig.add_subplot(grid[(1 if has_pressure else 0) + len(visible_pl), 0])
        alone = ax_pressure is None and not visible_pl and len(visible_spectra) == 1
        _style_panel(
            ax_spectrum,
            title=spec.title if alone else "PL spectrum",
            xlabel="Wavelength (nm)",
            ylabel="Intensity",
            yscale="linear",
            grid=spec.grid,
        )
        for curve in visible_spectra:
            loaded = spectra[curve.path]
            keep = wavelength_indices(loaded.wavelengths, spec.wl_min, spec.wl_max)
            shown_wl = np.asarray(loaded.wavelengths, dtype=float)[keep]
            shown_y = np.asarray(loaded.intensities, dtype=float)[keep]
            _plot_pairs(
                ax_spectrum,
                list(zip(shown_wl.tolist(), shown_y.tolist())),
                curve,
                spec,
            )
        _legend(ax_spectrum, spec)

    log_y = spec.yscale == "log"
    if ax_pressure is not None:
        _style_panel(
            ax_pressure,
            title=spec.title,
            xlabel="" if visible_pl else spec.xlabel,
            ylabel=spec.ylabel_text(),
            yscale=spec.yscale,
            grid=spec.grid,
        )
        if visible_pl:
            ax_pressure.tick_params(labelbottom=False)
        for curve in spec.curves:
            if not curve.visible:
                continue
            run = runs.get(curve.path)
            if run is None:
                continue
            pairs = []
            for t, p in zip(run.times, run.pressures):
                pressure = convert_pressure(p, run.units, spec.units)
                if log_y and pressure <= 0:
                    continue
                pairs.append((t, pressure))
            _plot_pairs(ax_pressure, pairs, curve, spec)
        for curve in visible_pl:
            loaded = series[curve.path]
            if loaded.pressure is None or Path(curve.path).stem in csv_stems:
                continue
            pairs = []
            for t, p in zip(loaded.pressure_elapsed_s, loaded.pressure):
                pressure = convert_pressure(float(p), loaded.pressure_units, spec.units)
                if log_y and pressure <= 0:
                    continue
                pairs.append((float(t), pressure))
            _plot_pairs(ax_pressure, pairs, curve, spec)
        ax_pressure.relim()
        ax_pressure.autoscale_view(scalex=False)
        _legend(ax_pressure, spec)
        if spec.ymin is not None or spec.ymax is not None:
            ax_pressure.set_ylim(bottom=spec.ymin, top=spec.ymax)

    if shared is not None and (spec.xmin is not None or spec.xmax is not None):
        shared.set_xlim(left=spec.xmin, right=spec.xmax)
    elif shared is not None and ax_pressure is not None:
        spans = []
        for ax in heat_axes:
            left, right = ax.images[0].get_extent()[:2]
            spans.append((left, right))
        xs = [
            float(x)
            for line in ax_pressure.get_lines()
            for x in line.get_xdata()
            if np.isfinite(x)
        ]
        if xs:
            spans.append((min(xs), max(xs)))
        if spans:
            lo = min(span[0] for span in spans)
            hi = max(span[1] for span in spans)
            if hi <= lo:
                hi = lo + 1.0
            shared.set_xlim(lo, hi)

    lo, hi = wavelength_limits(spec.wl_min, spec.wl_max)
    if lo is not None or hi is not None:
        for ax in heat_axes:
            bottom, top = ax.get_ylim()
            ax.set_ylim(lo if lo is not None else bottom, hi if hi is not None else top)
        if visible_spectra:
            left, right = ax_spectrum.get_xlim()
            ax_spectrum.set_xlim(lo if lo is not None else left, hi if hi is not None else right)
