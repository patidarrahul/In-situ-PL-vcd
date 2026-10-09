"""Comparison-plot window: overlay CSVs, edit chart details, save figure + spec."""

from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog

import customtkinter as ctk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from charts import (
    LEGEND_LOCS,
    LINESTYLE_LABELS,
    LINESTYLES,
    UNITS,
    ChartSpec,
    add_csv_paths,
    add_pl_paths,
    load_chart,
    load_pl_for_spec,
    load_runs_for_spec,
    normalize_linestyle,
    render_chart,
    render_recording,
    save_chart,
    wavelength_limits,
)
from config import remember_browse_dir, resolve_browse_dir, resolve_data_dir, save_settings
from ui import (
    ACCENT,
    ACCENT_HOVER,
    BORDER,
    CARD,
    ERROR,
    FIELD,
    FIELD_HOVER,
    HEAD_FONT,
    MUTED,
    ON_ACCENT,
    PAGE,
    TEXT,
    SegmentedToggle,
)


class PlotterWindow(ctk.CTkToplevel):
    def __init__(self, master, data_dir: str | Path, settings: dict | None = None):
        super().__init__(master)
        self.title("Compare plots")
        self.geometry("1280x820")
        self.minsize(980, 680)
        self.configure(fg_color=PAGE)
        self.transient(master)

        self.data_dir = resolve_data_dir(data_dir)
        self.settings = settings if settings is not None else {}
        self.spec = ChartSpec()
        self._runs = {}
        self._pl = {}
        self._spectra = {}
        self._loading = False
        self._redraw_job = None
        self._curve_vars: list[tk.StringVar] = []

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self.lift)
        self.after(80, self._redraw)

    def _browse_dir(self) -> str:
        return str(
            resolve_browse_dir(
                self.settings.get("last_browse_dir"), fallback=self.data_dir
            )
        )

    def _remember_path(self, path: str | Path) -> None:
        remember_browse_dir(self.settings, path)

    def _on_close(self) -> None:
        if self._redraw_job is not None:
            try:
                self.after_cancel(self._redraw_job)
            except tk.TclError:
                pass
        self.destroy()

    def _card(self, parent) -> ctk.CTkFrame:
        return ctk.CTkFrame(
            parent, fg_color=CARD, corner_radius=12, border_width=1, border_color=BORDER
        )

    def _card_title(self, parent, text: str) -> None:
        ctk.CTkLabel(
            parent,
            text=text,
            text_color=TEXT,
            font=ctk.CTkFont(family=HEAD_FONT, size=14, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(14, 10))

    def _field_label(self, parent, text: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            parent,
            text=text,
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
        )

    def _entry(self, parent, variable: tk.StringVar) -> ctk.CTkEntry:
        entry = ctk.CTkEntry(
            parent,
            textvariable=variable,
            fg_color=FIELD,
            border_color=BORDER,
            border_width=1,
            text_color=TEXT,
            placeholder_text_color=MUTED,
            height=32,
        )
        entry.bind("<KeyRelease>", self._queue_redraw)
        return entry

    def _menu(self, parent, values: list[str], variable: tk.StringVar, command=None):
        return ctk.CTkOptionMenu(
            parent,
            values=values,
            variable=variable,
            command=command,
            fg_color=FIELD,
            button_color=FIELD_HOVER,
            button_hover_color=BORDER,
            text_color=TEXT,
            dropdown_fg_color=CARD,
            dropdown_hover_color=FIELD,
            dropdown_text_color=TEXT,
            height=32,
        )

    def _ghost_button(self, parent, text: str, command, width: int = 110) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            fg_color=CARD,
            hover_color=FIELD,
            text_color=TEXT,
            border_width=1,
            border_color=BORDER,
            height=32,
            width=width,
            font=ctk.CTkFont(family=HEAD_FONT, size=12),
        )

    def _primary_button(self, parent, text: str, command, width: int = 120) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color=ON_ACCENT,
            height=34,
            width=width,
            font=ctk.CTkFont(family=HEAD_FONT, size=13, weight="bold"),
        )

    def _build(self) -> None:
        root = ctk.CTkFrame(self, fg_color=PAGE)
        root.pack(fill="both", expand=True, padx=20, pady=18)
        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkScrollableFrame(
            root, fg_color="transparent", width=340, scrollbar_button_color=BORDER
        )
        sidebar.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        self._build_curves_card(sidebar)
        self._build_details_card(sidebar)
        self._build_file_card(sidebar)

        plot_card = self._card(root)
        plot_card.grid(row=0, column=1, sticky="nsew")
        self.fig = Figure(figsize=(8.2, 5.6), dpi=100, facecolor=CARD)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_card)
        widget = self.canvas.get_tk_widget()
        widget.configure(background=CARD, highlightthickness=0)
        widget.pack(fill="both", expand=True, padx=12, pady=12)

    def _build_curves_card(self, parent) -> None:
        card = self._card(parent)
        card.pack(fill="x")
        self._card_title(card, "Curves")
        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.pack(fill="x", padx=16, pady=(0, 8))
        self._primary_button(buttons, "Add CSVs…", self.add_csvs, width=148).pack(
            side="left", padx=(0, 8)
        )
        self._ghost_button(buttons, "Add PL…", self.add_pl, width=148).pack(side="left")
        self._ghost_button(card, "Remove all", self.remove_all, width=120).pack(
            anchor="w", padx=16, pady=(0, 8)
        )
        self.curves_box = ctk.CTkFrame(card, fg_color="transparent")
        self.curves_box.pack(fill="x", padx=10, pady=(0, 4))
        self.curves_hint = tk.StringVar(
            value="Add a pressure CSV, a single PL spectrum, or a PL time series"
        )
        ctk.CTkLabel(
            self.curves_box,
            textvariable=self.curves_hint,
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
            justify="left",
            wraplength=280,
        ).pack(fill="x", padx=6, pady=(0, 12))

    def _build_details_card(self, parent) -> None:
        card = self._card(parent)
        card.pack(fill="x", pady=(14, 0))
        self._card_title(card, "Chart details")

        self.title_var = tk.StringVar(value=self.spec.title)
        self.xlabel_var = tk.StringVar(value=self.spec.xlabel)
        self.ylabel_var = tk.StringVar(value=self.spec.ylabel)
        self.units_var = tk.StringVar(value=self.spec.units)
        self.legend_var = tk.StringVar(value=self.spec.legend_loc)
        self.width_var = tk.StringVar(value=str(self.spec.line_width))
        self.xmin_var = tk.StringVar(value="")
        self.xmax_var = tk.StringVar(value="")
        self.ymin_var = tk.StringVar(value="")
        self.ymax_var = tk.StringVar(value="")

        self._field_label(card, "Title").pack(fill="x", padx=16, pady=(0, 2))
        self._entry(card, self.title_var).pack(fill="x", padx=16)
        self._field_label(card, "X axis label").pack(fill="x", padx=16, pady=(10, 2))
        self._entry(card, self.xlabel_var).pack(fill="x", padx=16)
        self._field_label(card, "Y axis label (blank = Pressure (units))").pack(
            fill="x", padx=16, pady=(10, 2)
        )
        self._entry(card, self.ylabel_var).pack(fill="x", padx=16)

        self._field_label(card, "Units").pack(fill="x", padx=16, pady=(10, 2))
        self._menu(card, list(UNITS), self.units_var, command=self._queue_redraw).pack(
            fill="x", padx=16
        )

        self._field_label(card, "Y scale").pack(fill="x", padx=16, pady=(10, 2))
        self.yscale = SegmentedToggle(
            card, ["Log", "Linear"], command=self._queue_redraw, height=30
        )
        self.yscale.pack(fill="x", padx=16)
        if self.spec.yscale == "linear":
            self.yscale.set("Linear")

        limits = ctk.CTkFrame(card, fg_color="transparent")
        limits.pack(fill="x", padx=16, pady=(10, 0))
        left = ctk.CTkFrame(limits, fg_color="transparent")
        left.pack(side="left", expand=True, fill="x", padx=(0, 6))
        right = ctk.CTkFrame(limits, fg_color="transparent")
        right.pack(side="left", expand=True, fill="x")
        self._field_label(left, "X min").pack(fill="x")
        self._entry(left, self.xmin_var).pack(fill="x")
        self._field_label(left, "X max").pack(fill="x", pady=(6, 0))
        self._entry(left, self.xmax_var).pack(fill="x")
        self._field_label(right, "Y min").pack(fill="x")
        self._entry(right, self.ymin_var).pack(fill="x")
        self._field_label(right, "Y max").pack(fill="x", pady=(6, 0))
        self._entry(right, self.ymax_var).pack(fill="x")

        wave = ctk.CTkFrame(card, fg_color="transparent")
        wave.pack(fill="x", padx=16, pady=(10, 0))
        wave_left = ctk.CTkFrame(wave, fg_color="transparent")
        wave_left.pack(side="left", expand=True, fill="x", padx=(0, 6))
        wave_right = ctk.CTkFrame(wave, fg_color="transparent")
        wave_right.pack(side="left", expand=True, fill="x")
        self.wl_min_var = tk.StringVar(value=str(self.settings.get("wl_min_nm") or ""))
        self.wl_max_var = tk.StringVar(value=str(self.settings.get("wl_max_nm") or ""))
        self._field_label(wave_left, "Wavelength from (nm)").pack(fill="x")
        self._entry(wave_left, self.wl_min_var).pack(fill="x")
        self._field_label(wave_right, "to (nm)").pack(fill="x")
        self._entry(wave_right, self.wl_max_var).pack(fill="x")
        ctk.CTkLabel(
            card,
            text="Blank shows every wavelength. Saved files still keep the full spectrum.",
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
            justify="left",
            wraplength=290,
        ).pack(fill="x", padx=16, pady=(4, 0))

        self._field_label(card, "Legend").pack(fill="x", padx=16, pady=(10, 2))
        legend_row = ctk.CTkFrame(card, fg_color="transparent")
        legend_row.pack(fill="x", padx=16)
        self.legend_switch = ctk.CTkSwitch(
            legend_row,
            text="Show",
            command=self._queue_redraw,
            progress_color=ACCENT,
            button_color=CARD,
            button_hover_color=FIELD,
            fg_color=FIELD_HOVER,
            text_color=TEXT,
            font=ctk.CTkFont(family=HEAD_FONT, size=12),
            width=36,
        )
        self.legend_switch.select()
        self.legend_switch.pack(side="left", padx=(0, 10))
        self._menu(
            legend_row, list(LEGEND_LOCS), self.legend_var, command=self._queue_redraw
        ).pack(side="left", expand=True, fill="x")

        self._field_label(card, "Line width").pack(fill="x", padx=16, pady=(10, 2))
        self._entry(card, self.width_var).pack(fill="x", padx=16, pady=(0, 10))

        self.grid_switch = ctk.CTkSwitch(
            card,
            text="Grid",
            command=self._queue_redraw,
            progress_color=ACCENT,
            button_color=CARD,
            button_hover_color=FIELD,
            fg_color=FIELD_HOVER,
            text_color=TEXT,
            font=ctk.CTkFont(family=HEAD_FONT, size=12),
        )
        self.grid_switch.select()
        self.grid_switch.pack(anchor="w", padx=16, pady=(0, 16))

    def _build_file_card(self, parent) -> None:
        card = self._card(parent)
        card.pack(fill="x", pady=(14, 0))
        self._card_title(card, "Save")
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 8))
        self._ghost_button(row, "Load chart…", self.load_spec, width=130).pack(
            side="left", padx=(0, 8)
        )
        self._ghost_button(row, "Save chart…", self.save_spec, width=130).pack(side="left")
        self._primary_button(card, "Export figure…", self.export_figure, width=170).pack(
            anchor="w", padx=16, pady=(0, 8)
        )
        self.status_var = tk.StringVar(value="")
        ctk.CTkLabel(
            card,
            textvariable=self.status_var,
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
            justify="left",
            wraplength=290,
        ).pack(fill="x", padx=16, pady=(0, 4))
        self.error_var = tk.StringVar(value="")
        ctk.CTkLabel(
            card,
            textvariable=self.error_var,
            text_color=ERROR,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
            justify="left",
            wraplength=290,
        ).pack(fill="x", padx=16, pady=(0, 16))

    def _curve_group(self, kind: str) -> list:
        return self.spec.curves if kind == "csv" else self.spec.pl_series

    def _rebuild_curve_rows(self) -> None:
        for child in self.curves_box.winfo_children():
            child.destroy()
        self._curve_vars = []
        groups = [("csv", self.spec.curves, "Pressure"), ("pl", self.spec.pl_series, "PL")]
        shown = [(kind, curves, title) for kind, curves, title in groups if curves]
        if not shown:
            ctk.CTkLabel(
                self.curves_box,
                textvariable=self.curves_hint,
                text_color=MUTED,
                font=ctk.CTkFont(family=HEAD_FONT, size=11),
                anchor="w",
                justify="left",
                wraplength=280,
            ).pack(fill="x", padx=6, pady=(0, 4))
            self.curves_hint.set(
                "Add a pressure CSV, a single PL spectrum, or a PL time series"
            )
            return

        n_csv = len(self.spec.curves)
        n_pl = len(self.spec.pl_series)
        parts = []
        if n_csv:
            parts.append(f"{n_csv} pressure")
        if n_pl:
            parts.append(f"{n_pl} PL")
        self.curves_hint.set(", ".join(parts))
        ctk.CTkLabel(
            self.curves_box,
            textvariable=self.curves_hint,
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
        ).pack(fill="x", padx=6, pady=(0, 4))

        for kind, curves, title in shown:
            if len(shown) > 1:
                ctk.CTkLabel(
                    self.curves_box,
                    text=title,
                    text_color=MUTED,
                    font=ctk.CTkFont(family=HEAD_FONT, size=11),
                    anchor="w",
                ).pack(fill="x", padx=6, pady=(6, 0))
            for index, curve in enumerate(curves):
                self._build_curve_row(kind, index, curve)

    def _build_curve_row(self, kind: str, index: int, curve) -> None:
        row = ctk.CTkFrame(self.curves_box, fg_color=FIELD, corner_radius=8)
        row.pack(fill="x", padx=6, pady=3)
        ctk.CTkButton(
            row,
            text="",
            width=28,
            height=28,
            fg_color=curve.color,
            hover_color=curve.color,
            corner_radius=6,
            command=lambda k=kind, i=index: self._choose_color(k, i),
        ).pack(side="left", padx=(6, 4), pady=6)
        style_code = normalize_linestyle(curve.linestyle)
        style_var = tk.StringVar(value=LINESTYLE_LABELS.get(style_code, "Solid"))
        style_menu = ctk.CTkOptionMenu(
            row,
            values=list(LINESTYLES.keys()),
            variable=style_var,
            command=lambda choice, k=kind, i=index: self._on_linestyle(k, i, choice),
            width=78,
            height=28,
            fg_color=CARD,
            button_color=FIELD_HOVER,
            button_hover_color=BORDER,
            text_color=TEXT,
            dropdown_fg_color=CARD,
            dropdown_hover_color=FIELD,
            dropdown_text_color=TEXT,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
        )
        style_menu.pack(side="left", padx=(0, 4), pady=6)
        label_var = tk.StringVar(value=curve.label)
        self._curve_vars.append(label_var)
        entry = ctk.CTkEntry(
            row,
            textvariable=label_var,
            fg_color=CARD,
            border_color=BORDER,
            border_width=1,
            text_color=TEXT,
            height=28,
        )
        entry.pack(side="left", expand=True, fill="x", pady=6)
        entry.bind(
            "<KeyRelease>",
            lambda _e, k=kind, i=index, v=label_var: self._on_label(k, i, v),
        )
        visible = ctk.CTkSwitch(
            row,
            text="",
            width=36,
            progress_color=ACCENT,
            button_color=CARD,
            button_hover_color=FIELD,
            fg_color=FIELD_HOVER,
        )
        visible.configure(command=lambda k=kind, i=index, sw=visible: self._on_visible(k, i, sw))
        if curve.visible:
            visible.select()
        else:
            visible.deselect()
        visible.pack(side="left", padx=(6, 2))
        self._ghost_button(
            row, "×", lambda k=kind, i=index: self._remove_curve(k, i), width=32
        ).pack(side="left", padx=(0, 6), pady=6)

    def _on_label(self, kind: str, index: int, var: tk.StringVar) -> None:
        group = self._curve_group(kind)
        if 0 <= index < len(group):
            group[index].label = var.get()
            self._queue_redraw()

    def _on_visible(self, kind: str, index: int, switch) -> None:
        group = self._curve_group(kind)
        if 0 <= index < len(group):
            group[index].visible = bool(switch.get())
            self._queue_redraw()

    def _on_linestyle(self, kind: str, index: int, choice: str) -> None:
        group = self._curve_group(kind)
        if 0 <= index < len(group):
            group[index].linestyle = normalize_linestyle(choice)
            self._queue_redraw()

    def _choose_color(self, kind: str, index: int) -> None:
        group = self._curve_group(kind)
        if not (0 <= index < len(group)):
            return
        _rgb, hex_color = colorchooser.askcolor(
            color=group[index].color, parent=self, title="Curve color"
        )
        if not hex_color:
            return
        group[index].color = hex_color
        self._rebuild_curve_rows()
        self._queue_redraw()

    def _remove_curve(self, kind: str, index: int) -> None:
        group = self._curve_group(kind)
        if not (0 <= index < len(group)):
            return
        path = group[index].path
        del group[index]
        if kind == "csv":
            self._runs.pop(path, None)
        else:
            self._pl.pop(path, None)
            self._spectra.pop(path, None)
        self._rebuild_curve_rows()
        self._queue_redraw()

    def remove_all(self) -> None:
        self.spec.curves.clear()
        self.spec.pl_series.clear()
        self._runs = {}
        self._pl = {}
        self._spectra = {}
        self._rebuild_curve_rows()
        self._queue_redraw()

    def _pair_linked_colors(self) -> None:
        """A PL file and the CSV with the same name share one color."""
        csv_by_stem = {Path(curve.path).stem: curve for curve in self.spec.curves}
        for pl in self.spec.pl_series:
            match = csv_by_stem.get(Path(pl.path).stem)
            if match is not None:
                pl.color = match.color

    def _siblings(self, paths, suffix: str) -> list[str]:
        found = []
        for raw in paths:
            sibling = Path(raw).with_suffix(suffix)
            if sibling.is_file():
                found.append(str(sibling))
        return found

    def add_csvs(self) -> None:
        chosen = filedialog.askopenfilenames(
            parent=self,
            title="Choose pressure CSVs to plot",
            initialdir=self._browse_dir(),
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not chosen:
            return
        self._remember_path(chosen[0])
        before_csv = len(self.spec.curves)
        before_pl = len(self.spec.pl_series)
        errors = add_csv_paths(self.spec, chosen)
        pl_errors = add_pl_paths(self.spec, self._siblings(chosen, ".npz"))
        self._pair_linked_colors()
        runs, load_errors = load_runs_for_spec(self.spec)
        series, spectra, series_errors = load_pl_for_spec(self.spec)
        self._runs.update(runs)
        self._pl.update(series)
        self._spectra.update(spectra)
        self.error_var.set("\n".join(errors + pl_errors + load_errors + series_errors))
        self._report_added(len(self.spec.curves) - before_csv, len(self.spec.pl_series) - before_pl)
        self._rebuild_curve_rows()
        self._queue_redraw()

    def add_pl(self) -> None:
        chosen = filedialog.askopenfilenames(
            parent=self,
            title="Choose PL files to plot",
            initialdir=self._browse_dir(),
            filetypes=[("PL files", "*.npz"), ("All files", "*.*")],
        )
        if not chosen:
            return
        self._remember_path(chosen[0])
        before_csv = len(self.spec.curves)
        before_pl = len(self.spec.pl_series)
        errors = add_pl_paths(self.spec, chosen)
        csv_errors = add_csv_paths(self.spec, self._siblings(chosen, ".csv"))
        self._pair_linked_colors()
        runs, load_errors = load_runs_for_spec(self.spec)
        series, spectra, series_errors = load_pl_for_spec(self.spec)
        self._runs.update(runs)
        self._pl.update(series)
        self._spectra.update(spectra)
        self.error_var.set("\n".join(errors + csv_errors + load_errors + series_errors))
        self._report_added(len(self.spec.curves) - before_csv, len(self.spec.pl_series) - before_pl)
        self._rebuild_curve_rows()
        self._queue_redraw()

    def _report_added(self, n_csv: int, n_pl: int) -> None:
        parts = []
        if n_csv:
            parts.append(f"{n_csv} pressure curve{'s' if n_csv != 1 else ''}")
        if n_pl:
            parts.append(f"{n_pl} PL time series")
        if parts:
            self.status_var.set("Added " + " and ".join(parts))

    def _parse_limit(self, raw: str):
        text = raw.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _sync_spec_from_ui(self) -> None:
        self.spec.title = self.title_var.get()
        self.spec.xlabel = self.xlabel_var.get()
        self.spec.ylabel = self.ylabel_var.get()
        self.spec.units = self.units_var.get() if self.units_var.get() in UNITS else "Pa"
        self.spec.yscale = "log" if self.yscale.get() == "Log" else "linear"
        self.spec.xmin = self._parse_limit(self.xmin_var.get())
        self.spec.xmax = self._parse_limit(self.xmax_var.get())
        self.spec.ymin = self._parse_limit(self.ymin_var.get())
        self.spec.ymax = self._parse_limit(self.ymax_var.get())
        self.spec.legend = bool(self.legend_switch.get())
        loc = self.legend_var.get()
        self.spec.legend_loc = loc if loc in LEGEND_LOCS else "best"
        self.spec.grid = bool(self.grid_switch.get())
        try:
            width = float(self.width_var.get())
        except ValueError:
            width = 1.8
        self.spec.line_width = max(0.4, min(6.0, width))
        self.spec.wl_min, self.spec.wl_max = wavelength_limits(
            self._parse_limit(self.wl_min_var.get()),
            self._parse_limit(self.wl_max_var.get()),
        )
        self._remember_wavelength_range()

    def _apply_spec_to_ui(self) -> None:
        self._loading = True
        self.title_var.set(self.spec.title)
        self.xlabel_var.set(self.spec.xlabel)
        self.ylabel_var.set(self.spec.ylabel)
        self.units_var.set(self.spec.units)
        self.yscale.set("Log" if self.spec.yscale == "log" else "Linear")
        self.xmin_var.set("" if self.spec.xmin is None else str(self.spec.xmin))
        self.xmax_var.set("" if self.spec.xmax is None else str(self.spec.xmax))
        self.ymin_var.set("" if self.spec.ymin is None else str(self.spec.ymin))
        self.ymax_var.set("" if self.spec.ymax is None else str(self.spec.ymax))
        self.legend_var.set(self.spec.legend_loc)
        if self.spec.legend:
            self.legend_switch.select()
        else:
            self.legend_switch.deselect()
        if self.spec.grid:
            self.grid_switch.select()
        else:
            self.grid_switch.deselect()
        self.width_var.set(str(self.spec.line_width))
        self.wl_min_var.set("" if self.spec.wl_min is None else f"{self.spec.wl_min:g}")
        self.wl_max_var.set("" if self.spec.wl_max is None else f"{self.spec.wl_max:g}")
        self._loading = False
        self._rebuild_curve_rows()

    def _remember_wavelength_range(self) -> None:
        saved = (
            "" if self.spec.wl_min is None else f"{self.spec.wl_min:g}",
            "" if self.spec.wl_max is None else f"{self.spec.wl_max:g}",
        )
        current = (str(self.settings.get("wl_min_nm") or ""), str(self.settings.get("wl_max_nm") or ""))
        if saved == current:
            return
        self.settings["wl_min_nm"] = saved[0]
        self.settings["wl_max_nm"] = saved[1]
        save_settings(self.settings)
        master = self.master
        if hasattr(master, "wl_min_var"):
            master.wl_min_var.set(saved[0])
            master.wl_max_var.set(saved[1])
            master._apply_wavelength_view(persist=False)

    def _queue_redraw(self, *_args) -> None:
        if self._loading:
            return
        if self._redraw_job is not None:
            try:
                self.after_cancel(self._redraw_job)
            except tk.TclError:
                pass
        self._redraw_job = self.after(80, self._redraw)

    def _redraw(self) -> None:
        self._redraw_job = None
        self._sync_spec_from_ui()
        self.fig.clear()
        visible_pl = any(
            curve.visible and (curve.path in self._pl or curve.path in self._spectra)
            for curve in self.spec.pl_series
        )
        if visible_pl:
            render_recording(self.fig, self.spec, self._runs, self._pl, self._spectra)
            self.fig.subplots_adjust(
                left=0.10, right=0.96, top=0.94, bottom=0.08, hspace=0.38, wspace=0.08
            )
        else:
            ax = self.fig.add_subplot(111)
            render_chart(ax, self.spec, self._runs)
            self.fig.tight_layout(pad=1.6)
        self.canvas.draw_idle()

    def save_spec(self) -> None:
        self._sync_spec_from_ui()
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Save chart details",
            initialdir=self._browse_dir(),
            defaultextension=".json",
            initialfile=time.strftime("chart_%Y%m%d_%H%M%S.json"),
            filetypes=[("Chart JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        self._remember_path(path)
        try:
            save_chart(self.spec, path)
        except OSError as exc:
            self.error_var.set(str(exc))
            return
        self.error_var.set("")
        self.status_var.set(f"Saved {Path(path).name}")

    def load_spec(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Load chart details",
            initialdir=self._browse_dir(),
            filetypes=[("Chart JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        self._remember_path(path)
        try:
            self.spec = load_chart(path)
        except (OSError, ValueError) as exc:
            self.error_var.set(str(exc))
            return
        self._runs, errors = load_runs_for_spec(self.spec)
        self._pl, self._spectra, pl_errors = load_pl_for_spec(self.spec)
        self.error_var.set("\n".join(errors + pl_errors))
        self.status_var.set(f"Loaded {Path(path).name}")
        self._apply_spec_to_ui()
        self._queue_redraw()

    def export_figure(self) -> None:
        self._sync_spec_from_ui()
        has_pressure = any(c.visible and c.path in self._runs for c in self.spec.curves)
        has_pl = any(
            c.visible and (c.path in self._pl or c.path in self._spectra)
            for c in self.spec.pl_series
        )
        if not has_pressure and not has_pl:
            self.error_var.set("Add at least one visible curve before exporting")
            return
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Export figure",
            initialdir=self._browse_dir(),
            defaultextension=".png",
            initialfile=time.strftime("chart_%Y%m%d_%H%M%S.png"),
            filetypes=[
                ("PNG", "*.png"),
                ("PDF", "*.pdf"),
                ("SVG", "*.svg"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self._remember_path(path)
        self._redraw()
        try:
            self.fig.savefig(path, dpi=200, facecolor="white", bbox_inches="tight")
        except OSError as exc:
            self.error_var.set(str(exc))
            return
        self.error_var.set("")
        self.status_var.set(f"Exported {Path(path).name}")
