#!/usr/bin/env python3
"""Locate fixed-frequency two-beep markers in WAV/PCM audio.

Default target: 16 kHz, mono, signed 16-bit little-endian PCM, 1 kHz marker.
The marker pattern is about 1 s beep + 1 s gap + 1 s beep.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import wave
from array import array
from pathlib import Path


DEFAULT_SR = 16000
DEFAULT_FREQ = 1000.0
DEFAULT_FRAME_MS = 50.0
DEFAULT_HOP_MS = 50.0
SAMPLE_WIDTH = 2


def time_text(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    minutes = total_ms // 60000
    sec = (total_ms % 60000) // 1000
    ms = total_ms % 1000
    return f"{minutes}分{sec:02d}秒{ms:03d}毫秒"


def load_samples(
    path: Path,
    sample_rate: int,
    channels: int,
    channel_index: int,
    sample_format: str,
) -> tuple[array, int, float]:
    suffix = path.suffix.lower()
    if suffix == ".wav":
        with wave.open(str(path), "rb") as wav:
            sr = wav.getframerate()
            ch = wav.getnchannels()
            width = wav.getsampwidth()
            raw = wav.readframes(wav.getnframes())
        if width != SAMPLE_WIDTH:
            raise ValueError(f"{path}: only 16-bit WAV is supported, got {width * 8}-bit")
        sample_rate = sr
        channels = ch
    else:
        if sample_format.lower() != "s16le":
            raise ValueError("only s16le PCM is supported by this script")
        raw = path.read_bytes()

    samples = array("h")
    usable_bytes = len(raw) - (len(raw) % SAMPLE_WIDTH)
    samples.frombytes(raw[:usable_bytes])
    if sys.byteorder != "little":
        samples.byteswap()

    if channels < 1:
        raise ValueError("channels must be >= 1")
    if not 0 <= channel_index < channels:
        raise ValueError("channel-index must be in [0, channels)")
    if channels > 1:
        samples = array("h", samples[channel_index::channels])

    duration = len(samples) / sample_rate
    return samples, sample_rate, duration


def segment_stats(
    samples: array,
    sample_rate: int,
    frequency: float,
    start_sample: int,
    end_sample: int,
) -> dict:
    start_sample = max(0, start_sample)
    end_sample = min(len(samples), end_sample)
    n = end_sample - start_sample
    if n <= 0:
        return {
            "rms": 0.0,
            "rms_dbfs": None,
            "tone_ratio": 0.0,
            "tone_rms_dbfs": None,
            "peak": 0,
            "zero_percent": None,
        }

    omega = 2.0 * math.pi * frequency / sample_rate
    cos_vals = [math.cos(omega * i) for i in range(n)]
    sin_vals = [math.sin(omega * i) for i in range(n)]
    energy = 0
    re = 0.0
    im = 0.0
    peak = 0
    zero_count = 0
    for i, value in enumerate(samples[start_sample:end_sample]):
        energy += value * value
        re += value * cos_vals[i]
        im += value * sin_vals[i]
        abs_value = -value if value < 0 else value
        if abs_value > peak:
            peak = abs_value
        if value == 0:
            zero_count += 1

    if energy <= 0:
        return {
            "rms": 0.0,
            "rms_dbfs": None,
            "tone_ratio": 0.0,
            "tone_rms_dbfs": None,
            "peak": peak,
            "zero_percent": zero_count / n * 100.0,
        }

    rms = math.sqrt(energy / n)
    rms_dbfs = 20.0 * math.log10(rms / 32768.0) if rms > 0 else None
    tone_ratio = 2.0 * (re * re + im * im) / (n * energy)
    tone_ratio = max(0.0, min(1.0, tone_ratio))
    tone_rms = rms * math.sqrt(tone_ratio)
    tone_rms_dbfs = 20.0 * math.log10(tone_rms / 32768.0) if tone_rms > 0 else None
    return {
        "rms": rms,
        "rms_dbfs": rms_dbfs,
        "tone_ratio": tone_ratio,
        "tone_rms_dbfs": tone_rms_dbfs,
        "peak": peak,
        "zero_percent": zero_count / n * 100.0,
    }


def frame_metrics(
    samples: array,
    sample_rate: int,
    frequency: float,
    frame_ms: float,
    hop_ms: float,
) -> list[dict]:
    frame_n = max(1, int(round(sample_rate * frame_ms / 1000.0)))
    hop_n = max(1, int(round(sample_rate * hop_ms / 1000.0)))
    frames = []
    for start in range(0, max(0, len(samples) - frame_n + 1), hop_n):
        stats = segment_stats(samples, sample_rate, frequency, start, start + frame_n)
        stats["time"] = start / sample_rate
        frames.append(stats)
    return frames


def runs_for_threshold(frames: list[dict], threshold_dbfs: float, hop_sec: float) -> list[dict]:
    active = [
        f
        for f in frames
        if f["tone_rms_dbfs"] is not None
        and f["tone_rms_dbfs"] >= threshold_dbfs
        and f["tone_ratio"] >= 0.08
        and f["peak"] >= 200
    ]
    if not active:
        return []

    runs = []
    start = active[0]["time"]
    last = active[0]["time"]
    max_tone = active[0]["tone_rms_dbfs"]
    sum_tone = active[0]["tone_rms_dbfs"]
    sum_ratio = active[0]["tone_ratio"]
    max_peak = active[0]["peak"]
    count = 1
    for frame in active[1:]:
        t = frame["time"]
        if t - last <= max(0.151, hop_sec * 3.01):
            last = t
            max_tone = max(max_tone, frame["tone_rms_dbfs"])
            sum_tone += frame["tone_rms_dbfs"]
            sum_ratio += frame["tone_ratio"]
            max_peak = max(max_peak, frame["peak"])
            count += 1
        else:
            end = last + hop_sec
            runs.append(
                {
                    "start": start,
                    "end": end,
                    "duration": end - start,
                    "frames": count,
                    "max_tone_rms_dbfs": max_tone,
                    "mean_tone_rms_dbfs": sum_tone / count,
                    "mean_frame_tone_ratio": sum_ratio / count,
                    "max_peak": max_peak,
                    "threshold_dbfs": threshold_dbfs,
                }
            )
            start = t
            last = t
            max_tone = frame["tone_rms_dbfs"]
            sum_tone = frame["tone_rms_dbfs"]
            sum_ratio = frame["tone_ratio"]
            max_peak = frame["peak"]
            count = 1

    end = last + hop_sec
    runs.append(
        {
            "start": start,
            "end": end,
            "duration": end - start,
            "frames": count,
            "max_tone_rms_dbfs": max_tone,
            "mean_tone_rms_dbfs": sum_tone / count,
            "mean_frame_tone_ratio": sum_ratio / count,
            "max_peak": max_peak,
            "threshold_dbfs": threshold_dbfs,
        }
    )
    return runs


def find_marker_pairs(frames: list[dict], hop_sec: float) -> list[dict]:
    candidates = []
    thresholds = [-34, -36, -38, -40, -42, -44, -46, -48, -50, -52, -54, -56]
    for threshold in thresholds:
        runs = [r for r in runs_for_threshold(frames, threshold, hop_sec) if 0.50 <= r["duration"] <= 1.55]
        for i, first in enumerate(runs):
            for second in runs[i + 1 : i + 4]:
                start_delta = second["start"] - first["start"]
                gap = second["start"] - first["end"]
                if 1.70 <= start_delta <= 2.30 and 0.45 <= gap <= 1.45:
                    dur_err = abs(first["duration"] - 1.0) + abs(second["duration"] - 1.0)
                    gap_err = abs(gap - 1.0)
                    score = 100.0 - 35.0 * dur_err - 20.0 * gap_err + max(
                        first["max_tone_rms_dbfs"], second["max_tone_rms_dbfs"]
                    ) / 2.0
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

    # Weak channels may show the two beeps as short bursts. Keep them only if
    # later raw-tone validation confirms the 1 kHz component.
    for threshold in [-44, -46, -48, -50, -52, -54, -56]:
        runs = [r for r in runs_for_threshold(frames, threshold, hop_sec) if 0.05 <= r["duration"] <= 0.60]
        for i, first in enumerate(runs):
            for second in runs[i + 1 : i + 8]:
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
    deduped = []
    for candidate in candidates:
        existing = next((item for item in deduped if abs(item["start"] - candidate["start"]) < 0.25), None)
        if existing is None:
            deduped.append(candidate)
        elif candidate["score"] > existing["score"]:
            existing.update(candidate)
    return sorted(deduped, key=lambda item: item["start"])


def validate_markers(
    samples: array,
    sample_rate: int,
    frequency: float,
    markers: list[dict],
) -> list[dict]:
    validated = []
    for marker in markers:
        for beep in marker["beeps"]:
            start = int(round(beep["start"] * sample_rate))
            end = int(round(beep["end"] * sample_rate))
            # Use the central 0.5 s when possible to avoid edge transition noise.
            if end - start > sample_rate // 2:
                center = (start + end) // 2
                half = sample_rate // 4
                start = max(start, center - half)
                end = min(end, center + half)
            stats = segment_stats(samples, sample_rate, frequency, start, end)
            beep["raw_rms_dbfs"] = stats["rms_dbfs"]
            beep["tone_ratio_1khz"] = stats["tone_ratio"]
            beep["raw_peak"] = stats["peak"]
            beep["zero_percent"] = stats["zero_percent"]

        ratios = [beep.get("tone_ratio_1khz", 0.0) for beep in marker["beeps"]]
        peaks = [beep.get("raw_peak", 0) for beep in marker["beeps"]]
        marker["mean_tone_ratio_1khz"] = sum(ratios) / len(ratios)
        marker["max_raw_peak"] = max(peaks) if peaks else 0
        if marker["mean_tone_ratio_1khz"] < 0.12 or marker["max_raw_peak"] < 300:
            continue
        if marker["mean_tone_ratio_1khz"] >= 0.35:
            marker["confidence"] = "high"
        elif marker["mean_tone_ratio_1khz"] >= 0.20:
            marker["confidence"] = "medium"
        else:
            marker["confidence"] = "weak"
        validated.append(marker)

    validated.sort(key=lambda item: item["start"])
    deduped = []
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


def edge_filter(markers: list[dict], duration: float, head_sec: float, tail_sec: float) -> list[dict]:
    return [m for m in markers if m["start"] <= head_sec or m["end"] >= duration - tail_sec]


def result_text(path: Path, markers: list[dict], sample_rate: int, frequency: float, duration: float) -> str:
    lines = [
        f"{path.name} 实际蜂鸣特征位置（{sample_rate}Hz / 16bit PCM/WAV / 目标频率 {frequency:g}Hz）",
        "",
    ]
    if not markers:
        lines.append("未检测到实际蜂鸣特征")
    else:
        for idx, marker in enumerate(markers, 1):
            lines.append(
                f"{idx}. {time_text(marker['start'])} → {time_text(marker['end'])}"
                f"（持续 {time_text(marker['duration'])}）"
            )
    lines.extend(
        [
            "",
            "说明：以上只表示该音频自身实际检测到的蜂鸣特征；不要把同组通道同步推断窗口写成实际蜂鸣。",
            f"音频时长：{time_text(duration)}",
        ]
    )
    return "\n".join(lines) + "\n"


def scan_one(path: Path, args: argparse.Namespace) -> dict:
    samples, sample_rate, duration = load_samples(
        path,
        args.sample_rate,
        args.channels,
        args.channel_index,
        args.sample_format,
    )
    hop_sec = args.hop_ms / 1000.0
    frames = frame_metrics(samples, sample_rate, args.frequency, args.frame_ms, args.hop_ms)
    markers = find_marker_pairs(frames, hop_sec)
    markers = validate_markers(samples, sample_rate, args.frequency, markers)
    if args.edge_only:
        markers = edge_filter(markers, duration, args.head_sec, args.tail_sec)
    result = {
        "path": str(path),
        "duration_sec": duration,
        "sample_rate": sample_rate,
        "channels": args.channels,
        "channel_index": args.channel_index,
        "sample_format": args.sample_format,
        "frequency_hz": args.frequency,
        "edge_only": args.edge_only,
        "markers": markers,
    }
    if args.write_result:
        out_path = path.with_name(args.out_name.format(stem=path.stem, suffix=path.suffix.lstrip(".")))
        out_path.write_text(result_text(path, markers, sample_rate, args.frequency, duration), encoding="utf-8")
        result["result_file"] = str(out_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Locate fixed 1 kHz two-beep markers in audio.")
    parser.add_argument("audio", nargs="+", type=Path, help="Audio files: .pcm or .wav")
    parser.add_argument("--sample-rate", type=int, default=DEFAULT_SR, help="PCM sample rate; WAV reads header")
    parser.add_argument("--channels", type=int, default=1, help="PCM channel count")
    parser.add_argument("--channel-index", type=int, default=0, help="Channel to analyze when channels > 1")
    parser.add_argument("--sample-format", default="s16le", help="PCM sample format; currently s16le only")
    parser.add_argument("--frequency", type=float, default=DEFAULT_FREQ, help="Target beep frequency in Hz")
    parser.add_argument("--frame-ms", type=float, default=DEFAULT_FRAME_MS, help="Analysis frame length")
    parser.add_argument("--hop-ms", type=float, default=DEFAULT_HOP_MS, help="Analysis hop length")
    parser.add_argument("--edge-only", action="store_true", help="Keep only markers near head/tail")
    parser.add_argument("--head-sec", type=float, default=60.0, help="Head window for --edge-only")
    parser.add_argument("--tail-sec", type=float, default=35.0, help="Tail window for --edge-only")
    parser.add_argument(
        "--out-name",
        default="{stem}_beep_result.txt",
        help="Result filename pattern written beside each input",
    )
    parser.add_argument("--no-write-result", dest="write_result", action="store_false")
    parser.set_defaults(write_result=True)
    args = parser.parse_args()

    all_results = []
    for audio_path in args.audio:
        result = scan_one(audio_path.resolve(), args)
        all_results.append(result)
        print(result_text(audio_path, result["markers"], result["sample_rate"], result["frequency_hz"], result["duration_sec"]))
        if "result_file" in result:
            print(f"结果文件：{result['result_file']}")
    print(json.dumps(all_results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
