# WaveAlign

WaveAlign is a measurement-driven room correction script for stereo or 2.1 systems.

It is designed around a practical workflow:
- Use multiple global listening-position measurements to model room behavior.
- Keep a focus-position measurement for image precision.
- Apply conservative magnitude correction that avoids chasing deep nulls.
- Optionally add low-frequency excess-phase alignment around crossover.

The tool exports FIR filters as WAV files for convolver-based playback chains.

## Features

- Robust global averaging using median statistics across multiple IR captures.
- Focus-aware correction blending (global in bass, focus in mids/highs).
- Frequency-dependent correction limits to reduce overfitting risk.
- Null guard to avoid unsafe boost in deep cancellation zones.
- Optional split output (magnitude + phase) or combined FIR output.
- Optional diagnostic plot and JSON report.

## Requirements

- Python 3.10+
- Dependencies listed in `Required.txt`

## Setup

Create and activate a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r Required.txt
```

## Input Measurements

WaveAlign expects mono impulse-response WAV files (same sample rate):
- `L_Global*.wav` and `R_Global*.wav`: multi-position measurements.
- `L_focus.wav` and `R_focus.wav`: focus-seat measurements.

## Quick Start

Run with combined output (single FIR per channel):

```bash
python3 wavealign.py \
	--left_global '/path/to/L_Global*.wav' \
	--right_global '/path/to/R_Global*.wav' \
	--left_focus '/path/to/L_focus.wav' \
	--right_focus '/path/to/R_focus.wav' \
	--target_curve custom \
	--bass_boost_db 6.0 \
	--treble_tilt_db -0.8 \
	--combined \
	--out wavealign_filter.wav \
	--plot wavealign_plot.png \
	--report_json wavealign_report.json
```

Run with split output (magnitude + phase files):

```bash
python3 wavealign.py \
	--left_global '/path/to/L_Global*.wav' \
	--right_global '/path/to/R_Global*.wav' \
	--left_focus '/path/to/L_focus.wav' \
	--right_focus '/path/to/R_focus.wav' \
	--out wavealign_filter.wav
```

This produces:
- `wavealign_filter_magnitude.wav`
- `wavealign_filter_phase.wav` (if phase is enabled)

## Useful Options

- `--target_curve {flat,custom,harman}`
- `--crossover_freq 60`
- `--phase_correction_octaves 1.6`
- `--preserve_bass_floor_db -1.5`
- `--no_phase` to disable phase correction
- `--output_sample_rate 48000` to resample output filter
- `--bit_depth {16,24,32}`

See all options:

```bash
python3 wavealign.py --help
```

## Notes

- Input files must share the same sample rate as `--sample_rate`.
- The script trims leading latency in IRs by default to stabilize alignment.
- Filters are normalized conservatively to avoid clipping and preserve bass balance.

## License

This project is licensed under AGPL-3.0. See `LICENSE`.
