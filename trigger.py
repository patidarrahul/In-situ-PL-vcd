"""Sudden-drop detection so a run can be captured without pressing Start.

The gauge is polled continuously while armed; a run begins once the pressure
falls by `drop_decades` within any `window_s` stretch. Comparing decades makes
the test independent of the starting pressure and of the selected units.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, List, Optional, Tuple

# Real pump-downs on the lab system fall 0.9-3.3 decades per 0.5 s sample,
# while the gauge sits within 0.04 decades of atmosphere when idle.
DEFAULT_DROP_DECADES = 0.30
DEFAULT_WINDOW_S = 2.0
DEFAULT_PRETRIGGER_S = 5.0

# How far below the resting pressure a reading may sit and still count as
# "not pumping yet" — 0.02 decades is ~4.5%, well above the gauge's noise at
# atmosphere and far below any real pump-down step.
BASELINE_TOLERANCE_DECADES = 0.02

# Venting back to this share of the starting pressure ends a captured run.
DEFAULT_RECOVERY_PERCENT = 95.0
# The reading must stay up this long, so one noisy sample cannot end a run.
DEFAULT_RECOVERY_DWELL_S = 1.0

Sample = Tuple[float, float, Any]


def trim_to_baseline(
    samples: List[Sample], tolerance_decades: float = BASELINE_TOLERANCE_DECADES
) -> List[Sample]:
    """Drop the flat lead-in, keeping the last reading at the resting pressure.

    The gauge sits at its starting pressure until pumping begins, so the
    highest reading in the buffer is the baseline and the last sample still at
    that level is the moment the pressure starts to fall. Anchoring the run
    there puts t = 0 on the start of the pump-down instead of somewhere in the
    idle stretch before it.
    """
    positives = [p for _, p, _ in samples if p > 0]
    if len(samples) < 2 or not positives:
        return list(samples)

    floor = max(positives) * 10 ** (-abs(tolerance_decades))
    start = 0
    for index, (_, pressure, _) in enumerate(samples):
        if pressure > 0 and pressure >= floor:
            start = index
    # Always leave the drop itself in place.
    return list(samples[min(start, len(samples) - 2) :])


@dataclass(frozen=True)
class TriggerConfig:
    drop_decades: float = DEFAULT_DROP_DECADES
    window_s: float = DEFAULT_WINDOW_S
    pretrigger_s: float = DEFAULT_PRETRIGGER_S

    def validated(self) -> "TriggerConfig":
        return TriggerConfig(
            drop_decades=max(0.01, float(self.drop_decades)),
            window_s=max(0.1, float(self.window_s)),
            pretrigger_s=max(0.0, float(self.pretrigger_s)),
        )

    def describe(self) -> str:
        return (
            f"drop of {self.drop_decades:.2f} decades "
            f"within {self.window_s:g} s"
        )


class RecoveryWatcher:
    """Notices when a vented system has climbed back to its starting pressure.

    This is what closes a captured run during unattended cycling, so the next
    pump-down is recorded as its own curve without anyone pressing Stop.
    """

    def __init__(
        self,
        baseline: float,
        recovery_percent: float = DEFAULT_RECOVERY_PERCENT,
        dwell_s: float = DEFAULT_RECOVERY_DWELL_S,
    ) -> None:
        percent = min(99.0, max(1.0, float(recovery_percent)))
        self.percent = percent
        self.threshold = max(0.0, float(baseline)) * percent / 100.0
        self.dwell_s = max(0.0, float(dwell_s))
        self._above_since: Optional[float] = None

    def add(self, t: float, pressure: float) -> bool:
        """Record a sample; True once the pressure has stayed recovered."""
        if self.threshold <= 0 or pressure <= 0 or pressure < self.threshold:
            self._above_since = None
            return False
        if self._above_since is None:
            self._above_since = t
        return (t - self._above_since) >= self.dwell_s


class DropDetector:
    """Rolling buffer that reports the first sudden pressure drop it sees."""

    def __init__(self, config: Optional[TriggerConfig] = None) -> None:
        self.config = (config or TriggerConfig()).validated()
        self._samples: Deque[Sample] = deque()
        self.triggered_at: Optional[float] = None

    def reset(self) -> None:
        self._samples.clear()
        self.triggered_at = None

    @property
    def horizon_s(self) -> float:
        return max(self.config.pretrigger_s, self.config.window_s)

    def add(self, t: float, pressure: float, payload: Any = None) -> bool:
        """Record a sample; True on the reading that completes a drop."""
        self._samples.append((t, pressure, payload))
        while self._samples and t - self._samples[0][0] > self.horizon_s:
            self._samples.popleft()

        if self.triggered_at is not None or pressure <= 0:
            return False

        window = [s for s in self._samples if t - s[0] <= self.config.window_s]
        if len(window) < 2:
            return False
        baseline = max((p for _, p, _ in window if p > 0), default=0.0)
        if baseline <= 0:
            return False
        if math.log10(baseline / pressure) >= self.config.drop_decades:
            self.triggered_at = t
            return True
        return False

    def pretrigger(self) -> List[Sample]:
        """Buffered samples that lead into the drop, oldest first."""
        if self.triggered_at is None:
            return []
        cutoff = self.triggered_at - self.config.pretrigger_s
        return [s for s in self._samples if s[0] >= cutoff]

    def run_samples(self) -> List[Sample]:
        """The buffered samples that open the run, starting at the baseline."""
        return trim_to_baseline(self.pretrigger())

    def watch_points(self) -> List[Tuple[float, float]]:
        return [(t, p) for t, p, _ in self._samples]
