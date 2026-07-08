from __future__ import annotations

import argparse
import csv
import json
import math
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np


DEFAULT_ROOT_NAME = "\u6d77\u5c14h12\u9762\u677f16k16bit6\u901a\u9053"


@dataclass(frozen=True)
class AudioInfo:
    path: Path
    sample_rate: int
    channels: int
    bits: int
    data_offset: int
    frame_count: int
    source: str

    @property
    def bytes_per_sample(self) -> int:
        return self.bits // 8

    @property
    def frame_bytes(self) -> int:
        return self.channels * self.bytes_per_sample

    @property
    def duration(self) -> float:
        return self.frame_count / self.sample_rate

    @property
    def dtype(self) -> np.dtype:
        if self.bits == 16:
            return np.dtype("<i2")
        if self.bits == 32:
            return np.dtype("<i4")
        raise ValueError(f"unsupported bit depth: {self.bits}")

    @property
    def full_scale(self) -> float:
        return float(2 ** (self.bits - 1))


@dataclass(frozen=True)
class ScanWindow:
    label: str
    start: float
    end: float


def time_text(seconds: float | None) -> str:
    if seconds is None:
        return ""
    total_ms = int(round(max(0.0, seconds) * 1000.0))
    minutes = total_ms // 60000
    sec = (total_ms % 60000) // 1000
    ms = total_ms % 1000
    return f"{minutes}\u5206{sec:02d}\u79d2{ms:03d}\u6beb\u79d2"


def find_default_root() -> Path:
    base = Path(r"E:\tempData\beedata")
    return next(path for path in base.iterdir() if path.is_dir() and DEFAULT_ROOT_NAME in path.name)


def wave_data_offset(path: Path) -> int:
    raw = path.read_bytes()[:4096]
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    cursor = 12
    while cursor + 8 <= len(raw):
        chunk_id = raw[cursor : cursor + 4]
        chunk_size = int.from_bytes(raw[cursor + 4 : cursor + 8], "little", signed=False)
        cursor += 8
        if chunk_id == b"data":
            return cursor
        cursor += chunk_size + (chunk_size % 2)
    raise ValueError("data chunk not found in first 4096 bytes")


