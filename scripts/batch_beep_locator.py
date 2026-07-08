#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量定位 beedata 目录中的固定频率双蜂鸣特征。

本脚本按 beep-locator skill 的 1kHz 双蜂鸣模板实现批处理扩展：
- 所有音频先检查文件头；即使扩展名是 `.pcm`，只要包含 RIFF/WAVE 头就以头信息为准；
- 从目录/文件名自动推断 PCM 采样率、位深、通道数；
- 自动区分多通道未拆分文件与 `_0.._N` 拆分出的单通道文件；
- 多通道文件逐通道检测，并先判断空通道/无有效数据通道；
- 大音频只流式读取前后指定时间窗，默认前后 1 小时；
- 输出每个音频同目录结果文件，以及根目录 beep_results 汇总表。
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import re
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np


DEFAULT_ROOT = Path(r"E:\tempData\beedata")
SKILL_SCRIPT = Path(r"C:\Users\Administrator\.codex\skills\beep-locator\scripts\locate_beep.py")
SAMPLE_WIDTH_BYTES = 2


def load_skill_time_text():
    """复用 beep-locator skill 的时间格式函数；失败时使用本地等价实现。"""
    if SKILL_SCRIPT.exists():
        try:
            spec = importlib.util.spec_from_file_location("beep_locator_skill", SKILL_SCRIPT)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module.time_text
        except Exception:
            pass

    def fallback(seconds: float) -> str:
        total_ms = int(round(seconds * 1000))
        minutes = total_ms // 60000
        sec = (total_ms % 60000) // 1000
        ms = total_ms % 1000
        return f"{minutes}分{sec:02d}秒{ms:03d}毫秒"

    return fallback


time_text = load_skill_time_text()


@dataclass(frozen=True)
class AudioFormat:
    sample_rate: int
    bits_per_sample: int
    declared_channels: int
    physical_channels: int
    sample_format: str
    split_channel_index: int | None
    source: str
    data_offset: int = 0
    wave_frames: int | None = None

    @property
    def bytes_per_sample(self) -> int:
        return self.bits_per_sample // 8

    @property
    def frame_bytes(self) -> int:
        return self.bytes_per_sample * self.physical_channels

    @property
    def is_split_mono(self) -> bool:
        return self.split_channel_index is not None

    @property
    def dtype(self) -> np.dtype:
        if self.bits_per_sample == 16:
            return np.dtype("<i2")
        if self.bits_per_sample == 32:
            return np.dtype("<i4")
        raise ValueError(f"当前脚本只支持 16bit/32bit PCM/WAV，实际 {self.bits_per_sample}bit")

    @property
    def full_scale(self) -> float:
        return float(2 ** (self.bits_per_sample - 1))


@dataclass(frozen=True)
class ScanWindow:
    label: str
    start_sec: float
    end_sec: float

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


def parse_channel_list(text: str | None, max_channels: int) -> list[int]:
    if not text or text.lower() == "all":
        return list(range(max_channels))
    result: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            result.extend(range(int(left), int(right) + 1))
        else:
            result.append(int(part))
    result = sorted(set(result))
    invalid = [idx for idx in result if idx < 0 or idx >= max_channels]
    if invalid:
        raise ValueError(f"channel index out of range: {invalid}, max_channels={max_channels}")
    return result


def infer_declared_format(path: Path, default_sample_rate: int, default_channels: int) -> tuple[int, int, int, str]:
    """从路径中的 `16k16bit8通道` / `16k16bit单通道` 等说明推断 PCM 格式。"""
    text = str(path)
    multi = re.search(r"(\d+)k\s*(\d+)bit\s*(\d+)\s*通道", text, flags=re.IGNORECASE)
    if multi:
        return int(multi.group(1)) * 1000, int(multi.group(2)), int(multi.group(3)), "path-profile"

    mono = re.search(r"(\d+)k\s*(\d+)bit\s*单通道", text, flags=re.IGNORECASE)
    if mono:
        return int(mono.group(1)) * 1000, int(mono.group(2)), 1, "path-profile-mono"

    rate_bits = re.search(r"(\d+)k\s*(\d+)bit", text, flags=re.IGNORECASE)
    if rate_bits:
        return int(rate_bits.group(1)) * 1000, int(rate_bits.group(2)), default_channels, "path-rate-bits"

    return default_sample_rate, 16, default_channels, "defaults"


