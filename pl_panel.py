"""Spectrometer (PL) half of the monitor: widgets, live spectrum, PL recording.

Mixed into ``MonitorApp``; it relies on the app's widget helpers
(``_card``, ``_entry``, ``_ghost_button`` ...) and its ``data_dir``/``settings``.
"""

from __future__ import annotations

import math
import time
import tkinter as tk
from pathlib import Path

import customtkinter as ctk
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from charts import wavelength_indices, wavelength_limits
from runs import Run, sanitize_save_name, unique_save_path
from spectrometer import device as spectro_device
from spectrometer import storage as spectro_storage
from spectrometer.controller import SpectrometerController
from ui import (
    ACCENT,
    ACCENT_HOVER,
    AMBER,
    BORDER,
    CARD,
    ERROR,
    FIELD,
    FIELD_HOVER,
    GREEN,
    GRID,
    HEAD_FONT,
    MONO_FONT,
    MUTED,
    ON_ACCENT,
    TEXT,
    SegmentedToggle,
)

SPECTRO_POLL_MS = 100
SPECTRUM_REDRAW_S = 0.15
DEFAULT_INTEGRATION_MS = 100.0
DEFAULT_PL_INTERVAL_S = 0.5
NO_SPECTROMETER = "(no spectrometer found)"
BACKENDS = ["Default", "cseabreeze", "pyseabreeze"]


def _nm_setting(value) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return ""