def inspect_audio(path: Path) -> AudioInfo:
    head = path.read_bytes()[:128]
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        with wave.open(str(path), "rb") as wav:
            sample_rate = wav.getframerate()
            channels = wav.getnchannels()
            bits = wav.getsampwidth() * 8
            frame_count = wav.getnframes()
        return AudioInfo(
            path=path,
            sample_rate=sample_rate,
            channels=channels,
            bits=bits,
            data_offset=wave_data_offset(path),
            frame_count=frame_count,
            source="wave-header",
        )

    # Two H12 files have a zero-filled header area but the payload is still
    # 16000 Hz / 6-channel / 32-bit PCM. Prefer the aligned 44-byte offset.
    size = path.stat().st_size
    for offset in (44, 0):
        frame_bytes = 6 * 4
        if size > offset and (size - offset) % frame_bytes == 0:
            return AudioInfo(
                path=path,
                sample_rate=16000,
                channels=6,
                bits=32,
                data_offset=offset,
                frame_count=(size - offset) // frame_bytes,
                source="raw-32le-offset44" if offset == 44 else "raw-32le",
            )

    # Last fallback for genuinely raw files matching the directory profile.
    frame_bytes = 6 * 2
    if size % frame_bytes == 0:
        return AudioInfo(path, 16000, 6, 16, 0, size // frame_bytes, "raw-16le-path-profile")
    raise ValueError(f"cannot infer audio format: {path}")


def scan_windows(duration: float, edge_sec: float) -> list[ScanWindow]:
    if duration <= edge_sec * 2:
        return [ScanWindow("full", 0.0, duration)]
    return [
        ScanWindow("head", 0.0, min(edge_sec, duration)),
        ScanWindow("tail", max(0.0, duration - edge_sec), duration),
    ]


def iter_blocks(info: AudioInfo, window: ScanWindow, chunk_sec: float) -> Iterable[tuple[int, np.ndarray]]:
    start_frame = max(0, int(math.floor(window.start * info.sample_rate)))
    end_frame = min(info.frame_count, int(math.ceil(window.end * info.sample_rate)))
    chunk_frames = max(1, int(round(chunk_sec * info.sample_rate)))
    with info.path.open("rb") as handle:
        handle.seek(info.data_offset + start_frame * info.frame_bytes)
        current = start_frame
        while current < end_frame:
            want = min(chunk_frames, end_frame - current)
            raw = handle.read(want * info.frame_bytes)
            if not raw:
                break
            usable = len(raw) - (len(raw) % info.frame_bytes)
            if usable <= 0:
                break
            frames = usable // info.frame_bytes
            data = np.frombuffer(raw[:usable], dtype=info.dtype).reshape(frames, info.channels)
            yield current, data
            current += frames


def frame_metrics(
    values: np.ndarray,
    base_frame: int,
    sample_rate: int,
    full_scale: float,
    frequency: float,
    frame_n: int,
    hop_n: int,
    cos_vals: np.ndarray,
    sin_vals: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    if len(values) < frame_n:
        empty_f = np.array([], dtype=np.float64)
        empty_i = np.array([], dtype=np.int64)
        return empty_f, empty_f, empty_f, empty_i
    count = 1 + (len(values) - frame_n) // hop_n
    starts = np.arange(count, dtype=np.float64) * hop_n
    times = (base_frame + starts) / sample_rate
    stride = values.strides[0]
    frames_i = np.lib.stride_tricks.as_strided(
        values,
        shape=(count, frame_n),
        strides=(hop_n * stride, stride),
        writeable=False,
    )
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
    peak = np.max(np.abs(frames_i.astype(np.int64, copy=False)), axis=1)
    return times, tone_db, ratio, peak


def collect_coarse(info: AudioInfo, windows: list[ScanWindow], channels: list[int], args: argparse.Namespace) -> dict[int, dict[str, np.ndarray]]:
    frame_n = max(1, int(round(info.sample_rate * args.frame_ms / 1000.0)))
    hop_n = max(1, int(round(info.sample_rate * args.hop_ms / 1000.0)))
    idx = np.arange(frame_n, dtype=np.float64)
    omega = 2.0 * math.pi * args.frequency / info.sample_rate
    cos_vals = np.cos(omega * idx)
    sin_vals = np.sin(omega * idx)
    metrics = {ch: {"time": [], "tone_db": [], "ratio": [], "peak": []} for ch in channels}

    for window in windows:
        carry = np.empty((0, info.channels), dtype=info.dtype)
        carry_base = int(round(window.start * info.sample_rate))
        for block_start, block in iter_blocks(info, window, args.chunk_sec):
            if len(carry):
                data = np.vstack([carry, block])
                base_frame = carry_base
            else:
                data = block
                base_frame = block_start
            processed = 0
            for channel in channels:
                t, db, ratio, peak = frame_metrics(
                    np.ascontiguousarray(data[:, channel]),
                    base_frame,
                    info.sample_rate,
                    info.full_scale,
                    args.frequency,
                    frame_n,
                    hop_n,
                    cos_vals,
                    sin_vals,
                )
                if len(t):
                    metrics[channel]["time"].append(t)
                    metrics[channel]["tone_db"].append(db.astype(np.float32))
                    metrics[channel]["ratio"].append(ratio.astype(np.float32))
                    metrics[channel]["peak"].append(peak.astype(np.int64))
                    processed = len(t)
            if processed <= 0:
                carry = data
                carry_base = base_frame
                keep = max(frame_n - 1, hop_n)
                if len(carry) > keep:
                    carry = carry[-keep:]
                    carry_base = base_frame + len(data) - len(carry)
            else:
                next_start = processed * hop_n
                carry = data[next_start:]
                carry_base = base_frame + next_start

    result: dict[int, dict[str, np.ndarray]] = {}
    for channel in channels:
        result[channel] = {
            key: np.concatenate(chunks) if chunks else np.array([], dtype=np.float64)
            for key, chunks in metrics[channel].items()
        }
    return result


def runs_from_metrics(arrays: dict[str, np.ndarray], hop_sec: float, args: argparse.Namespace) -> list[dict[str, float]]:
    times = arrays["time"]
    tone_db = arrays["tone_db"]
    ratio = arrays["ratio"]
    peak = arrays["peak"]
    mask = (
        np.isfinite(tone_db)
        & (tone_db >= args.coarse_min_tone_db)
        & (ratio >= args.coarse_min_ratio)
        & (peak >= args.min_peak)
    )
    active = np.flatnonzero(mask)
    if len(active) == 0:
        return []
    gaps = np.diff(times[active])
    groups = np.split(active, np.flatnonzero(gaps > max(0.151, hop_sec * 3.01)) + 1)
    runs: list[dict[str, float]] = []
    for group in groups:
        if len(group) == 0:
            continue
        start = float(times[group[0]])
        end = float(times[group[-1]] + hop_sec)
        duration = end - start
        if args.min_beep_sec <= duration <= args.max_beep_sec:
            runs.append(
                {
                    "start": start,
                    "end": end,
                    "duration": duration,
                    "mean_ratio": float(np.nanmean(ratio[group])),
                    "max_db": float(np.nanmax(tone_db[group])),
                    "max_peak": float(np.max(peak[group])),
                }
            )
    return runs


def pairs_from_runs(runs: list[dict[str, float]], channel: int, args: argparse.Namespace) -> list[dict[str, float]]:
    pairs = []
    for index, first in enumerate(runs):
        for second in runs[index + 1 : index + 8]:
            delta = second["start"] - first["start"]
            gap = second["start"] - first["end"]
            if args.min_pair_delta <= delta <= args.max_pair_delta and args.min_gap <= gap <= args.max_gap:
                score = (
                    (first["mean_ratio"] + second["mean_ratio"]) * 50.0
                    + (first["max_db"] + second["max_db"]) / 4.0
                    - abs(delta - args.target_pair_delta) * 20.0
                    - abs(first["duration"] - 1.0) * 10.0
                    - abs(second["duration"] - 1.0) * 10.0
                )
                pairs.append(
                    {
                        "channel": float(channel),
                        "start": first["start"],
                        "end": second["end"],
                        "score": score,
                        "mean_ratio": (first["mean_ratio"] + second["mean_ratio"]) / 2.0,
                        "delta": delta,
                        "gap": gap,
                    }
                )
    return pairs


def cluster_pairs(pairs: list[dict[str, float]], args: argparse.Namespace) -> list[dict[str, object]]:
    pairs = sorted(pairs, key=lambda item: (item["start"], -item["score"]))
    clusters: list[list[dict[str, float]]] = []
    for pair in pairs:
        found = None
        for cluster in clusters:
            center = sum(item["start"] for item in cluster) / len(cluster)
            if abs(pair["start"] - center) <= args.cluster_sec:
                found = cluster
                break
        if found is None:
            clusters.append([pair])
        else:
            found.append(pair)

    events: list[dict[str, object]] = []
    for cluster in clusters:
        channels = sorted({int(item["channel"]) for item in cluster})
        if len(channels) < args.min_support:
            continue
        start = min(item["start"] for item in cluster)
        end = max(item["end"] for item in cluster)
        events.append(
            {
                "start": start,
                "end": end,
                "channels": channels,
                "support": len(channels),
                "score": max(item["score"] for item in cluster),
                "mean_ratio": sum(item["mean_ratio"] for item in cluster) / len(cluster),
            }
        )
    events.sort(key=lambda item: float(item["start"]))
    return events


def refine_event(info: AudioInfo, event: dict[str, object], channels: list[int], args: argparse.Namespace) -> dict[str, object]:
    start = max(0.0, float(event["start"]) - args.refine_margin_sec)
    end = min(info.duration, float(event["end"]) + args.refine_margin_sec)
    frame_n = max(1, int(round(info.sample_rate * args.refine_frame_ms / 1000.0)))
    hop_n = max(1, int(round(info.sample_rate * args.refine_hop_ms / 1000.0)))
    hop_sec = hop_n / info.sample_rate
    idx = np.arange(frame_n, dtype=np.float64)
    omega = 2.0 * math.pi * args.frequency / info.sample_rate
    cos_vals = np.cos(omega * idx)
    sin_vals = np.sin(omega * idx)

    window = ScanWindow("refine", start, end)
    start_frame = int(math.floor(start * info.sample_rate))
    end_frame = int(math.ceil(end * info.sample_rate))
    with info.path.open("rb") as handle:
        handle.seek(info.data_offset + start_frame * info.frame_bytes)
        raw = handle.read((end_frame - start_frame) * info.frame_bytes)
    usable = len(raw) - (len(raw) % info.frame_bytes)
    if usable <= 0:
        return event
    data = np.frombuffer(raw[:usable], dtype=info.dtype).reshape(-1, info.channels)

    channel_runs: dict[int, list[tuple[float, float, float]]] = {}
    for channel in channels:
        t, db, ratio, peak = frame_metrics(
            np.ascontiguousarray(data[:, channel]),
            start_frame,
            info.sample_rate,
            info.full_scale,
            args.frequency,
            frame_n,
            hop_n,
            cos_vals,
            sin_vals,
        )
        active = np.flatnonzero(
            np.isfinite(db)
            & (db >= args.refine_min_tone_db)
            & (ratio >= args.refine_min_ratio)
            & (peak >= args.min_peak)
        )
        runs: list[tuple[float, float, float]] = []
        if len(active):
            groups = np.split(active, np.flatnonzero(np.diff(t[active]) > args.refine_gap_merge_sec) + 1)
            for group in groups:
                if len(group) >= args.refine_min_frames:
                    runs.append((float(t[group[0]]), float(t[group[-1]] + frame_n / info.sample_rate), float(np.mean(ratio[group]))))
        channel_runs[channel] = runs

    # Pick the pair that overlaps the coarse event and has enough channel support.
    pair_candidates: list[dict[str, object]] = []
    for channel, runs in channel_runs.items():
        for index, first in enumerate(runs):
            for second in runs[index + 1 : index + 6]:
                delta = second[0] - first[0]
                gap = second[0] - first[1]
                if args.min_pair_delta <= delta <= args.max_pair_delta and args.min_gap <= gap <= args.max_gap:
                    pair_candidates.append(
                        {
                            "channel": channel,
                            "start": first[0],
                            "end": second[1],
                            "first_end": first[1],
                            "second_start": second[0],
                            "ratio": (first[2] + second[2]) / 2.0,
                        }
                    )
                    break

    if not pair_candidates:
        return event
    center = float(event["start"])
    pair_candidates = [item for item in pair_candidates if abs(float(item["start"]) - center) <= args.refine_cluster_sec]
    if not pair_candidates:
        return event
    support_channels = sorted({int(item["channel"]) for item in pair_candidates})
    if len(support_channels) < args.min_support:
        return event
    refined = dict(event)
    refined["start"] = min(float(item["start"]) for item in pair_candidates)
    refined["end"] = max(float(item["end"]) for item in pair_candidates)
    refined["channels"] = support_channels
    refined["support"] = len(support_channels)
    refined["mean_ratio"] = sum(float(item["ratio"]) for item in pair_candidates) / len(pair_candidates)
    return refined


def scan_file(path: Path, args: argparse.Namespace) -> dict[str, object]:
    info = inspect_audio(path)
    channels = [idx for idx in range(min(4, info.channels))]
    windows = scan_windows(info.duration, args.edge_window_sec)
    coarse = collect_coarse(info, windows, channels, args)
    hop_sec = args.hop_ms / 1000.0
    pairs: list[dict[str, float]] = []
    channel_candidate_counts: dict[int, int] = {}
    for channel in channels:
        runs = runs_from_metrics(coarse[channel], hop_sec, args)
        channel_pairs = pairs_from_runs(runs, channel, args)
        channel_candidate_counts[channel] = len(channel_pairs)
        pairs.extend(channel_pairs)
    events = cluster_pairs(pairs, args)
    refined = [refine_event(info, event, channels, args) for event in events]
    return {
        "file": str(path),
        "format": {
            "sample_rate": info.sample_rate,
            "channels": info.channels,
            "bits": info.bits,
            "data_offset": info.data_offset,
            "source": info.source,
            "duration": info.duration,
        },
        "primary_channel_count": len(channels),
        "events": refined,
        "candidate_counts": channel_candidate_counts,
    }


def official_events(result: dict[str, object], args: argparse.Namespace) -> tuple[list[dict[str, object]], str]:
    events = sorted(result["events"], key=lambda item: float(item["start"]))
    primary_count = max(1, int(result.get("primary_channel_count", 1)))
    ratio_support = int(math.ceil(primary_count * args.official_min_support_ratio))
    min_support = args.official_min_support if args.official_min_support > 0 else max(1, ratio_support)
    reliable = [
        event
        for event in events
        if int(event["support"]) >= min_support
        and float(event["mean_ratio"]) >= args.official_min_ratio
    ]
    reasons: list[str] = []
    if not events:
        reasons.append("未检出可靠蜂鸣")
    if events and len(reliable) < len(events):
        reasons.append("存在通道支持不足或 tone_ratio 偏低的候选")
    if not args.allow_incomplete_edges:
        duration = float(result["format"]["duration"])
        if len(reliable) < 2:
            reasons.append("头尾蜂鸣证据不完整")
        else:
            first = float(reliable[0]["start"])
            last = float(reliable[-1]["start"])
            if first > min(args.edge_window_sec, duration) or last < max(0.0, duration - args.edge_window_sec):
                reasons.append("头部或尾部蜂鸣缺失")
    if reasons:
        return [], "；".join(dict.fromkeys(reasons))
    return reliable, ""


def review_row(root: Path, result: dict[str, object], args: argparse.Namespace) -> dict[str, str]:
    path = Path(str(result["file"]))
    rel = str(path.relative_to(root))
    events = sorted(result["events"], key=lambda item: float(item["start"]))
    duration = float(result["format"]["duration"])
    primary_count = max(1, int(result.get("primary_channel_count", 1)))
    support_need = max(1, int(math.ceil(primary_count * args.official_min_support_ratio)))

    head = None
    tail = None
    if len(events) >= 2:
        head = events[0]
        tail = events[-1]
    elif len(events) == 1:
        if float(events[0]["start"]) <= duration / 2.0:
            head = events[0]
        else:
            tail = events[0]

    notes: list[str] = []
    if not events:
        notes.append("未检出可靠特征")
    if events and head is None:
        notes.append("仅尾部，头部未找到可靠特征")
    if events and tail is None:
        notes.append("仅头部，尾部未找到可靠特征")

    shown = [event for event in [head, tail] if event is not None]
    weak = [event for event in shown if float(event["mean_ratio"]) < args.review_strong_ratio]
    low_support = [event for event in shown if int(event["support"]) < primary_count]
    insufficient = [
        event
        for event in shown
        if int(event["support"]) < support_need or float(event["mean_ratio"]) < args.official_min_ratio
    ]
    if insufficient:
        notes.append("证据不足")
    elif weak:
        notes.append("特征偏弱")
    if low_support:
        channel_notes = []
        for event in low_support:
            channels = ",".join(f"ch{ch}" for ch in event["channels"])
            channel_notes.append(channels)
        notes.append("mic缺陷/通道不足：" + "；".join(channel_notes))

    if not notes and events:
        notes.append("稳定")

    return {
        "file": rel,
        "head_bee_start": time_text(float(head["start"])) if head else "",
        "head_bee_end": time_text(float(head["end"])) if head else "",
        "tail_bee_start": time_text(float(tail["start"])) if tail else "",
        "tail_bee_end": time_text(float(tail["end"])) if tail else "",
        "remark": "；".join(dict.fromkeys(notes)),
    }


def write_outputs(root: Path, results: list[dict[str, object]], out_dir: Path, args: argparse.Namespace) -> tuple[Path, Path, Path, Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    detail_path = out_dir / f"{args.output_prefix}_positions.csv"
    simple_path = out_dir / f"{args.output_prefix}_positions_simple.csv"
    not_found_path = out_dir / f"{args.output_prefix}_not_found.csv"
    review_path = out_dir / f"{args.output_prefix}_review.csv"
    json_path = out_dir / f"{args.output_prefix}_audit_candidates.json"

    detail_rows = []
    simple_rows = []
    not_found_rows = []
    review_rows = []
    for result in results:
        path = Path(str(result["file"]))
        rel = str(path.relative_to(root))
        fmt = result["format"]
        events, reject_reason = official_events(result, args)
        review_rows.append(review_row(root, result, args))
        if events:
            for index, event in enumerate(events, 1):
                channels = ",".join(f"ch{ch}" for ch in event["channels"])
                row = {
                    "file": rel,
                    "index": index,
                    "bee_start": time_text(float(event["start"])),
                    "bee_end": time_text(float(event["end"])),
                    "start_sec": f"{float(event['start']):.3f}",
                    "end_sec": f"{float(event['end']):.3f}",
                    "channels": channels,
                    "support": event["support"],
                    "tone_ratio": f"{float(event['mean_ratio']):.3f}",
                    "format": f"{fmt['sample_rate']}Hz/{fmt['bits']}bit/{fmt['channels']}ch/{fmt['source']}/offset{fmt['data_offset']}",
                    "duration": time_text(float(fmt["duration"])),
                }
                detail_rows.append(row)
                simple_rows.append({"file": rel, "bee_start": row["bee_start"], "bee_end": row["bee_end"]})
        else:
            not_found_rows.append(
                {
                    "file": rel,
                    "result": "not_found_or_incomplete",
                    "reason": reject_reason,
                    "format": f"{fmt['sample_rate']}Hz/{fmt['bits']}bit/{fmt['channels']}ch/{fmt['source']}/offset{fmt['data_offset']}",
                    "duration": time_text(float(fmt["duration"])),
                    "candidate_counts": json.dumps(result["candidate_counts"], ensure_ascii=False),
                }
            )

    for path, rows, fields in [
        (
            detail_path,
            detail_rows,
            ["file", "index", "bee_start", "bee_end", "start_sec", "end_sec", "channels", "support", "tone_ratio", "format", "duration"],
        ),
        (simple_path, simple_rows, ["file", "bee_start", "bee_end"]),
        (not_found_path, not_found_rows, ["file", "result", "reason", "format", "duration", "candidate_counts"]),
        (review_path, review_rows, ["file", "head_bee_start", "head_bee_end", "tail_bee_start", "tail_bee_end", "remark"]),
    ]:
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(results, handle, ensure_ascii=False, indent=2)
    return detail_path, simple_path, not_found_path, review_path, json_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Header-aware multichannel edge beep scanner.")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=Path(r"E:\tempData\beedata\beep_results"))
    parser.add_argument("--output-prefix", default="header_aware_edge_beeps")
    parser.add_argument("--frequency", type=float, default=1000.0)
    parser.add_argument("--edge-window-sec", type=float, default=3600.0)
    parser.add_argument("--chunk-sec", type=float, default=60.0)
    parser.add_argument("--frame-ms", type=float, default=100.0)
    parser.add_argument("--hop-ms", type=float, default=50.0)
    parser.add_argument("--coarse-min-ratio", type=float, default=0.35)
    parser.add_argument("--coarse-min-tone-db", type=float, default=-60.0)
    parser.add_argument("--min-peak", type=int, default=1000)
    parser.add_argument("--min-beep-sec", type=float, default=0.50)
    parser.add_argument("--max-beep-sec", type=float, default=1.60)
    parser.add_argument("--min-pair-delta", type=float, default=1.70)
    parser.add_argument("--max-pair-delta", type=float, default=2.30)
    parser.add_argument("--target-pair-delta", type=float, default=2.0)
    parser.add_argument("--min-gap", type=float, default=0.40)
    parser.add_argument("--max-gap", type=float, default=1.45)
    parser.add_argument("--cluster-sec", type=float, default=0.35)
    parser.add_argument("--min-support", type=int, default=2)
    parser.add_argument("--refine-margin-sec", type=float, default=0.35)
    parser.add_argument("--refine-frame-ms", type=float, default=20.0)
    parser.add_argument("--refine-hop-ms", type=float, default=1.0)
    parser.add_argument("--refine-min-ratio", type=float, default=0.60)
    parser.add_argument("--refine-min-tone-db", type=float, default=-55.0)
    parser.add_argument("--refine-min-frames", type=int, default=20)
    parser.add_argument("--refine-gap-merge-sec", type=float, default=0.006)
    parser.add_argument("--refine-cluster-sec", type=float, default=0.30)
    parser.add_argument("--official-min-support", type=int, default=0, help="正式输出所需主通道支持数；0 表示按比例自动计算")
    parser.add_argument("--official-min-support-ratio", type=float, default=0.50, help="正式输出所需主通道支持比例，默认 50%")
    parser.add_argument("--official-min-ratio", type=float, default=0.50, help="正式输出最低 tone_ratio")
    parser.add_argument("--review-strong-ratio", type=float, default=0.70, help="review 表中标记稳定/偏弱的 tone_ratio 分界")
    parser.add_argument("--allow-incomplete-edges", action="store_true", help="允许只找到头或尾时仍输出位置")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    root = args.root or find_default_root()
    files = sorted(root.rglob("*.pcm"))
    if args.limit:
        files = files[: args.limit]
    results = []
    for index, path in enumerate(files, 1):
        result = scan_file(path, args)
        results.append(result)
        official, reason = official_events(result, args)
        event_text = ", ".join(f"{time_text(float(ev['start']))}->{time_text(float(ev['end']))}" for ev in official) or f"not_found_or_incomplete:{reason}"
        print(f"[{index}/{len(files)}] {path.relative_to(root)} | {event_text}", flush=True)
    detail_path, simple_path, not_found_path, review_path, json_path = write_outputs(root, results, args.out_dir, args)
    official = [official_events(item, args)[0] for item in results]
    found_files = sum(1 for events in official if events)
    event_count = sum(len(events) for events in official)
    print(f"done files={len(results)} official_found_files={found_files} official_events={event_count}")
    print(detail_path)
    print(simple_path)
    print(not_found_path)
    print(review_path)
    print(json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
