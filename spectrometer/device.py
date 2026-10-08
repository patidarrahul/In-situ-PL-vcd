"""Spectrometer access through python-seabreeze, plus a simulated device.

All methods of a spectrometer object must be called from a single thread
(the acquisition thread) once acquisition has started.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SIMULATED_SERIAL = "SIMULATED"

# seabreeze's C++ driver needs msvcp140.dll. Without the system-wide Visual C++
# Redistributable, the pip package `msvc-runtime` supplies it in the venv root,
# which Windows does not search for extension-module dependencies.
if sys.platform == "win32" and (Path(sys.prefix) / "msvcp140.dll").exists():
    os.add_dll_directory(sys.prefix)


@dataclass(frozen=True)
class DeviceInfo:
    model: str
    serial: str

    @property
    def label(self) -> str:
        return f"{self.model}  (S/N {self.serial})"


def configure_backend(name: str | None) -> None:
    """Select the seabreeze backend ('cseabreeze' or 'pyseabreeze').

    Must be called before anything imports ``seabreeze.spectrometers``.
    """
    if name:
        import seabreeze

        seabreeze.use(name)


def _missing_vc_runtime() -> bool:
    if sys.platform != "win32":
        return False
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    return not any((d / "msvcp140.dll").exists() for d in (system32, Path(sys.prefix)))


def list_devices() -> list[DeviceInfo]:
    """Return connected Ocean Optics spectrometers. Raises on backend errors."""
    try:
        from seabreeze.spectrometers import list_devices as sb_list_devices

        return [DeviceInfo(d.model, d.serial_number) for d in sb_list_devices()]
    except Exception as exc:
        if _missing_vc_runtime():
            raise RuntimeError(
                "seabreeze needs msvcp140.dll: run `pip install msvc-runtime` in the "
                "venv or install the Microsoft Visual C++ Redistributable (x64)"
            ) from exc
        raise


def open_device(info: DeviceInfo):
    if info.serial == SIMULATED_SERIAL:
        return SimulatedSpectrometer()
    return SeabreezeSpectrometer(info.serial)


class SeabreezeSpectrometer:
    """Thin wrapper around ``seabreeze.spectrometers.Spectrometer``."""

    def __init__(self, serial: str):
        from seabreeze.spectrometers import Spectrometer

        self._spec = Spectrometer.from_serial_number(serial)
        self.model: str = self._spec.model
        self.serial: str = self._spec.serial_number
        self.wavelengths = np.asarray(self._spec.wavelengths(), dtype=float)
        self.pixels: int = int(self.wavelengths.size)
        lo, hi = self._spec.integration_time_micros_limits
        self.integration_limits_us: tuple[int, int] = (int(lo), int(hi))
        self.max_intensity: float = float(self._spec.max_intensity)

        # Probe optional corrections with a short exposure.
        self.set_integration_time_us(max(self.integration_limits_us[0], 10_000))
        self.supports_electric_dark = self._probe(correct_dark_counts=True)
        self.supports_nonlinearity = self._probe(correct_nonlinearity=True)

    def _probe(self, **kwargs) -> bool:
        try:
            self._spec.intensities(**kwargs)
            return True
        except Exception:
            return False

    def set_integration_time_us(self, micros: int) -> None:
        self._spec.integration_time_micros(int(micros))

    def read(self, electric_dark: bool = False, nonlinearity: bool = False) -> np.ndarray:
        return np.asarray(
            self._spec.intensities(
                correct_dark_counts=electric_dark and self.supports_electric_dark,
                correct_nonlinearity=nonlinearity and self.supports_nonlinearity,
            ),
            dtype=float,
        )

    def close(self) -> None:
        self._spec.close()


class SimulatedSpectrometer:
    """Imitates a USB4000 so the GUI can be used without hardware."""

    model = "USB4000 (simulated)"
    serial = SIMULATED_SERIAL
    supports_electric_dark = True
    supports_nonlinearity = True

    _DARK_LEVEL = 1500.0

    def __init__(self):
        self.pixels = 3648
        self.wavelengths = np.linspace(345.0, 1040.0, self.pixels)
        self.integration_limits_us = (10, 65_000_000)
        self.max_intensity = 65535.0
        self._int_us = 10_000
        self._rng = np.random.default_rng()
        wl = self.wavelengths
        self._signal_per_ms = (
            40.0 * np.exp(-0.5 * ((wl - 532.0) / 1.5) ** 2)
            + 15.0 * np.exp(-0.5 * ((wl - 650.0) / 25.0) ** 2)
            + 8.0 * np.exp(-0.5 * ((wl - 780.0) / 60.0) ** 2)
        )
        self._fixed_pattern = self._rng.normal(0.0, 20.0, self.pixels)

    def set_integration_time_us(self, micros: int) -> None:
        self._int_us = int(micros)

    def read(self, electric_dark: bool = False, nonlinearity: bool = False) -> np.ndarray:
        time.sleep(self._int_us / 1e6)
        t_ms = self._int_us / 1000.0
        drift = 1.0 + 0.05 * np.sin(time.time() / 5.0)
        signal = self._signal_per_ms * t_ms * drift
        dark = self._DARK_LEVEL + self._fixed_pattern + 0.5 * t_ms
        counts = signal + dark
        counts += self._rng.normal(0.0, 8.0, self.pixels) + np.sqrt(np.clip(signal, 0, None)) * self._rng.normal(
            0.0, 1.0, self.pixels
        )
        counts = np.clip(np.round(counts), 0, self.max_intensity)
        if electric_dark:
            counts = counts - self._DARK_LEVEL
        return counts

    def close(self) -> None:
        pass
