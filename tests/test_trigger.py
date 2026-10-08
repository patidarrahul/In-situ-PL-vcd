import unittest
from pathlib import Path

from runs import load_run_csv
from trigger import DropDetector, RecoveryWatcher, TriggerConfig, trim_to_baseline

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
REAL_RUNS = sorted(str(path) for path in DATA_DIR.glob("pressure_*.csv"))


def replay(detector: DropDetector, times, pressures):
    """Feed a whole run in; returns the time of the trigger, or None."""
    for t, p in zip(times, pressures):
        if detector.add(t, p, "row"):
            return t
    return None


class TestDropDetector(unittest.TestCase):
    def setUp(self):
        self.config = TriggerConfig(drop_decades=0.3, window_s=2.0, pretrigger_s=10.0)

    def test_flat_pressure_never_triggers(self):
        detector = DropDetector(self.config)
        times = [i * 0.5 for i in range(40)]
        self.assertIsNone(replay(detector, times, [1.0e5] * 40))
        self.assertIsNone(detector.triggered_at)

    def test_small_wobble_never_triggers(self):
        """The gauge drifting a few percent at atmosphere is not a pump-down."""
        detector = DropDetector(self.config)
        noisy = [1.0e5, 9.2e4, 9.4e4, 9.8e4, 9.9e4, 1.0e5, 1.0e5, 9.5e4]
        times = [i * 0.5 for i in range(len(noisy))]
        self.assertIsNone(replay(detector, times, noisy))

    def test_sudden_drop_triggers(self):
        detector = DropDetector(self.config)
        pressures = [1.0e5, 1.0e5, 1.0e5, 4.2e3, 1.3e3]
        times = [i * 0.5 for i in range(len(pressures))]
        self.assertEqual(replay(detector, times, pressures), 1.5)

    def test_slow_drift_below_threshold_is_ignored(self):
        """A 0.1 decade/s leak-up style ramp stays under a 0.3 decade window."""
        detector = DropDetector(TriggerConfig(drop_decades=0.3, window_s=1.0))
        pressures = [1.0e5 * (10 ** (-0.05 * i)) for i in range(30)]
        times = [i * 0.5 for i in range(30)]
        self.assertIsNone(replay(detector, times, pressures))

    def test_single_sample_cannot_trigger(self):
        detector = DropDetector(self.config)
        self.assertFalse(detector.add(0.0, 1.0))

    def test_non_positive_pressure_is_safe(self):
        detector = DropDetector(self.config)
        detector.add(0.0, 1.0e5)
        self.assertFalse(detector.add(0.5, 0.0))
        self.assertFalse(detector.add(1.0, -1.0))

    def test_only_fires_once(self):
        detector = DropDetector(self.config)
        detector.add(0.0, 1.0e5)
        self.assertTrue(detector.add(0.5, 1.0e3))
        self.assertFalse(detector.add(1.0, 1.0e1))
        self.assertEqual(detector.triggered_at, 0.5)

    def test_pretrigger_keeps_history_before_the_drop(self):
        detector = DropDetector(TriggerConfig(pretrigger_s=2.0))
        for i in range(10):
            detector.add(i * 0.5, 1.0e5, f"row{i}")
        self.assertTrue(detector.add(5.0, 1.0e3, "drop"))

        pretrigger = detector.pretrigger()
        self.assertEqual(pretrigger[0][0], 3.0)
        self.assertEqual(pretrigger[-1][2], "drop")
        self.assertEqual(pretrigger[0][1], 1.0e5)

    def test_pretrigger_empty_before_trigger(self):
        detector = DropDetector(self.config)
        detector.add(0.0, 1.0e5)
        self.assertEqual(detector.pretrigger(), [])

    def test_zero_pretrigger_keeps_only_the_drop(self):
        detector = DropDetector(TriggerConfig(pretrigger_s=0.0))
        detector.add(0.0, 1.0e5)
        self.assertTrue(detector.add(0.5, 1.0e3))
        self.assertEqual(len(detector.pretrigger()), 1)

    def test_buffer_is_bounded(self):
        detector = DropDetector(TriggerConfig(pretrigger_s=5.0, window_s=2.0))
        for i in range(1000):
            detector.add(i * 0.5, 1.0e5)
        self.assertLessEqual(len(detector.watch_points()), 12)

    def test_config_rejects_nonsense(self):
        config = TriggerConfig(drop_decades=-1, window_s=0, pretrigger_s=-5).validated()
        self.assertGreater(config.drop_decades, 0)
        self.assertGreater(config.window_s, 0)
        self.assertEqual(config.pretrigger_s, 0.0)

    def test_reset_clears_state(self):
        detector = DropDetector(self.config)
        detector.add(0.0, 1.0e5)
        self.assertTrue(detector.add(0.5, 1.0e2))
        detector.reset()
        self.assertIsNone(detector.triggered_at)
        self.assertEqual(detector.watch_points(), [])


