"""Saved comparison charts: curve lists, axis details, JSON round-trip."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from matplotlib.ticker import LogFormatterSciNotation, LogLocator

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

    def ylabel_text(self) -> str:
        if self.ylabel.strip():
            return self.ylabel
        return f"Pressure ({self.units})"

    def to_dict(self, json_path: str | Path | None = None) -> dict:
        payload = asdict(self)
        if json_path is not None:
            for curve in payload["curves"]:
                curve["path"] = path_for_save(curve["path"], json_path)
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> "ChartSpec":
        raw_curves = data.get("curves") or []
        curves = []
        for index, item in enumerate(raw_curves):
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
    for curve in spec.curves:
        curve.path = str(resolve_curve_path(curve.path, json_path))
    return spec


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
