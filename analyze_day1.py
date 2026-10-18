import os
import glob
import json
import subprocess
import tempfile
import cv2
import numpy as np
from faster_whisper import WhisperModel

# Known Whisper Korean hallucinations on background noise
HALLUCINATION_PHRASES = [
    "구독과 좋아요",
    "시청해주셔서 감사합니다",
    "시청해 주셔서 감사합니다",
    "MBC뉴스",
    "SBS뉴스",
    "KBS뉴스",
    "자막 제공",
    "자막제공",
    "한글자막",
    "감사합니다.",
    "고맙습니다.",
    "다음 영상에서",
    "알림 설정",
    "구독",
    "좋아요",
]


def is_hallucination(text: str, no_speech_prob: float, avg_logprob: float, compression_ratio: float) -> bool:
    t = text.strip()
    if not t or len(t) <= 1:
        return True
    if no_speech_prob > 0.65:
        return True
    if avg_logprob < -1.15:
        return True
    if compression_ratio > 2.2:
        return True
    # Check repeated characters/words
    for phrase in HALLUCINATION_PHRASES:
        if phrase in t and (len(t) <= len(phrase) + 4 or no_speech_prob > 0.35):
            return True
    # Filter pure punctuation or meaningless filler
    cleaned = "".join(ch for ch in t if ch.isalnum())
    if not cleaned:
        return True
    return False


def extract_denoised_wav(video_path: str, wav_path: str):
    """Extract 16kHz mono audio with bandpass + FFT spectral denoise for wind/cabin rumble."""
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "quiet",
        "-i",
        video_path,
        "-vn",
        "-af",
        "highpass=f=120,lowpass=f=3800,afftdn=nf=-25:tn=1,loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ac",
        "1",
        "-ar",
        "16000",
        wav_path,
    ]
    subprocess.run(cmd, check=True)


