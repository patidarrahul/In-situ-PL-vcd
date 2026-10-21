#!/usr/bin/env python3
"""
Pump-down + PL monitor — desktop UI for an Edwards APG200 / nAPG200 gauge
and an Ocean Optics spectrometer (USB4000) recorded on one time axis.

    python app.py
"""

from __future__ import annotations

import csv
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog

# Imports stall if site-packages live on OneDrive; hop to the local venv.
_LOCAL_PY = Path.home() / ".venvs" / "onelab_pump_down_curve_monitor" / "bin" / "python"
if _LOCAL_PY.exists() and (
    "CloudStorage" in sys.prefix or "OneDrive" in sys.prefix
):
    os.execv(str(_LOCAL_PY), [str(_LOCAL_PY), *sys.argv])
elif "CloudStorage" in sys.prefix or "OneDrive" in sys.prefix:
    print("Starting… a venv on OneDrive can take a while to import.", flush=True)

import customtkinter as ctk
import matplotlib

matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.ticker import LogFormatterSciNotation, LogLocator

from config import (
    load_settings,
    remember_browse_dir,
    resolve_browse_dir,
    resolve_data_dir,
    save_settings,
)
from gauge import (
    NAPG200,
    GaugeError,
    list_serial_ports,
    open_link,
    preferred_serial_port,
    status_flags,
)
from metrics import (
    format_elapsed,
    format_pressure,
    format_sample_count,
    sample_rate_hz,
)
from pl_panel import SpectrometerPanel
from runs import (
    REFERENCE_COLOR,
    Run,
    color_for_index,
    load_run_csv,
    sanitize_save_name,
    save_basename,
    thin_points,
    unique_save_path,
)
from trigger import (
    DEFAULT_DROP_DECADES,
    DEFAULT_PRETRIGGER_S,
    DEFAULT_RECOVERY_PERCENT,
    DEFAULT_WINDOW_S,
    DropDetector,
    RecoveryWatcher,
    TriggerConfig,
)
from ui import (
    ACCENT,
    ACCENT_HOVER,
    AMBER,
    BLUE,
    BORDER,
    CARD,
    ERROR,
    FIELD,
    FIELD_HOVER,
    GREEN,
    GRID,
    GRID_MINOR,
    HEAD_FONT,
    MONO_FONT,
    MUTED,
    ON_ACCENT,
    PAGE,
    TEXT,
    SegmentedToggle,
)

DEFAULT_HOST = "10.10.100.254"
DEFAULT_TCP_PORT = 8899
DEFAULT_BAUD = 9600
DEFAULT_INTERVAL = 0.1
PLOT_REFRESH_MS = 400

CSV_FLUSH_S = 0.5
BACKOFF_STEP_S = 0.05
MAX_INTERVAL_S = 1.0
# A read is a ~30 ms round trip at 9600 baud, so a few intervals is generous;
# a tight timeout keeps one dropped reply from stalling a fast run.
MIN_READ_TIMEOUT_S = 0.35
MAX_READ_TIMEOUT_S = 2.0
# Redrawing every sample of a long fast run is wasted work on a 1000 px axis.
MAX_PLOT_POINTS = 4000

NO_REFERENCE = "None"
LOAD_REFERENCE = "Load CSV…"

MANUAL_MODE = "Manual"
AUTO_MODE = "Auto-trigger"


