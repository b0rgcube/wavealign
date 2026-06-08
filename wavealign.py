#!/usr/bin/env python3
"""
Room correction v4 (Lyngdorf-inspired workflow)

Design goals:
- Use multi-point global measurements for robust room behavior.
- Preserve focus-position imaging while avoiding overfitting.
- Protect bass energy for 2.1 systems with sub integration at crossover.
- Keep correction conservative where measurements are unreliable.
- Optionally add low-frequency excess-phase alignment around crossover.

The script outputs either:
1) Separate magnitude and phase filters, or
2) A single combined FIR filter.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import soundfile as sf
from scipy import signal
from scipy.fft import irfft, rfft, rfftfreq


EPS = 1e-12


def _next_pow2(n: int) -> int:
	"""Return the next power-of-two FFT size for stable, efficient transforms."""
	return 1 << int(np.ceil(np.log2(max(1, n))))


def _db(x: np.ndarray) -> np.ndarray:
	"""Convert linear amplitude to dB with numerical floor protection."""
	return 20.0 * np.log10(np.maximum(np.abs(x), EPS))


def _db_to_lin(x_db: np.ndarray) -> np.ndarray:
	"""Convert dB values back to linear amplitude."""
	return 10.0 ** (x_db / 20.0)


def _raised_cosine_transition(freqs: np.ndarray, f1: float, f2: float, invert: bool = False) -> np.ndarray:
	"""Smooth transition: 1 below f1, 0 above f2 (or inverted)."""
	out = np.ones_like(freqs, dtype=float)
	below = freqs <= f1
	above = freqs >= f2
	mid = ~(below | above)
	out[above] = 0.0
	if np.any(mid):
		out[mid] = 0.5 * (1.0 + np.cos(np.pi * (freqs[mid] - f1) / max(f2 - f1, 1e-9)))
	if invert:
		return 1.0 - out
	return out


def _smooth_log_freq(freqs: np.ndarray, values: np.ndarray, octave_fraction: float) -> np.ndarray:
	"""Median smoothing in log-frequency windows."""
	out = values.copy()
	n = len(freqs)
	for i in range(n):
		f = freqs[i]
		if f < 15.0:
			continue
		f_low = f / (2 ** (1.0 / (2.0 * octave_fraction)))
		f_high = f * (2 ** (1.0 / (2.0 * octave_fraction)))
		s = np.searchsorted(freqs, f_low)
		e = np.searchsorted(freqs, f_high)
		if e > s:
			out[i] = np.median(values[s:e])
	return out


def _minimum_phase_spectrum_from_mag(mag_lin: np.ndarray, n_fft: int) -> np.ndarray:
	"""Real cepstrum minimum-phase reconstruction from one-sided magnitude.

	Input: one-sided rfft-shaped magnitude array of length n_fft//2 + 1.
	Output: one-sided complex spectrum of the same length.

	irfft reconstructs the full even-symmetric two-sided log-magnitude so
	the cepstrum is computed correctly rather than zero-padding the missing
	negative-frequency half (the ifft path's failure mode).
	"""
	log_mag = np.log(np.maximum(mag_lin, EPS))
	# irfft treats log_mag as one-sided and returns a real, length-n_fft cepstrum.
	ceps = irfft(log_mag, n=n_fft)
	ceps_min = np.zeros_like(ceps)
	ceps_min[0] = ceps[0]
	if n_fft % 2 == 0:
		# Even: keep DC, double positive-quefrency bins, keep Nyquist, zero negative.
		nyq = n_fft // 2
		ceps_min[1:nyq] = 2.0 * ceps[1:nyq]
		ceps_min[nyq] = ceps[nyq]
	else:
		# Odd: keep DC, double positive-quefrency bins, zero negative.
		ceps_min[1:(n_fft + 1) // 2] = 2.0 * ceps[1:(n_fft + 1) // 2]
	# rfft of the liftered real cepstrum gives the one-sided minimum-phase spectrum.
	return np.exp(rfft(ceps_min, n=n_fft))


def _safe_unwrap(phase: np.ndarray) -> np.ndarray:
	if phase.size == 0:
		return phase
	return np.unwrap(phase)


def _load_ir_mono(path: str, sample_rate: int) -> np.ndarray:
	x, fs = sf.read(path)
	if x.ndim > 1:
		x = x[:, 0]
	if fs != sample_rate:
		raise ValueError(f"Sample rate mismatch for {path}: got {fs}, expected {sample_rate}")
	return x.astype(np.float64, copy=False)


def _trim_to_impulse_start(ir: np.ndarray, threshold: float = 0.01) -> np.ndarray:
	p = np.max(np.abs(ir))
	if p < EPS:
		return ir
	idx = np.where(np.abs(ir) >= p * threshold)[0]
	if idx.size == 0:
		return ir
	s = max(0, int(idx[0]) - 8)
	return ir[s:]


def _load_ir_list(pattern: str, sample_rate: int, trim_latency: bool) -> List[np.ndarray]:
	paths = sorted(glob.glob(pattern))
	if not paths:
		raise FileNotFoundError(f"No files match pattern: {pattern}")

	out: List[np.ndarray] = []
	for p in paths:
		ir = _load_ir_mono(p, sample_rate)
		if trim_latency:
			ir = _trim_to_impulse_start(ir)
		out.append(ir)
	return out


def _robust_global_average(
	global_irs: List[np.ndarray],
	n_fft: int,
	fs: int,
	octave_fraction: float = 12.0,
) -> np.ndarray:
	"""Return robust average magnitude (dB) from multi-position measurements."""
	mags = []
	freqs = rfftfreq(n_fft, 1 / fs)
	for ir in global_irs:
		h = rfft(ir, n=n_fft)
		m = _db(h)
		m = _smooth_log_freq(freqs, m, octave_fraction=octave_fraction)
		mags.append(m)
	return np.median(np.vstack(mags), axis=0)


def _focus_response(focus_ir: np.ndarray, n_fft: int, fs: int, octave_fraction: float = 24.0) -> np.ndarray:
	"""Return a lightly smoothed focus-position response in dB."""
	freqs = rfftfreq(n_fft, 1 / fs)
	h = rfft(focus_ir, n=n_fft)
	m = _db(h)
	return _smooth_log_freq(freqs, m, octave_fraction=octave_fraction)


@dataclass
class TargetCurveParams:
	"""All parameters that shape the target equalization curve."""
	curve: str = "custom"
	bass_boost_db: float = 5.0
	treble_tilt_db_per_oct: float = -0.8
	tilt_anchor_hz: float = 1000.0
	mid_bass_lift_db: float = 0.0
	mid_bass_lift_hz: float = 280.0
	mid_bass_lift_q: float = 1.2
	presence_shelf_db: float = 0.0
	presence_shelf_hz: float = 3000.0
	air_shelf_db: float = 0.0
	air_shelf_hz: float = 10000.0
	# Crossover Hz is needed for the bass-shelf corner (shelf_corner = crossover*2).
	crossover_hz: float = 60.0


def _high_shelf_db(freqs: np.ndarray, corner_hz: float, gain_db: float) -> np.ndarray:
	"""Shelf using a raised-cosine transition: 0 below corner/sqrt(2), gain_db above corner*sqrt(2)."""
	if gain_db == 0.0:
		return np.zeros_like(freqs)
	f_lo = corner_hz / np.sqrt(2.0)
	f_hi = corner_hz * np.sqrt(2.0)
	fade = _raised_cosine_transition(freqs, f_lo, f_hi, invert=True)
	return gain_db * fade


def _target_curve_db(freqs: np.ndarray, params: TargetCurveParams) -> np.ndarray:
	"""Return the target EQ curve in dB at each frequency bin.

	LAYER 0 — flat: all zeros.
	LAYER 1 — bass shelf: log-linear taper from bass_boost_db at DC to 0 dB at shelf_corner.
	LAYER 2 — treble tilt: slope in dB/octave above tilt_anchor_hz.
	LAYER 3 — mid-bass bell: symmetrical log-frequency bell (skip when lift == 0).
	LAYER 4 — presence shelf: high shelf at presence_shelf_hz.
	LAYER 5 — air shelf: high shelf at air_shelf_hz.

	Harman branch overrides layers 1-5 with a fixed shape; user params are ignored there.
	"""
	target = np.zeros_like(freqs, dtype=float)
	if params.curve == "flat":
		return target

	if params.curve == "harman":
		# Fixed Harman shape — does not recurse, user bass/treble/etc. params are ignored.
		# Layer 1: bass shelf with fixed corner=100 Hz and boost=4 dB.
		harman_shelf_corner = 100.0
		log_20 = np.log2(20.0)
		log_corner = np.log2(harman_shelf_corner)
		f_clamp = np.clip(freqs, 20.0, harman_shelf_corner)
		t = (np.log2(f_clamp) - log_20) / (log_corner - log_20)
		bass = np.where(freqs < 20.0, 4.0, np.where(freqs < harman_shelf_corner, 4.0 * (1.0 - t), 0.0))
		# Layer 2: fixed tilt -0.8 dB/oct from 1000 Hz.
		tilt = -0.8 * np.log2(np.clip(freqs, 1000.0, None) / 1000.0)
		# Layer 4: presence shelf at 2500 Hz, -1.5 dB.
		presence = _high_shelf_db(freqs, 2500.0, -1.5)
		target = bass + tilt + presence
		return target

	# Custom (and any future) curve — additive layers.

	# Layer 1 — bass shelf (vectorized).
	shelf_corner = params.crossover_hz * 2.0
	log_20 = np.log2(20.0)
	log_corner = np.log2(shelf_corner)
	f_clamp = np.clip(freqs, 20.0, shelf_corner)
	t = (np.log2(f_clamp) - log_20) / (log_corner - log_20)
	bass = np.where(
		freqs < 20.0,
		params.bass_boost_db,
		np.where(freqs < shelf_corner, params.bass_boost_db * (1.0 - t), 0.0),
	)
	target += bass

	# Layer 2 — treble tilt above tilt_anchor_hz.
	tilt = params.treble_tilt_db_per_oct * np.log2(np.clip(freqs, params.tilt_anchor_hz, None) / params.tilt_anchor_hz)
	target += tilt

	# Layer 3 — mid-bass bell (skip when lift is zero).
	if params.mid_bass_lift_db != 0.0:
		log_ratio = np.log2(np.clip(freqs, 1.0, None) / params.mid_bass_lift_hz)
		bell = params.mid_bass_lift_db / (1.0 + (log_ratio * params.mid_bass_lift_q) ** 2)
		target += bell

	# Layer 4 — presence shelf.
	target += _high_shelf_db(freqs, params.presence_shelf_hz, params.presence_shelf_db)

	# Layer 5 — air shelf.
	target += _high_shelf_db(freqs, params.air_shelf_hz, params.air_shelf_db)

	return target


def _null_guard_limit(freqs: np.ndarray, focus_mag_db: np.ndarray) -> np.ndarray:
	"""Limit boosts around deep dips to avoid trying to fill room nulls."""
	local_trend = _smooth_log_freq(freqs, focus_mag_db, octave_fraction=6.0)
	dip_depth = local_trend - focus_mag_db
	max_boost = np.full_like(freqs, 2.5)

	severe = dip_depth > 8.0
	moderate = (dip_depth > 4.0) & ~severe
	max_boost[moderate] = 1.0
	max_boost[severe] = 0.3

	# In deep bass, still allow a little support, but never large null filling.
	bass = freqs < 90.0
	max_boost[bass] = np.minimum(max_boost[bass], 1.5)
	return max_boost


def _freq_dependent_limits(freqs: np.ndarray, crossover_hz: float = 60.0) -> Tuple[np.ndarray, np.ndarray]:
	"""Return (max_cut_db, max_boost_db) per frequency.

	The bass/low-mid boundary tracks crossover_hz * 2.0 so that limit behavior
	scales consistently when the crossover is changed.
	"""
	bass_boundary = crossover_hz * 2.0
	max_cut = np.full_like(freqs, 4.0)
	max_boost = np.full_like(freqs, 1.2)

	bass = freqs <= bass_boundary
	low_mid = (freqs > bass_boundary) & (freqs <= 500.0)
	high = freqs > 500.0

	max_cut[bass] = 6.0
	max_boost[bass] = 2.5
	max_cut[low_mid] = 5.0
	max_boost[low_mid] = 1.8
	max_cut[high] = 4.0
	max_boost[high] = 1.0

	return max_cut, max_boost


def _design_magnitude_correction(
	freqs: np.ndarray,
	global_mag_db: np.ndarray,
	focus_mag_db: np.ndarray,
	target_db: np.ndarray,
	crossover_hz: float,
	preserve_bass_floor_db: float,
) -> Dict[str, np.ndarray]:
	"""Blend global+focus model and create safe correction curve."""
	# Blend: heavier global weight in bass, gradually more focus in mids/highs.
	w_global = _raised_cosine_transition(freqs, crossover_hz * 1.5, 1200.0)
	room_model = w_global * global_mag_db + (1.0 - w_global) * focus_mag_db

	# Normalize model and target to 1 kHz anchor.
	ref_idx = int(np.argmin(np.abs(freqs - 1000.0)))
	room_model_norm = room_model - room_model[ref_idx]
	target_norm = target_db - target_db[ref_idx]

	raw = target_norm - room_model_norm

	# Modal emphasis: add extra peak cuts where persistent positive excess exists.
	smooth_room = _smooth_log_freq(freqs, room_model_norm, octave_fraction=4.0)
	modal_excess = np.maximum(room_model_norm - smooth_room, 0.0)
	modal_cut = -0.4 * np.clip(modal_excess, 0.0, 8.0)
	raw += modal_cut

	max_cut, max_boost = _freq_dependent_limits(freqs, crossover_hz=crossover_hz)
	max_boost = np.minimum(max_boost, _null_guard_limit(freqs, focus_mag_db))

	correction = np.clip(raw, -max_cut, max_boost)

	# Preserve bass energy around sub/main integration so system stays full-size.
	bass_anchor = (freqs >= 25.0) & (freqs <= max(95.0, crossover_hz * 1.8))
	correction[bass_anchor] = np.maximum(correction[bass_anchor], preserve_bass_floor_db)

	correction = _smooth_log_freq(freqs, correction, octave_fraction=10.0)
	correction[freqs < 12.0] = 0.0

	return {
		"room_model_db": room_model_norm,
		"target_db": target_norm,
		"raw_correction_db": raw,
		"correction_db": correction,
	}


def _synthesize_min_phase_fir(correction_db: np.ndarray, n_fft: int, fir_len: int, fs: int) -> np.ndarray:
	"""Convert magnitude correction curve to a minimum-phase FIR filter."""
	mag_lin = np.clip(_db_to_lin(correction_db), 0.08, 12.0)
	h_min_spec = _minimum_phase_spectrum_from_mag(mag_lin, n_fft=n_fft)
	h_full = irfft(h_min_spec, n=n_fft).real
	fir = h_full[:fir_len].copy()

	# Soft taper for cleaner tail.
	tail = min(512, max(16, fir_len // 8))
	taper = np.cos(np.linspace(0.0, np.pi / 2.0, tail)) ** 2
	fir[-tail:] *= taper

	# RMS-normalize around midband rather than peak-normalize to avoid bass thinning.
	w, H = signal.freqz(fir, worN=8192, fs=fs)
	mid = (w >= 500.0) & (w <= 2000.0)
	if np.any(mid):
		rms = np.sqrt(np.mean(np.abs(H[mid]) ** 2))
		if rms > EPS:
			fir /= rms

	peak = np.max(np.abs(fir))
	if peak > 0.99:
		clip_db = 20.0 * np.log10(peak / 0.99)
		print(
			f"WARNING: magnitude FIR peak-clipped by {clip_db:.2f} dB "
			"(midband RMS normalization overridden by peak limiter)",
			file=sys.stderr,
		)
		fir = fir / peak * 0.99
	return fir


def _design_phase_correction(
	focus_ir: np.ndarray,
	n_fft: int,
	fs: int,
	crossover_hz: float,
	correction_end_hz: float,
) -> Dict[str, np.ndarray]:
	freqs = rfftfreq(n_fft, 1 / fs)
	h_meas = rfft(focus_ir, n=n_fft)
	mag = np.maximum(np.abs(h_meas), EPS)

	# Minimum-phase equivalent from measured magnitude.
	h_min = _minimum_phase_spectrum_from_mag(mag, n_fft=n_fft)
	# Clamp denominator magnitude while preserving phase direction so the
	# complex division never produces infinite or NaN values.
	h_min_mag = np.abs(h_min)
	h_min_safe = np.where(h_min_mag > EPS, h_min, EPS * np.exp(1j * np.angle(h_min)))
	h_excess = h_meas / h_min_safe
	phase_excess = _safe_unwrap(np.angle(h_excess))

	# Remove linear trend (bulk delay) in bass alignment band.
	band = (freqs >= 20.0) & (freqs <= min(250.0, correction_end_hz))
	if np.sum(band) > 10:
		p = np.polyfit(freqs[band], phase_excess[band], 1)
		phase_excess = phase_excess - (p[0] * freqs + p[1])

	phase_corr = -phase_excess

	# Apply smooth correction window that peaks AT the crossover frequency.
	# win_low: 0→1 ramp from 15→25 Hz (sub-region gate).
	# win_rise: 0→1 ramp from crossover/2 → crossover (rising flank).
	# win_fall: 1→0 taper from crossover → correction_end_hz (falling flank).
	# Together they form a tent with its apex at crossover_hz.
	win_low = _raised_cosine_transition(freqs, 15.0, 25.0, invert=True)
	win_rise = _raised_cosine_transition(freqs, crossover_hz / 2.0, crossover_hz, invert=True)
	win_fall = _raised_cosine_transition(freqs, crossover_hz, correction_end_hz)
	win = win_low * win_rise * win_fall
	phase_corr *= win
	phase_corr = _smooth_log_freq(freqs, phase_corr, octave_fraction=8.0)

	max_rad = np.deg2rad(220.0)
	phase_corr = np.clip(phase_corr, -max_rad, max_rad)
	phase_corr[freqs < 12.0] = 0.0
	phase_corr[freqs > correction_end_hz] = 0.0

	return {
		"freqs": freqs,
		"phase_excess": phase_excess,
		"phase_correction": phase_corr,
	}


def _synthesize_phase_fir(phase_corr: np.ndarray, n_fft: int, fir_len: int) -> np.ndarray:
	"""Synthesize an all-pass style FIR implementing the requested phase correction."""
	allpass = np.exp(1j * phase_corr)
	h_full = irfft(allpass, n=n_fft).real

	peak_idx = int(np.argmax(np.abs(h_full)))
	center = fir_len // 2
	shifted = np.roll(h_full, center - peak_idx)
	fir = shifted[:fir_len].copy()

	# Symmetric edge taper.
	edge = min(2048, max(64, fir_len // 12))
	fade_in = 0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, edge)))
	fade_out = fade_in[::-1]
	fir[:edge] *= fade_in
	fir[-edge:] *= fade_out

	peak = np.max(np.abs(fir))
	if peak > 0.99:
		clip_db = 20.0 * np.log10(peak / 0.99)
		print(
			f"WARNING: phase FIR peak-clipped by {clip_db:.2f} dB",
			file=sys.stderr,
		)
		fir = fir / peak * 0.99
	return fir


@dataclass
class ChannelResult:
	freqs: np.ndarray
	global_mag_db: np.ndarray
	focus_mag_db: np.ndarray
	room_model_db: np.ndarray
	target_db: np.ndarray
	correction_db: np.ndarray
	mag_fir: np.ndarray
	phase_excess: Optional[np.ndarray]
	phase_correction: Optional[np.ndarray]
	phase_fir: Optional[np.ndarray]


class RoomCorrectionV4:
	def __init__(
		self,
		sample_rate: int = 48000,
		mag_fir_length: int = 8192,
		phase_fir_length: int = 16384,
		target_curve: str = "custom",
		bass_boost_db: float = 5.0,
		treble_tilt_db: float = -0.8,
		crossover_freq: float = 60.0,
		phase_correction_octaves: float = 1.6,
		preserve_bass_floor_db: float = -1.5,
		enable_phase: bool = True,
		trim_latency: bool = True,
		output_sample_rate: Optional[int] = None,
		bit_depth: int = 32,
		# New target-curve shaping params.
		tilt_anchor_hz: float = 1000.0,
		mid_bass_lift_db: float = 0.0,
		mid_bass_lift_hz: float = 280.0,
		mid_bass_lift_q: float = 1.2,
		presence_shelf_db: float = 0.0,
		presence_shelf_hz: float = 3000.0,
		air_shelf_db: float = 0.0,
		air_shelf_hz: float = 10000.0,
	) -> None:
		self.fs = int(sample_rate)
		self.output_fs = int(output_sample_rate) if output_sample_rate else self.fs
		self.mag_fir_length = int(mag_fir_length)
		self.phase_fir_length = int(phase_fir_length)
		self.crossover_freq = float(crossover_freq)
		self.phase_correction_octaves = float(phase_correction_octaves)
		self.phase_correction_end = self.crossover_freq * (2.0 ** self.phase_correction_octaves)
		self.preserve_bass_floor_db = float(preserve_bass_floor_db)
		self.enable_phase = bool(enable_phase)
		self.trim_latency = bool(trim_latency)
		self.bit_depth = int(bit_depth)
		self.target_params = TargetCurveParams(
			curve=target_curve,
			bass_boost_db=float(bass_boost_db),
			treble_tilt_db_per_oct=float(treble_tilt_db),
			tilt_anchor_hz=float(tilt_anchor_hz),
			mid_bass_lift_db=float(mid_bass_lift_db),
			mid_bass_lift_hz=float(mid_bass_lift_hz),
			mid_bass_lift_q=float(mid_bass_lift_q),
			presence_shelf_db=float(presence_shelf_db),
			presence_shelf_hz=float(presence_shelf_hz),
			air_shelf_db=float(air_shelf_db),
			air_shelf_hz=float(air_shelf_hz),
			crossover_hz=self.crossover_freq,
		)

	def _build_channel(
		self,
		global_pattern: str,
		focus_file: str,
	) -> ChannelResult:
		"""Build correction filters for one channel from global + focus measurements."""
		global_irs = _load_ir_list(global_pattern, self.fs, self.trim_latency)
		focus_ir = _load_ir_mono(focus_file, self.fs)
		if self.trim_latency:
			focus_ir = _trim_to_impulse_start(focus_ir)

		max_len = max(max(len(x) for x in global_irs), len(focus_ir))
		# Ensure FFT is long enough for both analysis and stable FIR synthesis.
		n_fft = _next_pow2(max(max_len, self.phase_fir_length * 2, self.mag_fir_length * 2))
		n_fft = max(32768, n_fft)
		freqs = rfftfreq(n_fft, 1.0 / self.fs)

		global_mag = _robust_global_average(global_irs, n_fft=n_fft, fs=self.fs)
		focus_mag = _focus_response(focus_ir, n_fft=n_fft, fs=self.fs)
		target = _target_curve_db(freqs, self.target_params)

		mag = _design_magnitude_correction(
			freqs=freqs,
			global_mag_db=global_mag,
			focus_mag_db=focus_mag,
			target_db=target,
			crossover_hz=self.crossover_freq,
			preserve_bass_floor_db=self.preserve_bass_floor_db,
		)
		mag_fir = _synthesize_min_phase_fir(mag["correction_db"], n_fft=n_fft, fir_len=self.mag_fir_length, fs=self.fs)

		if self.enable_phase:
			phase = _design_phase_correction(
				focus_ir=focus_ir,
				n_fft=n_fft,
				fs=self.fs,
				crossover_hz=self.crossover_freq,
				correction_end_hz=self.phase_correction_end,
			)
			phase_fir = _synthesize_phase_fir(phase["phase_correction"], n_fft=n_fft, fir_len=self.phase_fir_length)
			return ChannelResult(
				freqs=freqs,
				global_mag_db=global_mag,
				focus_mag_db=focus_mag,
				room_model_db=mag["room_model_db"],
				target_db=mag["target_db"],
				correction_db=mag["correction_db"],
				mag_fir=mag_fir,
				phase_excess=phase["phase_excess"],
				phase_correction=phase["phase_correction"],
				phase_fir=phase_fir,
			)

		return ChannelResult(
			freqs=freqs,
			global_mag_db=global_mag,
			focus_mag_db=focus_mag,
			room_model_db=mag["room_model_db"],
			target_db=mag["target_db"],
			correction_db=mag["correction_db"],
			mag_fir=mag_fir,
			phase_excess=None,
			phase_correction=None,
			phase_fir=None,
		)

	def process(
		self,
		left_global: str,
		right_global: str,
		left_focus: str,
		right_focus: str,
	) -> Tuple[ChannelResult, ChannelResult]:
		"""Run full correction design for left/right channels."""
		left = self._build_channel(left_global, left_focus)
		right = self._build_channel(right_global, right_focus)

		# Match channel levels in midband by attenuating the louder side only.
		wl, Hl = signal.freqz(left.mag_fir, worN=8192, fs=self.fs)
		wr, Hr = signal.freqz(right.mag_fir, worN=8192, fs=self.fs)
		idx_mid_l = (wl >= 500.0) & (wl <= 2000.0)
		idx_mid_r = (wr >= 500.0) & (wr <= 2000.0)
		l_mid = np.mean(_db(Hl[idx_mid_l]))
		r_mid = np.mean(_db(Hr[idx_mid_r]))
		diff = l_mid - r_mid
		if diff > 0.1:
			left.mag_fir *= 10 ** (-diff / 20.0)
		elif diff < -0.1:
			right.mag_fir *= 10 ** (diff / 20.0)

		return left, right

	def export_stereo_filter(self, left: np.ndarray, right: np.ndarray, out_file: str) -> None:
		"""Export stereo FIR file, optionally resampled to the requested output rate."""
		stereo = np.column_stack([left, right])
		fs_out = self.output_fs
		if fs_out != self.fs:
			g = np.gcd(fs_out, self.fs)
			up = fs_out // g
			down = self.fs // g
			stereo = signal.resample_poly(stereo, up, down, axis=0)

		subtype = "FLOAT" if self.bit_depth == 32 else ("PCM_24" if self.bit_depth == 24 else "PCM_16")
		sf.write(out_file, stereo, fs_out, subtype=subtype)

	@staticmethod
	def combine(mag_fir: np.ndarray, phase_fir: Optional[np.ndarray]) -> np.ndarray:
		"""Combine magnitude and phase FIR blocks into a single convolution filter."""
		if phase_fir is None:
			return mag_fir.copy()
		y = signal.convolve(mag_fir, phase_fir, mode="full")
		peak = np.max(np.abs(y))
		if peak > 0.99:
			y = y / peak * 0.99
		return y


def _plot_results(
	left: ChannelResult,
	right: ChannelResult,
	out_png: Optional[str],
	crossover: float,
	phase_end: float,
	fs: int,
) -> None:
	"""Render diagnostic plots for magnitude design, phase alignment, and output FIR."""
	fig, ax = plt.subplots(3, 2, figsize=(14, 11))

	for col, ch, name, color in [(0, left, "Left", "tab:blue"), (1, right, "Right", "tab:red")]:
		f = ch.freqs
		ax[0, col].semilogx(f, ch.room_model_db, color=color, label="Room model")
		ax[0, col].semilogx(f, ch.target_db, color="tab:green", linestyle="--", label="Target")
		ax[0, col].semilogx(f, ch.correction_db, color="tab:orange", label="Correction")
		ax[0, col].axvline(crossover, color="gray", linestyle=":", alpha=0.7)
		ax[0, col].set_xlim(20, 20000)
		ax[0, col].set_ylim(-10, 8)
		ax[0, col].set_title(f"{name} Magnitude Design")
		ax[0, col].grid(alpha=0.25)
		ax[0, col].legend(fontsize=8)

		if ch.phase_excess is not None and ch.phase_correction is not None:
			m = (f >= 20) & (f <= max(400, phase_end * 1.2))
			ax[1, col].semilogx(f[m], np.degrees(ch.phase_excess[m]), color="tab:purple", label="Excess phase")
			ax[1, col].semilogx(f[m], np.degrees(ch.phase_correction[m]), color="tab:green", label="Applied")
			ax[1, col].axvline(crossover, color="gray", linestyle=":", alpha=0.7)
			ax[1, col].axvline(phase_end, color="tab:red", linestyle=":", alpha=0.7)
			ax[1, col].set_title(f"{name} Phase Alignment")
			ax[1, col].grid(alpha=0.25)
			ax[1, col].legend(fontsize=8)
		else:
			ax[1, col].text(0.5, 0.5, "Phase correction disabled", transform=ax[1, col].transAxes, ha="center")

		w, H = signal.freqz(ch.mag_fir, worN=16384, fs=fs)
		h_db = _db(H)
		ref = h_db[np.argmin(np.abs(w - 1000.0))]
		ax[2, col].semilogx(w, h_db - ref, color=color, label="Magnitude FIR response")
		ax[2, col].axvline(crossover, color="gray", linestyle=":", alpha=0.7)
		ax[2, col].set_xlim(20, 20000)
		ax[2, col].set_ylim(-10, 6)
		ax[2, col].set_title(f"{name} Output FIR")
		ax[2, col].grid(alpha=0.25)
		ax[2, col].legend(fontsize=8)

	plt.tight_layout()
	if out_png:
		plt.savefig(out_png, dpi=150, bbox_inches="tight")
		print(f"Saved plot: {out_png}")
	plt.show()


def main() -> int:
	parser = argparse.ArgumentParser(description="Room correction v4 (Lyngdorf-inspired, bass-preserving 2.1 flow)")

	parser.add_argument(
		"--left_global",
		type=str,
		default="/Users/bjwi/data/audio/measurments/L_Global*.wav",
		help="Glob for left-channel global IR captures (multi-position).",
	)
	parser.add_argument(
		"--right_global",
		type=str,
		default="/Users/bjwi/data/audio/measurments/R_Global*.wav",
		help="Glob for right-channel global IR captures (multi-position).",
	)
	parser.add_argument(
		"--left_focus",
		type=str,
		default="/Users/bjwi/data/audio/measurments/L_focus.wav",
		help="Single focus listening-position IR for left channel.",
	)
	parser.add_argument(
		"--right_focus",
		type=str,
		default="/Users/bjwi/data/audio/measurments/R_focus.wav",
		help="Single focus listening-position IR for right channel.",
	)

	parser.add_argument("--air_shelf_db", type=float, default=0.0, help="Air shelf gain in dB at air_shelf_hz (range -9..3).")
	parser.add_argument("--air_shelf_hz", type=float, default=10000.0, help="Air shelf corner frequency in Hz (range 6000..16000).")
	parser.add_argument("--bass_boost_db", type=float, default=5.0, help="Low-frequency target lift in dB.")
	parser.add_argument("--bit_depth", type=int, choices=[16, 24, 32], default=32, help="Output WAV bit depth.")
	parser.add_argument("--combined", action="store_true")
	parser.add_argument("--crossover_freq", type=float, default=60.0, help="Main/sub crossover reference in Hz.")
	parser.add_argument("--fir_length", type=int, default=16384, help="Phase FIR length.")
	parser.add_argument("--mag_fir_length", type=int, default=8192, help="Magnitude FIR length.")
	parser.add_argument("--mid_bass_lift_db", type=float, default=0.0, help="Mid-bass bell gain in dB (range -6..6).")
	parser.add_argument("--mid_bass_lift_hz", type=float, default=280.0, help="Mid-bass bell centre frequency in Hz (range 150..500).")
	parser.add_argument("--mid_bass_lift_q", type=float, default=1.2, help="Mid-bass bell Q in log-frequency units (range 0.5..4).")
	parser.add_argument("--no_phase", action="store_true")
	parser.add_argument("--out", type=str, default="wavealign_filter.wav", help="Output WAV path.")
	parser.add_argument("--output_sample_rate", type=int, default=None, help="Optional output filter sample rate.")
	parser.add_argument("--phase_correction_octaves", type=float, default=1.6, help="Phase-correction span above crossover.")
	parser.add_argument("--plot", type=str, default=None, help="Optional PNG output for diagnostics plot.")
	parser.add_argument("--presence_shelf_db", type=float, default=0.0, help="Presence shelf gain in dB (range -9..3).")
	parser.add_argument("--presence_shelf_hz", type=float, default=3000.0, help="Presence shelf corner frequency in Hz (range 1500..6000).")
	parser.add_argument("--preserve_bass_floor_db", type=float, default=-1.5, help="Minimum allowed correction around bass anchor.")
	parser.add_argument("--report_json", type=str, default=None, help="Optional JSON summary report path.")
	parser.add_argument("--sample_rate", type=int, default=48000, help="Input measurement sample rate in Hz.")
	parser.add_argument("--target_curve", choices=["flat", "custom", "harman"], default="custom", help="Target response profile.")
	parser.add_argument("--tilt_anchor_hz", type=float, default=1000.0, help="Treble tilt anchor frequency in Hz (range 200..5000).")
	parser.add_argument("--treble_tilt_db", type=float, default=-0.8, help="Treble tilt per octave above tilt_anchor_hz.")

	args = parser.parse_args()

	rc = RoomCorrectionV4(
		sample_rate=args.sample_rate,
		mag_fir_length=args.mag_fir_length,
		phase_fir_length=args.fir_length,
		target_curve=args.target_curve,
		bass_boost_db=args.bass_boost_db,
		treble_tilt_db=args.treble_tilt_db,
		crossover_freq=args.crossover_freq,
		phase_correction_octaves=args.phase_correction_octaves,
		preserve_bass_floor_db=args.preserve_bass_floor_db,
		enable_phase=not args.no_phase,
		output_sample_rate=args.output_sample_rate,
		bit_depth=args.bit_depth,
		tilt_anchor_hz=args.tilt_anchor_hz,
		mid_bass_lift_db=args.mid_bass_lift_db,
		mid_bass_lift_hz=args.mid_bass_lift_hz,
		mid_bass_lift_q=args.mid_bass_lift_q,
		presence_shelf_db=args.presence_shelf_db,
		presence_shelf_hz=args.presence_shelf_hz,
		air_shelf_db=args.air_shelf_db,
		air_shelf_hz=args.air_shelf_hz,
	)

	try:
		print("=" * 80)
		print("Room Correction v4")
		print("=" * 80)
		print(f"Crossover: {args.crossover_freq:.1f} Hz")
		print(f"Phase correction: {'enabled' if not args.no_phase else 'disabled'}")
		print(f"Bass target boost: {args.bass_boost_db:+.1f} dB")

		left, right = rc.process(
			left_global=args.left_global,
			right_global=args.right_global,
			left_focus=args.left_focus,
			right_focus=args.right_focus,
		)

		out = Path(args.out)
		base = out.with_suffix("")

		if args.combined:
			l = rc.combine(left.mag_fir, left.phase_fir)
			r = rc.combine(right.mag_fir, right.phase_fir)
			rc.export_stereo_filter(l, r, str(out))
			print(f"Exported combined filter: {out}")
		else:
			mag_file = f"{base}_magnitude.wav"
			rc.export_stereo_filter(left.mag_fir, right.mag_fir, mag_file)
			print(f"Exported magnitude filter: {mag_file}")

			if left.phase_fir is not None and right.phase_fir is not None:
				phase_file = f"{base}_phase.wav"
				rc.export_stereo_filter(left.phase_fir, right.phase_fir, phase_file)
				print(f"Exported phase filter: {phase_file}")

		if args.plot:
			_plot_results(left, right, args.plot, args.crossover_freq, rc.phase_correction_end, fs=rc.fs)

		if args.report_json:
			report = {
				"sample_rate": args.sample_rate,
				"crossover_hz": args.crossover_freq,
				"phase_end_hz": rc.phase_correction_end,
				"target_curve": args.target_curve,
				"bass_boost_db": args.bass_boost_db,
				"treble_tilt_db_per_oct": args.treble_tilt_db,
				"tilt_anchor_hz": args.tilt_anchor_hz,
				"mid_bass_lift_db": args.mid_bass_lift_db,
				"mid_bass_lift_hz": args.mid_bass_lift_hz,
				"mid_bass_lift_q": args.mid_bass_lift_q,
				"presence_shelf_db": args.presence_shelf_db,
				"presence_shelf_hz": args.presence_shelf_hz,
				"air_shelf_db": args.air_shelf_db,
				"air_shelf_hz": args.air_shelf_hz,
				"preserve_bass_floor_db": args.preserve_bass_floor_db,
				"left": {
					"correction_min_db": float(np.min(left.correction_db)),
					"correction_max_db": float(np.max(left.correction_db)),
				},
				"right": {
					"correction_min_db": float(np.min(right.correction_db)),
					"correction_max_db": float(np.max(right.correction_db)),
				},
			}
			Path(args.report_json).write_text(json.dumps(report, indent=2), encoding="utf-8")
			print(f"Saved report: {args.report_json}")

		print("Done.")
		return 0
	except Exception as exc:
		print(f"Error: {exc}")
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