def analyze_video_vision(video_path: str, sample_fps: float = 2.0):
    """Sample frames at sample_fps using ffmpeg fast scaling pipe and compute motion, sharpness, shake, brightness."""
    w, h = 320, 180
    frame_size = w * h
    cmd = [
        "ffmpeg",
        "-hwaccel",
        "auto",
        "-v",
        "quiet",
        "-i",
        video_path,
        "-vf",
        f"fps={sample_fps},scale={w}:{h}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    raw_bytes, _ = proc.communicate()

    n_frames = len(raw_bytes) // frame_size
    records = []
    prev_gray = None
    prev_flow_vec = None

    for idx in range(n_frames):
        buf = raw_bytes[idx * frame_size : (idx + 1) * frame_size]
        gray = np.frombuffer(buf, dtype=np.uint8).reshape((h, w))

        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        brightness = float(np.mean(gray))

        motion = 0.0
        jerk = 0.0
        if prev_gray is not None:
            diff = cv2.absdiff(gray, prev_gray)
            motion = float(np.mean(diff))
            shift, _ = cv2.phaseCorrelate(np.float32(prev_gray), np.float32(gray))
            vec = np.array(shift, dtype=np.float32)
            if prev_flow_vec is not None:
                jerk = float(np.linalg.norm(vec - prev_flow_vec))
            prev_flow_vec = vec

        prev_gray = gray
        t_sec = idx / sample_fps
        records.append(
            {
                "t": round(t_sec, 2),
                "sharpness": round(sharpness, 1),
                "brightness": round(brightness, 1),
                "motion": round(motion, 2),
                "jerk": round(jerk, 2),
            }
        )

    return records


def select_segments_for_clip(
    clip_name: str,
    duration: float,
    vision_records: list,
    speech_segments: list,
) -> list:
    """
    Normal Cut Mode (8~12m target for Day 1):
    - Keep all valid speech segments (with 0.4s lead-in and 0.5s lead-out).
    - Trim first 0.7s and last 0.7s of each clip if there is camera-button shake or no speech.
    - Exclude intervals with extreme camera jerk/blur unless speech is present.
    - For normal clips (<90s), keep good visual scenes and merge nearby intervals (<1.8s gap).
    - For very long static/tabletop clips (e.g. GX011871, 531.5s), keep speech segments + top active/clear highlights (~95-110s total) so it doesn't drag.
    """
    if duration <= 3.0:
        return []

    n_bins = int(np.ceil(duration * 2))  # 0.5s resolution
    keep = np.zeros(n_bins, dtype=bool)
    speech_mask = np.zeros(n_bins, dtype=bool)

    # Mark speech intervals
    for seg in speech_segments:
        s_idx = max(0, int(np.floor((seg["start"] - 0.4) * 2)))
        e_idx = min(n_bins, int(np.ceil((seg["end"] + 0.5) * 2)))
        keep[s_idx:e_idx] = True
        speech_mask[s_idx:e_idx] = True

    # Analyze vision quality per 0.5s bin
    sharp_arr = np.array([r["sharpness"] for r in vision_records]) if vision_records else np.array([100.0])
    jerk_arr = np.array([r["jerk"] for r in vision_records]) if vision_records else np.array([0.0])
    motion_arr = np.array([r["motion"] for r in vision_records]) if vision_records else np.array([5.0])

    min_sharp = max(25.0, float(np.percentile(sharp_arr, 12)))
    max_jerk = max(22.0, float(np.percentile(jerk_arr, 90)))

    is_long_clip = duration > 120.0

    if not is_long_clip:
        # Normal clip: start by keeping [0.6s, duration - 0.6s], then drop bad shake/blur or long dead static runs
        start_bin = 1 if duration > 5.0 else 0
        end_bin = n_bins - 1 if duration > 5.0 else n_bins
        keep[start_bin:end_bin] = True

        for i, r in enumerate(vision_records):
            if i >= n_bins:
                break
            if speech_mask[i]:
                continue
            # Drop severe blur or extreme camera jerk
            if r["sharpness"] < min_sharp or r["jerk"] > max_jerk or r["brightness"] < 18 or r["brightness"] > 248:
                keep[i] = False

        # If clip is medium length (35s ~ 120s), trim monotonous low-motion silent stretches > 6s
        if duration > 35.0:
            silent_run = 0
            for i in range(n_bins):
                if not speech_mask[i]:
                    silent_run += 1
                    # Keep first 5 seconds of a silent visual scene, trim excess dead air
                    if silent_run > 10 and (i < len(vision_records) and vision_records[i]["motion"] < 3.5):
                        keep[i] = False
                else:
                    silent_run = 0
    else:
        # Very long clip (like GX011871 = 531.5s restaurant tabletop):
        # Keep all speech segments, plus select top visual/action moments spaced across the clip
        # up to ~95 seconds total
        # First include opening 8 seconds (1.0s ~ 9.0s) to establish the scene
        keep[2:18] = True
        current_kept_sec = float(np.sum(keep)) * 0.5
        target_long_sec = 95.0
        if current_kept_sec < target_long_sec and len(vision_records) > 0:
            # Score windows of 4 seconds (8 bins) by moderate motion + high sharpness
            window = 8
            candidates = []
            for i in range(4, n_bins - window - 4, window):
                if np.any(keep[i : i + window]):
                    continue
                sub = vision_records[i : i + window]
                if not sub:
                    continue
                m_mean = np.mean([x["motion"] for x in sub])
                s_mean = np.mean([x["sharpness"] for x in sub])
                j_mean = np.mean([x["jerk"] for x in sub])
                if 2.0 <= m_mean <= 18.0 and j_mean < max_jerk:
                    score = s_mean * 0.5 + m_mean * 5.0
                    candidates.append((score, i))
            candidates.sort(reverse=True)
            for score, idx in candidates:
                if float(np.sum(keep)) * 0.5 >= target_long_sec:
                    break
                keep[idx : idx + window] = True

    # Convert boolean mask to [start, end] intervals
    intervals = []
    in_seg = False
    seg_start = 0.0
    for i in range(n_bins):
        if keep[i] and not in_seg:
            in_seg = True
            seg_start = i * 0.5
        elif not keep[i] and in_seg:
            in_seg = False
            intervals.append([round(seg_start, 2), round(i * 0.5, 2)])
    if in_seg:
        intervals.append([round(seg_start, 2), round(min(duration, n_bins * 0.5), 2)])

    # Merge intervals separated by small gaps (< 1.5s)
    merged = []
    for iv in intervals:
        if not merged:
            merged.append(iv)
        else:
            if iv[0] - merged[-1][1] <= 1.5:
                merged[-1][1] = iv[1]
            else:
                merged.append(iv)

    # Filter out tiny intervals (< 2.0s) unless they cover almost the whole clip
    final_intervals = [
        [round(s, 2), round(e, 2)]
        for s, e in merged
        if (e - s) >= 2.0
    ]
    if not final_intervals and duration >= 2.5:
        final_intervals = [[0.5, round(min(duration - 0.5, 8.0), 2)]]

    return final_intervals


def main():
    day1_dir = r"D:\편집할영상\2023_제주도여행\2023-09-20"
    out_dir = r"d:\workspace\autocut\output"
    os.makedirs(out_dir, exist_ok=True)

    # Initialize Whisper model (try CUDA first, fallback to CPU int8)
    print("Loading faster-whisper model (small)...")
    try:
        model = WhisperModel("small", device="cuda", compute_type="float16")
        print("Loaded faster-whisper 'small' on CUDA")
    except Exception as e:
        print("CUDA load failed, using CPU int8:", e)
        model = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=8)

    mp4_files = sorted(glob.glob(os.path.join(day1_dir, "*.MP4")))
    results = []
    total_orig_sec = 0.0
    total_edit_sec = 0.0

    with tempfile.TemporaryDirectory() as tmpdir:
        for idx, fpath in enumerate(mp4_files):
            fname = os.path.basename(fpath)
            # Get duration & creation_time
            cmd = [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_entries",
                "format=duration:format_tags=creation_time",
                fpath,
            ]
            meta = json.loads(subprocess.check_output(cmd).decode("utf-8", "ignore"))
            dur = float(meta["format"]["duration"])
            ct = meta["format"].get("tags", {}).get("creation_time", "")

            # 1. Audio Denoise + STT
            wav_path = os.path.join(tmpdir, f"{fname}.wav")
            extract_denoised_wav(fpath, wav_path)

            speech_list = []
            try:
                segments, _ = model.transcribe(
                    wav_path,
                    language="ko",
                    beam_size=5,
                    vad_filter=True,
                    vad_parameters=dict(min_silence_duration_ms=400, speech_pad_ms=250),
                    condition_on_previous_text=False,
                )
                for s in segments:
                    if not is_hallucination(s.text, s.no_speech_prob, s.avg_logprob, s.compression_ratio):
                        speech_list.append(
                            {
                                "start": round(s.start, 2),
                                "end": round(s.end, 2),
                                "text": s.text.strip(),
                                "no_speech_prob": round(s.no_speech_prob, 3),
                                "avg_logprob": round(s.avg_logprob, 3),
                            }
                        )
            except Exception as e:
                print(f"  STT error on {fname}: {e}")

            # 2. Vision & Motion analysis
            vision_recs = analyze_video_vision(fpath, sample_fps=2.0)

            # 3. Cut interval selection
            kept_intervals = select_segments_for_clip(fname, dur, vision_recs, speech_list)
            kept_dur = sum(e - s for s, e in kept_intervals)

            total_orig_sec += dur
            total_edit_sec += kept_dur

            print(
                f"[{idx+1:02d}/{len(mp4_files)}] {fname} | Orig: {dur:5.1f}s -> Kept: {kept_dur:5.1f}s ({len(kept_intervals)} cuts, {len(speech_list)} speech)"
            )
            for sp in speech_list:
                print(f"    Speech [{sp['start']:5.1f}s~{sp['end']:5.1f}s]: {sp['text']}")

            results.append(
                {
                    "file": fname,
                    "path": fpath,
                    "creation_time": ct,
                    "duration": round(dur, 2),
                    "kept_duration": round(kept_dur, 2),
                    "kept_intervals": kept_intervals,
                    "speech": speech_list,
                }
            )

    summary = {
        "date": "2023-09-20",
        "total_clips": len(mp4_files),
        "total_original_seconds": round(total_orig_sec, 2),
        "total_edited_seconds": round(total_edit_sec, 2),
        "clips": results,
    }
    out_json = os.path.join(out_dir, "day1_analysis.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\nSaved analysis to {out_json}")
    print(f"Total Original: {total_orig_sec/60:.2f} min -> Edited: {total_edit_sec/60:.2f} min")


if __name__ == "__main__":
    main()