def split_suffix(path: Path) -> tuple[str, int] | None:
    match = re.match(r"^(.*)_([0-9]+)$", path.stem)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def has_riff_wave_header(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(12)
    except OSError:
        return False
    return head[:4] == b"RIFF" and head[8:12] == b"WAVE"


def wave_data_offset(path: Path) -> int:
    """返回 WAV data chunk 的 payload 偏移；扩展名不是 .wav 也可识别。"""
    with path.open("rb") as handle:
        raw = handle.read(4096)
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError(f"{path}: 不是 RIFF/WAVE 文件头")
    cursor = 12
    while cursor + 8 <= len(raw):
        chunk_id = raw[cursor : cursor + 4]
        chunk_size = int.from_bytes(raw[cursor + 4 : cursor + 8], "little", signed=False)
        cursor += 8
        if chunk_id == b"data":
            return cursor
        cursor += chunk_size + (chunk_size % 2)
    raise ValueError(f"{path}: 未在前 4096 字节找到 WAV data chunk")


def detect_split_pcm_files(
    pcm_files: list[Path],
    default_sample_rate: int,
    default_channels: int,
    size_tolerance_ratio: float,
) -> dict[Path, int]:
    """识别同目录下按 `_0.._N` 命名且大小一致的拆分单通道文件。"""
    by_parent: dict[Path, list[Path]] = {}
    for path in pcm_files:
        by_parent.setdefault(path.parent, []).append(path)

    split_map: dict[Path, int] = {}
    for parent, files in by_parent.items():
        _, _, declared_channels, _ = infer_declared_format(parent, default_sample_rate, default_channels)
        if declared_channels <= 1:
            continue
        groups: dict[str, list[tuple[int, Path]]] = {}
        for path in files:
            suffix = split_suffix(path)
            if suffix:
                prefix, index = suffix
                groups.setdefault(prefix, []).append((index, path))

        expected = set(range(declared_channels))
        for members in groups.values():
            by_index = {index: member_path for index, member_path in members}
            if not expected.issubset(by_index):
                continue
            sizes = [by_index[index].stat().st_size for index in expected]
            tolerance = max(4096, max(sizes) * size_tolerance_ratio)
            if max(sizes) - min(sizes) <= tolerance:
                for index in expected:
                    split_map[by_index[index]] = index
    return split_map


def infer_audio_format(
    path: Path,
    split_map: dict[Path, int],
    default_sample_rate: int,
    default_channels: int,
) -> AudioFormat:
    if path.suffix.lower() == ".wav" or has_riff_wave_header(path):
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            sample_rate = wav.getframerate()
            bits = wav.getsampwidth() * 8
            frame_count = wav.getnframes()
        return AudioFormat(
            sample_rate=sample_rate,
            bits_per_sample=bits,
            declared_channels=channels,
            physical_channels=channels,
            sample_format=f"s{bits}le",
            split_channel_index=None,
            source="wav-header" if path.suffix.lower() == ".wav" else "riff-wave-header-in-pcm",
            data_offset=wave_data_offset(path),
            wave_frames=frame_count,
        )

    sample_rate, bits, declared_channels, source = infer_declared_format(
        path, default_sample_rate, default_channels
    )
    split_index = split_map.get(path)
    physical_channels = 1 if split_index is not None else declared_channels
    return AudioFormat(
        sample_rate=sample_rate,
        bits_per_sample=bits,
        declared_channels=declared_channels,
        physical_channels=physical_channels,
        sample_format=f"s{bits}le",
        split_channel_index=split_index,
        source=source + ("+split-mono" if split_index is not None else ""),
    )


def audio_duration(path: Path, fmt: AudioFormat) -> float:
    if fmt.wave_frames is not None:
        return fmt.wave_frames / fmt.sample_rate
    if fmt.bits_per_sample not in (16, 32):
        raise ValueError(f"{path}: 当前脚本只支持 16bit/32bit PCM/WAV，实际 {fmt.bits_per_sample}bit")
    data_bytes = max(0, path.stat().st_size - fmt.data_offset)
    return data_bytes // fmt.frame_bytes / fmt.sample_rate


def build_scan_windows(duration: float, edge_window_sec: float, force_full_scan: bool) -> list[ScanWindow]:
    if force_full_scan or edge_window_sec <= 0 or duration <= edge_window_sec * 2:
        return [ScanWindow("full", 0.0, duration)]
    return [
        ScanWindow("head", 0.0, min(edge_window_sec, duration)),
        ScanWindow("tail", max(0.0, duration - edge_window_sec), duration),
    ]


def iter_audio_blocks(
    path: Path,
    fmt: AudioFormat,
    window: ScanWindow,
    chunk_sec: float,
) -> Iterable[tuple[int, np.ndarray]]:
    """流式读取窗口内音频块，返回绝对起始帧和 shape=(frames, channels) 的整数数组。"""
    start_frame = max(0, int(math.floor(window.start_sec * fmt.sample_rate)))
    end_frame = max(start_frame, int(math.ceil(window.end_sec * fmt.sample_rate)))
    chunk_frames = max(1, int(round(chunk_sec * fmt.sample_rate)))

    with path.open("rb") as handle:
        handle.seek(fmt.data_offset + start_frame * fmt.frame_bytes)
        current = start_frame
        while current < end_frame:
            want_frames = min(chunk_frames, end_frame - current)
            raw = handle.read(want_frames * fmt.frame_bytes)
            if not raw:
                break
            usable = len(raw) - (len(raw) % fmt.frame_bytes)
            if usable <= 0:
                break
            frames = usable // fmt.frame_bytes
            data = np.frombuffer(raw[:usable], dtype=fmt.dtype).reshape(frames, fmt.physical_channels)
            yield current, data
            current += frames


def channel_label(fmt: AudioFormat, physical_channel: int) -> str:
    if fmt.is_split_mono:
        return f"split_ch{fmt.split_channel_index}"
    return f"ch{physical_channel}"


def dbfs_from_rms(rms: float) -> float | None:
    if rms <= 0:
        return None
    return 20.0 * math.log10(rms / 32768.0)


def frame_view_1d(samples: np.ndarray, frame_n: int, hop_n: int) -> np.ndarray:
    count = 1 + (len(samples) - frame_n) // hop_n
    if count <= 0:
        return np.empty((0, frame_n), dtype=samples.dtype)
    stride = samples.strides[0]
    return np.lib.stride_tricks.as_strided(
        samples,
        shape=(count, frame_n),
        strides=(hop_n * stride, stride),
        writeable=False,
    )


def append_frame_metrics(
    metrics: dict[int, dict[str, list[np.ndarray]]],
    data: np.ndarray,
    base_frame: int,
    channel_indices: list[int],
    sample_rate: int,
    full_scale: float,
    frequency: float,
    frame_n: int,
    hop_n: int,
    cos_vals: np.ndarray,
    sin_vals: np.ndarray,
) -> int:
    if len(data) < frame_n:
        return 0
    frame_count = 1 + (len(data) - frame_n) // hop_n
    starts = np.arange(frame_count, dtype=np.float64) * hop_n
    times = (base_frame + starts) / sample_rate

    for channel in channel_indices:
        values = np.ascontiguousarray(data[:, channel])
        frames_i = frame_view_1d(values, frame_n, hop_n)
        if len(frames_i) == 0:
            continue
        frames = frames_i.astype(np.float64, copy=False)
        energy = np.einsum("ij,ij->i", frames, frames)
        rms = np.sqrt(np.divide(energy, frame_n, out=np.zeros_like(energy), where=energy > 0))
        re_part = frames @ cos_vals
        im_part = frames @ sin_vals
        ratio = np.divide(
            2.0 * (re_part * re_part + im_part * im_part),
            frame_n * energy,
            out=np.zeros_like(energy),
            where=energy > 0,
        )
        ratio = np.clip(ratio, 0.0, 1.0)
        tone_rms = rms * np.sqrt(ratio)
        with np.errstate(divide="ignore", invalid="ignore"):
            tone_db = 20.0 * np.log10(tone_rms / full_scale)
        tone_db[~np.isfinite(tone_db)] = np.nan
        peak = np.max(np.abs(frames_i.astype(np.int64, copy=False)), axis=1).astype(np.int64)

        channel_metrics = metrics[channel]
        channel_metrics["time"].append(times.copy())
        channel_metrics["tone_db"].append(tone_db.astype(np.float32))
        channel_metrics["ratio"].append(ratio.astype(np.float32))
        channel_metrics["peak"].append(peak)
    return int(frame_count)


def update_channel_stats(stats: dict[int, dict[str, Any]], data: np.ndarray, channel_indices: list[int]) -> None:
    for channel in channel_indices:
        values_i16 = data[:, channel]
        values = values_i16.astype(np.float64, copy=False)
        item = stats[channel]
        item["samples"] += int(len(values_i16))
        item["energy"] += float(np.dot(values, values))
        item["zero_count"] += int(np.count_nonzero(values_i16 == 0))
        peak = int(np.max(np.abs(values_i16.astype(np.int64, copy=False)))) if len(values_i16) else 0
        item["peak"] = max(item["peak"], peak)


def concat_metric_arrays(metrics: dict[str, list[np.ndarray]]) -> dict[str, np.ndarray]:
    result: dict[str, np.ndarray] = {}
    for key, chunks in metrics.items():
        if chunks:
            result[key] = np.concatenate(chunks)
        else:
            dtype = np.float64 if key == "time" else (np.int32 if key == "peak" else np.float32)
            result[key] = np.array([], dtype=dtype)
    return result


def runs_for_threshold_arrays(
    time_values: np.ndarray,
    tone_db: np.ndarray,
    ratios: np.ndarray,
    peaks: np.ndarray,
    threshold_dbfs: float,
    hop_sec: float,
) -> list[dict[str, Any]]:
    mask = np.isfinite(tone_db) & (tone_db >= threshold_dbfs) & (ratios >= 0.08) & (peaks >= 200)
    active_idx = np.flatnonzero(mask)
    if len(active_idx) == 0:
        return []

    active_times = time_values[active_idx]
    gaps = np.diff(active_times)
    split_after = np.flatnonzero(gaps > max(0.151, hop_sec * 3.01)) + 1
    groups = np.split(active_idx, split_after)

    runs: list[dict[str, Any]] = []
    for group in groups:
        if len(group) == 0:
            continue
        start = float(time_values[group[0]])
        end = float(time_values[group[-1]] + hop_sec)
        runs.append(
            {
                "start": start,
                "end": end,
                "duration": end - start,
                "frames": int(len(group)),
                "max_tone_rms_dbfs": float(np.nanmax(tone_db[group])),
                "mean_tone_rms_dbfs": float(np.nanmean(tone_db[group])),
                "mean_frame_tone_ratio": float(np.nanmean(ratios[group])),
                "max_peak": int(np.max(peaks[group])),
                "threshold_dbfs": threshold_dbfs,
            }
        )
    return runs


def find_marker_pairs_arrays(
    time_values: np.ndarray,
    tone_db: np.ndarray,
    ratios: np.ndarray,
    peaks: np.ndarray,
    hop_sec: float,
) -> list[dict[str, Any]]:
    """按 beep-locator skill 的阈值/结构规则查找 1s+1s+1s 双蜂鸣候选。"""
    candidates: list[dict[str, Any]] = []
    for threshold in [-34, -36, -38, -40, -42, -44, -46, -48, -50, -52, -54, -56]:
        runs = [
            run
            for run in runs_for_threshold_arrays(time_values, tone_db, ratios, peaks, threshold, hop_sec)
            if 0.50 <= run["duration"] <= 1.55
        ]
        for index, first in enumerate(runs):
            for second in runs[index + 1 : index + 4]:
                start_delta = second["start"] - first["start"]
                gap = second["start"] - first["end"]
                if 1.70 <= start_delta <= 2.30 and 0.45 <= gap <= 1.45:
                    dur_err = abs(first["duration"] - 1.0) + abs(second["duration"] - 1.0)
                    gap_err = abs(gap - 1.0)
                    score = (
                        100.0
                        - 35.0 * dur_err
                        - 20.0 * gap_err
                        + max(first["max_tone_rms_dbfs"], second["max_tone_rms_dbfs"]) / 2.0
                    )
                    candidates.append(
                        {
                            "detection_type": "continuous_pair",
                            "start": first["start"],
                            "end": second["end"],
                            "duration": second["end"] - first["start"],
                            "gap": gap,
                            "threshold_dbfs": threshold,
                            "score": score,
                            "beeps": [first, second],
                        }
                    )

    for threshold in [-44, -46, -48, -50, -52, -54, -56]:
        runs = [
            run
            for run in runs_for_threshold_arrays(time_values, tone_db, ratios, peaks, threshold, hop_sec)
            if 0.05 <= run["duration"] <= 0.60
        ]
        for index, first in enumerate(runs):
            for second in runs[index + 1 : index + 8]:
                start_delta = second["start"] - first["start"]
                if 1.70 <= start_delta <= 2.30:
                    score = 70.0 - 30.0 * abs(start_delta - 2.0) + (
                        first["max_tone_rms_dbfs"] + second["max_tone_rms_dbfs"]
                    ) / 4.0
                    candidates.append(
                        {
                            "detection_type": "burst_pair",
                            "start": first["start"],
                            "end": first["start"] + 3.0,
                            "duration": 3.0,
                            "gap": second["start"] - first["end"],
                            "threshold_dbfs": threshold,
                            "score": score,
                            "beeps": [first, second],
                        }
                    )

    candidates.sort(key=lambda item: (item["start"], -item["score"]))
    deduped: list[dict[str, Any]] = []
    for candidate in candidates:
        existing = next((item for item in deduped if abs(item["start"] - candidate["start"]) < 0.25), None)
        if existing is None:
            deduped.append(candidate)
        elif candidate["score"] > existing["score"]:
            existing.update(candidate)
    return sorted(deduped, key=lambda item: item["start"])


def read_channel_samples(
    path: Path,
    fmt: AudioFormat,
    physical_channel: int,
    start_sec: float,
    end_sec: float,
) -> np.ndarray:
    start_frame = max(0, int(math.floor(start_sec * fmt.sample_rate)))
    end_frame = max(start_frame, int(math.ceil(end_sec * fmt.sample_rate)))
    if end_frame <= start_frame:
        return np.array([], dtype=fmt.dtype)

    with path.open("rb") as handle:
        handle.seek(fmt.data_offset + start_frame * fmt.frame_bytes)
        raw = handle.read((end_frame - start_frame) * fmt.frame_bytes)

    usable = len(raw) - (len(raw) % fmt.frame_bytes)
    if usable <= 0:
        return np.array([], dtype=fmt.dtype)
    data = np.frombuffer(raw[:usable], dtype=fmt.dtype).reshape(-1, fmt.physical_channels)
    return np.ascontiguousarray(data[:, physical_channel])


def segment_stats_np(samples: np.ndarray, sample_rate: int, frequency: float, full_scale: float) -> dict[str, Any]:
    n = int(len(samples))
    if n <= 0:
        return {
            "rms": 0.0,
            "rms_dbfs": None,
            "tone_ratio": 0.0,
            "tone_rms_dbfs": None,
            "peak": 0,
            "zero_percent": None,
        }

    values = samples.astype(np.float64, copy=False)
    energy = float(np.dot(values, values))
    peak = int(np.max(np.abs(samples.astype(np.int64, copy=False)))) if n else 0
    zero_percent = float(np.count_nonzero(samples == 0) / n * 100.0)
    if energy <= 0:
        return {
            "rms": 0.0,
            "rms_dbfs": None,
            "tone_ratio": 0.0,
            "tone_rms_dbfs": None,
            "peak": peak,
            "zero_percent": zero_percent,
        }

    omega = 2.0 * math.pi * frequency / sample_rate
    idx = np.arange(n, dtype=np.float64)
    cos_vals = np.cos(omega * idx)
    sin_vals = np.sin(omega * idx)
    re_part = float(values @ cos_vals)
    im_part = float(values @ sin_vals)
    rms = math.sqrt(energy / n)
    rms_dbfs = 20.0 * math.log10(rms / full_scale) if rms > 0 else None
    tone_ratio = 2.0 * (re_part * re_part + im_part * im_part) / (n * energy)
    tone_ratio = max(0.0, min(1.0, tone_ratio))
    tone_rms = rms * math.sqrt(tone_ratio)
    tone_rms_dbfs = 20.0 * math.log10(tone_rms / full_scale) if tone_rms > 0 else None
    return {
        "rms": rms,
        "rms_dbfs": rms_dbfs,
        "tone_ratio": tone_ratio,
        "tone_rms_dbfs": tone_rms_dbfs,
        "peak": peak,
        "zero_percent": zero_percent,
    }


def validate_markers_from_file(
    path: Path,
    fmt: AudioFormat,
    physical_channel: int,
    frequency: float,
    markers: list[dict[str, Any]],
    min_tone_ratio: float,
    min_peak: int,
) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for marker in markers:
        marker = json.loads(json.dumps(marker, ensure_ascii=False))
        for beep in marker["beeps"]:
            start = float(beep["start"])
            end = float(beep["end"])
            # 与 skill 保持一致：较长蜂鸣取中间 0.5s 复核，避开边缘过渡噪声。
            if end - start > 0.5:
                center = (start + end) / 2.0
                start = max(0.0, center - 0.25)
                end = center + 0.25
            samples = read_channel_samples(path, fmt, physical_channel, start, end)
            stats = segment_stats_np(samples, fmt.sample_rate, frequency, fmt.full_scale)
            beep["raw_rms_dbfs"] = stats["rms_dbfs"]
            beep["tone_ratio_1khz"] = stats["tone_ratio"]
            beep["raw_peak"] = stats["peak"]
            beep["zero_percent"] = stats["zero_percent"]

        ratios = [beep.get("tone_ratio_1khz", 0.0) for beep in marker["beeps"]]
        peaks = [beep.get("raw_peak", 0) for beep in marker["beeps"]]
        marker["mean_tone_ratio_1khz"] = sum(ratios) / len(ratios) if ratios else 0.0
        marker["max_raw_peak"] = max(peaks) if peaks else 0
        if marker["mean_tone_ratio_1khz"] < min_tone_ratio or marker["max_raw_peak"] < min_peak:
            continue
        if marker["mean_tone_ratio_1khz"] >= 0.35:
            marker["confidence"] = "high"
        elif marker["mean_tone_ratio_1khz"] >= 0.20:
            marker["confidence"] = "medium"
        else:
            marker["confidence"] = "weak"
        validated.append(marker)

    validated.sort(key=lambda item: item["start"])
    deduped: list[dict[str, Any]] = []
    for marker in validated:
        best_idx = None
        best_overlap = 0.0
        for idx, existing in enumerate(deduped):
            overlap = max(0.0, min(existing["end"], marker["end"]) - max(existing["start"], marker["start"]))
            if overlap > best_overlap:
                best_overlap = overlap
                best_idx = idx
        if best_idx is not None and best_overlap >= 1.0:
            existing = deduped[best_idx]
            existing_key = (
                existing.get("mean_tone_ratio_1khz", 0.0),
                existing.get("score", 0.0),
                1 if existing.get("detection_type") == "continuous_pair" else 0,
            )
            marker_key = (
                marker.get("mean_tone_ratio_1khz", 0.0),
                marker.get("score", 0.0),
                1 if marker.get("detection_type") == "continuous_pair" else 0,
            )
            if marker_key > existing_key:
                deduped[best_idx] = marker
        else:
            deduped.append(marker)
    return sorted(deduped, key=lambda item: item["start"])


def finalize_channel_stats(
    item: dict[str, Any],
    empty_peak_threshold: int,
    empty_rms_dbfs: float,
    full_scale: float,
) -> dict[str, Any]:
    samples = int(item["samples"])
    energy = float(item["energy"])
    peak = int(item["peak"])
    rms = math.sqrt(energy / samples) if samples and energy > 0 else 0.0
    rms_dbfs = 20.0 * math.log10(rms / full_scale) if rms > 0 else None
    zero_percent = (item["zero_count"] / samples * 100.0) if samples else None
    is_empty = peak < empty_peak_threshold or rms_dbfs is None or rms_dbfs < empty_rms_dbfs
    return {
        "samples": samples,
        "rms": rms,
        "rms_dbfs": rms_dbfs,
        "peak": peak,
        "zero_percent": zero_percent,
        "data_status": "空通道/无有效数据" if is_empty else "有有效数据",
        "is_empty": is_empty,
    }


def scan_audio(path: Path, fmt: AudioFormat, duration: float, windows: list[ScanWindow], args: argparse.Namespace) -> dict[str, Any]:
    frame_n = max(1, int(round(fmt.sample_rate * args.frame_ms / 1000.0)))
    hop_n = max(1, int(round(fmt.sample_rate * args.hop_ms / 1000.0)))
    hop_sec = hop_n / fmt.sample_rate
    omega = 2.0 * math.pi * args.frequency / fmt.sample_rate
    idx = np.arange(frame_n, dtype=np.float64)
    cos_vals = np.cos(omega * idx)
    sin_vals = np.sin(omega * idx)
    channel_indices = parse_channel_list(args.channels, fmt.physical_channels)

    metrics = {
        channel: {"time": [], "tone_db": [], "ratio": [], "peak": []}
        for channel in channel_indices
    }
    stats = {
        channel: {"samples": 0, "energy": 0.0, "zero_count": 0, "peak": 0}
        for channel in channel_indices
    }
    scan_read_bytes = 0

    for window in windows:
        carry = np.empty((0, fmt.physical_channels), dtype=fmt.dtype)
        carry_base_frame = int(round(window.start_sec * fmt.sample_rate))
        for block_start_frame, block in iter_audio_blocks(path, fmt, window, args.chunk_sec):
            scan_read_bytes += int(block.nbytes)
            update_channel_stats(stats, block, channel_indices)
            if len(carry):
                data = np.vstack([carry, block])
                base_frame = carry_base_frame
            else:
                data = block
                base_frame = block_start_frame

            processed_frames = append_frame_metrics(
                metrics,
                data,
                base_frame,
                channel_indices,
                fmt.sample_rate,
                fmt.full_scale,
                args.frequency,
                frame_n,
                hop_n,
                cos_vals,
                sin_vals,
            )

            if processed_frames <= 0:
                carry = data
                carry_base_frame = base_frame
                # 避免异常参数导致 carry 无限增长。
                keep = max(frame_n - 1, hop_n)
                if len(carry) > keep:
                    carry = carry[-keep:]
                    carry_base_frame = base_frame + len(data) - len(carry)
                continue

            next_start = processed_frames * hop_n
            carry = data[next_start:]
            carry_base_frame = base_frame + next_start

    channel_results = []
    for channel in channel_indices:
        stat = finalize_channel_stats(stats[channel], args.empty_peak_threshold, args.empty_rms_dbfs, fmt.full_scale)
        arrays = concat_metric_arrays(metrics[channel])
        candidates: list[dict[str, Any]] = []
        markers: list[dict[str, Any]] = []
        if not stat["is_empty"] and len(arrays["time"]) > 0:
            candidates = find_marker_pairs_arrays(
                arrays["time"],
                arrays["tone_db"],
                arrays["ratio"],
                arrays["peak"],
                hop_sec,
            )
            markers = validate_markers_from_file(
                path,
                fmt,
                channel,
                args.frequency,
                candidates,
                args.min_tone_ratio,
                args.min_peak,
            )
        channel_results.append(
            {
                "physical_channel": channel,
                "channel_label": channel_label(fmt, channel),
                "data_status": stat["data_status"],
                "is_empty": stat["is_empty"],
                "rms_dbfs": stat["rms_dbfs"],
                "peak": stat["peak"],
                "zero_percent": stat["zero_percent"],
                "candidate_count": len(candidates),
                "detected_count": len(markers),
                "markers": markers,
            }
        )

    return {
        "path": str(path),
        "duration_sec": duration,
        "sample_rate": fmt.sample_rate,
        "bits_per_sample": fmt.bits_per_sample,
        "declared_channels": fmt.declared_channels,
        "physical_channels": fmt.physical_channels,
        "split_channel_index": fmt.split_channel_index,
        "format_source": fmt.source,
        "frequency_hz": args.frequency,
        "scan_windows": [window.__dict__ for window in windows],
        "scan_read_bytes": scan_read_bytes,
        "channels": channel_results,
    }


def format_scan_windows(windows: list[ScanWindow]) -> str:
    parts = []
    for window in windows:
        parts.append(f"{window.label}:{time_text(window.start_sec)}-{time_text(window.end_sec)}")
    return "；".join(parts)


def result_text(path: Path, result: dict[str, Any], windows: list[ScanWindow]) -> str:
    lines = [
        (
            f"{path.name} 实际蜂鸣特征位置"
            f"（{result['sample_rate']}Hz / {result['bits_per_sample']}bit PCM/WAV / "
            f"声明{result['declared_channels']}通道 / 实际分析{result['physical_channels']}通道 / "
            f"目标频率 {result['frequency_hz']:g}Hz）"
        ),
        "",
        f"扫描范围：{format_scan_windows(windows)}",
    ]
    if result["split_channel_index"] is not None:
        lines.append(f"拆分通道文件：原通道 {result['split_channel_index']}")
    lines.append("")

    any_marker = False
    for channel in result["channels"]:
        lines.append(
            f"[{channel['channel_label']}] {channel['data_status']}；"
            f"RMS={format_float(channel['rms_dbfs'], 2)} dBFS；"
            f"峰值={channel['peak']}；零样本={format_float(channel['zero_percent'], 2)}%"
        )
        if channel["is_empty"]:
            lines.append("未检测到实际蜂鸣特征（该通道为空或无有效数据，不做同步推断）。")
        elif not channel["markers"]:
            lines.append("未检测到实际蜂鸣特征。")
        else:
            any_marker = True
            for index, marker in enumerate(channel["markers"], 1):
                lines.append(
                    f"{index}. {time_text(marker['start'])} → {time_text(marker['end'])}"
                    f"（持续 {time_text(marker['duration'])}，置信度 {marker['confidence']}，"
                    f"tone_ratio={marker['mean_tone_ratio_1khz']:.3f}，peak={marker['max_raw_peak']}）"
                )
        lines.append("")

    if not any_marker:
        lines.append("文件结论：未检测到实际蜂鸣特征")
    else:
        lines.append("文件结论：检测到实际蜂鸣特征")
    lines.extend(
        [
            "",
            "说明：以上只表示该音频自身实际检测到的蜂鸣特征；空通道和同组通道同步推断窗口未写成实际蜂鸣。",
            f"音频时长：{time_text(result['duration_sec'])}",
        ]
    )
    return "\n".join(lines) + "\n"


def format_float(value: Any, digits: int) -> str:
    if value is None:
        return "NA"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "NA"


def flatten_rows(result: dict[str, Any], root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    path = Path(result["path"])
    rel_path = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    base = {
        "文件": str(path),
        "相对路径": rel_path,
        "目录": str(path.parent),
        "文件名": path.name,
        "采样率Hz": result["sample_rate"],
        "位深bit": result["bits_per_sample"],
        "声明通道数": result["declared_channels"],
        "实际分析通道数": result["physical_channels"],
        "拆分原通道": "" if result["split_channel_index"] is None else result["split_channel_index"],
        "格式来源": result["format_source"],
        "时长秒": round(float(result["duration_sec"]), 3),
        "扫描窗口": "; ".join(
            f"{w['label']} {round(w['start_sec'], 3)}-{round(w['end_sec'], 3)}" for w in result["scan_windows"]
        ),
        "读取字节": result["scan_read_bytes"],
        "目标频率Hz": result["frequency_hz"],
    }
    summary_rows: list[dict[str, Any]] = []
    not_found_rows: list[dict[str, Any]] = []
    for channel in result["channels"]:
        channel_base = {
            **base,
            "通道": channel["channel_label"],
            "通道状态": channel["data_status"],
            "通道RMS_dBFS": format_float(channel["rms_dbfs"], 3),
            "通道峰值": channel["peak"],
            "通道零样本%": format_float(channel["zero_percent"], 3),
            "候选数": channel["candidate_count"],
            "检出数": channel["detected_count"],
        }
        if channel["markers"]:
            for index, marker in enumerate(channel["markers"], 1):
                summary_rows.append(
                    {
                        **channel_base,
                        "结果": "检测到实际蜂鸣",
                        "序号": index,
                        "开始秒": round(float(marker["start"]), 3),
                        "结束秒": round(float(marker["end"]), 3),
                        "开始时间": time_text(marker["start"]),
                        "结束时间": time_text(marker["end"]),
                        "持续秒": round(float(marker["duration"]), 3),
                        "置信度": marker.get("confidence", ""),
                        "tone_ratio_1kHz": round(float(marker.get("mean_tone_ratio_1khz", 0.0)), 6),
                        "峰值": marker.get("max_raw_peak", ""),
                        "检测类型": marker.get("detection_type", ""),
                        "说明": "",
                    }
                )
        else:
            reason = "空通道/无有效数据" if channel["is_empty"] else "未检测到实际蜂鸣特征"
            row = {
                **channel_base,
                "结果": reason,
                "序号": "",
                "开始秒": "",
                "结束秒": "",
                "开始时间": "",
                "结束时间": "",
                "持续秒": "",
                "置信度": "",
                "tone_ratio_1kHz": "",
                "峰值": "",
                "检测类型": "",
                "说明": "前后/全量扫描窗口内无实际蜂鸣；未做同步推断",
            }
            summary_rows.append(row)
            not_found_rows.append(row)
    return summary_rows, not_found_rows


def flatten_file_row(result: dict[str, Any], root: Path) -> dict[str, Any]:
    """生成一行文件级汇总，单独记录整文件未检出，避免只看通道级明细时误读。"""
    path = Path(result["path"])
    rel_path = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    channels = result.get("channels", [])
    detected_channels = [channel for channel in channels if channel.get("detected_count", 0) > 0]
    empty_channels = [channel for channel in channels if channel.get("is_empty")]
    marker_count = sum(channel.get("detected_count", 0) for channel in channels)
    first_start: float | None = None
    last_end: float | None = None
    max_ratio = 0.0
    for channel in channels:
        for marker in channel.get("markers", []):
            start = float(marker["start"])
            end = float(marker["end"])
            first_start = start if first_start is None else min(first_start, start)
            last_end = end if last_end is None else max(last_end, end)
            max_ratio = max(max_ratio, float(marker.get("mean_tone_ratio_1khz", 0.0)))

    if marker_count > 0:
        result_status = "检测到实际蜂鸣"
        reason = ""
    elif channels and len(empty_channels) == len(channels):
        result_status = "未检测到实际蜂鸣"
        reason = "所有分析通道为空或无有效数据"
    else:
        result_status = "未检测到实际蜂鸣"
        reason = "前后/全量扫描窗口内未找到符合 1kHz 双蜂鸣结构的实际蜂鸣"

    return {
        "文件": str(path),
        "相对路径": rel_path,
        "一级目录": rel_path.split("\\", 1)[0] if "\\" in rel_path else rel_path,
        "文件名": path.name,
        "采样率Hz": result["sample_rate"],
        "位深bit": result["bits_per_sample"],
        "声明通道数": result["declared_channels"],
        "实际分析通道数": result["physical_channels"],
        "拆分原通道": "" if result["split_channel_index"] is None else result["split_channel_index"],
        "格式来源": result["format_source"],
        "时长秒": round(float(result["duration_sec"]), 3),
        "扫描窗口": "; ".join(
            f"{w['label']} {round(w['start_sec'], 3)}-{round(w['end_sec'], 3)}" for w in result["scan_windows"]
        ),
        "读取字节": result["scan_read_bytes"],
        "目标频率Hz": result["frequency_hz"],
        "文件结果": result_status,
        "原因": reason,
        "分析通道数": len(channels),
        "检出通道数": len(detected_channels),
        "空通道数": len(empty_channels),
        "蜂鸣条数": marker_count,
        "最早开始秒": "" if first_start is None else round(first_start, 3),
        "最晚结束秒": "" if last_end is None else round(last_end, 3),
        "最早开始时间": "" if first_start is None else time_text(first_start),
        "最晚结束时间": "" if last_end is None else time_text(last_end),
        "最高tone_ratio_1kHz": "" if marker_count == 0 else round(max_ratio, 6),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def discover_audio_files(root: Path, include_regex: str | None = None, exclude_regex: str | None = None) -> list[Path]:
    include = re.compile(include_regex) if include_regex else None
    exclude = re.compile(exclude_regex) if exclude_regex else None
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in {".pcm", ".wav"}
        and "_beep_result" not in path.stem
    )
    if include:
        files = [path for path in files if include.search(str(path))]
    if exclude:
        files = [path for path in files if not exclude.search(str(path))]
    return files


def load_completed(progress_path: Path) -> dict[str, dict[str, Any]]:
    if not progress_path.exists():
        return {}
    completed: dict[str, dict[str, Any]] = {}
    with progress_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("path"):
                completed[item["path"]] = item
    return completed


def write_manifest(output_dir: Path, files: list[Path], split_map: dict[Path, int], args: argparse.Namespace) -> Path:
    manifest_path = output_dir / "beep_audio_manifest.csv"
    rows = []
    for path in files:
        fmt = infer_audio_format(path, split_map, args.default_sample_rate, args.default_channels)
        try:
            duration = audio_duration(path, fmt)
        except Exception:
            duration = None
        rows.append(
            {
                "文件": str(path),
                "采样率Hz": fmt.sample_rate,
                "位深bit": fmt.bits_per_sample,
                "声明通道数": fmt.declared_channels,
                "实际分析通道数": fmt.physical_channels,
                "拆分原通道": "" if fmt.split_channel_index is None else fmt.split_channel_index,
                "格式来源": fmt.source,
                "大小字节": path.stat().st_size,
                "时长秒": "" if duration is None else round(duration, 3),
            }
        )
    write_csv(manifest_path, rows)
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description="批量流式定位固定频率双蜂鸣特征。")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="待分析音频根目录")
    parser.add_argument("--frequency", type=float, default=1000.0, help="目标蜂鸣频率 Hz")
    parser.add_argument("--default-sample-rate", type=int, default=16000, help="路径未说明时的 PCM 采样率")
    parser.add_argument("--default-channels", type=int, default=1, help="路径未说明时的 PCM 通道数")
    parser.add_argument("--frame-ms", type=float, default=50.0, help="分析帧长度 ms")
    parser.add_argument("--hop-ms", type=float, default=50.0, help="分析帧步进 ms")
    parser.add_argument("--edge-window-sec", type=float, default=3600.0, help="大音频前后扫描窗口，默认 1 小时")
    parser.add_argument("--full-scan", action="store_true", help="强制全量扫描，不只扫描大音频前后窗口")
    parser.add_argument("--chunk-sec", type=float, default=60.0, help="流式读取块长度")
    parser.add_argument("--channels", default="all", help="实际物理通道索引，如 all 或 0,1,3 或 0-3")
    parser.add_argument("--min-tone-ratio", type=float, default=0.12, help="候选复核最低 1kHz 占比")
    parser.add_argument("--min-peak", type=int, default=300, help="候选复核最低峰值")
    parser.add_argument("--empty-peak-threshold", type=int, default=20, help="空通道判定峰值阈值")
    parser.add_argument("--empty-rms-dbfs", type=float, default=-90.0, help="空通道判定 RMS dBFS 阈值")
    parser.add_argument("--split-size-tolerance-ratio", type=float, default=0.001, help="拆分通道文件大小容差比例")
    parser.add_argument("--output-dir", type=Path, default=None, help="集中汇总输出目录，默认 root/beep_results")
    parser.add_argument("--out-name", default="{stem}_beep_result.txt", help="写到音频同目录的结果文件名模板")
    parser.add_argument("--no-write-per-file", action="store_true", help="不写每个音频同目录结果文件")
    parser.add_argument("--include-regex", default=None, help="只处理路径匹配该正则的音频")
    parser.add_argument("--exclude-regex", default=None, help="跳过路径匹配该正则的音频")
    parser.add_argument("--limit", type=int, default=0, help="仅处理前 N 个文件，调试用")
    parser.add_argument("--dry-run", action="store_true", help="只生成清单，不执行分析")
    parser.add_argument("--resume", action="store_true", help="读取 progress jsonl 并跳过已完成文件")
    args = parser.parse_args()

    root = args.root.resolve()
    output_dir = (args.output_dir or (root / "beep_results")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    progress_path = output_dir / "beep_progress.jsonl"

    all_root_files = discover_audio_files(root)
    files = discover_audio_files(root, args.include_regex, args.exclude_regex)
    pcm_files = [path for path in all_root_files if path.suffix.lower() == ".pcm"]
    split_map = detect_split_pcm_files(
        pcm_files,
        args.default_sample_rate,
        args.default_channels,
        args.split_size_tolerance_ratio,
    )
    if args.limit and args.limit > 0:
        files = files[: args.limit]

    manifest_path = write_manifest(output_dir, files, split_map, args)
    print(f"发现音频 {len(files)} 个；拆分单通道文件 {sum(1 for path in files if path in split_map)} 个")
    print(f"清单：{manifest_path}")
    if args.dry_run:
        print("dry-run 完成，未执行蜂鸣检测。")
        return 0

    completed = load_completed(progress_path) if args.resume else {}
    all_results: list[dict[str, Any]] = []
    if completed:
        all_results.extend(completed.values())
        print(f"resume: 已读取完成记录 {len(completed)} 条")

    started_at = time.time()
    with progress_path.open("a", encoding="utf-8") as progress:
        for index, path in enumerate(files, 1):
            path_key = str(path.resolve())
            if path_key in completed:
                print(f"[{index}/{len(files)}] 跳过已完成：{path}")
                continue
            try:
                fmt = infer_audio_format(path, split_map, args.default_sample_rate, args.default_channels)
                duration = audio_duration(path, fmt)
                windows = build_scan_windows(duration, args.edge_window_sec, args.full_scan)
                print(
                    f"[{index}/{len(files)}] 分析：{path} | "
                    f"{fmt.sample_rate}Hz/{fmt.bits_per_sample}bit/声明{fmt.declared_channels}通道/"
                    f"分析{fmt.physical_channels}通道 | {time_text(duration)} | {format_scan_windows(windows)}",
                    flush=True,
                )
                result = scan_audio(path, fmt, duration, windows, args)
                result["error"] = ""
                if not args.no_write_per_file:
                    out_path = path.with_name(args.out_name.format(stem=path.stem, suffix=path.suffix.lstrip(".")))
                    out_path.write_text(result_text(path, result, windows), encoding="utf-8")
                    result["result_file"] = str(out_path)
                all_results.append(result)
                progress.write(json.dumps(result, ensure_ascii=False) + "\n")
                progress.flush()
            except Exception as exc:
                error_result = {
                    "path": path_key,
                    "duration_sec": "",
                    "sample_rate": "",
                    "bits_per_sample": "",
                    "declared_channels": "",
                    "physical_channels": "",
                    "split_channel_index": "",
                    "format_source": "",
                    "frequency_hz": args.frequency,
                    "scan_windows": [],
                    "scan_read_bytes": 0,
                    "channels": [],
                    "error": repr(exc),
                }
                all_results.append(error_result)
                progress.write(json.dumps(error_result, ensure_ascii=False) + "\n")
                progress.flush()
                print(f"ERROR: {path}: {exc}", file=sys.stderr, flush=True)

    summary_rows: list[dict[str, Any]] = []
    not_found_rows: list[dict[str, Any]] = []
    file_summary_rows: list[dict[str, Any]] = []
    file_not_found_rows: list[dict[str, Any]] = []
    error_rows: list[dict[str, Any]] = []
    for result in all_results:
        if result.get("error"):
            error_rows.append({"文件": result.get("path", ""), "错误": result["error"]})
            continue
        rows, missing = flatten_rows(result, root)
        summary_rows.extend(rows)
        not_found_rows.extend(missing)
        file_row = flatten_file_row(result, root)
        file_summary_rows.append(file_row)
        if file_row["文件结果"] == "未检测到实际蜂鸣":
            file_not_found_rows.append(file_row)

    file_summary_path = output_dir / "beep_file_summary.csv"
    file_not_found_path = output_dir / "beep_file_not_found.csv"
    summary_path = output_dir / "beep_summary.csv"
    not_found_path = output_dir / "beep_not_found.csv"
    errors_path = output_dir / "beep_errors.csv"
    json_path = output_dir / "beep_summary.json"
    write_csv(file_summary_path, file_summary_rows)
    write_csv(file_not_found_path, file_not_found_rows)
    write_csv(summary_path, summary_rows)
    write_csv(not_found_path, not_found_rows)
    write_csv(errors_path, error_rows)
    json_path.write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")

    detected_files = {
        result["path"]
        for result in all_results
        if not result.get("error")
        and any(channel.get("detected_count", 0) > 0 for channel in result.get("channels", []))
    }
    elapsed = time.time() - started_at
    print("")
    print(f"完成：文件 {len(all_results)} 个，检出实际蜂鸣文件 {len(detected_files)} 个，耗时 {elapsed:.1f}s")
    print(f"文件级汇总表：{file_summary_path}")
    print(f"文件级未检出记录：{file_not_found_path}")
    print(f"汇总表：{summary_path}")
    print(f"未检出记录：{not_found_path}")
    print(f"错误记录：{errors_path}")
    print(f"JSON：{json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