class TestTrimToBaseline(unittest.TestCase):
    """t = 0 must land on the last reading at the resting pressure."""

    def test_drops_the_flat_lead_in(self):
        samples = [(i * 0.5, 1.0e5, None) for i in range(8)]
        samples += [(4.0, 4.2e3, None), (4.5, 1.3e3, None)]
        trimmed = trim_to_baseline(samples)
        self.assertEqual(len(trimmed), 3)
        self.assertEqual(trimmed[0][0], 3.5)
        self.assertEqual(trimmed[0][1], 1.0e5)

    def test_tolerates_small_baseline_noise(self):
        """A percent-level wobble at atmosphere still counts as the baseline."""
        samples = [
            (0.0, 1.0e5, None),
            (0.5, 9.9e4, None),
            (1.0, 9.8e4, None),
            (1.5, 2.0e3, None),
        ]
        trimmed = trim_to_baseline(samples)
        self.assertEqual([s[0] for s in trimmed], [1.0, 1.5])

    def test_keeps_everything_when_already_falling(self):
        samples = [(0.0, 1.0e5, None), (0.5, 1.0e4, None), (1.0, 1.0e3, None)]
        self.assertEqual(len(trim_to_baseline(samples)), 3)

    def test_short_input_is_returned_as_is(self):
        self.assertEqual(trim_to_baseline([]), [])
        one = [(0.0, 1.0e5, None)]
        self.assertEqual(trim_to_baseline(one), one)

    def test_always_keeps_two_points(self):
        flat = [(i * 0.5, 1.0e5, None) for i in range(5)]
        self.assertEqual(len(trim_to_baseline(flat)), 2)

    def test_ignores_non_positive_readings(self):
        samples = [(0.0, 0.0, None), (0.5, 1.0e5, None), (1.0, 1.0e3, None)]
        trimmed = trim_to_baseline(samples)
        self.assertEqual(trimmed[0][1], 1.0e5)

    def test_run_samples_anchors_the_run(self):
        detector = DropDetector(TriggerConfig(pretrigger_s=10.0))
        for i in range(12):
            detector.add(i * 0.5, 1.0e5, f"flat{i}")
        self.assertTrue(detector.add(6.0, 3.0e3, "drop"))

        opening = detector.run_samples()
        self.assertEqual(len(opening), 2)
        self.assertEqual(opening[0][1], 1.0e5)
        self.assertEqual(opening[0][0], 5.5)
        self.assertEqual(opening[-1][2], "drop")