class MonitorApp(SpectrometerPanel, ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.title("Pump-down + PL monitor")
        self.geometry("1600x920")
        self.minsize(1240, 760)
        self.configure(fg_color=PAGE)

        self.settings = load_settings()
        self.data_dir = resolve_data_dir(self.settings.get("data_dir"))
        self._settings_win = None
        self._plotter_win = None
        self._init_spectro_state()
        self._init_setting_vars()

        self.link = None
        self.gauge: NAPG200 | None = None
        self.logging = False
        self.armed = False
        self.detector: DropDetector | None = None
        self.recovery: RecoveryWatcher | None = None
        self.run_index = 0
        self._run_label: str | None = None
        # Captured when arming: the sampling thread must not read widgets.
        self._cycle_config: TriggerConfig | None = None
        self._cycling = False
        self._recovery_percent = DEFAULT_RECOVERY_PERCENT
        self._trigger_note = ""
        self._cycle_note = ""
        # Set by whichever path opens a run; only stop_run clears it, so a drop
        # detected while stopping still gets archived.
        self._ran_logging = False
        self.t0: float | None = None
        self.times: list[float] = []
        self.pressures: list[float] = []
        self.units = "Pa"
        self.csv_path: Path | None = None
        self._csv_file = None
        self._csv_writer = None
        self._csv_flushed_at = 0.0
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.lock = threading.Lock()
        self._closed = False
        # Widgets may only be touched from the main thread; the sampling thread
        # queues callables here and _tick drains them.
        self._ui_queue: queue.Queue = queue.Queue()

        self.runs: list[Run] = []
        self.loaded_curves: dict[str, Run] = {}
        self.reference: Run | None = None
        self._overlay_artists: list = []
        self._curves_dirty = True

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._bind_shortcuts()
        self.after(PLOT_REFRESH_MS, self._tick)
        self._start_spectro_loop()

    def _init_setting_vars(self) -> None:
        saved = self.settings
        ports = list_serial_ports()
        saved_port = str(saved.get("serial_port", ""))
        if saved_port and saved_port in ports:
            default_port = saved_port
        else:
            default_port = preferred_serial_port(ports)
        self.host_var = tk.StringVar(value=str(saved.get("host", DEFAULT_HOST)))
        self.tcp_var = tk.StringVar(value=str(saved.get("tcp_port", DEFAULT_TCP_PORT)))
        self.port_var = tk.StringVar(value=default_port)
        self.baud_var = tk.StringVar(value=str(saved.get("baud", DEFAULT_BAUD)))
        self.units_var = tk.StringVar(value=str(saved.get("units", "Pa")))
        self.interval_var = tk.StringVar(value=str(saved.get("interval", DEFAULT_INTERVAL)))
        self.drop_var = tk.StringVar(value=str(saved.get("drop_decades", DEFAULT_DROP_DECADES)))
        self.window_var = tk.StringVar(value=str(saved.get("window_s", DEFAULT_WINDOW_S)))
        self.pretrigger_var = tk.StringVar(
            value=str(saved.get("pretrigger_s", DEFAULT_PRETRIGGER_S))
        )
        self.recovery_var = tk.StringVar(
            value=str(saved.get("recovery_percent", DEFAULT_RECOVERY_PERCENT))
        )
        self.folder_var = tk.StringVar(value=str(self.data_dir))
        self.folder_caption = tk.StringVar(value=self._folder_caption())
        self.folder_var.trace_add("write", lambda *_: self.folder_caption.set(self._folder_caption()))
        self.file_name_var = tk.StringVar(value=str(saved.get("file_name", "")))
        self.link_summary_var = tk.StringVar(value="")
        self._save_name = sanitize_save_name(self.file_name_var.get())
        self._save_stamp = ""
        self._init_spectro_vars()

    def _bind_shortcuts(self) -> None:
        # bind_all so keys work wherever focus is; skip when typing in a field.
        for key, handler in (
            ("s", self.start_run),
            ("S", self.start_run),
            ("x", self.stop_run),
            ("X", self.stop_run),
            ("c", self.clear_curve),
            ("C", self.clear_curve),
            ("p", self.save_spectrum),
            ("P", self.save_spectrum),
        ):
            self.bind_all(
                f"<KeyPress-{key}>",
                lambda e, h=handler: self._on_shortcut(h),
                add="+",
            )

    def _typing_in_field(self) -> bool:
        """True when focus is in a text entry (so s/x/c type normally)."""
        widget = self.focus_get()
        if widget is None:
            return False
        try:
            cls = widget.winfo_class()
        except tk.TclError:
            return False
        name = type(widget).__name__
        return cls in ("Entry", "Text", "TEntry", "Spinbox") or "Entry" in name or "Text" in name

    def _on_shortcut(self, handler) -> str | None:
        if self._closed or self._typing_in_field():
            return None
        handler()
        return "break"

    # ---------------------------------------------------------------- widgets

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

    def _entry(self, parent, value: str | tk.StringVar) -> ctk.CTkEntry:
        kwargs = {}
        if isinstance(value, tk.StringVar):
            kwargs["textvariable"] = value
        entry = ctk.CTkEntry(
            parent,
            fg_color=FIELD,
            border_color=BORDER,
            border_width=1,
            text_color=TEXT,
            placeholder_text_color=MUTED,
            height=32,
            **kwargs,
        )
        if not isinstance(value, tk.StringVar):
            entry.insert(0, value)
        return entry

    def _menu(
        self, parent, values: list[str], value: str, command=None, variable=None
    ) -> ctk.CTkOptionMenu:
        menu = ctk.CTkOptionMenu(
            parent,
            values=values,
            command=command,
            variable=variable,
            fg_color=FIELD,
            button_color=FIELD_HOVER,
            button_hover_color=BORDER,
            text_color=TEXT,
            dropdown_fg_color=CARD,
            dropdown_hover_color=FIELD,
            dropdown_text_color=TEXT,
            height=32,
        )
        if value:
            menu.set(value)
        return menu

    def _ghost_button(self, parent, text: str, command, width: int = 100) -> ctk.CTkButton:
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

    def _stat_tile(self, parent, label: str, last: bool = False, accent: bool = False) -> tk.StringVar:
        tile = ctk.CTkFrame(
            parent, fg_color=CARD, corner_radius=12, border_width=1, border_color=BORDER
        )
        tile.pack(side="left", expand=True, fill="both", padx=(0, 0 if last else 10))
        ctk.CTkLabel(
            tile, text=label, text_color=MUTED, font=ctk.CTkFont(family=HEAD_FONT, size=11)
        ).pack(anchor="w", padx=12, pady=(10, 0))
        var = tk.StringVar(value="--")
        ctk.CTkLabel(
            tile,
            textvariable=var,
            text_color=ACCENT if accent else TEXT,
            font=ctk.CTkFont(family=MONO_FONT, size=16, weight="bold" if accent else "normal"),
        ).pack(anchor="w", padx=12, pady=(2, 10))
        return var

    # ----------------------------------------------------------------- layout

    def _build(self) -> None:
        root = ctk.CTkFrame(self, fg_color=PAGE)
        root.pack(fill="both", expand=True, padx=20, pady=18)
        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="Disconnected")

        sidebar = ctk.CTkScrollableFrame(
            root, fg_color="transparent", width=360, scrollbar_button_color=BORDER
        )
        sidebar.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        self._build_acquisition_card(sidebar)
        self._build_spectrometer_card(sidebar)
        self._build_curves_card(sidebar)

        main = ctk.CTkFrame(root, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(3, weight=1)
        main.grid_columnconfigure((0, 1), weight=1, uniform="main")

        self._build_connections(main)
        self._build_status_card(main)
        self._build_stats(main)
        self._build_plot(main)
        self._build_spectrum_plot(main)

        self._refresh_link_summary()
        self._refresh_run_button()
        self._refresh_status_card()

    def _build_status_card(self, parent) -> None:
        card = self._card(parent)
        self.status_card = card
        card.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        self._card_title(card, "Status")
        self.activity_var = tk.StringVar(value="")
        self.pl_activity_var = tk.StringVar(value="")
        self._activity_tone = ""
        self._pl_activity_tone = ""
        self.activity_label = ctk.CTkLabel(
            card,
            textvariable=self.activity_var,
            text_color=TEXT,
            font=ctk.CTkFont(family=HEAD_FONT, size=16, weight="bold"),
            anchor="w",
            justify="left",
            wraplength=720,
        )
        self.pl_activity_label = ctk.CTkLabel(
            card,
            textvariable=self.pl_activity_var,
            text_color=TEXT,
            font=ctk.CTkFont(family=HEAD_FONT, size=16, weight="bold"),
            anchor="w",
            justify="left",
            wraplength=720,
        )
        self.status_idle_label = ctk.CTkLabel(
            card,
            text="Disconnected  ·  Spectrometer off",
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=16, weight="bold"),
            anchor="w",
        )
        self.status_idle_label.pack(fill="x", padx=16, pady=10)

    def _build_acquisition_card(self, parent) -> None:
        card = self._card(parent)
        card.pack(fill="x")
        self._card_title(card, "Record")

        link_row = ctk.CTkFrame(card, fg_color="transparent")
        link_row.pack(fill="x", padx=16, pady=(0, 10))
        self.link_switch = self._switch(link_row, "Record PL with the run")
        self.link_switch.pack(side="left")
        if self.settings.get("pl_with_run", True):
            self.link_switch.select()
        self._ghost_button(link_row, "Settings…", self.open_settings, width=110).pack(side="right")

        self.mode_toggle = SegmentedToggle(
            card, [MANUAL_MODE, AUTO_MODE], command=self._on_mode, height=32
        )
        self.mode_toggle.pack(fill="x", padx=16, pady=(0, 8))

        self._field_label(card, "File name").pack(fill="x", padx=16, pady=(0, 2))
        self.file_name_entry = self._entry(card, self.file_name_var)
        self.file_name_entry.configure(placeholder_text="pressure")
        self.file_name_entry.pack(fill="x", padx=16)

        folder_row = ctk.CTkFrame(card, fg_color="transparent")
        folder_row.pack(fill="x", padx=16, pady=(8, 0))
        ctk.CTkLabel(
            folder_row,
            textvariable=self.folder_caption,
            text_color=TEXT,
            font=ctk.CTkFont(family=MONO_FONT, size=11),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._ghost_button(folder_row, "Choose folder", self.choose_data_dir, width=120).pack(
            side="right"
        )

        self.cycle_box = ctk.CTkFrame(card, fg_color="transparent")
        self.cycle_switch = ctk.CTkSwitch(
            self.cycle_box,
            text="Cycle runs automatically",
            progress_color=ACCENT,
            button_color=CARD,
            button_hover_color=FIELD,
            fg_color=FIELD_HOVER,
            text_color=TEXT,
            font=ctk.CTkFont(family=HEAD_FONT, size=12),
        )
        self.cycle_switch.pack(anchor="w")

        self.run_row = ctk.CTkFrame(card, fg_color="transparent")
        self.run_row.pack(fill="x", padx=16, pady=(8, 6))
        self.start_button = self._primary_button(
            self.run_row, "Record  ·  S", self.start_run, width=140
        )
        self.start_button.pack(side="left", padx=(0, 8))
        self._ghost_button(self.run_row, "Stop  ·  X", self.stop_run, width=110).pack(side="left")

        tail_row = ctk.CTkFrame(card, fg_color="transparent")
        tail_row.pack(fill="x", padx=16, pady=(0, 16))
        self._ghost_button(tail_row, "Clear curve  ·  C", self.clear_curve, width=140).pack(
            side="left", padx=(0, 8)
        )
        self._ghost_button(tail_row, "Save plot", self.save_plot, width=110).pack(side="left")

    def _build_connections(self, parent) -> None:
        card = self._card(parent)
        card.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        card.grid_columnconfigure((0, 1), weight=1, uniform="connect")

        gauge = ctk.CTkFrame(card, fg_color="transparent")
        gauge.grid(row=0, column=0, sticky="nsew", padx=(4, 8))
        self._card_title(gauge, "Gauge")
        saved_transport = str(self.settings.get("transport", "WiFi"))
        if saved_transport not in ("WiFi", "Serial", "Simulated"):
            saved_transport = "WiFi"
        row = ctk.CTkFrame(gauge, fg_color="transparent")
        row.pack(fill="x", padx=16)
        self.transport = self._menu(
            row,
            ["WiFi", "Serial", "Simulated"],
            saved_transport,
            command=self._on_transport,
        )
        self.transport.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.connect_button = self._primary_button(row, "Connect", self.connect, width=110)
        self.connect_button.pack(side="left")
        ctk.CTkLabel(
            gauge,
            textvariable=self.link_summary_var,
            text_color=MUTED,
            font=ctk.CTkFont(family=MONO_FONT, size=11),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(6, 14))

        spectro = ctk.CTkFrame(card, fg_color="transparent")
        spectro.grid(row=0, column=1, sticky="nsew", padx=(8, 4))
        self._build_spectro_connect(spectro)

    def _build_curves_card(self, parent) -> None:
        card = self._card(parent)
        card.pack(fill="x", pady=(14, 0))
        self._card_title(card, "Curves")

        ref_row = ctk.CTkFrame(card, fg_color="transparent")
        ref_row.pack(fill="x", padx=16)
        ctk.CTkLabel(
            ref_row,
            text="Reference",
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=12),
        ).pack(side="left", padx=(0, 8))
        self.reference_menu = self._menu(
            ref_row, [NO_REFERENCE, LOAD_REFERENCE], NO_REFERENCE, command=self._on_reference_choice
        )
        self.reference_menu.pack(side="left", fill="x", expand=True)

        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill="x", padx=16, pady=(8, 14))
        self._ghost_button(actions, "Compare plots…", self.open_plotter, width=140).pack(
            side="left", padx=(0, 8)
        )
        self._ghost_button(actions, "Clear stored runs", self.clear_runs, width=140).pack(side="left")

    def _build_stats(self, parent) -> None:
        block = ctk.CTkFrame(parent, fg_color="transparent")
        block.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        stats = ctk.CTkFrame(block, fg_color="transparent")
        stats.pack(fill="x")
        self.pressure_var = self._stat_tile(stats, "Current pressure", accent=True)
        self.pl_peak_var = self._stat_tile(stats, "PL peak", accent=True)
        self.elapsed_var = self._stat_tile(stats, "Elapsed", accent=True)
        self.samples_var = self._stat_tile(stats, "Samples · rate", last=True, accent=True)
        notes = ctk.CTkFrame(block, fg_color="transparent")
        notes.pack(fill="x", pady=(6, 0))
        self.meta_var = tk.StringVar(value="Connect to the gauge to begin")
        ctk.CTkLabel(
            notes,
            textvariable=self.meta_var,
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
        ).pack(side="left")
        self.error_var = tk.StringVar(value="")
        ctk.CTkLabel(
            notes,
            textvariable=self.error_var,
            text_color=ERROR,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
        ).pack(side="left", padx=(12, 0))
        self.pl_error_var = tk.StringVar(value="")
        ctk.CTkLabel(
            notes,
            textvariable=self.pl_error_var,
            text_color=ERROR,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="e",
        ).pack(side="right")
        self.pl_meta_var = tk.StringVar(value="Connect the spectrometer to see PL")
        ctk.CTkLabel(
            notes,
            textvariable=self.pl_meta_var,
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="e",
        ).pack(side="right", padx=(0, 12))

    def _build_plot(self, parent) -> None:
        card = self._card(parent)
        card.grid(row=3, column=0, sticky="nsew")

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(14, 6))
        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.pack(side="left")
        ctk.CTkLabel(
            titles,
            text="Pressure vs time",
            text_color=TEXT,
            font=ctk.CTkFont(family=HEAD_FONT, size=14, weight="bold"),
        ).pack(anchor="w")
        self.plot_caption = tk.StringVar(value="Live gauge reading · log scale")
        ctk.CTkLabel(
            titles,
            textvariable=self.plot_caption,
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
        ).pack(anchor="w")

        plot_tools = ctk.CTkFrame(head, fg_color="transparent")
        plot_tools.pack(side="right")
        self.overlay_switch = ctk.CTkSwitch(
            plot_tools,
            text="Overlay all",
            command=self._mark_curves_dirty,
            progress_color=ACCENT,
            button_color=CARD,
            button_hover_color=FIELD,
            fg_color=FIELD_HOVER,
            text_color=TEXT,
            font=ctk.CTkFont(family=HEAD_FONT, size=12),
            width=36,
        )
        self.overlay_switch.pack(side="left", padx=(0, 14))
        self.scale_toggle = SegmentedToggle(
            plot_tools, ["Log", "Linear"], command=self._on_scale
        )
        self.scale_toggle.pack(side="left")

        self.fig = Figure(figsize=(7, 4.2), dpi=100, facecolor=CARD)
        self.ax = self.fig.add_subplot(111)
        (self.line,) = self.ax.plot(
            [], [], color=ACCENT, lw=2.0, solid_capstyle="round", label="Live run"
        )
        (self.head_point,) = self.ax.plot(
            [],
            [],
            "o",
            color=ACCENT,
            markersize=5,
            markeredgecolor=CARD,
            markeredgewidth=1.2,
            label="_nolegend_",
        )
        self._style_axes()

        self.canvas = FigureCanvasTkAgg(self.fig, master=card)
        widget = self.canvas.get_tk_widget()
        widget.configure(background=CARD, highlightthickness=0)
        widget.pack(fill="both", expand=True, padx=12, pady=(0, 14))

    def _style_axes(self) -> None:
        self.ax.set_facecolor(CARD)
        self.ax.set_xlabel("Time (s)", color=MUTED, fontsize=11, labelpad=8)
        self.ax.set_ylabel(f"Pressure ({self.units})", color=MUTED, fontsize=11, labelpad=8)
        self.ax.tick_params(colors=MUTED, labelsize=10, length=4, width=0.8)
        for side, spine in self.ax.spines.items():
            spine.set_visible(side in ("left", "bottom"))
            spine.set_color(BORDER)
        self.ax.grid(True, which="major", color=GRID, linewidth=0.9)
        self.ax.grid(True, which="minor", color=GRID_MINOR, linewidth=0.7)
        self._apply_scale()
        self.fig.tight_layout(pad=1.8)

    def _apply_scale(self) -> None:
        if self.scale_toggle.get() == "Log":
            self.ax.set_yscale("log")
            self.ax.yaxis.set_major_locator(LogLocator(base=10.0))
            self.ax.yaxis.set_major_formatter(LogFormatterSciNotation(base=10.0))
            self.ax.yaxis.set_minor_locator(
                LogLocator(base=10.0, subs=tuple(range(2, 10)), numticks=100)
            )
        else:
            self.ax.set_yscale("linear")

    # ------------------------------------------------------------- callbacks

    def _on_scale(self, _value: str) -> None:
        self._apply_scale()
        scale = "log" if self.scale_toggle.get() == "Log" else "linear"
        self.plot_caption.set(f"Live gauge reading · {scale} scale")
        self._mark_curves_dirty()

    def _on_transport(self, _value: str) -> None:
        self._refresh_link_summary()

    def _refresh_link_summary(self) -> None:
        choice = self.transport.get()
        if choice == "WiFi":
            host = self.host_var.get().strip() or DEFAULT_HOST
            port = self.tcp_var.get().strip() or str(DEFAULT_TCP_PORT)
            self.link_summary_var.set(f"{host}:{port}")
        elif choice == "Serial":
            port = self.port_var.get() or "(no port)"
            self.link_summary_var.set(f"{port}  ·  {self.baud_var.get()} baud")
        else:
            self.link_summary_var.set("No hardware · atmosphere, pump-down, vent")

    def _recording_pl_with_pressure(self) -> bool:
        """True when a pressure recording will also write the linked PL file."""
        switch = getattr(self, "link_switch", None)
        spectro = getattr(self, "spectro", None)
        if switch is None or spectro is None or not spectro.connected:
            return False
        try:
            return bool(switch.get())
        except tk.TclError:
            return False

    def _refresh_run_button(self) -> None:
        if not hasattr(self, "start_button"):
            return
        verb = "Arm" if self.mode_toggle.get() == AUTO_MODE else "Record"
        self.start_button.configure(text=f"{verb}  ·  S")

    def _on_mode(self, value: str) -> None:
        if self.logging or self.armed:
            self.stop_run()
        if value == AUTO_MODE:
            self.cycle_box.pack(fill="x", padx=16, before=self.run_row)
            self.meta_var.set("Auto-trigger ready — arm, then start the pump")
        else:
            self.cycle_box.pack_forget()
            self.meta_var.set("Manual mode")
        self._refresh_run_button()

    def _connection_status_line(self) -> str:
        gauge = self.status_var.get().strip() if hasattr(self, "status_var") else ""
        pl = self.pl_status_var.get().strip() if hasattr(self, "pl_status_var") else ""
        parts = [part for part in (gauge, pl) if part]
        return "  ·  ".join(parts) or "Ready"

    def _show_connection_status(self) -> None:
        """Connection text lives on the idle status line, not its own row."""
        label = getattr(self, "status_idle_label", None)
        if label is None or not hasattr(self, "activity_var"):
            return
        if self.activity_var.get().strip():
            return
        if getattr(self, "pl_activity_var", None) is not None and self.pl_activity_var.get().strip():
            return
        label.configure(text=self._connection_status_line(), text_color=MUTED)

    def _set_status(self, text: str, color: str) -> None:
        self.status_var.set(text)
        self._show_connection_status()

    def _set_activity(self, text: str | None, tone: str = "run") -> None:
        """Show a run or cycle message in the status card."""
        if not hasattr(self, "activity_var"):
            return
        self.activity_var.set(text or "")
        self._activity_tone = tone if text else ""
        self._refresh_status_card()

    def _refresh_status_card(self) -> None:
        card = getattr(self, "status_card", None)
        if card is None:
            return
        pressure = self.activity_var.get().strip()
        pl_text = self.pl_activity_var.get().strip() if hasattr(self, "pl_activity_var") else ""
        tone = self._activity_tone or getattr(self, "_pl_activity_tone", "")
        colors = {
            "run": ("#fef3c7", "#92400e"),
            "watch": ("#dbeafe", "#1e3a8a"),
            "done": ("#d1fae5", "#065f46"),
        }
        background, foreground = colors.get(tone, (CARD, TEXT))
        card.configure(fg_color=background)
        self.activity_label.pack_forget()
        self.pl_activity_label.pack_forget()
        self.status_idle_label.pack_forget()
        if not pressure and not pl_text:
            self.status_idle_label.configure(text=self._connection_status_line(), text_color=MUTED)
            self.status_idle_label.pack(fill="x", padx=16, pady=10)
            return
        self.activity_label.configure(text_color=foreground)
        self.pl_activity_label.configure(text_color=foreground)
        if pressure:
            self.activity_label.pack(fill="x", padx=16, pady=(10, 4 if pl_text else 10))
        if pl_text:
            self.pl_activity_label.pack(fill="x", padx=16, pady=(0, 10))

    def _folder_caption(self) -> str:
        """One-line folder path, keeping the selected folder name visible."""
        raw = self.folder_var.get().strip()
        path = Path(raw) if raw else self.data_dir
        tail = path.name
        parent = path.parent.name
        text = f"{parent}\\{tail}" if parent else tail
        full = str(path)
        if text != full and text != full.replace("/", "\\"):
            text = f"…\\{text}"
        if len(text) <= 28:
            return text
        return "…" + text[-27:]

    def _browse_dir(self) -> str:
        return str(
            resolve_browse_dir(
                self.settings.get("last_browse_dir"), fallback=self.data_dir
            )
        )

    def choose_data_dir(self) -> None:
        chosen = filedialog.askdirectory(
            title="Choose a folder for pressure CSVs", initialdir=self._browse_dir()
        )
        if not chosen:
            return
        self.data_dir = Path(chosen)
        self.folder_var.set(str(self.data_dir))
        remember_browse_dir(self.settings, chosen)
        self._persist_settings()
        self.error_var.set("")

    def _persist_settings(self) -> None:
        self.settings.update(
            {
                "data_dir": str(self.data_dir),
                "last_browse_dir": str(
                    resolve_browse_dir(
                        self.settings.get("last_browse_dir"), fallback=self.data_dir
                    )
                ),
                "transport": self.transport.get(),
                "host": self.host_var.get().strip(),
                "tcp_port": self.tcp_var.get().strip(),
                "serial_port": (
                    ""
                    if self.port_var.get().startswith("(")
                    else self.port_var.get()
                ),
                "baud": self.baud_var.get(),
                "units": self.units_var.get(),
                "interval": self.interval_var.get(),
                "drop_decades": self.drop_var.get(),
                "window_s": self.window_var.get(),
                "pretrigger_s": self.pretrigger_var.get(),
                "recovery_percent": self.recovery_var.get(),
                "file_name": self.file_name_var.get().strip(),
                **self._spectro_settings_dict(),
            }
        )
        save_settings(self.settings)

    def open_settings(self) -> None:
        if self._settings_win is not None and self._settings_win.winfo_exists():
            self._settings_win.lift()
            self._settings_win.focus()
            return
        win = ctk.CTkToplevel(self)
        win.title("Settings")
        win.geometry("440x720")
        win.minsize(400, 640)
        win.configure(fg_color=PAGE)
        win.transient(self)
        self._settings_win = win
        win.protocol("WM_DELETE_WINDOW", self._close_settings)
        self._build_settings(win)

    def open_plotter(self) -> None:
        if self._plotter_win is not None and self._plotter_win.winfo_exists():
            self._plotter_win.lift()
            self._plotter_win.focus()
            return
        from plotter import PlotterWindow

        win = PlotterWindow(self, self.data_dir, settings=self.settings)
        self._plotter_win = win
        win.protocol("WM_DELETE_WINDOW", self._close_plotter)

    def _close_plotter(self) -> None:
        if self._plotter_win is not None:
            try:
                self._plotter_win._on_close()
            except tk.TclError:
                pass
        self._plotter_win = None

    def _close_settings(self) -> None:
        self._refresh_link_summary()
        self._apply_spectro_settings()
        self._persist_settings()
        if self._settings_win is not None:
            try:
                self._settings_win.destroy()
            except tk.TclError:
                pass
        self._settings_win = None

    def _build_settings(self, win) -> None:
        body = ctk.CTkScrollableFrame(
            win, fg_color="transparent", scrollbar_button_color=BORDER
        )
        body.pack(fill="both", expand=True, padx=18, pady=16)

        link = self._card(body)
        link.pack(fill="x")
        self._card_title(link, "Connection")
        self._field_label(link, "WiFi host").pack(fill="x", padx=16, pady=(0, 2))
        self._entry(link, self.host_var).pack(fill="x", padx=16)
        self._field_label(link, "TCP port").pack(fill="x", padx=16, pady=(10, 2))
        self._entry(link, self.tcp_var).pack(fill="x", padx=16)

        ports = list_serial_ports()
        current = self.port_var.get()
        if current and current not in ports:
            ports = [current, *ports]
        if not current:
            current = preferred_serial_port(ports)
            self.port_var.set(current)
        self._field_label(link, "Serial port").pack(fill="x", padx=16, pady=(10, 2))
        self._menu(
            link, ports or ["(no ports)"], current, variable=self.port_var
        ).pack(fill="x", padx=16)
        self._field_label(link, "Baud").pack(fill="x", padx=16, pady=(10, 2))
        self._menu(
            link,
            ["9600", "19200", "38400", "57600"],
            self.baud_var.get(),
            variable=self.baud_var,
        ).pack(fill="x", padx=16, pady=(0, 16))

        acq = self._card(body)
        acq.pack(fill="x", pady=(14, 0))
        self._card_title(acq, "Acquisition")
        self._field_label(acq, "Units").pack(fill="x", padx=16, pady=(0, 2))
        self._menu(
            acq,
            ["mbar", "Pa", "Torr"],
            self.units_var.get(),
            command=self._on_units_change,
            variable=self.units_var,
        ).pack(fill="x", padx=16)
        self._field_label(acq, "Sample interval (s)").pack(fill="x", padx=16, pady=(10, 2))
        self._entry(acq, self.interval_var).pack(fill="x", padx=16)
        ctk.CTkLabel(
            acq,
            text="0.1 s = 10 Hz. The stats tile shows the rate actually achieved.",
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
            justify="left",
            wraplength=360,
        ).pack(fill="x", padx=16, pady=(4, 0))
        self._field_label(acq, "Save CSVs to").pack(fill="x", padx=16, pady=(10, 2))
        ctk.CTkLabel(
            acq,
            textvariable=self.folder_var,
            text_color=TEXT,
            font=ctk.CTkFont(family=MONO_FONT, size=11),
            anchor="w",
            justify="left",
            wraplength=360,
        ).pack(fill="x", padx=16)
        self._ghost_button(acq, "Choose folder…", self.choose_data_dir, width=150).pack(
            anchor="w", padx=16, pady=(8, 16)
        )

        auto = self._card(body)
        auto.pack(fill="x", pady=(14, 0))
        self._card_title(auto, "Auto-trigger")
        self._field_label(auto, "Trigger on a drop of (decades)").pack(
            fill="x", padx=16, pady=(0, 2)
        )
        self._entry(auto, self.drop_var).pack(fill="x", padx=16)
        self._field_label(auto, "…within (s)").pack(fill="x", padx=16, pady=(10, 2))
        self._entry(auto, self.window_var).pack(fill="x", padx=16)
        self._field_label(auto, "Baseline lookback (s)").pack(
            fill="x", padx=16, pady=(10, 2)
        )
        self._entry(auto, self.pretrigger_var).pack(fill="x", padx=16)
        self._field_label(auto, "End run at (% of starting pressure)").pack(
            fill="x", padx=16, pady=(10, 2)
        )
        self._entry(auto, self.recovery_var).pack(fill="x", padx=16)
        ctk.CTkLabel(
            auto,
            text="t = 0 is the last reading at the resting pressure. "
            "Cycling closes a run once pressure stays at the recovery "
            "threshold for one second.",
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
            justify="left",
            wraplength=360,
        ).pack(fill="x", padx=16, pady=(8, 16))

        self._build_spectro_settings(body)

        self._primary_button(body, "Done", self._close_settings, width=120).pack(
            anchor="e", pady=(16, 4)
        )

    def connect(self) -> None:
        self.disconnect()
        choice = self.transport.get()
        transport = {"WiFi": "wifi", "Serial": "serial"}.get(choice, "simulated")
        link = None
        try:
            kwargs = {}
            if transport == "serial":
                kwargs = {
                    "serial_port": self.port_var.get(),
                    "baud": int(self.baud_var.get()),
                }
            elif transport == "wifi":
                kwargs = {
                    "host": self.host_var.get().strip(),
                    "tcp_port": int(str(self.tcp_var.get()).strip()),
                }
            link = open_link(transport, **kwargs)
            gauge = NAPG200(link)
            identity = gauge.identity()
            serial = gauge.serial_number()
            gauge.set_units(self.units_var.get())
            pressure, status = gauge.read_pressure()
        except Exception as exc:
            if link is not None:
                try:
                    link.close()
                except Exception:
                    pass
            self.error_var.set(str(exc))
            self._set_status("Disconnected", MUTED)
            return

        self.link = link
        self.gauge = gauge
        self.units = status["units"]
        self.pressure_var.set(format_pressure(pressure, self.units))
        self.meta_var.set(
            f"{identity.split(';')[0]}  ·  S/N {serial}  ·  {status['gas']}  ·  {status_flags(status)}"
        )
        self.error_var.set("")
        self._persist_settings()
        self._set_status("Connected", GREEN)
        self._sync_connect_button(True)
        self.ax.set_ylabel(f"Pressure ({self.units})", color=MUTED, fontsize=11, labelpad=8)
        self.canvas.draw_idle()
        # Keep the menu in sync with what the gauge actually reports
        self._sync_units_var()

    def _sync_units_var(self) -> None:
        if self.units in ("mbar", "Pa", "Torr"):
            self.units_var.set(self.units)

    def _on_units_change(self, choice: str) -> None:
        """Apply units immediately if already connected (not only on Connect)."""
        if self.gauge is None:
            return
        if self.logging or self.armed:
            self.error_var.set("Stop the run before changing units")
            self._sync_units_var()
            return
        try:
            self.gauge.set_units(choice)
            pressure, status = self.gauge.read_pressure()
        except Exception as exc:
            self.error_var.set(str(exc))
            self._sync_units_var()
            return
        self.units = status["units"]
        self.pressure_var.set(format_pressure(pressure, self.units))
        self.error_var.set("")
        self._sync_units_var()
        self.ax.set_ylabel(f"Pressure ({self.units})", color=MUTED, fontsize=11, labelpad=8)
        self.canvas.draw_idle()

    def disconnect(self) -> None:
        self.stop_run()
        if self.link is not None:
            try:
                self.link.close()
            except Exception:
                pass
        self.link = None
        self.gauge = None
        self._set_status("Disconnected", MUTED)
        self._sync_connect_button(False)

    def _sync_connect_button(self, connected: bool) -> None:
        button = getattr(self, "connect_button", None)
        if button is None:
            return
        if connected:
            button.configure(
                text="Disconnect",
                command=self.disconnect,
                fg_color=CARD,
                hover_color=FIELD,
                text_color=TEXT,
                border_width=1,
                border_color=BORDER,
            )
            return
        button.configure(
            text="Connect",
            command=self.connect,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color=ON_ACCENT,
            border_width=0,
        )

    def _read_interval(self) -> float:
        return self._read_var(self.interval_var, DEFAULT_INTERVAL)

    def _read_var(self, var: tk.StringVar, fallback: float, allow_zero: bool = False) -> float:
        try:
            value = float(var.get())
            if value < 0 or (value == 0 and not allow_zero):
                raise ValueError
        except ValueError:
            value = fallback
            var.set(str(fallback))
        return value

    def _read_trigger_config(self) -> TriggerConfig:
        return TriggerConfig(
            drop_decades=self._read_var(self.drop_var, DEFAULT_DROP_DECADES),
            window_s=self._read_var(self.window_var, DEFAULT_WINDOW_S),
            pretrigger_s=self._read_var(
                self.pretrigger_var, DEFAULT_PRETRIGGER_S, allow_zero=True
            ),
        ).validated()

    def _read_recovery_percent(self) -> float:
        return self._read_var(self.recovery_var, DEFAULT_RECOVERY_PERCENT)

    def _capture_save_batch(self) -> None:
        """Remember the typed name and one time stamp for this start or arm.

        The sampling thread must not read the entry. A cycled batch reuses the
        stamp and adds ``_run1``, ``_run2``, …
        """
        self._save_name = sanitize_save_name(self.file_name_var.get())
        self._save_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._save_both = self._recording_pl_with_pressure()
        self._persist_settings()

    def _set_file_name_locked(self, locked: bool) -> None:
        if getattr(self, "file_name_entry", None) is None:
            return
        self.file_name_entry.configure(state="disabled" if locked else "normal")

    def _open_csv(self, run: int | None = None) -> str | None:
        """Start a fresh CSV; returns an error message when the file is unusable.

        The handle stays open for the whole run: reopening it per sample is fine
        at 2 Hz but wasteful at 20, especially on a synced folder.
        """
        self._close_csv()
        self.data_dir = resolve_data_dir(self.data_dir)
        stem = save_basename(
            self._save_name,
            self._save_stamp or None,
            run=run,
            both=bool(getattr(self, "_save_both", False)),
        )
        path = unique_save_path(self.data_dir, stem, ".csv")
        try:
            handle = path.open("w", newline="")
        except OSError as exc:
            self.csv_path = None
            return f"Cannot write to {self.data_dir}: {exc}"

        self._csv_file = handle
        self._csv_writer = csv.writer(handle)
        self._csv_writer.writerow(
            [
                "iso_time",
                "elapsed_s",
                f"pressure_{self.units}",
                "gas",
                "status_hex",
                "flags",
            ]
        )
        handle.flush()
        self._csv_flushed_at = time.time()
        self.csv_path = path
        return None

    def _write_rows(self, rows: list[list[str]]) -> None:
        writer = self._csv_writer
        if writer is None or not rows:
            return
        try:
            writer.writerows(rows)
            now = time.time()
            if now - self._csv_flushed_at >= CSV_FLUSH_S:
                self._csv_file.flush()
                self._csv_flushed_at = now
        except (OSError, ValueError) as exc:
            self._post_error(f"CSV write failed: {exc}")

    def _close_csv(self) -> None:
        handle, self._csv_file = self._csv_file, None
        self._csv_writer = None
        if handle is None:
            return
        try:
            handle.flush()
            handle.close()
        except OSError as exc:
            self._post_error(f"CSV close failed: {exc}")

    def start_run(self) -> None:
        if self.gauge is None:
            self.error_var.set("Connect to the gauge first")
            return
        if self.logging or self.armed:
            return

        interval = self._read_interval()
        if self.mode_toggle.get() == AUTO_MODE:
            self._arm(interval)
        else:
            self._start_logging(interval)

    def _start_logging(self, interval_s: float) -> None:
        with self.lock:
            self.times.clear()
            self.pressures.clear()

        self._run_label = None
        self._cycling = False
        self._capture_save_batch()
        self._set_file_name_locked(True)
        error = self._open_csv()
        self.folder_var.set(str(self.data_dir))
        if error is not None:
            self._set_file_name_locked(False)
            self.error_var.set(error)
            return

        self.stop_event.clear()
        self.logging = True
        self._ran_logging = True
        self.t0 = time.time()
        self.error_var.set("")
        self._trigger_note = ""
        self._set_activity(f"Writing {self.csv_path.name}", "run")
        self._pl_run_started(self.t0, self.csv_path, None)
        self._set_status("Logging", AMBER)
        self._start_worker(interval_s)

    def _arm(self, interval_s: float) -> None:
        config = self._read_trigger_config()
        cycling = bool(self.cycle_switch.get())
        self._cycling = cycling
        self._recovery_percent = self._read_recovery_percent()
        with self.lock:
            self.times.clear()
            self.pressures.clear()

        self.csv_path = None
        self._run_label = None
        self._capture_save_batch()
        self._set_file_name_locked(True)
        self._cycle_config = config
        self.detector = DropDetector(config)
        self.recovery = None
        self.stop_event.clear()
        self.armed = True
        self._ran_logging = False
        self.t0 = time.time()
        self.error_var.set("")
        self._trigger_note = ""
        self._cycle_note = ""
        tail = " · cycling" if cycling else ""
        self._set_activity(f"Armed · watching for a {config.describe()}{tail}", "watch")
        self._set_status("Armed · watching", BLUE)
        self._start_worker(interval_s)

    def _start_worker(self, interval_s: float) -> None:
        self._set_link_timeout(
            min(MAX_READ_TIMEOUT_S, max(MIN_READ_TIMEOUT_S, 4 * interval_s))
        )
        self.worker = threading.Thread(
            target=self._acquire_loop, args=(interval_s,), daemon=True
        )
        self.worker.start()

    def _set_link_timeout(self, seconds: float) -> None:
        setter = getattr(self.link, "set_timeout", None)
        if setter is None:
            return
        try:
            setter(seconds)
        except Exception:
            pass

    def stop_run(self) -> None:
        was_armed = self.armed
        self.stop_event.set()
        worker = self.worker
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=3.0)
        self.worker = None
        self.logging = False
        self.armed = False
        self.recovery = None
        self._set_file_name_locked(False)
        self._close_csv()
        self._set_link_timeout(MAX_READ_TIMEOUT_S)
        if self.gauge is not None:
            self._set_status("Connected", GREEN)
        if self._ran_logging:
            self._ran_logging = False
            self._pl_run_stopped(self._archive_run())
        elif was_armed:
            with self.lock:
                self.times.clear()
                self.pressures.clear()
            self.detector = None
            self._set_activity("Disarmed · no drop was seen", "done")

    def _snapshot_run(self) -> Run | None:
        """Copy the live curve into a stored Run. Safe to call off the main thread."""
        with self.lock:
            times = list(self.times)
            pressures = list(self.pressures)
            units = self.units
        if not times:
            return None
        label = self._run_label or (
            self.csv_path.stem if self.csv_path else time.strftime("run_%H%M%S")
        )
        return Run(
            label=label,
            times=times,
            pressures=pressures,
            units=units,
            source=str(self.csv_path or ""),
        )

    def _unique_label(self, base: str) -> str:
        if base not in self._all_curves():
            return base
        return f"{base} · {time.strftime('%H:%M:%S')}"

    def _archive_run(self) -> Run | None:
        run = self._snapshot_run()
        if run is None:
            return None
        self.runs.append(run)
        if self.csv_path is not None:
            self._set_activity(f"Saved {self.csv_path.name}", "done")
        self._refresh_curve_widgets()
        return run

    def _acquire_loop(self, interval_s: float) -> None:
        """Poll the gauge, either watching for a drop or logging a run."""
        gauge = self.gauge
        if gauge is None or self.t0 is None:
            self.logging = False
            self.armed = False
            return

        while not self.stop_event.is_set():
            loop_start = time.time()
            try:
                pressure, status = gauge.read_pressure()
                stamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                if self.armed:
                    self._watch_sample(loop_start, pressure, status, stamp)
                else:
                    self._record_sample(loop_start, pressure, status, stamp)
            except GaugeError as exc:
                interval_s = self._handle_gauge_error(str(exc), interval_s)
            except Exception as exc:
                self._post_error(str(exc))
                break

            self.stop_event.wait(max(0.0, interval_s - (time.time() - loop_start)))

        self.logging = False
        self.armed = False

    def _handle_gauge_error(self, message: str, interval_s: float) -> float:
        """Report a read failure, easing off if the gauge says we are too fast."""
        if "error 08" not in message:
            self._post_error(message)
            return interval_s

        slower = min(MAX_INTERVAL_S, interval_s + BACKOFF_STEP_S)
        if slower > interval_s:
            self._post_error(
                f"Gauge cannot keep up — polling eased to {slower:.2f} s "
                f"({1.0 / slower:.1f} Hz)"
            )
        return slower

    def _record_sample(self, t: float, pressure: float, status: dict, stamp: str) -> None:
        elapsed = t - (self.t0 or t)
        with self.lock:
            self.times.append(elapsed)
            self.pressures.append(pressure)
            self.units = status["units"]
        self._write_rows([self._csv_row(stamp, elapsed, pressure, status)])

        recovery = self.recovery
        if recovery is not None and recovery.add(t, pressure):
            self._complete_cycle()

    def _complete_cycle(self) -> None:
        """Close the vented run and go straight back to watching for the next."""
        self._close_csv()
        run = self._snapshot_run()
        ended_at = time.time()

        with self.lock:
            self.times = []
            self.pressures = []
            self.t0 = ended_at

        self.csv_path = None
        self.recovery = None
        self.detector = DropDetector(self._cycle_config or TriggerConfig())
        self.logging = False
        self._ran_logging = False
        self.armed = True

        def announce() -> None:
            self._pl_run_stopped(run, until=ended_at)
            if run is not None:
                self.runs.append(run)
                self._refresh_curve_widgets()
            if not self.armed:
                return  # stopped while this was in flight
            self._trigger_note = ""
            self._cycle_note = f"{run.label} saved" if run is not None else "Run saved"
            self._set_activity(f"{self._cycle_note} · watching for the next drop", "watch")
            self._set_status("Armed · watching", BLUE)

        self._post(announce)

    def _watch_sample(self, t: float, pressure: float, status: dict, stamp: str) -> None:
        detector = self.detector
        if detector is None:
            return
        fired = detector.add(t, pressure, (stamp, status))
        armed_at = self.t0 or t
        with self.lock:
            self.units = status["units"]
            if not fired:
                points = detector.watch_points()
                self.times = [point[0] - armed_at for point in points]
                self.pressures = [point[1] for point in points]
        if fired:
            self._begin_triggered_run(detector)

    def _begin_triggered_run(self, detector: DropDetector) -> None:
        """Promote the watched buffer into a real run once a drop is seen."""
        opening = detector.run_samples()
        started_at = opening[0][0] if opening else (detector.triggered_at or time.time())
        baseline = opening[0][1] if opening else 0.0

        self.run_index += 1
        self._run_label = self._unique_label(f"Run {self.run_index}")
        self.recovery = (
            RecoveryWatcher(baseline, self._recovery_percent) if self._cycling else None
        )

        error = self._open_csv(self.run_index if self._cycling else None)
        path = self.csv_path

        rows = []
        with self.lock:
            self.t0 = started_at
            self.times = []
            self.pressures = []
            for t, pressure, (stamp, status) in opening:
                elapsed = t - started_at
                self.times.append(elapsed)
                self.pressures.append(pressure)
                rows.append(self._csv_row(stamp, elapsed, pressure, status))
        self._write_rows(rows)

        self.detector = None
        self.armed = False
        self._ran_logging = True
        self.logging = True

        trigger_pressure = opening[-1][1] if opening else 0.0
        units = self.units
        label = self._run_label
        cycling = self._cycling

        def announce() -> None:
            self.error_var.set(error or "")
            self.folder_var.set(str(self.data_dir))
            self._set_status("Logging", AMBER)
            self._cycle_note = ""
            self._trigger_note = (
                label
                if cycling and label
                else f"Drop detected at {format_pressure(trigger_pressure, units)}"
            )
            if path is None:
                self._set_activity(f"{self._trigger_note} · memory only", "run")
            # Skip if the run was stopped before this reached the main thread.
            if self.logging and self._run_label == label:
                self._pl_run_started(started_at, path, label)

        self._post(announce)

    def _csv_row(
        self, stamp: str, elapsed: float, pressure: float, status: dict
    ) -> list[str]:
        return [
            stamp,
            f"{elapsed:.3f}",
            f"{pressure:.6e}",
            status["gas"],
            status["raw_status"],
            status_flags(status),
        ]

    def _post(self, action) -> None:
        """Hand a UI update to the main thread."""
        self._ui_queue.put(action)

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                action = self._ui_queue.get_nowait()
            except queue.Empty:
                return
            try:
                action()
            except tk.TclError:
                return

    def _post_error(self, message: str) -> None:
        self._post(lambda: self.error_var.set(message))

    # -------------------------------------------------------- curve handling

    def clear_curve(self) -> None:
        if self.logging or self.armed:
            self.error_var.set("Stop the run before clearing the curve")
            return
        with self.lock:
            self.times.clear()
            self.pressures.clear()
            self.t0 = None
        self.error_var.set("")
        self._mark_curves_dirty()

    def clear_runs(self) -> None:
        self.runs.clear()
        self.loaded_curves.clear()
        self.run_index = 0
        self.reference = None
        self.reference_menu.set(NO_REFERENCE)
        self.error_var.set("")
        self._refresh_curve_widgets()

    def save_plot(self) -> None:
        with self.lock:
            has_live = bool(self.times)
        if not (has_live or self.runs or self.reference):
            self.error_var.set("Nothing to save yet — start a run first")
            return
        stem = save_basename(self.file_name_var.get())
        path = unique_save_path(resolve_data_dir(self.data_dir), stem, ".png")
        self.fig.savefig(path, dpi=200, facecolor=CARD, bbox_inches="tight")
        self.error_var.set("")
        self.meta_var.set(f"Saved {path.name}")

    def _all_curves(self) -> dict[str, Run]:
        curves = {run.label: run for run in self.runs}
        curves.update(self.loaded_curves)
        return curves

    def _refresh_curve_widgets(self) -> None:
        curves = self._all_curves()
        values = [NO_REFERENCE, *curves.keys(), LOAD_REFERENCE]
        current = self.reference.label if self.reference else NO_REFERENCE
        self.reference_menu.configure(values=values)
        self.reference_menu.set(current if current in values else NO_REFERENCE)

        self._mark_curves_dirty()

    def _mark_curves_dirty(self, *_args) -> None:
        self._curves_dirty = True
        if not self._closed:
            self._redraw_curve()

    def _on_reference_choice(self, choice: str) -> None:
        if choice == LOAD_REFERENCE:
            self._load_reference_csv()
            return
        if choice == NO_REFERENCE:
            self.reference = None
        else:
            self.reference = self._all_curves().get(choice)
        self._mark_curves_dirty()

    def _load_reference_csv(self) -> None:
        previous = self.reference.label if self.reference else NO_REFERENCE
        chosen = filedialog.askopenfilename(
            title="Choose a pressure CSV to use as reference",
            initialdir=self._browse_dir(),
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not chosen:
            self.reference_menu.set(previous)
            return
        remember_browse_dir(self.settings, chosen)
        try:
            run = load_run_csv(chosen)
        except (OSError, ValueError) as exc:
            self.error_var.set(str(exc))
            self.reference_menu.set(previous)
            return
        self.loaded_curves[run.label] = run
        self.reference = run
        self.error_var.set("")
        self._refresh_curve_widgets()

    def _rebuild_overlays(self, log_scale: bool) -> None:
        for artist in self._overlay_artists:
            artist.remove()
        self._overlay_artists = []

        reference_label = self.reference.label if self.reference else None

        if self.overlay_switch.get():
            index = 0
            for label, run in self._all_curves().items():
                if label == reference_label:
                    continue  # drawn separately, as a dashed reference
                points = thin_points(run.points(positive_only=log_scale), MAX_PLOT_POINTS)
                if not points:
                    continue
                xs, ys = zip(*points)
                (line,) = self.ax.plot(
                    xs, ys, color=color_for_index(index), lw=1.4, alpha=0.9, label=label
                )
                self._overlay_artists.append(line)
                index += 1

        if self.reference is not None:
            points = thin_points(
                self.reference.points(positive_only=log_scale), MAX_PLOT_POINTS
            )
            if points:
                xs, ys = zip(*points)
                (line,) = self.ax.plot(
                    xs,
                    ys,
                    color=REFERENCE_COLOR,
                    lw=1.8,
                    linestyle="--",
                    label=f"Reference · {self.reference.label}",
                )
                self._overlay_artists.append(line)

        legend = self.ax.get_legend()
        if legend is not None:
            legend.remove()
        if self._overlay_artists:
            self.ax.legend(
                frameon=False, fontsize=9, labelcolor=MUTED, loc="best", ncols=1
            )

    def _redraw_curve(self) -> None:
        with self.lock:
            times = list(self.times)
            pressures = list(self.pressures)
            units = self.units

        log_scale = self.scale_toggle.get() == "Log"
        if self._curves_dirty:
            self._rebuild_overlays(log_scale)
            self._curves_dirty = False

        points = [(t, p) for t, p in zip(times, pressures) if p > 0] if log_scale else list(
            zip(times, pressures)
        )
        points = thin_points(points, MAX_PLOT_POINTS)
        if points:
            xs, ys = zip(*points)
            self.line.set_data(xs, ys)
            self.head_point.set_data([xs[-1]], [ys[-1]])
        else:
            self.line.set_data([], [])
            self.head_point.set_data([], [])

        self.ax.set_ylabel(f"Pressure ({units})", color=MUTED, fontsize=11, labelpad=8)
        self.ax.relim()
        self.ax.autoscale_view()
        self.canvas.draw_idle()

    # ------------------------------------------------------------- refreshing

    def _tick(self) -> None:
        if self._closed:
            return

        self._drain_ui_queue()

        with self.lock:
            times = list(self.times)
            pressures = list(self.pressures)
            units = self.units
            path = self.csv_path

        if pressures:
            self.pressure_var.set(format_pressure(pressures[-1], units))
            self.elapsed_var.set(format_elapsed(times[-1]))
            self.samples_var.set(
                format_sample_count(len(pressures), sample_rate_hz(times))
            )
            if self.logging and path is not None:
                progress = f"Writing {path.name}  ·  {len(pressures)} samples"
                if self._trigger_note:
                    progress = f"{self._trigger_note}  ·  {progress}"
                self._set_activity(progress, "run")
            elif self.armed and self.detector is not None:
                watching = (
                    f"Armed {format_elapsed(times[-1])} · "
                    f"watching for a {self.detector.config.describe()}"
                )
                if self._cycle_note:
                    watching = f"{self._cycle_note} · {watching}"
                self._set_activity(watching, "watch")
        else:
            self.elapsed_var.set("--:--")
            self.samples_var.set("--")

        self._redraw_curve()
        self.after(PLOT_REFRESH_MS, self._tick)

    def _on_close(self) -> None:
        self._closed = True
        for key in ("s", "S", "x", "X", "c", "C", "p", "P"):
            try:
                self.unbind_all(f"<KeyPress-{key}>")
            except tk.TclError:
                pass
        self._close_settings()
        self._close_plotter()
        self.disconnect()
        self._spectro_shutdown()
        self.destroy()


def main() -> None:
    MonitorApp().mainloop()


if __name__ == "__main__":
    main()
