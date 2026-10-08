"""Shared colors and the segmented toggle used by the monitor and plotter."""

from __future__ import annotations

import sys

import customtkinter as ctk

PAGE = "#eff2f7"
CARD = "#ffffff"
BORDER = "#dfe6ef"
FIELD = "#f6f8fb"
FIELD_HOVER = "#e8edf4"
TEXT = "#111a27"
MUTED = "#6b7684"
ACCENT = "#0f766e"
ACCENT_HOVER = "#0b5f58"
ON_ACCENT = "#ffffff"
GREEN = "#12805c"
AMBER = "#b45309"
BLUE = "#1d4ed8"
ERROR = "#b42318"
GRID = "#e6ebf2"
GRID_MINOR = "#f0f3f8"

if sys.platform == "win32":
    HEAD_FONT = "Segoe UI"
    MONO_FONT = "Consolas"
else:
    HEAD_FONT = "Helvetica Neue"
    MONO_FONT = "Menlo"


class SegmentedToggle(ctk.CTkFrame):
    """Selector whose active segment keeps white text on the accent."""

    def __init__(
        self, parent, values: list[str], command=None, height: int = 30, button_width: int = 140
    ):
        super().__init__(parent, fg_color=FIELD, corner_radius=8)
        self._command = command
        self._value = values[0]
        self._buttons: dict[str, ctk.CTkButton] = {}
        for value in values:
            button = ctk.CTkButton(
                self,
                text=value,
                height=height,
                width=button_width,
                corner_radius=6,
                font=ctk.CTkFont(family=HEAD_FONT, size=12),
                command=lambda v=value: self.set(v, notify=True),
            )
            button.pack(side="left", expand=True, fill="x", padx=3, pady=3)
            self._buttons[value] = button
        self._paint()

    def get(self) -> str:
        return self._value

    def set(self, value: str, notify: bool = False) -> None:
        if value not in self._buttons:
            return
        self._value = value
        self._paint()
        if notify and self._command is not None:
            self._command(value)

    def _paint(self) -> None:
        for value, button in self._buttons.items():
            if value == self._value:
                button.configure(
                    fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ON_ACCENT
                )
            else:
                button.configure(
                    fg_color=FIELD, hover_color=FIELD_HOVER, text_color=MUTED
                )
