# Pump-down + PL monitor

Live pressure logging and plotting for Edwards **APG200 / nAPG200** digital Pirani gauges,
over **WiFi** (USR-W610 RS485↔TCP bridge) or a **USB-RS485** adapter, together with
live photoluminescence (PL) spectra from an **Ocean Optics USB4000** spectrometer
(via [python-seabreeze](https://github.com/ap--/python-seabreeze)).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

On the Windows lab PC the venv is `.venv\` and `run_windows.bat` launches the app.
The spectrometer additionally needs, once per PC:

- `msvcp140.dll` for seabreeze's driver DLL — `requirements.txt` installs it
  into the venv (`msvc-runtime`), so no system-wide Visual C++ install is needed;
- the Ocean Optics USB driver: run `.venv\Scripts\seabreeze_os_setup.exe` as
  administrator, then plug in the USB4000.

A **Simulated USB4000** is always in the spectrometer list, so the PL side can be
tried without hardware.

## Pump-down monitor

Pick a transport, **Connect**, then **Start run**. Host, port, baud, units,
interval and trigger thresholds live in **Settings…** so Arm stays on screen
without scrolling. **Overlay all** sits next to the plot's log/linear toggle.
The app shows the live reading, elapsed time, sample count, lowest pressure
reached, and pump-down rate in decades/minute, plus a log-scale pressure-vs-time
curve. Each run writes a timestamped CSV in `data/` (`pressure_YYYYmmdd_HHMMSS.csv`);
**Save plot** writes a PNG there too. Recorded CSVs, PL files and plots are
gitignored so lab data is not pushed to GitHub.

Keyboard: **S** start/arm, **X** stop, **C** clear the live curve, **P** save a PL spectrum.

## PL spectrometer

The **Spectrometer** card connects the USB4000 and shows its live spectrum next to
the pressure curve. **Integration (ms)** and **Boxcar width (px)** apply on Enter.
Boxcar width N averages each pixel with N neighbours on each side (2N+1 pixels).
**Electric dark** uses the detector's masked pixels; **Capture dark** (block the
light first) stores a dark spectrum and turns on **Subtract dark**. Scans to
average, nonlinearity correction and the PL polling interval are in **Settings…**.

- **Record PL with each pump-down run** (on by default) — every run, manual or
  auto-triggered, also records PL at the polling interval into
  `pl_<timestamp>[_runNN].npz` next to its `pressure_….csv`. `elapsed_s` in the
  PL file shares the run's t = 0 (spectra from just before an auto-trigger are
  kept for this), and the file also holds the run's `pressure_elapsed_s`,
  `pressure`, `pressure_units` and `pressure_csv`, so PL vs time vs pressure can
  be plotted from one file.
- **Save spectrum · P** — saves the next fresh spectrum to `pl_…_single.npz`,
  with the current gauge reading if a run is live.
- **Record PL** — a standalone PL time series, independent of the gauge.

Acquisition settings are locked while PL is recording, so all spectra in a file are
comparable. Each `.npz` holds `wavelengths`, `intensities` (as displayed),
`raw_intensities` (before stored-dark subtraction and boxcar), `dark_spectrum` and
all settings; time series add `timestamps`, `elapsed_s`, `saturated` and `complete`
(False means a mid-run autosave). Load with `np.load(path)`.

### Sampling rate

The default interval is **0.1 s (10 Hz)**. Lower it to go faster; the
**Samples · measured rate** tile reports the rate actually achieved, which is
what matters because the limit is the gauge round trip, not the app:

| Round trip | Achievable rate |
|-----------|-----------------|
| 30 ms (9600 baud, wire time only) | ~30 Hz |
| 50 ms | ~19 Hz |
| 100 ms | ~10 Hz |

A `?V752` request plus its reply is about 28 bytes, so 9600 baud alone costs
~29 ms per sample and caps polling near 30 Hz; gauge processing and, over WiFi,
the bridge's UART buffering add to that. The app's own cost is 1.3 ms per sample,
so it is never the constraint — raising the gauge's baud rate is the only way
past ~30 Hz. If the gauge replies `error 08` (commands sent too fast), the
polling interval eases off automatically and says so.

### Auto-trigger mode

Switch the **Run** card to **Auto-trigger** and press **Arm** instead of Start.
The gauge is polled continuously without writing anything; the run starts by
itself the moment the pressure falls by the configured amount, so you can arm the
app and then walk over and open the valve.

- **Trigger on a drop of (decades)** / **…within (s)** — the run starts when the
  reading falls this far below the highest reading in the trailing window.
  Comparing decades keeps the test independent of the starting pressure and of
  the selected units. The default 0.30 decades in 2 s sits well clear of gauge
  noise at atmosphere (~0.04 decades) and well below a real pump-down on this
  rig (0.9–3.3 decades per 0.5 s sample).
- **Baseline lookback (s)** — how far back the buffered history is searched for
  the resting pressure. The run is anchored at the **last reading still at that
  pressure**, so `t = 0` is the moment the pressure starts to fall and the flat
  idle stretch before it is discarded. A reading counts as "still resting" while
  it is within 0.02 decades (~4.5%) of the highest pressure in the buffer.

Everything else behaves as in manual mode: same CSV format, stats, overlays,
reference curves and shortcuts. **Stop** while armed just disarms.

### Cycling through several devices

Turn on **Cycle runs automatically** to capture a whole batch from a single
**Arm** press — useful when each pump-down is one spin-coated device and nobody
wants to press Start and Stop twenty times:

1. The drop is detected and the run records as usual.
2. When the chamber vents back to **% of starting pressure** (default 95%) and
   stays there for a second, the run is closed and saved.
3. The app re-arms immediately, so the next pump-down becomes the next run.

Runs are numbered in sequence — `Run 1`, `Run 2`, `Run 3` in the legend, with
matching files `pressure_<timestamp>_run01.csv`, `_run02`, and so on. Turn on
**Overlay all curves** to watch the batch stack up on one plot. Each curve
includes the vent back to atmosphere at its end, since that is what closes it.
Numbering restarts at 1 after **Clear stored runs**.

The one-second dwell means a vent that only touches atmosphere briefly will not
end a run; it has to stay up.

### Comparing runs

- **Save CSVs to** — in Settings; the choice is remembered between launches in
  `~/.pumpdown_pl_monitor.json`.
- **Overlay all** — every completed run from this session is drawn behind the
  live curve in its own color, and the axes rescale to fit them all.
- **Reference curve** — pin any stored run, or load a CSV from an earlier
  session, as a dashed grey baseline that stays on the plot while a new run
  records against it.
- **Clear stored runs** — drops the overlay history and the reference; the CSVs
  on disk are untouched.

## Tests

```bash
python -m unittest discover -s tests -t .
```

Protocol parsing, metrics and drop detection are covered with a fake serial link,
so no hardware is needed. `tests/test_trigger.py` also replays recorded
`data/pressure_*.csv` runs through the default trigger and checks each one fires
on the real pump start.

## Files

| Path | Purpose |
|------|---------|
| `main.py` | Launch the desktop app |
| `app.py` | UI, sampling loop, live plot |
| `pl_panel.py` | Spectrometer card, live spectrum, PL recording linked to runs |
| `spectrometer/` | seabreeze device + simulator, acquisition thread, dark/boxcar, `.npz` storage |
| `gauge.py` | RS485/TCP transports and the gauge ASCII protocol |
| `trigger.py` | Drop detection, baseline anchoring, vent/recovery watching |
| `metrics.py` | Derived run statistics (elapsed, rate, minimum) |
| `runs.py` | Stored runs, CSV loading, overlay colors |
| `config.py` | Remembers Settings (folder, host, trigger values) |
| `data/` | Recorded CSVs, PL `.npz` files and saved plots (not in git) |
| `run_windows.bat` | Launch from `.venv` on Windows |
| `tests/` | Unit tests (no hardware required) |

## Wiring notes

- Gauge needs **15–48 V** (24 V typical) — the bridge's 5–36 V terminals are a
  passthrough, not a boost, so 12 V in will not run the gauge.
- Share signal ground: gauge **pin 5** and supply **0 V** to the adapter ground.
- RS485: gauge **pin 9 → A**, **pin 8 → B**. If the idle bus streams `0x00`,
  swap A and B.
- Verified link settings: **9600 8N1**, transparent TCP server on port **8899**.
  A faster UART baud on both the gauge and the bridge is what raises the
  achievable sample rate.