class SpectrometerPanel:
    # ------------------------------------------------------------- state

    def _init_spectro_state(self) -> None:
        backend = str(self.settings.get("seabreeze_backend", "Default"))
        if backend in BACKENDS[1:]:
            try:
                spectro_device.configure_backend(backend)
            except Exception as exc:
                print(f"Could not select seabreeze backend {backend}: {exc}")
        self.spectro = SpectrometerController()
        self._spectro_devices: dict[str, spectro_device.DeviceInfo] = {}
        self._pl_note = ""
        self._pl_drawn_at = 0.0
        self._linked_label: str | None = None
        self._spectro_locked_widgets: list = []

    def _init_spectro_vars(self) -> None:
        saved = self.settings
        self.spectro_int_var = tk.StringVar(value=str(saved.get("integration_ms", DEFAULT_INTEGRATION_MS)))
        self.spectro_boxcar_var = tk.StringVar(value=str(saved.get("boxcar", 0)))
        self.spectro_scans_var = tk.StringVar(value=str(saved.get("scans_to_average", 1)))
        self.pl_interval_var = tk.StringVar(value=str(saved.get("pl_interval_s", DEFAULT_PL_INTERVAL_S)))
        self.pl_count_var = tk.StringVar(value=str(saved.get("pl_series_count", 0)))
        self.backend_var = tk.StringVar(value=str(saved.get("seabreeze_backend", "Default")))
        self.nonlin_var = tk.BooleanVar(value=bool(saved.get("nonlinearity", False)))
        self.spectro_info_var = tk.StringVar(value="Not connected")
        self.dark_info_var = tk.StringVar(value="No dark spectrum stored")
        self.wl_min_var = tk.StringVar(value=_nm_setting(saved.get("wl_min_nm")))
        self.wl_max_var = tk.StringVar(value=_nm_setting(saved.get("wl_max_nm")))

    def _spectro_settings_dict(self) -> dict:
        return {
            "integration_ms": self.spectro_int_var.get(),
            "boxcar": self.spectro_boxcar_var.get(),
            "scans_to_average": self.spectro_scans_var.get(),
            "pl_interval_s": self.pl_interval_var.get(),
            "pl_series_count": self.pl_count_var.get(),
            "seabreeze_backend": self.backend_var.get(),
            "nonlinearity": bool(self.nonlin_var.get()),
            "electric_dark": bool(self.edark_switch.get()),
            "pl_with_run": bool(self.link_switch.get()),
            "wl_min_nm": self.wl_min_var.get().strip(),
            "wl_max_nm": self.wl_max_var.get().strip(),
        }

    def _switch(self, parent, text: str, command=None, width: int = 36) -> ctk.CTkSwitch:
        return ctk.CTkSwitch(
            parent,
            text=text,
            command=command,
            progress_color=ACCENT,
            button_color=CARD,
            button_hover_color=FIELD,
            fg_color=FIELD_HOVER,
            text_color=TEXT,
            font=ctk.CTkFont(family=HEAD_FONT, size=12),
            width=width,
        )

    # ----------------------------------------------------------- widgets

    def _build_spectro_connect(self, parent) -> None:
        self._card_title(parent, "Spectrometer")
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16)
        self.spectro_menu = self._menu(row, [NO_SPECTROMETER], NO_SPECTROMETER)
        self.spectro_menu.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.spectro_refresh_btn = self._ghost_button(row, "↻", self.refresh_spectrometers, width=36)
        self.spectro_refresh_btn.pack(side="left", padx=(0, 8))
        self.spectro_connect_btn = self._primary_button(
            row, "Connect", self.connect_spectrometer, width=110
        )
        self.spectro_connect_btn.pack(side="left")
        self.pl_status_var = tk.StringVar(value="Spectrometer off")
        ctk.CTkLabel(
            parent,
            textvariable=self.spectro_info_var,
            text_color=MUTED,
            font=ctk.CTkFont(family=MONO_FONT, size=11),
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=16, pady=(8, 14))

    def _build_spectrometer_card(self, parent) -> None:
        card = self._card(parent)
        card.pack(fill="x", pady=(14, 0))
        self._card_title(card, "PL")

        fields = ctk.CTkFrame(card, fg_color="transparent")
        fields.pack(fill="x", padx=16)
        fields.grid_columnconfigure((0, 1), weight=1, uniform="pl")
        self._field_label(fields, "Wavelength from (nm)").grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._field_label(fields, "to (nm)").grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.wl_min_entry = self._entry(fields, self.wl_min_var)
        self.wl_max_entry = self._entry(fields, self.wl_max_var)
        self.wl_min_entry.grid(row=1, column=0, sticky="ew", padx=(0, 6))
        self.wl_max_entry.grid(row=1, column=1, sticky="ew", padx=(6, 0))
        for entry in (self.wl_min_entry, self.wl_max_entry):
            entry.bind("<Return>", lambda _e: self._apply_wavelength_view())
            entry.bind("<FocusOut>", lambda _e: self._apply_wavelength_view())
        ctk.CTkLabel(
            card,
            text="Blank shows the full spectrum. Saved files still keep every wavelength.",
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
            justify="left",
            wraplength=300,
        ).pack(fill="x", padx=16, pady=(4, 0))

        switches = ctk.CTkFrame(card, fg_color="transparent")
        switches.pack(fill="x", padx=16, pady=(12, 0))
        self.edark_switch = self._switch(switches, "Electric dark", self._apply_corrections)
        self.edark_switch.pack(side="left", padx=(0, 14))
        if self.settings.get("electric_dark", False):
            self.edark_switch.select()
        self.subtract_switch = self._switch(switches, "Subtract dark", self._apply_processing)
        self.subtract_switch.pack(side="left")
        self.subtract_switch.configure(state="disabled")

        dark_row = ctk.CTkFrame(card, fg_color="transparent")
        dark_row.pack(fill="x", padx=16, pady=(10, 0))
        self.capture_dark_btn = self._ghost_button(dark_row, "Capture dark", self.capture_dark, width=130)
        self.capture_dark_btn.pack(side="left", padx=(0, 8))
        self.clear_dark_btn = self._ghost_button(dark_row, "Clear dark", self.clear_dark, width=110)
        self.clear_dark_btn.pack(side="left")
        ctk.CTkLabel(
            card,
            textvariable=self.dark_info_var,
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
            justify="left",
            wraplength=280,
        ).pack(fill="x", padx=16, pady=(4, 0))

        record_row = ctk.CTkFrame(card, fg_color="transparent")
        record_row.pack(fill="x", padx=16, pady=(10, 16))
        self._ghost_button(record_row, "Record PL  ·  P", self.save_spectrum, width=150).pack(side="left")
        self.pl_series_btn = self._ghost_button(
            record_row, "Record series", self.toggle_pl_series, width=130
        )
        self.pl_series_btn.pack(side="left", padx=(8, 0))
        self._spectro_locked_widgets = [
            self.edark_switch,
            self.subtract_switch,
            self.capture_dark_btn,
            self.clear_dark_btn,
        ]

    def _build_spectrum_plot(self, parent) -> None:
        card = self._card(parent)
        card.grid(row=3, column=1, sticky="nsew", padx=(14, 0))

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(14, 6))
        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.pack(side="left")
        ctk.CTkLabel(
            titles,
            text="PL spectrum",
            text_color=TEXT,
            font=ctk.CTkFont(family=HEAD_FONT, size=14, weight="bold"),
        ).pack(anchor="w")
        self.spectrum_caption = tk.StringVar(value="Live spectrometer reading")
        ctk.CTkLabel(
            titles,
            textvariable=self.spectrum_caption,
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
        ).pack(anchor="w")

        tools = ctk.CTkFrame(head, fg_color="transparent")
        tools.pack(side="right")
        self.autoscale_switch = self._switch(tools, "Auto-scale", self._redraw_spectrum)
        self.autoscale_switch.select()
        self.autoscale_switch.pack(side="left", padx=(0, 14))
        self.spectrum_toggle = SegmentedToggle(tools, ["Processed", "Raw"], command=lambda _v: self._redraw_spectrum())
        self.spectrum_toggle.pack(side="left")

        self.spec_fig = Figure(figsize=(6, 4.2), dpi=100, facecolor=CARD)
        self.spec_ax = self.spec_fig.add_subplot(111)
        (self.spec_line,) = self.spec_ax.plot([], [], color=ACCENT, lw=1.2)
        ax = self.spec_ax
        ax.set_facecolor(CARD)
        ax.set_xlabel("Wavelength (nm)", color=MUTED, fontsize=11, labelpad=8)
        ax.set_ylabel("Intensity (counts)", color=MUTED, fontsize=11, labelpad=8)
        ax.tick_params(colors=MUTED, labelsize=10, length=4, width=0.8)
        for side, spine in ax.spines.items():
            spine.set_visible(side in ("left", "bottom"))
            spine.set_color(BORDER)
        ax.grid(True, color=GRID, linewidth=0.9)
        self.spec_fig.tight_layout(pad=1.8)

        self.spec_canvas = FigureCanvasTkAgg(self.spec_fig, master=card)
        widget = self.spec_canvas.get_tk_widget()
        widget.configure(background=CARD, highlightthickness=0)
        widget.pack(fill="both", expand=True, padx=12, pady=(0, 14))

    def _build_spectro_settings(self, body) -> None:
        card = self._card(body)
        card.pack(fill="x", pady=(14, 0))
        self._card_title(card, "Spectrometer")
        fields = ctk.CTkFrame(card, fg_color="transparent")
        fields.pack(fill="x", padx=16)
        fields.grid_columnconfigure((0, 1), weight=1, uniform="plset")
        self._field_label(fields, "Integration (ms)").grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._field_label(fields, "Boxcar width (px)").grid(row=0, column=1, sticky="ew", padx=(6, 0))
        int_entry = self._entry(fields, self.spectro_int_var)
        boxcar_entry = self._entry(fields, self.spectro_boxcar_var)
        int_entry.grid(row=1, column=0, sticky="ew", padx=(0, 6))
        boxcar_entry.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(0, 8))
        int_entry.bind("<Return>", lambda _e: self._apply_integration())
        int_entry.bind("<FocusOut>", lambda _e: self._apply_integration())
        boxcar_entry.bind("<Return>", lambda _e: self._apply_processing())
        boxcar_entry.bind("<FocusOut>", lambda _e: self._apply_processing())
        self._field_label(card, "Scans to average").pack(fill="x", padx=16, pady=(0, 2))
        self._entry(card, self.spectro_scans_var).pack(fill="x", padx=16)
        ctk.CTkCheckBox(
            card,
            text="Nonlinearity correction",
            variable=self.nonlin_var,
            text_color=TEXT,
            fg_color=ACCENT,
            border_color=BORDER,
            font=ctk.CTkFont(family=HEAD_FONT, size=12),
        ).pack(anchor="w", padx=16, pady=(10, 0))
        self._field_label(card, "PL polling interval (s)").pack(fill="x", padx=16, pady=(10, 2))
        self._entry(card, self.pl_interval_var).pack(fill="x", padx=16)
        self._field_label(card, "Time series: number of spectra (0 = until stopped)").pack(
            fill="x", padx=16, pady=(10, 2)
        )
        self._entry(card, self.pl_count_var).pack(fill="x", padx=16)
        self._field_label(card, "seabreeze backend (applies after restart)").pack(fill="x", padx=16, pady=(10, 2))
        self._menu(card, BACKENDS, self.backend_var.get(), variable=self.backend_var).pack(fill="x", padx=16)
        ctk.CTkLabel(
            card,
            text="The polling interval applies to PL recorded with each run and to "
            "Record time series PL. 0 records every spectrum. PL files (.npz) are saved next to "
            "the pressure CSVs.",
            text_color=MUTED,
            font=ctk.CTkFont(family=HEAD_FONT, size=11),
            anchor="w",
            justify="left",
            wraplength=360,
        ).pack(fill="x", padx=16, pady=(8, 16))

    # ----------------------------------------------------------- helpers

    def _set_pl_status(self, text: str, color: str) -> None:
        self.pl_status_var.set(text)
        show = getattr(self, "_show_connection_status", None)
        if show is not None:
            show()

    def _refresh_pl_status(self) -> None:
        sc = self.spectro
        if not sc.connected:
            self._set_pl_status("Spectrometer off", MUTED)
        elif sc.recording:
            label = "PL with run" if sc.linked else "Recording time series"
            self._set_pl_status(f"{label} · {sc.recorder.count}", AMBER)
        else:
            self._set_pl_status("Spectrometer live", GREEN)

    def _update_spectro_lock(self) -> None:
        state = "disabled" if self.spectro.recording else "normal"
        for widget in self._spectro_locked_widgets:
            widget.configure(state=state)
        if not self.spectro.recording and self.spectro.dark is None:
            self.subtract_switch.configure(state="disabled")
        if self.spectro.connected and not self.spectro.device.supports_electric_dark:
            self.edark_switch.configure(state="disabled")
        self.pl_series_btn.configure(
            text="Stop series" if self.spectro.recording and not self.spectro.linked else "Record series",
            state="disabled" if self.spectro.linked else "normal",
        )
        self._refresh_pl_status()

    def _pl_notice(self, text: str) -> None:
        self._pl_note = text
        if not text:
            self._set_pl_activity(None)
            return
        tone = "done" if text.lower().startswith("saved") else "run"
        self._set_pl_activity(text, tone)

    def _set_pl_activity(self, text: str | None, tone: str | None = "run") -> None:
        """Show a PL recording message in the status card."""
        if not hasattr(self, "pl_activity_var"):
            return
        self.pl_activity_var.set(text or "")
        self._pl_activity_tone = tone if text else ""
        self._refresh_status_card()

    def _read_float(self, var: tk.StringVar, fallback: float, minimum: float = 0.0) -> float:
        try:
            value = float(var.get())
            if not math.isfinite(value) or value < minimum:
                raise ValueError
        except ValueError:
            value = fallback
            var.set(f"{fallback:g}")
        return value

    def _read_int(self, var: tk.StringVar, fallback: int, minimum: int = 0) -> int:
        try:
            value = int(float(var.get()))
            if value < minimum:
                raise ValueError
        except ValueError:
            value = fallback
        var.set(str(value))
        return value

    def _peek_nm(self, var: tk.StringVar) -> float | None:
        text = var.get().strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _read_wavelength_range(self) -> tuple[float | None, float | None]:
        return wavelength_limits(self._peek_nm(self.wl_min_var), self._peek_nm(self.wl_max_var))

    def _apply_wavelength_view(self, persist: bool = True) -> None:
        """Limit the live plot and peak to the typed range. Acquisition is unchanged."""
        for var in (self.wl_min_var, self.wl_max_var):
            text = var.get().strip()
            if not text:
                continue
            try:
                float(text)
            except ValueError:
                var.set("")
        lo, hi = self._read_wavelength_range()
        typed_lo = self.wl_min_var.get().strip()
        typed_hi = self.wl_max_var.get().strip()
        if lo is not None and hi is not None and typed_lo and typed_hi:
            if float(typed_lo) != lo or float(typed_hi) != hi:
                self.wl_min_var.set(f"{lo:g}")
                self.wl_max_var.set(f"{hi:g}")
        if self.spectro.device is not None:
            wl = np.asarray(self.spectro.device.wavelengths, dtype=float)
            keep = wavelength_indices(wl, lo, hi)
            shown = wl[keep]
            left = float(shown.min()) if lo is None else lo
            right = float(shown.max()) if hi is None else hi
            if right > left:
                self.spec_ax.set_xlim(left, right)
        self._redraw_spectrum()
        if self.spectro.last_frame is not None:
            self._update_pl_readout()
        if persist:
            self._persist_settings()

    def _pl_interval(self) -> float:
        return self._read_float(self.pl_interval_var, DEFAULT_PL_INTERVAL_S)

    def _update_dark_info(self) -> None:
        sc = self.spectro
        if sc.dark_pending:
            self.dark_info_var.set("Capturing dark… keep the light blocked")
            return
        d = sc.dark_frame
        if d is None:
            self.dark_info_var.set("No dark spectrum stored")
            return
        text = (
            f"Dark {time.strftime('%H:%M:%S', time.localtime(d.t_end))} · "
            f"{d.integration_time_us / 1000:g} ms × {d.scans_averaged}"
        )
        mismatch = sc.dark_mismatch()
        if mismatch:
            text += f"  ·  ⚠ {mismatch}, recapture"
        self.dark_info_var.set(text)

    # ----------------------------------------------------------- device

    def refresh_spectrometers(self) -> None:
        if self.spectro.connected:
            return
        found = []
        try:
            found = spectro_device.list_devices()
        except Exception as exc:
            self.pl_error_var.set(f"Could not query spectrometers: {exc}")
        self._spectro_devices = {info.label: info for info in found}
        sim = spectro_device.DeviceInfo("USB4000", spectro_device.SIMULATED_SERIAL)
        self._spectro_devices["Simulated USB4000 (no hardware)"] = sim
        labels = list(self._spectro_devices)
        self.spectro_menu.configure(values=labels)
        self.spectro_menu.set(labels[0])
        if found:
            self.pl_error_var.set("")

    def connect_spectrometer(self) -> None:
        info = self._spectro_devices.get(self.spectro_menu.get())
        if info is None:
            self.pl_error_var.set("Choose a spectrometer first")
            return
        sc = self.spectro
        sc.integration_us = int(round(self._read_float(self.spectro_int_var, DEFAULT_INTEGRATION_MS) * 1000))
        sc.set_scans(self._read_int(self.spectro_scans_var, 1, minimum=1))
        sc.set_corrections(bool(self.edark_switch.get()), bool(self.nonlin_var.get()))
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            sc.connect(info)
        except Exception as exc:
            self.pl_error_var.set(f"Could not open {info.label}: {exc}")
            return
        finally:
            self.configure(cursor="")
        self.spectro_int_var.set(f"{sc.integration_us / 1000:g}")
        self._apply_processing()
        dev = sc.device
        self.spectro_info_var.set(
            f"{dev.model} · S/N {dev.serial} · "
            f"{dev.wavelengths[0]:.0f}–{dev.wavelengths[-1]:.0f} nm"
        )
        if not dev.supports_electric_dark:
            self.edark_switch.deselect()
        self.spectro_menu.configure(state="disabled")
        self.spectro_refresh_btn.configure(state="disabled")
        self._apply_wavelength_view(persist=False)
        self.pl_error_var.set("")
        self.pl_meta_var.set("Waiting for the first spectrum…")
        self._pl_note = ""
        self._set_pl_activity(None)
        self._update_dark_info()
        self._update_spectro_lock()
        self._refresh_run_button()
        self._persist_settings()
        self._sync_spectro_button(True)

    def disconnect_spectrometer(self) -> None:
        self._show_pl_notices(self.spectro.disconnect())
        self._linked_label = None
        self.spectro_menu.configure(state="normal")
        self.spectro_refresh_btn.configure(state="normal")
        self.spectro_info_var.set("Not connected")
        self.pl_peak_var.set("--")
        self.pl_meta_var.set("Connect the spectrometer to see PL")
        self._pl_note = ""
        self._set_pl_activity(None)
        self.subtract_switch.deselect()
        self._update_dark_info()
        self._update_spectro_lock()
        self._refresh_run_button()
        self._sync_spectro_button(False)

    def _sync_spectro_button(self, connected: bool) -> None:
        button = getattr(self, "spectro_connect_btn", None)
        if button is None:
            return
        if connected:
            button.configure(
                text="Disconnect",
                command=self.disconnect_spectrometer,
                fg_color=CARD,
                hover_color=FIELD,
                text_color=TEXT,
                border_width=1,
                border_color=BORDER,
            )
            return
        button.configure(
            text="Connect",
            command=self.connect_spectrometer,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color=ON_ACCENT,
            border_width=0,
        )

    def _show_pl_notices(self, notices: list[str]) -> None:
        if notices:
            self._pl_notice(notices[-1])

    # --------------------------------------------------------- settings

    def _apply_integration(self) -> None:
        sc = self.spectro
        if sc.recording:
            self.spectro_int_var.set(f"{sc.integration_us / 1000:g}")
            return
        ms = self._read_float(self.spectro_int_var, sc.integration_us / 1000, minimum=0.001)
        applied = sc.set_integration_time_us(ms * 1000)
        self.spectro_int_var.set(f"{applied / 1000:g}")
        self._update_dark_info()

    def _apply_processing(self) -> None:
        if self.spectro.recording:
            self.spectro_boxcar_var.set(str(self.spectro.boxcar_width))
            return
        boxcar = self._read_int(self.spectro_boxcar_var, self.spectro.boxcar_width)
        self.spectro.set_processing(bool(self.subtract_switch.get()), boxcar)
        self._redraw_spectrum()

    def _apply_corrections(self) -> None:
        if self.spectro.recording:
            return
        self.spectro.set_corrections(bool(self.edark_switch.get()), bool(self.nonlin_var.get()))
        self._update_dark_info()

    def _apply_spectro_settings(self) -> None:
        """Push values edited in the Settings window to the spectrometer."""
        sc = self.spectro
        self._pl_interval()
        self._read_int(self.pl_count_var, 0)
        if sc.recording:
            self.spectro_scans_var.set(str(sc.scans))
            self.nonlin_var.set(sc.nonlinearity)
            self.spectro_int_var.set(f"{sc.integration_us / 1000:g}")
            self.spectro_boxcar_var.set(str(sc.boxcar_width))
            return
        self._apply_integration()
        self._apply_processing()
        sc.set_scans(self._read_int(self.spectro_scans_var, sc.scans, minimum=1))
        self._apply_corrections()

    # ------------------------------------------------------------- dark

    def capture_dark(self) -> None:
        if not self.spectro.connected:
            self.pl_error_var.set("Connect the spectrometer first")
            return
        self.spectro.request_dark()
        self._update_dark_info()

    def clear_dark(self) -> None:
        self.spectro.clear_dark()
        self.subtract_switch.deselect()
        self._apply_processing()
        self._update_dark_info()
        self._update_spectro_lock()

    # -------------------------------------------------------- recording

    def _save_prefix(self) -> str:
        """Name typed on the Run card, used for spectra saved outside a gauge run."""
        return sanitize_save_name(self.file_name_var.get())

    def _pressure_extra(self) -> dict:
        """Gauge reading to store alongside a single spectrum."""
        with self.lock:
            times = list(self.times)
            pressures = list(self.pressures)
            units = self.units
        if not pressures or not (self.logging or self.armed):
            return {}
        return {
            "pressure": np.float64(pressures[-1]),
            "pressure_units": np.array(units),
            "run_elapsed_s": np.float64(times[-1] if self.logging else np.nan),
            "run_label": np.array(self._run_label or (self.csv_path.stem if self.csv_path else "")),
        }

    def save_spectrum(self) -> None:
        if not self.spectro.connected:
            self.pl_error_var.set("Connect the spectrometer first")
            return
        self.spectro.request_single(self.data_dir, self._save_prefix(), self._pressure_extra())
        self._pl_notice("Saving next spectrum…")

    def toggle_pl_series(self) -> None:
        sc = self.spectro
        if sc.recording:
            if not sc.linked:
                self._show_pl_notices(sc.stop_series())
                self._update_spectro_lock()
            return
        if not sc.connected:
            self.pl_error_var.set("Connect the spectrometer first")
            return
        try:
            path = spectro_storage.make_path(self.data_dir, self._save_prefix(), "timeseries")
            sc.start_series(path, self._pl_interval(), self._read_int(self.pl_count_var, 0))
        except Exception as exc:
            self.pl_error_var.set(str(exc))
            return
        self.pl_error_var.set("")
        self._pl_notice(f"Recording {path.name}")
        self._update_spectro_lock()

    def _pl_run_started(self, t0: float, csv_path: Path | None, label: str | None) -> None:
        """A pump-down run began: record PL on the same time axis."""
        sc = self.spectro
        if not self._recording_pl_with_pressure():
            return
        if sc.recording:
            if not sc.linked:
                self._pl_notice("Time series PL is running · this run's PL is not linked")
                return
            self._pl_run_stopped(None)
        if csv_path is not None:
            path = unique_save_path(csv_path.parent, csv_path.stem, ".npz")
        else:
            path = spectro_storage.make_path(self.data_dir, self._save_prefix(), "run")
        try:
            sc.start_series(path, self._pl_interval(), 0, t0=t0, linked=True)
        except Exception as exc:
            self.pl_error_var.set(f"PL not recorded: {exc}")
            return
        self._linked_label = label or (csv_path.stem if csv_path else None)
        self._pl_notice(f"Recording {path.name}")
        self._update_spectro_lock()

    def _pl_run_stopped(self, run: Run | None, until: float | None = None) -> None:
        """The pump-down run ended: close its PL file, with the pressure trace."""
        sc = self.spectro
        if not (sc.recording and sc.linked):
            return
        extra = {"run_label": np.array(self._linked_label or "")}
        if run is not None:
            extra.update(
                pressure_elapsed_s=np.asarray(run.times, dtype=float),
                pressure=np.asarray(run.pressures, dtype=float),
                pressure_units=np.array(run.units),
                pressure_csv=np.array(run.source),
            )
        self._show_pl_notices(sc.stop_series(until=until, extra=extra))
        self._linked_label = None
        self._update_spectro_lock()

    # ------------------------------------------------------------ loop

    def _start_spectro_loop(self) -> None:
        self.refresh_spectrometers()
        self._update_spectro_lock()
        self.after(SPECTRO_POLL_MS, self._spectro_tick)

    def _spectro_tick(self) -> None:
        if self._closed:
            return
        result = self.spectro.poll()
        self._show_pl_notices(result.notices)
        if result.error:
            self.pl_error_var.set(result.error)
            if not self.spectro.connected:
                self.disconnect_spectrometer()
        if result.series_finished:
            self._update_spectro_lock()
        if any(n.startswith("Dark") for n in result.notices):
            self.subtract_switch.configure(state="normal")
            self.subtract_switch.select()
            self._apply_processing()
            self._update_dark_info()
        if result.frame is not None:
            self._update_pl_readout()
            if time.monotonic() - self._pl_drawn_at >= SPECTRUM_REDRAW_S:
                self._redraw_spectrum()
        if self.spectro.recording:
            self._refresh_pl_status()
        self.after(SPECTRO_POLL_MS, self._spectro_tick)

    def _update_pl_readout(self) -> None:
        sc = self.spectro
        frame, processed = sc.last_frame, sc.last_processed
        if frame is None or processed is None:
            return
        wl = np.asarray(sc.device.wavelengths, dtype=float)
        lo, hi = self._read_wavelength_range()
        keep = wavelength_indices(wl, lo, hi)
        view = np.asarray(processed, dtype=float)[keep]
        i = int(keep[int(np.argmax(view))])
        self.pl_peak_var.set(f"{wl[i]:.1f} nm")
        rate = f"  ·  {sc.rate_hz:.1f} spectra/s" if sc.rate_hz else ""
        meta = (
            f"{processed[i]:,.0f} counts  ·  {frame.integration_time_us / 1000:g} ms × "
            f"{frame.scans_averaged}{rate}"
        )
        if sc.recording and sc.recorder is not None:
            rec = sc.recorder
            counted = f"{rec.count} recorded"
            if not sc.linked and rec.n_target:
                counted = f"{counted} / {rec.n_target}"
            note = f"{self._pl_note}  ·  {counted}" if self._pl_note else counted
            self._set_pl_activity(note, "run")
        self.pl_meta_var.set(meta)
        if frame.saturated:
            self.pl_error_var.set("Detector saturated · lower the integration time")
        elif self.pl_error_var.get().startswith("Detector saturated"):
            self.pl_error_var.set("")

    def _redraw_spectrum(self) -> None:
        sc = self.spectro
        self._pl_drawn_at = time.monotonic()
        frame = sc.last_frame
        if frame is None or sc.device is None:
            self.spec_line.set_data([], [])
            self.spec_canvas.draw_idle()
            return
        raw = self.spectrum_toggle.get() == "Raw"
        y = frame.counts if raw else sc.process(frame.counts)
        wl = np.asarray(sc.device.wavelengths, dtype=float)
        lo, hi = self._read_wavelength_range()
        keep = wavelength_indices(wl, lo, hi)
        self.spec_line.set_data(wl[keep], np.asarray(y, dtype=float)[keep])
        shown = wl[keep]
        left = float(shown.min()) if lo is None else lo
        right = float(shown.max()) if hi is None else hi
        if right > left:
            self.spec_ax.set_xlim(left, right)
        parts = [f"{sc.device.model}", "raw counts" if raw else "processed"]
        if not raw:
            if sc.active_dark() is not None:
                parts.append("dark subtracted")
            if sc.boxcar_width:
                parts.append(f"boxcar {sc.boxcar_width}")
        if sc.effective_electric_dark():
            parts.append("electric dark")
        self.spectrum_caption.set("  ·  ".join(parts))
        if self.autoscale_switch.get():
            self.spec_ax.relim()
            self.spec_ax.autoscale_view(scalex=False)
        self.spec_canvas.draw_idle()

    def _spectro_shutdown(self) -> None:
        self.spectro.disconnect()
