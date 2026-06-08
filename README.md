# WaveAlign

WaveAlign is a measurement-driven room-correction tool for stereo or 2.1 systems.
It reads multi-position impulse-response captures, designs FIR correction filters
(magnitude correction + optional excess-phase alignment around the crossover), and
exports them as WAVs for convolver-based playback.

## Workflow

- Use multiple global listening-position measurements to model room behavior.
- Keep a focus-position measurement for image precision.
- Apply conservative magnitude correction that avoids chasing deep nulls.
- Optionally add low-frequency excess-phase alignment around the crossover.
- Tilt and shape the corrected response with a layered target curve so the
  filter delivers both **room correction** and **voicing** in one pass.

## Features

- Robust global averaging using the median across multiple IR captures (suppresses
  any single bad position).
- Focus-aware blending: global model dominates the bass region, focus model
  dominates the midrange and above.
- Frequency-dependent correction limits — more cut authority than boost
  authority, with the bass band allowing the deepest cuts.
- Null guard: detects steep narrow notches in the focus response and clamps
  boost to a safe ceiling so the filter doesn't try to fill an SBIR null.
- Bass-floor preservation so the filter won't accidentally flatten a musically
  intended low-end shelf.
- Layered, fully parameterised target curve — bass shelf, treble tilt anchored
  at a configurable frequency, mid-bass warmth bell, presence shelf, air shelf.
- Standalone Harman target (independent of `custom` parameters).
- Per-channel correction floor (the algorithm reports each channel's actual
  correction range; symmetric extremes used to be a bug — see *Notes*).
- Optional excess-phase correction tapered around the crossover.
- Optional split output (magnitude FIR + phase FIR) or combined single FIR.
- Diagnostic plot and JSON report capturing every applied parameter.
- Clip warnings on stderr when peak-limiting overrides the midband RMS
  normalization.

## Requirements

- Python 3.10+
- Dependencies listed in `Required.txt` (numpy, scipy, soundfile, matplotlib).

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r Required.txt
```

## Input Measurements

WaveAlign expects mono impulse-response WAV files at a single shared sample rate:

- `L_Global*.wav`, `R_Global*.wav` — multi-position global captures (4+ each
  recommended).
- `L_focus.wav`, `R_focus.wav` — focus-seat measurements.

The script trims leading silence and aligns to the impulse onset before
analysis. Inputs must already share the same sample rate as `--sample_rate`
(default 48000 Hz); resampling on input is not performed.

## Quick start — neutral room correction

```bash
python3 wavealign.py \
  --left_global  '/path/to/L_Global_*.wav' \
  --right_global '/path/to/R_Global_*.wav' \
  --left_focus   '/path/to/L_focus.wav' \
  --right_focus  '/path/to/R_focus.wav' \
  --target_curve custom \
  --bass_boost_db 5.0 \
  --treble_tilt_db -0.8 \
  --crossover_freq 80 \
  --combined \
  --out wavealign_filter.wav \
  --plot wavealign_plot.png \
  --report_json wavealign_report.json
```

## Quick start — voiced ("smoky-club" warm/full) target

The new target-curve parameters let you stack a coherent voicing on top of the
room correction. This recipe produces a warm, dark, full-bodied character
(approx. −12 dB total spectral tilt, mid-bass warmth lift, tamed presence,
rolled air):

```bash
python3 wavealign.py \
  --left_global  '/path/to/L_Global_*.wav' \
  --right_global '/path/to/R_Global_*.wav' \
  --left_focus   '/path/to/L_focus.wav' \
  --right_focus  '/path/to/R_focus.wav' \
  --target_curve custom \
  --bass_boost_db 6.0 \
  --treble_tilt_db -1.2 \
  --tilt_anchor_hz 800 \
  --mid_bass_lift_db 2.5 \
  --mid_bass_lift_hz 280 \
  --mid_bass_lift_q 1.2 \
  --presence_shelf_db -2.5 \
  --presence_shelf_hz 3000 \
  --air_shelf_db -3.0 \
  --air_shelf_hz 10000 \
  --crossover_freq 60 \
  --preserve_bass_floor_db -1.0 \
  --combined \
  --out wavealign_smoky_club.wav \
  --plot wavealign_smoky_club_plot.png \
  --report_json wavealign_smoky_club_report.json
```

> **Caution.** `--bass_boost_db 6` combined with `--crossover_freq 60` sits at
> the edge of the safety threshold (the trip is `>= 8 dB` *and* `<= 60 Hz`).
> Confirm against your speaker's F3 / Xmax before applying at elevated SPL.

## Split output

Without `--combined`, the script writes the magnitude and phase FIRs as two
files — useful when your convolver chain handles them in separate stages:

```bash
python3 wavealign.py \
  --left_global  '/path/to/L_Global_*.wav' \
  --right_global '/path/to/R_Global_*.wav' \
  --left_focus   '/path/to/L_focus.wav' \
  --right_focus  '/path/to/R_focus.wav' \
  --out wavealign_filter.wav
