"""Run-plot window: one image-and-spectra figure per PL file, then the onset chart."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog

import customtkinter as ctk
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from charts import (
    UNITS,
    ChartSpec,
    PlSeries,
    add_csv_paths,
    load_pl_recording,
    load_runs_for_spec,
    render_chart,
)
from config import remember_browse_dir, resolve_browse_dir, resolve_data_dir
from gallery import render_run_pair, render_run_summary, resolve_limits, run_number
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
        self.title("Plots")
        self.geometry("1280x820")
        self.minsize(980, 680)
        self.configure(fg_color=PAGE)
        self.transient(master)

        self.data_dir = resolve_data_dir(data_dir)
        self.settings = settings if settings is not None else {}
        self._runs: list[tuple[str, PlSeries]] = []
        self._panels: list[tuple[Figure, FigureCanvasTkAgg]] = []
        self._toolbar: NavigationToolbar2Tk | None = None
        self._redraw_job = None
        self._pump_redraw_job = None
        self.pump_spec = ChartSpec()
        self._pump_runs = {}

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self.lift)
        self.after(80, self._redraw)
        self.after(80, self._pump_redraw)

    def _browse_dir(self) -> str:
        return str(
            resolve_browse_dir(
                self.settings.get("last_browse_dir"), fallback=self.data_dir
            )
        )

    def _remember_path(self, path: str | Path) -> None:
        remember_browse_dir(self.settings, path)

    def _on_close(self) -> None:
        for job in (self._redraw_job, self._pump_redraw_job):
            if job is None:
                continue
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        self._clear_panels()
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

    def _entry(
        self,
        parent,
        variable: tk.StringVar,
        placeholder: str = "",
        on_change=None,
    ) -> ctk.CTkEntry:
        entry = ctk.CTkEntry(
            parent,
            textvariable=variable,
            fg_color=FIELD,
            border_color=BORDER,
            border_width=1,
            text_color=TEXT,
            placeholder_text=placeholder,
            placeholder_text_color=MUTED,
            height=32,
        )
        entry.bind("<KeyRelease>", on_change or self._queue_redraw)
        return entry

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
        self.tabs = ctk.CTkTabview(
            root,
            fg_color=PAGE,
            segmented_button_fg_color=FIELD,
            segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT_HOVER,
            segmented_button_unselected_color=FIELD,
            segmented_button_unselected_hover_color=FIELD_HOVER,
            text_color=TEXT,
        )
        self.tabs.pack(fill="both", expand=True)
        self._build_pl_tab(self.tabs.add("PL runs"))
        self._build_pump_tab(self.tabs.add("Pump-down"))

    def _build_pl_tab(self, parent) -> None:
        parent.grid_columnconfigure(1, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkScrollableFrame(
            parent, fg_color="transparent", width=300, scrollbar_button_color=BORDER
        )
        sidebar.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        self._build_files_card(sidebar)
        self._build_window_card(sidebar)

        plot_card = self._card(parent)
        plot_card.grid(row=0, column=1, sticky="nsew")
        tool_row = tk.Frame(plot_card, bg=CARD, height=36)
        tool_row.pack(fill="x", padx=8, pady=(8, 0))
        self._primary_button(tool_row, "Save…", self.save_figures, width=90).pack(
            side="right", padx=(8, 4), pady=4
        )
        self.toolbar_host = tk.Frame(tool_row, bg=CARD)
        self.toolbar_host.pack(side="left", fill="x", expand=True)
        self.runs_scroll = ctk.CTkScrollableFrame(
            plot_card, fg_color=CARD, scrollbar_button_color=BORDER
        )
        self.runs_scroll.pack(fill="both", expand=True, padx=8, pady=8)

    def _build_pump_tab(self, parent) -> None:
        parent.grid_columnconfigure(1, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkScrollableFrame(
            parent, fg_color="transparent", width=300, scrollbar_button_color=BORDER
        )
        sidebar.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        self._build_pump_files_card(sidebar)
        self._build_pump_options_card(sidebar)

        plot_card = self._card(parent)
        plot_card.grid(row=0, column=1, sticky="nsew")
        tool_row = tk.Frame(plot_card, bg=CARD, height=36)
        tool_row.pack(fill="x", padx=8, pady=(8, 0))
        self._primary_button(tool_row, "Save…", self.save_pump, width=90).pack(
            side="right", padx=(8, 4), pady=4
        )
        self.pump_fig = Figure(figsize=(8.2, 5.6), dpi=100, facecolor=CARD)
        self.pump_ax = self.pump_fig.add_subplot(111)
        self.pump_canvas = FigureCanvasTkAgg(self.pump_fig, master=plot_card)
        widget = self.pump_canvas.get_tk_widget()
        widget.configure(background=CARD, highlightthickness=0)
        widget.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.pump_toolbar = NavigationToolbar2Tk(self.pump_canvas, tool_row, pack_toolbar=False)
        self.pump_toolbar.update()
        self.pump_toolbar.pack(side="left", fill="x", expand=True)

    def _build_pump_files_card(self, parent) -> None:
        card = self._card(parent)
        card.pack(fill="x")
        self._card_title(card, "Curves")
        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.pack(fill="x", padx=16, pady=(0, 8))
        self._primary_button(buttons, "Add folder…", self.add_pump_folder, width=130).pack(
            side="left", padx=(0, 8)
        )
        self._ghost_button(buttons, "Add CSVs…", self.add_pump_csvs, width=110).pack(side="left")
        self._ghost_button(card, "Remove all", self.remove_pump_all, width=110).pack(
            anchor="w", padx=16, pady=(0, 8)
        )
        self.pump_files_box = ctk.CTkFrame(card, fg_color="transparent")
        self.pump_files_box.pack(fill="x", padx=10, pady=(0, 8))
        self.pump_status_var = tk.StringVar(value="Add pressure CSV files to compare")
        ctk.CTkLabel(
            card,
            textvariable=self.pump_status_var,
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
            justify="left",
            wraplength=250,
        ).pack(fill="x", padx=16, pady=(0, 4))
        self.pump_error_var = tk.StringVar(value="")
        ctk.CTkLabel(
            card,
            textvariable=self.pump_error_var,
            text_color=ERROR,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
            justify="left",
            wraplength=250,
        ).pack(fill="x", padx=16, pady=(0, 14))

    def _build_pump_options_card(self, parent) -> None:
        card = self._card(parent)
        card.pack(fill="x", pady=(14, 0))
        self._card_title(card, "Chart")
        self.pump_units_var = tk.StringVar(value=self.pump_spec.units)
        self._field_label(card, "Units").pack(fill="x", padx=16)
        ctk.CTkOptionMenu(
            card,
            values=list(UNITS),
            variable=self.pump_units_var,
            command=self._queue_pump_redraw,
            fg_color=FIELD,
            button_color=FIELD_HOVER,
            button_hover_color=BORDER,
            text_color=TEXT,
            dropdown_fg_color=CARD,
            dropdown_hover_color=FIELD,
            dropdown_text_color=TEXT,
            height=32,
        ).pack(fill="x", padx=16, pady=(2, 10))
        self._field_label(card, "Y scale").pack(fill="x", padx=16)
        self.pump_scale = SegmentedToggle(
            card, ["Log", "Linear"], command=self._queue_pump_redraw, height=30
        )
        self.pump_scale.pack(fill="x", padx=16, pady=(2, 10))

        limits = ctk.CTkFrame(card, fg_color="transparent")
        limits.pack(fill="x", padx=16)
        left = ctk.CTkFrame(limits, fg_color="transparent")
        left.pack(side="left", expand=True, fill="x", padx=(0, 6))
        right = ctk.CTkFrame(limits, fg_color="transparent")
        right.pack(side="left", expand=True, fill="x")
        self.pump_xmin_var = tk.StringVar(value="")
        self.pump_xmax_var = tk.StringVar(value="")
        self.pump_ymin_var = tk.StringVar(value="")
        self.pump_ymax_var = tk.StringVar(value="")
        redraw = self._queue_pump_redraw
        self._field_label(left, "X min").pack(fill="x")
        self._entry(left, self.pump_xmin_var, on_change=redraw).pack(fill="x")
        self._field_label(left, "X max").pack(fill="x", pady=(6, 0))
        self._entry(left, self.pump_xmax_var, on_change=redraw).pack(fill="x")
        self._field_label(right, "Y min").pack(fill="x")
        self._entry(right, self.pump_ymin_var, on_change=redraw).pack(fill="x")
        self._field_label(right, "Y max").pack(fill="x", pady=(6, 0))
        self._entry(right, self.pump_ymax_var, on_change=redraw).pack(fill="x")
        self.pump_grid = ctk.CTkSwitch(
            card,
            text="Grid",
            command=self._queue_pump_redraw,
            progress_color=ACCENT,
            button_color=CARD,
            button_hover_color=FIELD,
            fg_color=FIELD_HOVER,
            text_color=TEXT,
            font=ctk.CTkFont(family=HEAD_FONT, size=12),
        )
        self.pump_grid.select()
        self.pump_grid.pack(anchor="w", padx=16, pady=16)

    def _build_files_card(self, parent) -> None:
        card = self._card(parent)
        card.pack(fill="x")
        self._card_title(card, "Runs")
        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.pack(fill="x", padx=16, pady=(0, 8))
        self._primary_button(buttons, "Add folder…", self.add_folder, width=130).pack(
            side="left", padx=(0, 8)
        )
        self._ghost_button(buttons, "Add files…", self.add_files, width=110).pack(side="left")
        self._ghost_button(card, "Remove all", self.remove_all, width=110).pack(
            anchor="w", padx=16, pady=(0, 8)
        )
        self.files_box = ctk.CTkFrame(card, fg_color="transparent")
        self.files_box.pack(fill="x", padx=10, pady=(0, 8))
        self.status_var = tk.StringVar(value="Add a folder of .npz files")
        ctk.CTkLabel(
            card,
            textvariable=self.status_var,
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
            justify="left",
            wraplength=250,
        ).pack(fill="x", padx=16, pady=(0, 4))
        self.error_var = tk.StringVar(value="")
        ctk.CTkLabel(
            card,
            textvariable=self.error_var,
            text_color=ERROR,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
            justify="left",
            wraplength=250,
        ).pack(fill="x", padx=16, pady=(0, 14))

    def _build_window_card(self, parent) -> None:
        card = self._card(parent)
        card.pack(fill="x", pady=(14, 0))
        self._card_title(card, "Window")

        self.t_min_var = tk.StringVar(value="")
        self.t_max_var = tk.StringVar(value="")
        self.w_min_var = tk.StringVar(value=str(self.settings.get("wl_min_nm") or ""))
        self.w_max_var = tk.StringVar(value=str(self.settings.get("wl_max_nm") or ""))
        self.min_peak_var = tk.StringVar(value="100")
        self.x_major_var = tk.StringVar(value="5")
        self.peak_text_x_var = tk.StringVar(value="50")
        self.peak_text_y_var = tk.StringVar(value="0")

        time_row = ctk.CTkFrame(card, fg_color="transparent")
        time_row.pack(fill="x", padx=16)
        left = ctk.CTkFrame(time_row, fg_color="transparent")
        left.pack(side="left", expand=True, fill="x", padx=(0, 6))
        right = ctk.CTkFrame(time_row, fg_color="transparent")
        right.pack(side="left", expand=True, fill="x")
        self._field_label(left, "Time from (s)").pack(fill="x")
        self._entry(left, self.t_min_var, "start").pack(fill="x")
        self._field_label(right, "to (s)").pack(fill="x")
        self._entry(right, self.t_max_var, "end").pack(fill="x")
        self._field_label(card, "Time tick (s)").pack(fill="x", padx=16, pady=(10, 2))
        self._entry(card, self.x_major_var).pack(fill="x", padx=16)

        wave_row = ctk.CTkFrame(card, fg_color="transparent")
        wave_row.pack(fill="x", padx=16, pady=(10, 0))
        wave_left = ctk.CTkFrame(wave_row, fg_color="transparent")
        wave_left.pack(side="left", expand=True, fill="x", padx=(0, 6))
        wave_right = ctk.CTkFrame(wave_row, fg_color="transparent")
        wave_right.pack(side="left", expand=True, fill="x")
        self._field_label(wave_left, "Wavelength from (nm)").pack(fill="x")
        self._entry(wave_left, self.w_min_var, "all").pack(fill="x")
        self._field_label(wave_right, "to (nm)").pack(fill="x")
        self._entry(wave_right, self.w_max_var, "all").pack(fill="x")

        text_row = ctk.CTkFrame(card, fg_color="transparent")
        text_row.pack(fill="x", padx=16, pady=(10, 0))
        text_left = ctk.CTkFrame(text_row, fg_color="transparent")
        text_left.pack(side="left", expand=True, fill="x", padx=(0, 6))
        text_right = ctk.CTkFrame(text_row, fg_color="transparent")
        text_right.pack(side="left", expand=True, fill="x")
        self._field_label(text_left, "Peak text x (nm)").pack(fill="x")
        self._entry(text_left, self.peak_text_x_var).pack(fill="x")
        self._field_label(text_right, "Peak text y").pack(fill="x")
        self._entry(text_right, self.peak_text_y_var).pack(fill="x")

        self._field_label(card, "Min peak counts").pack(fill="x", padx=16, pady=(10, 2))
        self._entry(card, self.min_peak_var).pack(fill="x", padx=16)
        ctk.CTkLabel(
            card,
            text="A peak below this many counts stays at the window edge, so noise cannot move it.",
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
            justify="left",
            wraplength=250,
        ).pack(fill="x", padx=16, pady=(4, 8))
        checks = ctk.CTkFrame(card, fg_color="transparent")
        checks.pack(fill="x", padx=16, pady=(0, 16))
        self.show_peak_var = tk.IntVar(value=1)
        self.show_onset_var = tk.IntVar(value=1)
        ctk.CTkCheckBox(
            checks,
            text="Max intensity",
            variable=self.show_peak_var,
            command=self._queue_redraw,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            border_color=BORDER,
            text_color=TEXT,
            font=ctk.CTkFont(family=HEAD_FONT, size=12),
        ).pack(anchor="w", pady=(0, 6))
        ctk.CTkCheckBox(
            checks,
            text="PL onset",
            variable=self.show_onset_var,
            command=self._queue_redraw,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            border_color=BORDER,
            text_color=TEXT,
            font=ctk.CTkFont(family=HEAD_FONT, size=12),
        ).pack(anchor="w")

    def _queue_redraw(self, *_args) -> None:
        if self._redraw_job is not None:
            try:
                self.after_cancel(self._redraw_job)
            except tk.TclError:
                pass
        self._redraw_job = self.after(200, self._redraw)

    def _parse_limit(self, raw: str):
        text = raw.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _min_peak(self) -> float:
        try:
            return float(self.min_peak_var.get())
        except (TypeError, ValueError):
            return 100.0

    def _x_major(self) -> float:
        try:
            gap = float(self.x_major_var.get())
        except (TypeError, ValueError):
            return 5.0
        return gap if gap > 0 else 5.0

    def _peak_text_offset(self) -> tuple[float, float]:
        dx = self._parse_limit(self.peak_text_x_var.get())
        dy = self._parse_limit(self.peak_text_y_var.get())
        return (50.0 if dx is None else dx, 0.0 if dy is None else dy)

    def _known_paths(self) -> set[str]:
        return {path for path, _series in self._runs}

    def _load_paths(self, paths) -> None:
        errors = []
        added = 0
        known = self._known_paths()
        for raw in paths:
            path = Path(raw)
            key = str(path.resolve()) if path.exists() else str(path)
            if key in known:
                continue
            try:
                loaded = load_pl_recording(path)
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
                continue
            if not isinstance(loaded, PlSeries):
                errors.append(f"{path.name} is one spectrum, not a time series")
                continue
            self._runs.append((key, loaded))
            known.add(key)
            added += 1
        self._runs.sort(
            key=lambda item: (
                run_number(item[1].label, item[1].source) is None,
                run_number(item[1].label, item[1].source) or 0,
                item[1].label.lower(),
            )
        )
        self.error_var.set("\n".join(errors))
        if added:
            self.status_var.set(f"{len(self._runs)} run{'s' if len(self._runs) != 1 else ''}")
        self._rebuild_file_rows()
        self._queue_redraw()

    def add_folder(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self,
            title="Choose a folder of PL runs",
            initialdir=self._browse_dir(),
        )
        if not chosen:
            return
        self._remember_path(chosen)
        paths = sorted(Path(chosen).glob("*.npz"), key=lambda path: run_number(path.name, str(path)) or 10**9)
        if not paths:
            self.error_var.set(f"No .npz files in {Path(chosen).name}")
            return
        self._load_paths(paths)

    def add_files(self) -> None:
        chosen = filedialog.askopenfilenames(
            parent=self,
            title="Choose PL runs",
            initialdir=self._browse_dir(),
            filetypes=[("PL files", "*.npz"), ("All files", "*.*")],
        )
        if not chosen:
            return
        self._remember_path(chosen[0])
        self._load_paths(chosen)

    def remove_all(self) -> None:
        self._runs.clear()
        self.error_var.set("")
        self.status_var.set("Add a folder of .npz files")
        self._rebuild_file_rows()
        self._queue_redraw()

    def _remove_at(self, index: int) -> None:
        if not (0 <= index < len(self._runs)):
            return
        del self._runs[index]
        self.status_var.set(
            f"{len(self._runs)} run{'s' if len(self._runs) != 1 else ''}"
            if self._runs
            else "Add a folder of .npz files"
        )
        self._rebuild_file_rows()
        self._queue_redraw()

    def _rebuild_file_rows(self) -> None:
        for child in self.files_box.winfo_children():
            child.destroy()
        for index, (_path, series) in enumerate(self._runs):
            row = ctk.CTkFrame(self.files_box, fg_color=FIELD, corner_radius=8)
            row.pack(fill="x", padx=6, pady=3)
            ctk.CTkLabel(
                row,
                text=series.label,
                text_color=TEXT,
                font=ctk.CTkFont(family=HEAD_FONT, size=11),
                anchor="w",
            ).pack(side="left", expand=True, fill="x", padx=(8, 4), pady=6)
            self._ghost_button(
                row, "×", lambda i=index: self._remove_at(i), width=32
            ).pack(side="left", padx=(0, 6), pady=6)

    def _clear_panels(self) -> None:
        self._set_toolbar(None)
        for fig, canvas in self._panels:
            try:
                canvas.get_tk_widget().destroy()
            except tk.TclError:
                pass
            plt.close(fig)
        self._panels.clear()

    def _set_toolbar(self, canvas: FigureCanvasTkAgg | None) -> None:
        if canvas is not None and self._toolbar is not None and self._toolbar.canvas is canvas:
            return
        if self._toolbar is not None:
            self._toolbar.destroy()
            self._toolbar = None
        if canvas is None:
            return
        self._toolbar = NavigationToolbar2Tk(canvas, self.toolbar_host, pack_toolbar=False)
        self._toolbar.update()
        self._toolbar.pack(side="left", fill="x", expand=True)

    def _figure_width(self) -> float:
        self.update_idletasks()
        pixels = self.runs_scroll.winfo_width()
        if pixels < 400:
            pixels = 900
        return max(7.0, (pixels - 36) / 100.0)

    def _mount_figure(self, fig: Figure) -> None:
        card = ctk.CTkFrame(self.runs_scroll, fg_color=CARD, corner_radius=0)
        card.pack(fill="x", pady=(0, 16))
        canvas = FigureCanvasTkAgg(fig, master=card)
        widget = canvas.get_tk_widget()
        width, height = fig.get_size_inches()
        widget.configure(
            background=CARD,
            highlightthickness=0,
            width=int(width * fig.dpi),
            height=int(height * fig.dpi),
        )
        widget.pack(fill="x")
        widget.bind("<Button-1>", lambda _event, target=canvas: self._set_toolbar(target))
        canvas.draw()
        self._panels.append((fig, canvas))

    def _titles(self) -> list[str]:
        numbers = [run_number(series.label, series.source) for _path, series in self._runs]
        counts: dict[int, int] = {}
        for number in numbers:
            if number is not None:
                counts[number] = counts.get(number, 0) + 1
        titles = []
        for number, (_path, series) in zip(numbers, self._runs):
            if number is not None and counts[number] == 1:
                titles.append(f"Run {number}")
            else:
                titles.append(series.label)
        return titles

    def _redraw(self) -> None:
        self._redraw_job = None
        self._clear_panels()
        for child in list(self.runs_scroll.winfo_children()):
            child.destroy()
        if not self._runs:
            ctk.CTkLabel(
                self.runs_scroll,
                text="Add a folder of .npz files.\nEach run is its own figure.",
                text_color=MUTED,
                font=ctk.CTkFont(family=HEAD_FONT, size=13),
                justify="left",
            ).pack(anchor="w", padx=16, pady=16)
            return

        items = [(series.label, series) for _path, series in self._runs]
        t_min, t_max, w_min, w_max = resolve_limits(
            items,
            self._parse_limit(self.t_min_var.get()),
            self._parse_limit(self.t_max_var.get()),
            self._parse_limit(self.w_min_var.get()),
            self._parse_limit(self.w_max_var.get()),
        )
        min_peak = self._min_peak()
        peak_text_dx, peak_text_dy = self._peak_text_offset()
        width = self._figure_width()
        titles = self._titles()
        onsets = []
        last_peaks = []
        for title, (_path, series) in zip(titles, self._runs):
            fig = Figure(figsize=(width, 4.4), dpi=100, facecolor=CARD)
            onset, last_peak = render_run_pair(
                fig,
                title,
                series,
                t_min=t_min,
                t_max=t_max,
                w_min=w_min,
                w_max=w_max,
                min_peak_intensity=min_peak,
                x_major=self._x_major(),
                show_peak=bool(self.show_peak_var.get()),
                show_onset=bool(self.show_onset_var.get()),
                peak_text_dx=peak_text_dx,
                peak_text_dy=peak_text_dy,
            )
            onsets.append(onset)
            last_peaks.append(last_peak)
            self._mount_figure(fig)

        summary = Figure(figsize=(width, 4.2), dpi=100, facecolor=CARD)
        render_run_summary(
            summary,
            titles,
            onsets,
            last_peaks,
            show_onset=bool(self.show_onset_var.get()),
        )
        self._mount_figure(summary)
        self._set_toolbar(self._panels[0][1])

    def _queue_pump_redraw(self, *_args) -> None:
        if self._pump_redraw_job is not None:
            try:
                self.after_cancel(self._pump_redraw_job)
            except tk.TclError:
                pass
        self._pump_redraw_job = self.after(200, self._pump_redraw)

    def _sync_pump_spec(self) -> None:
        units = self.pump_units_var.get()
        self.pump_spec.units = units if units in UNITS else "Pa"
        self.pump_spec.yscale = "log" if self.pump_scale.get() == "Log" else "linear"
        self.pump_spec.xmin = self._parse_limit(self.pump_xmin_var.get())
        self.pump_spec.xmax = self._parse_limit(self.pump_xmax_var.get())
        self.pump_spec.ymin = self._parse_limit(self.pump_ymin_var.get())
        self.pump_spec.ymax = self._parse_limit(self.pump_ymax_var.get())
        self.pump_spec.grid = bool(self.pump_grid.get())
        self.pump_spec.legend = True

    def _pump_redraw(self) -> None:
        self._pump_redraw_job = None
        self._sync_pump_spec()
        render_chart(self.pump_ax, self.pump_spec, self._pump_runs)
        self.pump_fig.tight_layout(pad=1.2)
        self.pump_canvas.draw_idle()

    def _load_pump_csvs(self, paths) -> None:
        before = len(self.pump_spec.curves)
        errors = add_csv_paths(self.pump_spec, paths)
        runs, load_errors = load_runs_for_spec(self.pump_spec)
        self._pump_runs.update(runs)
        self.pump_error_var.set("\n".join(errors + load_errors))
        added = len(self.pump_spec.curves) - before
        count = len(self.pump_spec.curves)
        if added:
            self.pump_status_var.set(f"{count} curve{'s' if count != 1 else ''}")
        elif not errors and not load_errors:
            self.pump_status_var.set("Those curves are already loaded")
        self._rebuild_pump_rows()
        self._queue_pump_redraw()

    def add_pump_folder(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self,
            title="Choose a folder of pump-down CSVs",
            initialdir=self._browse_dir(),
        )
        if not chosen:
            return
        self._remember_path(chosen)
        paths = sorted(
            Path(chosen).glob("*.csv"),
            key=lambda path: run_number(path.name, str(path)) or 10**9,
        )
        if not paths:
            self.pump_error_var.set(f"No .csv files in {Path(chosen).name}")
            return
        self._load_pump_csvs(paths)

    def add_pump_csvs(self) -> None:
        chosen = filedialog.askopenfilenames(
            parent=self,
            title="Choose pump-down CSVs",
            initialdir=self._browse_dir(),
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not chosen:
            return
        self._remember_path(chosen[0])
        self._load_pump_csvs(chosen)

    def remove_pump_all(self) -> None:
        self.pump_spec.curves.clear()
        self._pump_runs = {}
        self.pump_error_var.set("")
        self.pump_status_var.set("Add pressure CSV files to compare")
        self._rebuild_pump_rows()
        self._queue_pump_redraw()

    def _remove_pump_at(self, index: int) -> None:
        if not (0 <= index < len(self.pump_spec.curves)):
            return
        path = self.pump_spec.curves[index].path
        del self.pump_spec.curves[index]
        self._pump_runs.pop(path, None)
        count = len(self.pump_spec.curves)
        self.pump_status_var.set(
            f"{count} curve{'s' if count != 1 else ''}" if count else "Add pressure CSV files to compare"
        )
        self._rebuild_pump_rows()
        self._queue_pump_redraw()

    def _rebuild_pump_rows(self) -> None:
        for child in self.pump_files_box.winfo_children():
            child.destroy()
        for index, curve in enumerate(self.pump_spec.curves):
            row = ctk.CTkFrame(self.pump_files_box, fg_color=FIELD, corner_radius=8)
            row.pack(fill="x", padx=6, pady=3)
            ctk.CTkButton(
                row,
                text="",
                width=28,
                height=28,
                fg_color=curve.color,
                hover_color=curve.color,
                corner_radius=6,
                command=lambda i=index: self._choose_pump_color(i),
            ).pack(side="left", padx=(6, 4), pady=6)
            label_var = tk.StringVar(value=curve.label)
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
                lambda _event, i=index, var=label_var: self._on_pump_label(i, var),
            )
            visible = ctk.CTkSwitch(
                row,
                text="",
                width=36,
                command=lambda i=index: self._on_pump_visible(i),
                progress_color=ACCENT,
                button_color=CARD,
                button_hover_color=FIELD,
                fg_color=FIELD_HOVER,
            )
            if curve.visible:
                visible.select()
            visible.pack(side="left", padx=(6, 2))
            self._ghost_button(
                row, "×", lambda i=index: self._remove_pump_at(i), width=32
            ).pack(side="left", padx=(0, 6), pady=6)
            curve._switch = visible

    def _on_pump_label(self, index: int, var: tk.StringVar) -> None:
        if 0 <= index < len(self.pump_spec.curves):
            self.pump_spec.curves[index].label = var.get()
            self._queue_pump_redraw()

    def _on_pump_visible(self, index: int) -> None:
        if not (0 <= index < len(self.pump_spec.curves)):
            return
        switch = getattr(self.pump_spec.curves[index], "_switch", None)
        if switch is not None:
            self.pump_spec.curves[index].visible = bool(switch.get())
            self._queue_pump_redraw()

    def _choose_pump_color(self, index: int) -> None:
        if not (0 <= index < len(self.pump_spec.curves)):
            return
        _rgb, hex_color = colorchooser.askcolor(
            color=self.pump_spec.curves[index].color,
            parent=self,
            title="Curve color",
        )
        if not hex_color:
            return
        self.pump_spec.curves[index].color = hex_color
        self._rebuild_pump_rows()
        self._queue_pump_redraw()

    def save_pump(self) -> None:
        if not any(curve.visible and curve.path in self._pump_runs for curve in self.pump_spec.curves):
            self.pump_error_var.set("Add at least one visible curve before saving")
            return
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Save pump-down chart",
            initialdir=self._browse_dir(),
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("PDF", "*.pdf"), ("SVG", "*.svg")],
        )
        if not path:
            return
        self._remember_path(path)
        self._pump_redraw()
        try:
            self.pump_fig.savefig(path, dpi=200, facecolor="white", bbox_inches="tight")
        except OSError as exc:
            self.pump_error_var.set(str(exc))
            return
        self.pump_error_var.set("")
        self.pump_status_var.set(f"Saved {Path(path).name}")

    def save_figures(self) -> None:
        if not self._panels:
            self.error_var.set("Add at least one run before saving")
            return
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Save figures",
            initialdir=self._browse_dir(),
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("PDF", "*.pdf"), ("SVG", "*.svg")],
        )
        if not path:
            return
        self._remember_path(path)
        destination = Path(path)
        try:
            if destination.suffix.lower() == ".pdf":
                with PdfPages(destination) as pdf:
                    for fig, _canvas in self._panels:
                        pdf.savefig(fig, facecolor="white")
                saved = destination.name
            elif len(self._panels) == 1:
                self._panels[0][0].savefig(destination, dpi=200, facecolor="white")
                saved = destination.name
            else:
                for index, (fig, _canvas) in enumerate(self._panels, start=1):
                    fig.savefig(
                        destination.with_name(f"{destination.stem}_{index}{destination.suffix}"),
                        dpi=200,
                        facecolor="white",
                    )
                saved = f"{destination.stem}_1{destination.suffix} … _{len(self._panels)}"
        except OSError as exc:
            self.error_var.set(str(exc))
            return
        self.error_var.set("")
        self.status_var.set(f"Saved {saved}")
