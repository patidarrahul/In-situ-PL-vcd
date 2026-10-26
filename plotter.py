"""Run-plot window: one image-and-spectra figure per PL file, then the onset chart."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from charts import PlSeries, load_pl_recording
from config import remember_browse_dir, resolve_browse_dir, resolve_data_dir
from gallery import render_run_pair, render_run_summary, resolve_limits, run_number
from ui import (
    ACCENT,
    ACCENT_HOVER,
    BORDER,
    CARD,
    ERROR,
    FIELD,
    HEAD_FONT,
    MUTED,
    ON_ACCENT,
    PAGE,
    TEXT,
)


class PlotterWindow(ctk.CTkToplevel):
    def __init__(self, master, data_dir: str | Path, settings: dict | None = None):
        super().__init__(master)
        self.title("Plot runs")
        self.geometry("1280x820")
        self.minsize(980, 680)
        self.configure(fg_color=PAGE)
        self.transient(master)

        self.data_dir = resolve_data_dir(data_dir)
        self.settings = settings if settings is not None else {}
        self._runs: list[tuple[str, PlSeries]] = []
        self._panels: list[tuple[Figure, FigureCanvasTkAgg]] = []
        self._redraw_job = None

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

    def _entry(self, parent, variable: tk.StringVar, placeholder: str = "") -> ctk.CTkEntry:
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
        entry.bind("<KeyRelease>", self._queue_redraw)
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
        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkScrollableFrame(
            root, fg_color="transparent", width=300, scrollbar_button_color=BORDER
        )
        sidebar.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        self._build_files_card(sidebar)
        self._build_window_card(sidebar)

        plot_card = self._card(root)
        plot_card.grid(row=0, column=1, sticky="nsew")
        self.runs_scroll = ctk.CTkScrollableFrame(
            plot_card, fg_color=CARD, scrollbar_button_color=BORDER
        )
        self.runs_scroll.pack(fill="both", expand=True, padx=8, pady=8)

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
        self._primary_button(card, "Export PDF…", self.export_pdf, width=150).pack(
            anchor="w", padx=16, pady=(0, 16)
        )

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
        for fig, canvas in self._panels:
            try:
                canvas.get_tk_widget().destroy()
            except tk.TclError:
                pass
            plt.close(fig)
        self._panels.clear()

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
            )
            onsets.append(onset)
            last_peaks.append(last_peak)
            self._mount_figure(fig)

        summary = Figure(figsize=(width, 4.2), dpi=100, facecolor=CARD)
        render_run_summary(summary, titles, onsets, last_peaks)
        self._mount_figure(summary)

    def export_pdf(self) -> None:
        if not self._panels:
            self.error_var.set("Add at least one run before exporting")
            return
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Export figures",
            initialdir=self._browse_dir(),
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf"), ("All files", "*.*")],
        )
        if not path:
            return
        self._remember_path(path)
        try:
            with PdfPages(path) as pdf:
                for fig, _canvas in self._panels:
                    pdf.savefig(fig, facecolor="white")
        except OSError as exc:
            self.error_var.set(str(exc))
            return
        self.error_var.set("")
        self.status_var.set(f"Exported {Path(path).name}")