# produces:
#   wavealign_filter_magnitude.wav
#   wavealign_filter_phase.wav   (omitted if --no_phase)
```

## Target curve composition

The `custom` target is built additively in dB from the following layers, in
order:

1. **Bass shelf** — full `--bass_boost_db` at and below 20 Hz, tapering log-
   linearly to 0 dB at the shelf corner. The corner now tracks crossover:
   `corner_hz = crossover_freq * 2.0` (so 120 Hz at the default 60 Hz
   crossover, 160 Hz at 80 Hz crossover).
2. **Treble tilt** — `treble_tilt_db` per octave above `--tilt_anchor_hz`.
3. **Mid-bass bell** *(optional, off by default)* — symmetric Lorentzian bell
   in log-frequency centred at `--mid_bass_lift_hz`, height
   `--mid_bass_lift_db`, width set by `--mid_bass_lift_q`.
4. **Presence shelf** *(optional, off by default)* — high-shelf transition
   centred at `--presence_shelf_hz`, gain `--presence_shelf_db`.
5. **Air shelf** *(optional, off by default)* — high-shelf transition centred
   at `--air_shelf_hz`, gain `--air_shelf_db`.

The `harman` target is a standalone branch and ignores `custom`-style flags;
it produces a fixed Harman-shaped curve (4 dB shelf @ 100 Hz, −0.8 dB/oct
above 1 kHz, −1.5 dB presence shelf @ 2.5 kHz).

`flat` returns zeros — the filter then performs room correction only, with
no tonal target on top.

## Useful options

| Flag | Default | Purpose |
|---|---|---|
| `--target_curve {flat,custom,harman}` | `custom` | Target shape family |
| `--bass_boost_db` | `5.0` | Low-frequency target lift in dB |
| `--treble_tilt_db` | `-0.8` | dB/oct tilt above `--tilt_anchor_hz` |
| `--tilt_anchor_hz` | `1000.0` | Frequency where tilt = 0 dB |
| `--mid_bass_lift_db` | `0.0` | Mid-bass warmth bell gain (set non-zero to enable) |
| `--mid_bass_lift_hz` | `280.0` | Bell centre frequency |
| `--mid_bass_lift_q` | `1.2` | Bell Q (higher = narrower) |
| `--presence_shelf_db` | `0.0` | Presence shelf gain (set non-zero to enable) |
| `--presence_shelf_hz` | `3000.0` | Presence shelf corner |
| `--air_shelf_db` | `0.0` | Air shelf gain (set non-zero to enable) |
| `--air_shelf_hz` | `10000.0` | Air shelf corner |
| `--crossover_freq` | `60.0` | Sub/main crossover reference; sets bass shelf corner via × 2 |
| `--phase_correction_octaves` | `1.6` | Phase-correction span above crossover |
| `--preserve_bass_floor_db` | `-1.5` | Floor for correction in the bass-anchor zone |
| `--no_phase` | off | Disable excess-phase correction |
| `--mag_fir_length` | `8192` | Magnitude FIR taps |
| `--fir_length` | `16384` | Phase FIR taps |
| `--output_sample_rate` | input SR | Resample the output filter (e.g. 44100, 96000) |
| `--bit_depth {16,24,32}` | `32` | Output WAV bit depth (32 = float, others = PCM) |
| `--combined` | off | Convolve magnitude + phase into a single FIR |

See all options:

```bash
python3 wavealign.py --help
```

## Outputs

- **WAV filter** at `--out` (combined), or `<out>_magnitude.wav` +
  `<out>_phase.wav` (split).
- **Plot** at `--plot` showing per-channel room model, target, applied
  correction, residual, and FIR magnitude response.
- **Report JSON** at `--report_json` recording every applied parameter plus
  per-channel correction min/max — auditable per run.

## Notes

- Inputs must share the same sample rate as `--sample_rate`. The script does
  not resample on input.
- The IR onset is detected at 1 % of peak amplitude and pre-rolled by 8
  samples to stabilize alignment.
- The combined output filter length is `mag_fir_length + fir_length − 1`
  samples — at default settings, **24 575 taps (≈ 512 ms total at 48 kHz)**.
  The *effective playback latency* is roughly half that, since the linear-
  phase phase FIR is centred: about **171 ms** at default settings. That is
  imperceptible for music playback (Roon, Tidal, local) but starts to matter
  for video sync (consumer A/V tolerance is ~150–200 ms). If you route this
  filter through a video chain, halve `--fir_length` to 8192 (latency drops
  to ~85 ms) at the cost of about 3–4 dB of phase-correction fidelity in the
  crossover band — usually a worthwhile trade for video.
- Per-channel correction floors will normally differ between L and R in any
  asymmetric room — that is expected behavior. If you ever see them clamp to
  *exactly* the same value across channels, suspect the algorithm.
- Stderr will emit `peak-clip` warnings when the 0.99 normalization limiter
  overrides the midband RMS normalization. Sub-0.1 dB warnings are normal;
  larger ones flag a target curve that demands too much gain.

## Diagnosing a run

Open the diagnostic plot first. Each channel panel shows:

- Room model (blended global + focus) in blue/red.
- Target curve in dashed green.
- Applied correction in orange (clamped by the per-band correction limits).
- Resulting FIR magnitude response in the lower row.
- Excess phase and applied phase correction in the right-hand panels.

The JSON report records every parameter and the actual correction extremes
each channel hit. A run is suspicious if:

- Both channels report identical correction extremes in an asymmetric room.
- Phase correction shows excess phase that's not being acted on inside the
  taper window.
- Peak-clip warnings exceed ~0.5 dB.

## License

AGPL-3.0. See `LICENSE`.
