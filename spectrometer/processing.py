"""Spectrum post-processing: dark subtraction and boxcar smoothing."""

from __future__ import annotations

import numpy as np


def boxcar(y: np.ndarray, width: int) -> np.ndarray:
    """Boxcar smoothing in the Ocean Optics convention.

    ``width`` is the number of pixels on *each side* of a pixel included in the
    average, so each output pixel is the mean of ``2 * width + 1`` pixels.
    Near the edges only the pixels that exist are averaged.
    """
    width = int(width)
    if width <= 0:
        return np.array(y, dtype=float, copy=True)
    kernel = np.ones(2 * width + 1)
    summed = np.convolve(y, kernel, mode="same")
    counts = np.convolve(np.ones(len(y)), kernel, mode="same")
    return summed / counts


def process(counts: np.ndarray, dark: np.ndarray | None, boxcar_width: int) -> np.ndarray:
    y = np.asarray(counts, dtype=float)
    if dark is not None:
        y = y - dark
    return boxcar(y, boxcar_width)