class TestRecoveryWatcher(unittest.TestCase):
    """Venting back to atmosphere is what closes a run while cycling."""

    def test_fires_once_pressure_stays_up(self):
        watcher = RecoveryWatcher(1.0e5, recovery_percent=50, dwell_s=1.0)
        self.assertFalse(watcher.add(0.0, 5.0))
        self.assertFalse(watcher.add(1.0, 6.0e4))  # above, dwell starts here
        self.assertFalse(watcher.add(1.5, 7.0e4))
        self.assertTrue(watcher.add(2.0, 9.0e4))

    def test_stays_quiet_while_pumped_down(self):
        watcher = RecoveryWatcher(1.0e5, recovery_percent=50, dwell_s=1.0)
        for i in range(50):
            self.assertFalse(watcher.add(i * 0.5, 5.0))

    def test_single_spike_does_not_end_a_run(self):
        watcher = RecoveryWatcher(1.0e5, recovery_percent=50, dwell_s=1.0)
        watcher.add(0.0, 5.0)
        self.assertFalse(watcher.add(0.5, 9.9e4))  # one bad sample
        self.assertFalse(watcher.add(1.0, 5.0))    # back down: dwell resets
        self.assertFalse(watcher.add(1.5, 9.9e4))
        self.assertFalse(watcher.add(2.0, 9.9e4))
        self.assertTrue(watcher.add(2.5, 9.9e4))   # sustained for a full second

    def test_zero_dwell_fires_immediately(self):
        watcher = RecoveryWatcher(1.0e5, recovery_percent=50, dwell_s=0.0)
        self.assertTrue(watcher.add(0.0, 6.0e4))

    def test_percent_is_clamped(self):
        self.assertAlmostEqual(RecoveryWatcher(1.0e5, 0).percent, 1.0)
        self.assertAlmostEqual(RecoveryWatcher(1.0e5, 500).percent, 99.0)

    def test_threshold_scales_with_the_baseline(self):
        """A run that starts at a lower pressure recovers to a lower bar."""
        watcher = RecoveryWatcher(1.0e3, recovery_percent=50, dwell_s=0.0)
        self.assertAlmostEqual(watcher.threshold, 500.0)
        self.assertTrue(watcher.add(0.0, 600.0))

    def test_bad_baseline_never_fires(self):
        watcher = RecoveryWatcher(0.0, dwell_s=0.0)
        self.assertFalse(watcher.add(0.0, 1.0e5))

    def test_ignores_non_positive_readings(self):
        watcher = RecoveryWatcher(1.0e5, dwell_s=0.0)
        self.assertFalse(watcher.add(0.0, 0.0))
        self.assertFalse(watcher.add(0.5, -1.0))


@unittest.skipUnless(REAL_RUNS, "no recorded runs in the working directory")
class TestAgainstRecordedRuns(unittest.TestCase):
    """Replay real pump-downs from this rig through the default trigger."""

    def test_every_recorded_run_triggers(self):
        for path in REAL_RUNS:
            with self.subTest(path):
                run = load_run_csv(path)
                detector = DropDetector()
                fired_at = replay(detector, run.times, run.pressures)
                self.assertIsNotNone(fired_at, "should detect the pump starting")

                # The retained history must still begin near the starting pressure.
                # Cycled runs end vented back to atmosphere, so compare with the
                # lowest reading rather than the last one.
                pretrigger = detector.pretrigger()
                self.assertGreater(pretrigger[0][1], min(run.pressures) * 10)

    def test_run_starts_at_the_last_baseline_reading(self):
        for path in REAL_RUNS:
            with self.subTest(path):
                run = load_run_csv(path)
                detector = DropDetector()
                replay(detector, run.times, run.pressures)
                opening = detector.run_samples()

                # Cycled runs end with the vent, which can overshoot the start;
                # the baseline is the highest reading before the lowest one.
                lowest = run.pressures.index(min(run.pressures))
                baseline = max(run.pressures[: lowest + 1])
                self.assertGreaterEqual(len(opening), 2)
                # Within the resting band, as trim_to_baseline defines it.
                self.assertGreaterEqual(opening[0][1], baseline * 10 ** -0.02)
                # The very next sample is already out of the baseline band,
                # so t = 0 sits exactly where the pressure starts to fall.
                self.assertLess(opening[1][1], baseline * 10 ** -0.02)

    def test_trigger_lands_on_the_real_drop(self):
        """The steepest fall in each run should be at or after the trigger."""
        for path in REAL_RUNS:
            with self.subTest(path):
                run = load_run_csv(path)
                detector = DropDetector()
                fired_at = replay(detector, run.times, run.pressures)
                before = [p for t, p in zip(run.times, run.pressures) if t < fired_at]
                after = [p for t, p in zip(run.times, run.pressures) if t >= fired_at]
                # Nothing before the trigger dropped much; plenty does after.
                self.assertLess(min(before) / max(before), 10 ** 1.0)
                self.assertLess(min(after), min(before))


if __name__ == "__main__":
    unittest.main()
