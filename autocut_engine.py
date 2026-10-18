import os
import sys
import glob
import json
import random
import shutil
import tempfile
import subprocess
from datetime import datetime
from collections import defaultdict
from typing import Callable, List, Dict, Tuple, Optional

# Prevent any subprocess (ffmpeg, ffprobe, etc.) from popping up a cmd window on Windows
if os.name == "nt":
    _orig_popen = subprocess.Popen

    class _NoWindowPopen(_orig_popen):
        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoWindowPopen

# Ensure console stdout/stderr never fail on Windows cp949
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Ensure PyTorch CUDA DLLs (e.g., cublas64_12.dll, cudnn) are on PATH before importing ctranslate2/faster_whisper
try:
    import torch
    torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
    if os.path.isdir(torch_lib):
        os.environ["PATH"] = torch_lib + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(torch_lib)
except Exception:
    pass

import wave
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps, ExifTags
from faster_whisper import WhisperModel

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".insv", ".mts", ".m2ts", ".wmv", ".webm"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif", ".jfif", ".tif", ".tiff"}
SIDECAR_EXTS = {".thm", ".lrv", ".srt", ".xmp"}


def scan_media_files_in_folder(folder_path: str) -> Tuple[List[str], List[str]]:
    """
    Scan a date folder (including any nested subfolders such as extracted photo folders)
    and return (sorted_video_paths, sorted_photo_paths).
    """
    video_files: List[str] = []
    photo_files: List[str] = []
    if not os.path.isdir(folder_path):
        return video_files, photo_files

    for root, dirs, files in os.walk(folder_path):
        dirs[:] = sorted(d for d in dirs if not d.startswith("편집결과") and not d.startswith("."))
        for f in sorted(files):
            if f.startswith(".") or f.startswith("~$"):
                continue
            ext = os.path.splitext(f)[1].lower()
            full_p = os.path.join(root, f)
            if ext in VIDEO_EXTS:
                video_files.append(full_p)
            elif ext in PHOTO_EXTS:
                photo_files.append(full_p)
    return video_files, photo_files

# Rich pool of FFmpeg xfade transitions randomly applied between clips
TRANSITION_POOL = [
    "fade",
    "smoothleft",
    "smoothright",
    "smoothup",
    "smoothdown",
    "circleopen",
    "circleclose",
    "wipeleft",
    "wiperight",
    "slideleft",
    "slideright",
    "dissolve",
    "diagtl",
    "diagbr",
    "radial",
]

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
]


def _load_kr_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    font_candidates = (
        [r"C:\Windows\Fonts\malgunbd.ttf", r"C:\Windows\Fonts\malgun.ttf"]
        if bold
        else [r"C:\Windows\Fonts\malgun.ttf", r"C:\Windows\Fonts\malgunbd.ttf"]
    )
    for fp in font_candidates:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
    return ImageFont.load_default()


def get_video_info(filepath: str) -> Tuple[str, float]:
    """Return (YYYY-MM-DD, duration_sec) from ffprobe metadata."""
    date_str = ""
    duration = 0.0
    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_entries",
            "format=duration:format_tags",
            filepath,
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode("utf-8", "ignore")
        data = json.loads(out)
        fmt = data.get("format", {})
        duration = float(fmt.get("duration", 0.0) or 0.0)
        tags = fmt.get("tags", {}) or {}
        ct = (tags.get("com.apple.quicktime.creationdate") or tags.get("creation_time") or "").strip()
        if ct and len(ct) >= 10 and ct[:4].isdigit():
            date_str = ct[:10]
    except Exception:
        pass
    if not date_str:
        date_str = datetime.fromtimestamp(os.path.getmtime(filepath)).strftime("%Y-%m-%d")
    return date_str, duration


def get_video_datetime_iso(filepath: str) -> str:
    """Return 'YYYY-MM-DDTHH:MM:SS' from video metadata (preferring Apple QuickTime local creationdate, then creation_time, then mtime)."""
    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_entries",
            "format_tags",
            filepath,
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode("utf-8", "ignore")
        data = json.loads(out)
        tags = data.get("format", {}).get("tags", {}) or {}
        qt_ct = tags.get("com.apple.quicktime.creationdate", "").strip()
        if qt_ct and len(qt_ct) >= 19 and qt_ct[:4].isdigit():
            return qt_ct[:19].replace(" ", "T")
        ct = tags.get("creation_time", "").strip()
        if ct and len(ct) >= 19 and ct[:4].isdigit():
            return ct[:19].replace(" ", "T")
    except Exception:
        pass
    return datetime.fromtimestamp(os.path.getmtime(filepath)).strftime("%Y-%m-%dT%H:%M:%S")


def get_photo_datetime_iso(filepath: str) -> str:
    """Return 'YYYY-MM-DDTHH:MM:SS' from EXIF DateTimeOriginal (36867) / DateTimeDigitized (36868) / DateTime (306) or file mtime."""
    try:
        with Image.open(filepath) as img:
            exif = img.getexif() or getattr(img, "_getexif", lambda: None)() or {}
            # Check standard EXIF tags 36867 (DateTimeOriginal), 36868 (DateTimeDigitized), 306 (DateTime)
            for tag_id in (36867, 36868, 306):
                val = exif.get(tag_id)
                if isinstance(val, str) and len(val.strip()) >= 19:
                    raw = val.strip()[:19]
                    date_part = raw[:10].replace(":", "-")
                    time_part = raw[11:19]
                    if date_part[:4].isdigit():
                        return f"{date_part}T{time_part}"
            if hasattr(img, "_getexif"):
                ex2 = img._getexif() or {}
                for tag_id in (36867, 36868, 306):
                    val = ex2.get(tag_id)
                    if isinstance(val, str) and len(val.strip()) >= 19:
                        raw = val.strip()[:19]
                        date_part = raw[:10].replace(":", "-")
                        time_part = raw[11:19]
                        if date_part[:4].isdigit():
                            return f"{date_part}T{time_part}"
    except Exception:
        pass
    return datetime.fromtimestamp(os.path.getmtime(filepath)).strftime("%Y-%m-%dT%H:%M:%S")


def get_photo_date(filepath: str) -> str:
    """Return YYYY-MM-DD from EXIF DateTimeOriginal or file mtime."""
    iso_str = get_photo_datetime_iso(filepath)
    return iso_str[:10]


def _fps_to_ffmpeg_expr(out_fps: float = 60.0) -> str:
    if abs(out_fps - 60.0) < 1.5:
        return "60000/1001"
    if abs(out_fps - 30.0) < 1.5:
        return "30000/1001"
    if abs(out_fps - 24.0) < 1.5:
        return "24000/1001"
    return str(max(15, int(round(out_fps))))


def _build_vcodec_args(video_codec: str = "hevc_nvenc", video_bitrate_bps: int = 25000000) -> List[str]:
    codec = (video_codec or "hevc_nvenc").strip().lower()
    if "hevc" in codec or "265" in codec:
        enc = "hevc_nvenc"
        tag_args = ["-tag:v", "hvc1"]
    else:
        enc = "h264_nvenc"
        tag_args = []
    bps = max(2_000_000, int(video_bitrate_bps))
    max_bps = int(bps * 1.25)
    buf_bps = int(bps * 2.0)
    return [
        "-c:v",
        enc,
        *tag_args,
        "-preset",
        "p4",
        "-rc",
        "vbr",
        "-b:v",
        str(bps),
        "-maxrate",
        str(max_bps),
        "-bufsize",
        str(buf_bps),
    ]


# ==============================================================================
# Editing Modes (여행 기본값 / 그냥 대화 / 하이라이트 / 드라이브),
# Intro Styles (5종 + 랜덤), and DJI / D-Log Color Profiles
# ==============================================================================

EDIT_MODE_PRESETS: Dict[str, str] = {
    "여행 (기본값 — 풍경·인물 베스트컷 + 대화 조화)": "travel",
    "그냥 대화 (일상 토크 중심 — 말소리 위주 촘촘하게 연결)": "talk",
    "하이라이트 요약 (베스트 장면 + 핵심 대화 3~5분 압축)": "highlight",
    "드라이브·풍경 감상 (주행·드론 롱테이크 + BGM 중심)": "drive",
}

INTRO_STYLE_PRESETS: Dict[str, str] = {
    "클래식 액자형 (하얀 이중 테두리 + 반투명 중앙 타이틀)": "classic_frame",
    "시네마틱 레터박스 (상하 영화 블랙바 + 골드 라인 타이틀)": "cinematic_bar",
    "매거진 하단 배너 (좌측 하단 에디토리얼 카드 + 골드 포인트)": "magazine_lower",
    "미니멀 글래스 카드 (중앙 프로스트 블러 유리 카드)": "minimal_glass",
    "감성 브이로그 스탬프 (코너 브래킷 + 파스텔 필 타이틀)": "vlog_stamp",
    "랜덤 인트로 (5가지 디자인 중 매번 자동 선택)": "random",
}

COLOR_PROFILE_PRESETS: Dict[str, Dict[str, str]] = {
    "자동 감지 (일반 + DJI D-Log/D-Log M 자동 복원)": {
        "id": "auto",
        "vf": "",
        "desc": "DJI 드론/액션캠 D-Log 영상을 자동 판별하여 Rec.709 색감으로 복원합니다.",
    },
    "DJI D-Log ➔ Rec.709 표준 색감 복원 (Mavic / Air / Mini)": {
        "id": "dji_dlog",
        "vf": (
            "curves=r='0/0 0.18/0.11 0.50/0.52 0.82/0.89 1/1':"
            "g='0/0 0.18/0.11 0.50/0.52 0.82/0.89 1/1':"
            "b='0/0 0.18/0.11 0.50/0.51 0.82/0.88 1/1',"
            "eq=contrast=1.22:saturation=1.38:gamma=0.94"
        ),
        "desc": "물빠진 회색빛 DJI D-Log(10-bit/8-bit) 영상을 선명한 표준 Rec.709 색감으로 복원합니다.",
    },
    "DJI D-Log M ➔ 비비드 여행 색감 복원 (Osmo Pocket 3 / Action 4·5)": {
        "id": "dji_dlog_m",
        "vf": (
            "curves=all='0/0 0.16/0.11 0.50/0.53 0.84/0.90 1/1',"
            "eq=contrast=1.16:saturation=1.30:gamma=0.96"
        ),
        "desc": "오즈모 포켓3·액션4/5의 D-Log M 평면 컬러를 화사한 여행 브이로그 색감으로 복원합니다.",
    },
    "GoPro Flat / Log ➔ 선명한 색감 복원": {
        "id": "gopro_flat",
        "vf": (
            "curves=all='0/0 0.18/0.13 0.50/0.52 0.85/0.90 1/1',"
            "eq=contrast=1.14:saturation=1.24:gamma=0.96"
        ),
        "desc": "고프로 Flat 컬러 프로필 영상의 대비와 채도를 살려줍니다.",
    },
    "보정 안 함 (원본 색감 그대로 유지)": {
        "id": "none",
        "vf": "",
        "desc": "별도의 Log 컬러 복원 없이 카메라 원본 색감을 유지합니다.",
    },
}


def detect_dji_or_dlog_clip(filepath: str) -> Tuple[bool, str]:
    """
    Detect whether a video clip is from a DJI camera (Drone / Osmo Action / Osmo Pocket)
    and whether it uses a flat D-Log / D-Log M color profile.
    Returns (is_dlog, profile_id) where profile_id in ('dji_dlog', 'dji_dlog_m', 'none').
    """
    fname = os.path.basename(filepath).upper()
    is_dji_name = fname.startswith("DJI_") or "DLOG" in fname or "D-LOG" in fname or "OSMO" in fname

    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_entries",
            "format_tags:stream_tags:stream=color_transfer,color_primaries,pix_fmt",
            filepath,
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode("utf-8", "ignore")
        meta = json.loads(out)
        raw_str = json.dumps(meta).lower()
        if "dlog-m" in raw_str or "d-log m" in raw_str or "dlog_m" in raw_str:
            return True, "dji_dlog_m"
        if "dlog" in raw_str or "d-log" in raw_str:
            return True, "dji_dlog"
        if "dji" in raw_str:
            is_dji_name = True
    except Exception:
        pass

    if is_dji_name:
        # Sample one frame from the clip to check if it has a flat D-Log curve (low contrast std & muted saturation)
        try:
            cap = cv2.VideoCapture(filepath)
            total_f = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if total_f > 10:
                cap.set(cv2.CAP_PROP_POS_FRAMES, total_f // 3)
            ok, frame = cap.read()
            cap.release()
            if ok and frame is not None and frame.size > 0:
                small = cv2.resize(frame, (320, 180))
                hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
                luma_std = float(np.std(hsv[:, :, 2]))
                sat_mean = float(np.mean(hsv[:, :, 1]))
                p5 = float(np.percentile(hsv[:, :, 2], 5))
                # Flat Log footage has lifted shadows (p5 > 24), low luma std (< 44), and muted saturation (< 78)
                if luma_std < 44.0 and sat_mean < 82.0 and p5 > 22.0:
                    return True, "dji_dlog_m"
        except Exception:
            pass
        if "DLOG" in fname or "D-LOG" in fname:
            return True, "dji_dlog"

    return False, "none"


def get_color_profile_filter(
    color_profile_key_or_id: str,
    filepath: str = "",
    log_fn: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Return the FFmpeg video filter string (str) for the selected color profile preset.
    Logs a message via log_fn when DJI D-Log / D-Log M is auto-detected.
    """
    cfg = COLOR_PROFILE_PRESETS.get(color_profile_key_or_id)
    if cfg is None:
        for k, v in COLOR_PROFILE_PRESETS.items():
            if v["id"] == color_profile_key_or_id:
                cfg = v
                break
    if cfg is None:
        cfg = next(iter(COLOR_PROFILE_PRESETS.values()))

    cid = cfg["id"]
    if cid == "auto":
        if filepath and os.path.exists(filepath):
            is_dlog, det_id = detect_dji_or_dlog_clip(filepath)
            if is_dlog and det_id in ("dji_dlog", "dji_dlog_m"):
                for k, v in COLOR_PROFILE_PRESETS.items():
                    if v["id"] == det_id:
                        if log_fn:
                            log_fn(
                                f"  🚁 [{os.path.basename(filepath)}] DJI {det_id.upper()} 평면 프로파일 감지 -> Rec.709 색감 자동 복원 적용"
                            )
                        return str(v.get("vf", "")).strip()
        return ""
    return str(cfg.get("vf", "")).strip()



def classify_media_by_date(
    target_dir: str,
    move_files: bool = True,
    log_fn: Callable[[str], None] = print,
) -> Dict[str, List[str]]:
    """Classify videos, photos, and sidecar files in target_dir into YYYY-MM-DD subfolders."""
    entries = [
        f for f in os.listdir(target_dir)
        if os.path.isfile(os.path.join(target_dir, f))
    ]
    if not entries:
        return {}

    stem_to_date = {}
    for fname in sorted(entries):
        stem, ext = os.path.splitext(fname)
        ext_l = ext.lower()
        full_path = os.path.join(target_dir, fname)
        if ext_l in VIDEO_EXTS:
            d_str, _ = get_video_info(full_path)
            stem_to_date[stem] = d_str
        elif ext_l in PHOTO_EXTS:
            stem_to_date[stem] = get_photo_date(full_path)

    grouped = defaultdict(list)
    for fname in sorted(entries):
        stem, ext = os.path.splitext(fname)
        ext_l = ext.lower()
        if ext_l not in VIDEO_EXTS and ext_l not in PHOTO_EXTS and ext_l not in SIDECAR_EXTS:
            continue
        full_path = os.path.join(target_dir, fname)
        date_str = stem_to_date.get(stem)
        if not date_str:
            date_str, _ = get_video_info(full_path)
        grouped[date_str].append(fname)

        if move_files:
            date_dir = os.path.join(target_dir, date_str)
            os.makedirs(date_dir, exist_ok=True)
            shutil.move(full_path, os.path.join(date_dir, fname))

    for d_str, flist in sorted(grouped.items()):
        v_cnt = sum(1 for f in flist if os.path.splitext(f)[1].lower() in VIDEO_EXTS)
        p_cnt = sum(1 for f in flist if os.path.splitext(f)[1].lower() in PHOTO_EXTS)
        log_fn(f"📅 [{d_str}] 분류 완료: 영상 {v_cnt}개, 사진 {p_cnt}개 (총 파일 {len(flist)}개)")

    return dict(grouped)


def is_hallucination(text: str, no_speech_prob: float, avg_logprob: float, compression_ratio: float) -> bool:
    t = text.strip()
    if not t or len(t) <= 1:
        return True
    if no_speech_prob > 0.65 or avg_logprob < -1.15 or compression_ratio > 2.2:
        return True
    for phrase in HALLUCINATION_PHRASES:
        if phrase in t and (len(t) <= len(phrase) + 4 or no_speech_prob > 0.35):
            return True
    if not any(ch.isalnum() for ch in t):
        return True
    return False


def load_whisper_model(model_size: str = "small", log_fn: Callable[[str], None] = print) -> WhisperModel:
    """Load faster-whisper model on CUDA if cublas works, otherwise fast CPU int8."""
    try:
        model = WhisperModel(model_size, device="cuda", compute_type="float16")
        dummy = np.zeros(16000, dtype=np.float32)
        list(model.transcribe(dummy, language="ko"))
        log_fn(f"✅ AI 음성인식 모델('{model_size}') GPU(CUDA) 가속 로드 완료")
        return model
    except Exception as e:
        log_fn(f"ℹ️ GPU 라이브러리 호환 전환 -> 고속 멀티코어 CPU(int8) 모드로 '{model_size}' 모델 로드 ({e})")
        cpu_threads = max(4, (os.cpu_count() or 8) - 2)
        return WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=cpu_threads)


def extract_denoised_wav(video_path: str, wav_path: str, denoise: bool = True):
    """Extract 16kHz mono audio with wind/driving noise filtering."""
    af_filter = (
        "highpass=f=120,lowpass=f=3800,afftdn=nf=-25:tn=1"
        if denoise
        else "highpass=f=80,lowpass=f=4000"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        video_path,
        "-vn",
        "-af",
        af_filter,
        "-ac",
        "1",
        "-ar",
        "16000",
        wav_path,
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def analyze_video_keyframes(video_path: str, duration: float) -> List[Dict]:
    """
    Ultra-fast vision/motion analysis using hardware keyframe decoding (-discard nokey -fps_mode vfr).
    Decodes a 4GB 9-minute 4K 60fps HEVC video in ~3 seconds (1 frame per second).
    """
    w, h = 160, 90
    frame_size = w * h
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-hwaccel",
        "auto",
        "-discard",
        "nokey",
        "-i",
        video_path,
        "-vf",
        f"scale={w}:{h}",
        "-fps_mode",
        "vfr",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    raw_bytes, _ = proc.communicate()

    n_frames = len(raw_bytes) // frame_size
    if n_frames == 0:
        return []

    dt = duration / max(1, n_frames)
    records = []
    prev_gray = None
    prev_vec = None

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
            if prev_vec is not None:
                jerk = float(np.linalg.norm(vec - prev_vec))
            prev_vec = vec

        prev_gray = gray
        records.append(
            {
                "t": round(idx * dt, 2),
                "dt": round(dt, 3),
                "sharpness": round(sharpness, 1),
                "brightness": round(brightness, 1),
                "motion": round(motion, 2),
                "jerk": round(jerk, 2),
            }
        )
    return records


def select_clip_cuts(
    duration: float,
    vision_records: List[Dict],
    speech_segments: List[Dict],
    edit_mode: str = "travel",
) -> List[List[float]]:
    """
    Compute [start, end] cut intervals for a clip based on vision/motion + speech.
    Supported edit_mode values:
      - 'travel' (or 'normal'): Balanced travel vlog (speech + scenic/face highlights, 8~12m)
      - 'talk': Dialogue-centric ('그냥 대화' — keeps all conversations with generous padding, trims non-speaking dead air)
      - 'highlight': Fast summary reel (3~5m)
      - 'drive': Scenic / driving / DJI drone long-take mode
    """
    mode = (EDIT_MODE_PRESETS.get(edit_mode) or edit_mode or "travel").strip().lower()
    if mode == "normal":
        mode = "travel"

    if duration <= 2.5:
        return [[0.0, round(duration, 2)]] if duration >= 1.5 else []

    n_bins = int(np.ceil(duration))
    keep = np.zeros(n_bins, dtype=bool)
    speech_mask = np.zeros(n_bins, dtype=bool)

    pad_pre = 0.75 if mode == "talk" else 0.5
    pad_post = 0.90 if mode == "talk" else 0.6

    for seg in speech_segments:
        s_idx = max(0, int(np.floor(seg["start"] - pad_pre)))
        e_idx = min(n_bins, int(np.ceil(seg["end"] + pad_post)))
        keep[s_idx:e_idx] = True
        speech_mask[s_idx:e_idx] = True

    sharp_arr = np.array([r["sharpness"] for r in vision_records]) if vision_records else np.array([100.0])
    jerk_arr = np.array([r["jerk"] for r in vision_records]) if vision_records else np.array([0.0])

    min_sharp = max(20.0, float(np.percentile(sharp_arr, 12)))
    max_jerk = max(18.0, float(np.percentile(jerk_arr, 88)))

    if mode == "talk":
        # Dialogue-focused mode ('그냥 대화'): keep all spoken parts, and if a clip has zero speech, keep only a brief 3s establishing shot
        if not speech_segments and vision_records:
            best_i = max(range(len(vision_records)), key=lambda idx: vision_records[idx]["sharpness"] - vision_records[idx]["jerk"] * 2.0)
            s_i = max(0, best_i - 1)
            e_i = min(n_bins, s_i + 3)
            keep[s_i:e_i] = True
    elif mode == "highlight":
        max_clip_target = 40.0 if duration > 120.0 else min(duration * 0.45, 18.0)
        if float(np.sum(keep)) < max_clip_target and vision_records:
            window = 4
            candidates = []
            for i in range(1, max(2, len(vision_records) - window - 1), window):
                if i < n_bins and np.any(keep[i : min(n_bins, i + window)]):
                    continue
                sub = vision_records[i : i + window]
                s_mean = np.mean([x["sharpness"] for x in sub])
                m_mean = np.mean([x["motion"] for x in sub])
                j_mean = np.mean([x["jerk"] for x in sub])
                if j_mean <= max_jerk and s_mean >= min_sharp:
                    score = s_mean * 0.6 + min(m_mean, 20.0) * 4.0
                    candidates.append((score, i))
            candidates.sort(reverse=True)
            for _, idx in candidates:
                if float(np.sum(keep)) >= max_clip_target:
                    break
                keep[idx : min(n_bins, idx + window)] = True
    elif mode == "drive":
        # Scenic / Drone / Driving long-take mode: favor smooth, steady 8s windows
        window = 8
        target_sec = min(duration * 0.65, 110.0)
        if float(np.sum(keep)) < target_sec and vision_records:
            candidates = []
            for i in range(1, max(2, len(vision_records) - window - 1), window):
                sub = vision_records[i : i + window]
                s_mean = np.mean([x["sharpness"] for x in sub])
                m_mean = np.mean([x["motion"] for x in sub])
                j_mean = np.mean([x["jerk"] for x in sub])
                if j_mean <= max_jerk * 0.85 and s_mean >= min_sharp:
                    score = s_mean * 0.7 + min(m_mean, 18.0) * 3.5 - j_mean * 3.0
                    candidates.append((score, i))
            candidates.sort(reverse=True)
            for _, idx in candidates:
                if float(np.sum(keep)) >= target_sec:
                    break
                keep[idx : min(n_bins, idx + window)] = True
    else:
        # Default 'travel' mode: balanced travel vlog
        is_long_clip = duration > 120.0
        if not is_long_clip:
            start_b = 1 if duration > 5.0 else 0
            end_b = n_bins - 1 if duration > 5.0 else n_bins
            keep[start_b:end_b] = True

            for i, r in enumerate(vision_records):
                if i >= n_bins or speech_mask[i]:
                    continue
                if r["sharpness"] < min_sharp or r["jerk"] > max_jerk or r["brightness"] < 18 or r["brightness"] > 248:
                    keep[i] = False

            if duration > 30.0:
                silent_run = 0
                for i in range(n_bins):
                    if not speech_mask[i]:
                        silent_run += 1
                        mot = vision_records[i]["motion"] if i < len(vision_records) else 5.0
                        if silent_run > 6 and mot < 4.5:
                            keep[i] = False
                    else:
                        silent_run = 0
        else:
            keep[1 : min(n_bins, 8)] = True
            target_long_sec = 90.0
            if float(np.sum(keep)) < target_long_sec and vision_records:
                window = 5
                candidates = []
                for i in range(8, max(9, len(vision_records) - window - 2), window):
                    if i < n_bins and np.any(keep[i : min(n_bins, i + window)]):
                        continue
                    sub = vision_records[i : i + window]
                    m_mean = np.mean([x["motion"] for x in sub])
                    s_mean = np.mean([x["sharpness"] for x in sub])
                    j_mean = np.mean([x["jerk"] for x in sub])
                    if 2.0 <= m_mean <= 22.0 and j_mean <= max_jerk:
                        score = s_mean * 0.5 + m_mean * 5.0
                        candidates.append((score, i))
                candidates.sort(reverse=True)
                for _, idx in candidates:
                    if float(np.sum(keep)) >= target_long_sec:
                        break
                    keep[idx : min(n_bins, idx + window)] = True

    intervals = []
    in_seg = False
    seg_start = 0.0
    for i in range(n_bins):
        if keep[i] and not in_seg:
            in_seg = True
            seg_start = float(i)
        elif not keep[i] and in_seg:
            in_seg = False
            intervals.append([round(seg_start, 2), round(float(i), 2)])
    if in_seg:
        intervals.append([round(seg_start, 2), round(min(duration, float(n_bins)), 2)])

    merge_gap = 2.6 if mode == "talk" else 1.5
    merged = []
    for iv in intervals:
        if not merged:
            merged.append(iv)
        elif iv[0] - merged[-1][1] <= merge_gap:
            merged[-1][1] = iv[1]
        else:
            merged.append(iv)

    min_len = 1.6 if mode == "talk" else 2.0
    final_ivs = [[s, e] for s, e in merged if (e - s) >= min_len]
    if not final_ivs and duration >= 2.5:
        final_ivs = [[0.5, round(min(duration - 0.5, 6.0), 2)]]
    return final_ivs

KNOWN_SCENE_CAPTIONS = {
    "GX011864.MP4": ("10:40", "집에서 제주도 여행 출발 준비!"),
    "GX011865.MP4": ("12:04", "김포공항 주차장에서 공항으로 이동"),
    "GX011867.MP4": ("12:07", "공항 도착! 설레는 발걸음"),
    "GX011869.MP4": ("12:12", "셀프 체크인 & 탑승 수속"),
    "GX011871.MP4": ("12:37", "금강산도 식후경! 공항에서 점심 식사"),
    "GX011872.MP4": ("13:18", "탑승구 앞에서 간식 먹으며 대기 중"),
    "GX011875.MP4": ("14:16", "드디어 제주행 비행기 탑승!"),
    "GX011876.MP4": ("14:23", "비행기 안, 제주도로 출발~"),
    "GX011878.MP4": ("15:31", "비행기 안에서 새근새근 낮잠 타임"),
    "GX011881.MP4": ("15:48", "제주국제공항 도착! 수하물 찾기"),
    "GX011883.MP4": ("15:58", "제주공항 밖으로 이동"),
    "GX011884.MP4": ("16:03", "렌터카 셔틀버스 탑승"),
    "GX011886.MP4": ("16:20", "렌터카 픽업 완료! 본격 제주 여행 시작"),
    "GX011887.MP4": ("17:05", "제주 베이커리 & 매장 나들이"),
    "GX011888.MP4": ("17:31", "차 타고 다음 코스로 드라이브"),
    "GX011889.MP4": ("17:53", "제주 감성 건축물 & 수변 산책"),
}


DEFAULT_BGM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "calm_youtube_free_bgm.wav")


def ensure_calm_bgm_file(bgm_path: str = DEFAULT_BGM_PATH) -> str:
    """
    Generate (if not already present) a 150-second stereo 48kHz 16-bit WAV file of calm, warm,
    royalty-free / YouTube-safe acoustic piano & ambient guitar/pad background music.
    Progression: Cmaj9 - G/B - Am7 - Em7 - Fmaj7 - C/E - Dm9 - G13sus4 at 68 BPM.
    """
    if os.path.isfile(bgm_path) and os.path.getsize(bgm_path) > 100000:
        return bgm_path

    os.makedirs(os.path.dirname(bgm_path), exist_ok=True)
    sr = 48000
    bpm = 68.0
    beat_sec = 60.0 / bpm
    bar_sec = beat_sec * 4.0

    # 8-bar soothing family/travel acoustic progression (frequencies in Hz)
    # Each bar: (pad_chord_freqs, arpeggio_melody_freqs_8_eighths)
    chords = [
        # 1. Cmaj9 (C3, G3, B3, D4, E4)
        ([130.81, 196.00, 246.94, 293.66], [130.81, 196.00, 261.63, 329.63, 392.00, 329.63, 293.66, 246.94]),
        # 2. G6/B (B2, D3, G3, B3, D4)
        ([123.47, 196.00, 246.94, 293.66], [123.47, 196.00, 246.94, 293.66, 392.00, 329.63, 293.66, 196.00]),
        # 3. Am9 (A2, E3, G3, B3, C4)
        ([110.00, 164.81, 196.00, 261.63], [110.00, 164.81, 220.00, 261.63, 329.63, 293.66, 261.63, 196.00]),
        # 4. Em7 (E2, B2, G3, B3, D4)
        ([82.41, 164.81, 196.00, 246.94], [82.41, 123.47, 196.00, 246.94, 293.66, 246.94, 196.00, 164.81]),
        # 5. Fmaj9 (F2, C3, E3, G3, A3)
        ([87.31, 174.61, 220.00, 261.63], [87.31, 130.81, 174.61, 220.00, 261.63, 329.63, 261.63, 220.00]),
        # 6. C/E (E2, C3, G3, C4, E4)
        ([82.41, 130.81, 196.00, 261.63], [82.41, 130.81, 196.00, 261.63, 329.63, 392.00, 329.63, 261.63]),
        # 7. Dm9 (D2, A2, F3, A3, C4)
        ([73.42, 146.83, 220.00, 261.63], [73.42, 110.00, 146.83, 220.00, 261.63, 329.63, 293.66, 220.00]),
        # 8. G13sus4 -> G7 (G2, D3, F3, A3, B3)
        ([98.00, 146.83, 174.61, 246.94], [98.00, 146.83, 196.00, 261.63, 293.66, 246.94, 196.00, 146.83]),
    ]

    cycle_bars = len(chords)
    cycle_sec = cycle_bars * bar_sec
    n_cycle = int(round(cycle_sec * sr))
    left_cycle = np.zeros(n_cycle, dtype=np.float32)
    right_cycle = np.zeros(n_cycle, dtype=np.float32)

    # Synthesize one full 8-bar cycle (~28.23s) with overlapping piano sustain and stereo warmth
    pad_dur = bar_sec + 0.45
    t_pad = np.arange(int(pad_dur * sr), dtype=np.float32) / sr
    pad_env = np.minimum(1.0, t_pad / 0.45) * np.exp(-t_pad / (bar_sec * 0.85))

    eighth_sec = beat_sec * 0.5
    note_dur = eighth_sec * 3.2
    t_note = np.arange(int(note_dur * sr), dtype=np.float32) / sr
    # Felt piano envelope: soft 12ms hammer attack + natural exponential decay
    att = 1.0 - np.exp(-t_note / 0.012)
    dec1 = np.exp(-t_note / 1.15)
    dec2 = np.exp(-t_note / 0.55)
    dec3 = np.exp(-t_note / 0.25)

    for b_idx, (pad_freqs, arp_freqs) in enumerate(chords):
        bar_start = int(round(b_idx * bar_sec * sr))

        # 1. Warm Stereo Pad Layer
        pad_l = np.zeros_like(t_pad)
        pad_r = np.zeros_like(t_pad)
        for p_i, pf in enumerate(pad_freqs):
            detune = 0.25 if (p_i % 2 == 0) else -0.25
            pad_l += 0.045 * np.sin(2.0 * np.pi * (pf + detune) * t_pad)
            pad_r += 0.045 * np.sin(2.0 * np.pi * (pf - detune) * t_pad + 0.3)
        pad_l *= pad_env
        pad_r *= pad_env

        for k in range(len(t_pad)):
            pos = (bar_start + k) % n_cycle
            left_cycle[pos] += pad_l[k]
            right_cycle[pos] += pad_r[k]

        # 2. Expressive Felt Piano / Acoustic Arpeggio Layer
        for n_i, nf in enumerate(arp_freqs):
            note_start = bar_start + int(round(n_i * eighth_sec * sr))
            vel = 0.14 if (n_i in (0, 4)) else (0.115 if (n_i % 2 == 0) else 0.095)
            tone = vel * att * (
                1.00 * np.sin(2.0 * np.pi * nf * t_note) * dec1
                + 0.32 * np.sin(2.0 * np.pi * (nf * 2.0) * t_note) * dec2
                + 0.09 * np.sin(2.0 * np.pi * (nf * 3.0) * t_note) * dec3
            )
            # Subtle stereo pan across arpeggio notes
            pan = 0.35 + 0.30 * ((n_i % 4) / 3.0)
            gain_l = float(np.cos(pan * np.pi * 0.5))
            gain_r = float(np.sin(pan * np.pi * 0.5))
            idx_arr = (note_start + np.arange(len(t_note))) % n_cycle
            left_cycle[idx_arr] += tone * gain_l
            right_cycle[idx_arr] += tone * gain_r

    # 3. Warm Acoustic Room Reverb (multi-tap stereo cross reflection)
    reverb_taps = [(0.043, 0.26), (0.079, 0.20), (0.127, 0.15), (0.193, 0.11), (0.271, 0.07)]
    rev_l = left_cycle.copy()
    rev_r = right_cycle.copy()
    for delay_s, gain in reverb_taps:
        shift = int(round(delay_s * sr))
        rev_l += gain * np.roll(right_cycle, shift)
        rev_r += gain * np.roll(left_cycle, shift + 19)

    # Normalize softly to -3.5 dBFS peak so it has rich dynamic range without clipping
    peak = max(1e-4, float(max(np.max(np.abs(rev_l)), np.max(np.abs(rev_r)))))
    scale = 0.66 / peak
    rev_l = np.clip(rev_l * scale, -0.98, 0.98)
    rev_r = np.clip(rev_r * scale, -0.98, 0.98)

    # Repeat 6 cycles (~170 seconds) for continuous background playback
    repeats = 6
    full_l = np.tile(rev_l, repeats)
    full_r = np.tile(rev_r, repeats)

    stereo = np.empty((full_l.size * 2,), dtype=np.int16)
    stereo[0::2] = (full_l * 32767.0).astype(np.int16)
    stereo[1::2] = (full_r * 32767.0).astype(np.int16)

    with wave.open(bgm_path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(stereo.tobytes())

    return bgm_path


def _ensure_upright_bgr(img_bgr: np.ndarray, face_cascade=None) -> Tuple[np.ndarray, bool]:
    """
    Check if a BGR image is upside down (180 degrees inverted) even after EXIF transpose.
    Uses upright vs 180-rotated frontal face detection + sky/ground brightness & blue-channel gradient.
    Returns (upright_bgr, was_flipped_180).
    """
    if img_bgr is None or img_bgr.size == 0:
        return img_bgr, False

    small = cv2.resize(img_bgr, (480, 270))
    gray0 = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray180 = cv2.rotate(gray0, cv2.ROTATE_180)

    faces0_score = 0.0
    faces180_score = 0.0
    if face_cascade is not None:
        f0 = face_cascade.detectMultiScale(gray0, scaleFactor=1.12, minNeighbors=6, minSize=(32, 32))
        for (_, fy, fw, fh) in f0:
            if 10 <= fy <= 185:
                faces0_score += float(fw * fh)

        f180 = face_cascade.detectMultiScale(gray180, scaleFactor=1.12, minNeighbors=6, minSize=(32, 32))
        for (_, fy, fw, fh) in f180:
            if 10 <= fy <= 185:
                faces180_score += float(fw * fh)

    # Top third vs bottom third luminance and blue-sky Dominance check
    top_third = small[:85, :, :]
    bot_third = small[185:, :, :]
    top_lum = float(np.mean(gray0[:85, :]))
    bot_lum = float(np.mean(gray0[185:, :]))
    top_blue_excess = float(np.mean(top_third[:, :, 0].astype(np.float32) - top_third[:, :, 2].astype(np.float32)))
    bot_blue_excess = float(np.mean(bot_third[:, :, 0].astype(np.float32) - bot_third[:, :, 2].astype(np.float32)))

    # Flip 180 if:
    # Case 1: Strong face detected at 180° while no face at 0°, AND bottom is brighter/bluer than top
    if faces180_score > 1600.0 and faces0_score == 0.0 and (bot_lum > top_lum + 8.0 or bot_blue_excess > top_blue_excess + 6.0):
        return cv2.rotate(img_bgr, cv2.ROTATE_180), True

    # Case 2: Extreme outdoor sky inversion (bottom third has bright blue sky >185 luminance & strong blue excess while top third is dark ground)
    if faces0_score == 0.0 and bot_lum > 175.0 and top_lum < 120.0 and (bot_blue_excess - top_blue_excess) > 22.0:
        return cv2.rotate(img_bgr, cv2.ROTATE_180), True

    return img_bgr, False


def prepare_upright_photo_1080p(
    photo_path: str,
    out_jpg_path: str,
    cover_mode: bool = False,
    face_cascade=None,
    out_width: int = 1920,
    out_height: int = 1080,
) -> Tuple[bool, bool]:
    """
    Load a photo, apply EXIF orientation transpose (fixing iOS/Android Orientation=3/6/8 upside-down/rotated photos),
    verify visual upright orientation, and render a clean WxH RGB JPEG.
    - If cover_mode=True and landscape (w >= h): fills WxH cleanly via center-crop LANCZOS.
    - Otherwise: places aspect-ratio-preserved photo over a softly blurred WxH background of itself.
    Returns (success, is_landscape).
    """
    try:
        with Image.open(photo_path) as raw_im:
            # 1. CRITICAL: Apply EXIF Orientation (1..8, e.g., 3=180°, 6=90°CW, 8=270°CW)
            im = ImageOps.exif_transpose(raw_im).convert("RGB")

        w, h = im.size
        is_landscape = w >= h

        # 2. Also run visual upside-down check in case camera recorded upside down with Orientation=1
        bgr = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
        bgr, flipped = _ensure_upright_bgr(bgr, face_cascade=face_cascade)
        if flipped:
            im = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))

        W, H = int(out_width), int(out_height)
        if cover_mode and is_landscape:
            canvas = ImageOps.fit(im, (W, H), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        else:
            bg = ImageOps.fit(im, (W, H), method=Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(radius=28))
            dim = Image.new("RGB", (W, H), (12, 15, 24))
            bg = Image.blend(bg, dim, alpha=0.35)

            scale = min(W / max(1, w), H / max(1, h))
            nw = max(1, int(round(w * scale)))
            nh = max(1, int(round(h * scale)))
            fg = im.resize((nw, nh), Image.Resampling.LANCZOS)
            bg.paste(fg, ((W - nw) // 2, (H - nh) // 2))
            canvas = bg

        canvas.save(out_jpg_path, "JPEG", quality=95)
        return True, is_landscape
    except Exception:
        return False, False


def find_best_shot_frame(
    clips_meta: List[Dict],
    tmpdir: str,
    log_fn: Callable[[str], None] = print,
    out_width: int = 1920,
    out_height: int = 1080,
) -> str:
    """
    Find the best visual frame across all videos and photos in the date folder.
    - Guarantees upright orientation via EXIF transpose + AI visual orientation check (never upside down).
    - Scores frames by upright face presence, sharpness, color vibrancy, landscape full-frame fit, and balanced exposure.
    Returns path to a WxH JPEG image.
    """
    best_img_path = os.path.join(tmpdir, "best_shot_1080p.jpg")
    face_cascade = None
    try:
        cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        if os.path.exists(cascade_path):
            face_cascade = cv2.CascadeClassifier(cascade_path)
    except Exception:
        pass

    candidates = []
    step = max(1, len(clips_meta) // 14)
    sampled_clips = clips_meta[::step][:14]
    W, H = int(out_width), int(out_height)

    cand_counter = 0
    for clip in sampled_clips:
        fpath = clip["path"]
        fname = clip["file"]
        ctype = clip.get("type", "video")
        ivs = clip.get("kept_intervals", [])
        if not ivs:
            continue

        if ctype == "video":
            iv0_s, iv0_e = ivs[0][0], ivs[0][1]
            sample_times = [round(min(iv0_e - 0.2, iv0_s + 1.2), 2)]
            mid_t = round((iv0_s + iv0_e) * 0.5, 2)
            if abs(mid_t - sample_times[0]) >= 1.5:
                sample_times.append(mid_t)
        else:
            sample_times = [0.0]

        for t_val in sample_times:
            cand_counter += 1
            cand_jpg = os.path.join(tmpdir, f"cand_{cand_counter:02d}.jpg")
            is_landscape = True
            try:
                if ctype == "video":
                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-v",
                        "error",
                        "-hwaccel",
                        "auto",
                        "-ss",
                        f"{t_val:.2f}",
                        "-i",
                        fpath,
                        "-frames:v",
                        "1",
                        "-vf",
                        f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2",
                        "-q:v",
                        "2",
                        cand_jpg,
                    ]
                    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if not os.path.exists(cand_jpg):
                        continue
                    img_bgr = cv2.imread(cand_jpg)
                    if img_bgr is None:
                        continue
                    img_bgr, was_flipped = _ensure_upright_bgr(img_bgr, face_cascade=face_cascade)
                    if was_flipped:
                        cv2.imwrite(cand_jpg, img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
                else:
                    ok, is_landscape = prepare_upright_photo_1080p(
                        fpath,
                        cand_jpg,
                        cover_mode=True,
                        face_cascade=face_cascade,
                        out_width=W,
                        out_height=H,
                    )
                    if not ok or not os.path.exists(cand_jpg):
                        continue
                    img_bgr = cv2.imread(cand_jpg)
                    if img_bgr is None:
                        continue

                small = cv2.resize(img_bgr, (480, 270))
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)

                sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
                brightness = float(np.mean(gray))
                saturation = float(np.mean(hsv[:, :, 1]))

                exp_penalty = 1.0 if (65.0 <= brightness <= 205.0) else 0.4
                aspect_mult = 1.0 if is_landscape else 0.55

                face_bonus = 0.0
                if face_cascade is not None:
                    faces = face_cascade.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=5, minSize=(28, 28))
                    for (fx, fy, fw, fh) in faces:
                        if 15 <= fy <= 175 and 32 <= fw <= 160:
                            face_bonus += 600.0
                        else:
                            face_bonus += 200.0

                total_score = (min(sharpness, 1200.0) * 0.45 + saturation * 1.5 + face_bonus) * exp_penalty * aspect_mult
                candidates.append((total_score, cand_jpg, fname, t_val))
            except Exception:
                continue

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        best_score, best_cand, best_fname, best_t = candidates[0]
        shutil.copyfile(best_cand, best_img_path)
        log_fn(f"🌟 베스트 샷 인트로 선정 (정방향 자동 보정 완료): {best_fname} (점수 {best_score:.0f})")
    else:
        im = Image.new("RGB", (W, H), (24, 28, 44))
        im.save(best_img_path, quality=95)

    return best_img_path


def _format_date_korean_dot(date_name: str) -> str:
    """Convert '2023-09-20' -> '2023. 09. 20'."""
    if len(date_name) == 10 and date_name[4] == "-" and date_name[7] == "-":
        return f"{date_name[:4]}. {date_name[5:7]}. {date_name[8:10]}"
    return date_name


def create_hud_overlay_png(
    hud_png_path: str,
    date_name: str,
    logo_text: str,
    out_width: int = 1920,
    out_height: int = 1080,
):
    """
    Create a WxH transparent RGBA PNG with:
    - Top-Left: Video Date (좌측 상단 영상 날짜)
    - Top-Right: Family/Channel Logo (우측 상단 로고: 윰탱해윤해진이네)
    """
    W, H = int(out_width), int(out_height)
    s_fac = H / 1080.0
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")

    font_hud = _load_kr_font(max(16, int(round(28 * s_fac))), bold=True)
    date_disp = f"DATE  |  {_format_date_korean_dot(date_name)}"
    logo_disp = f"★  {logo_text.strip()}" if logo_text.strip() else ""

    pad_x = int(round(20 * s_fac))
    pad_y = int(round(10 * s_fac))
    x_left = int(round(42 * s_fac))
    y_top = int(round(34 * s_fac))
    rad = int(round(14 * s_fac))
    b_w = max(1, int(round(2 * s_fac)))

    bbox_d = draw.textbbox((0, 0), date_disp, font=font_hud)
    dw, dh = bbox_d[2] - bbox_d[0], bbox_d[3] - bbox_d[1]
    rect_d = [x_left, y_top, x_left + dw + pad_x * 2, y_top + dh + pad_y * 2 + int(round(4 * s_fac))]
    draw.rounded_rectangle(rect_d, radius=rad, fill=(16, 20, 34, 165), outline=(255, 255, 255, 235), width=b_w)
    draw.text(
        (x_left + pad_x, y_top + pad_y - int(round(2 * s_fac))),
        date_disp,
        font=font_hud,
        fill=(255, 255, 255, 255),
        stroke_width=b_w,
        stroke_fill=(12, 16, 28, 240),
    )

    if logo_disp:
        bbox_l = draw.textbbox((0, 0), logo_disp, font=font_hud)
        lw, lh = bbox_l[2] - bbox_l[0], bbox_l[3] - bbox_l[1]
        bw = lw + pad_x * 2
        bh = lh + pad_y * 2 + int(round(4 * s_fac))
        x_right = W - x_left - bw
        rect_l = [x_right, y_top, W - x_left, y_top + bh]
        draw.rounded_rectangle(rect_l, radius=rad, fill=(16, 20, 34, 175), outline=(255, 255, 255, 240), width=b_w)
        draw.text(
            (x_right + pad_x, y_top + pad_y - int(round(2 * s_fac))),
            logo_disp,
            font=font_hud,
            fill=(255, 242, 195, 255),
            stroke_width=b_w,
            stroke_fill=(12, 16, 28, 240),
        )

    overlay.save(hud_png_path, "PNG")


def render_intro_frame_by_style(
    base_rgba: Image.Image,
    title_text: str,
    date_name: str,
    logo_text: str,
    intro_style: str = "classic_frame",
) -> Tuple[Image.Image, str]:
    """
    Render one of 5 distinct intro designs onto base_rgba (W x H RGBA image):
    - classic_frame: 클래식 화이트 프레임 (반투명 중앙 박스 + 이중 테두리)
    - cinematic_bar: 시네마틱 레터박스 (상하 영화관 블랙바 + 와이드 타이틀)
    - magazine_lower: 매거진 에디토리얼 (좌하단 감성 배너 + 세로 포인트 라인)
    - minimal_glass: 미니멀 글래스 카드 (라운드 반투명 글래스 + 뱃지 태그)
    - vlog_stamp: 폴라로이드·다이어리 스탬프 (화이트 프레임 + 여행 스탬프 감성)
    Returns (composed_rgb_image, resolved_style_id).
    """
    valid_styles = ["classic_frame", "cinematic_bar", "magazine_lower", "minimal_glass", "vlog_stamp"]
    style_id = intro_style
    if style_id in INTRO_STYLE_PRESETS:
        style_id = INTRO_STYLE_PRESETS[style_id]
    if style_id == "random" or style_id not in valid_styles:
        style_id = random.choice(valid_styles)

    W, H = base_rgba.size
    s_fac = H / 1080.0
    base = base_rgba.copy().convert("RGBA")
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")

    main_title = title_text.strip() or "우리 가족 여행 기록"
    sub_title = _format_date_korean_dot(date_name)
    date_disp = f"DATE  |  {sub_title}"
    logo_disp = f"★  {logo_text.strip()}" if logo_text.strip() else ""

    if style_id == "classic_frame":
        margin_outer = int(round(36 * s_fac))
        margin_inner = int(round(48 * s_fac))
        draw.rounded_rectangle(
            [margin_outer, margin_outer, W - margin_outer, H - margin_outer],
            radius=int(round(18 * s_fac)),
            outline=(255, 255, 255, 250),
            width=max(2, int(round(6 * s_fac))),
        )
        draw.rounded_rectangle(
            [margin_inner, margin_inner, W - margin_inner, H - margin_inner],
            radius=int(round(12 * s_fac)),
            outline=(255, 255, 255, 160),
            width=max(1, int(round(2 * s_fac))),
        )

        font_top = _load_kr_font(max(16, int(round(30 * s_fac))), bold=True)
        draw.text(
            (margin_inner + int(round(28 * s_fac)), margin_inner + int(round(22 * s_fac))),
            date_disp,
            font=font_top,
            fill=(255, 255, 255, 255),
            stroke_width=max(1, int(round(3 * s_fac))),
            stroke_fill=(15, 18, 30, 235),
        )
        if logo_disp:
            bbox_logo = draw.textbbox((0, 0), logo_disp, font=font_top)
            lw = bbox_logo[2] - bbox_logo[0]
            draw.text(
                (W - margin_inner - int(round(28 * s_fac)) - lw, margin_inner + int(round(22 * s_fac))),
                logo_disp,
                font=font_top,
                fill=(255, 242, 195, 255),
                stroke_width=max(1, int(round(3 * s_fac))),
                stroke_fill=(15, 18, 30, 235),
            )

        font_title = _load_kr_font(max(32, int(round(80 * s_fac))), bold=True)
        font_sub = _load_kr_font(max(18, int(round(32 * s_fac))), bold=True)
        st_w_title = max(3, int(round(7 * s_fac)))
        st_w_sub = max(1, int(round(2 * s_fac)))

        tb = draw.textbbox((0, 0), main_title, font=font_title, stroke_width=st_w_title)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        sb = draw.textbbox((0, 0), sub_title, font=font_sub, stroke_width=st_w_sub)
        sw, sh = sb[2] - sb[0], sb[3] - sb[1]

        box_w = max(tw, sw) + int(round(130 * s_fac))
        box_h = th + sh + int(round(90 * s_fac))
        bx0 = (W - box_w) // 2
        by0 = (H - box_h) // 2
        bx1 = bx0 + box_w
        by1 = by0 + box_h

        draw.rounded_rectangle(
            [bx0, by0, bx1, by1],
            radius=int(round(24 * s_fac)),
            fill=(14, 18, 32, 118),
            outline=(255, 255, 255, 235),
            width=max(2, int(round(4 * s_fac))),
        )
        tx = (W - tw) // 2
        ty = by0 + int(round(22 * s_fac))
        draw.text(
            (tx + int(round(4 * s_fac)), ty + int(round(5 * s_fac))),
            main_title,
            font=font_title,
            fill=(0, 0, 0, 120),
            stroke_width=st_w_title,
            stroke_fill=(0, 0, 0, 120),
        )
        draw.text(
            (tx, ty),
            main_title,
            font=font_title,
            fill=(24, 42, 86, 255),
            stroke_width=st_w_title,
            stroke_fill=(255, 255, 255, 255),
        )
        sx = (W - sw) // 2
        sy = ty + th + int(round(20 * s_fac))
        draw.text(
            (sx, sy),
            sub_title,
            font=font_sub,
            fill=(255, 244, 205, 255),
            stroke_width=st_w_sub,
            stroke_fill=(12, 16, 28, 220),
        )

    elif style_id == "cinematic_bar":
        bar_h = int(round(138 * s_fac))
        draw.rectangle([0, 0, W, bar_h], fill=(8, 10, 16, 238))
        draw.rectangle([0, H - bar_h, W, H], fill=(8, 10, 16, 238))
        gold_line_y1 = bar_h
        gold_line_y2 = H - bar_h
        draw.line([(0, gold_line_y1), (W, gold_line_y1)], fill=(235, 198, 112, 220), width=max(2, int(round(3 * s_fac))))
        draw.line([(0, gold_line_y2), (W, gold_line_y2)], fill=(235, 198, 112, 220), width=max(2, int(round(3 * s_fac))))

        font_top = _load_kr_font(max(16, int(round(26 * s_fac))), bold=True)
        draw.text(
            (int(round(64 * s_fac)), int(round(52 * s_fac))),
            f"CINEMATIC MEMORY  |  {sub_title}",
            font=font_top,
            fill=(235, 205, 135, 255),
        )
        if logo_disp:
            bbox_l = draw.textbbox((0, 0), logo_disp, font=font_top)
            lw = bbox_l[2] - bbox_l[0]
            draw.text(
                (W - int(round(64 * s_fac)) - lw, int(round(52 * s_fac))),
                logo_disp,
                font=font_top,
                fill=(240, 244, 255, 245),
            )

        font_title = _load_kr_font(max(34, int(round(84 * s_fac))), bold=True)
        font_sub = _load_kr_font(max(18, int(round(30 * s_fac))), bold=True)
        st_w = max(2, int(round(5 * s_fac)))
        tb = draw.textbbox((0, 0), main_title, font=font_title, stroke_width=st_w)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]

        band_h = th + int(round(86 * s_fac))
        by0 = (H - band_h) // 2
        draw.rectangle([0, by0, W, by0 + band_h], fill=(10, 14, 24, 128))
        draw.line([(int(round(220 * s_fac)), by0), (W - int(round(220 * s_fac)), by0)], fill=(255, 255, 255, 180), width=max(1, int(round(2 * s_fac))))
        draw.line([(int(round(220 * s_fac)), by0 + band_h), (W - int(round(220 * s_fac)), by0 + band_h)], fill=(255, 255, 255, 180), width=max(1, int(round(2 * s_fac))))

        tx = (W - tw) // 2
        ty = by0 + int(round(34 * s_fac))
        draw.text(
            (tx, ty),
            main_title,
            font=font_title,
            fill=(255, 255, 255, 255),
            stroke_width=st_w,
            stroke_fill=(12, 18, 32, 245),
        )

        bottom_tag = f"—  {sub_title} · SPECIAL EDITION  —"
        bb = draw.textbbox((0, 0), bottom_tag, font=font_sub)
        bw = bb[2] - bb[0]
        draw.text(
            ((W - bw) // 2, H - bar_h + int(round(50 * s_fac))),
            bottom_tag,
            font=font_sub,
            fill=(235, 205, 135, 255),
        )

    elif style_id == "magazine_lower":
        margin = int(round(44 * s_fac))
        draw.rounded_rectangle(
            [margin, margin, W - margin, H - margin],
            radius=int(round(14 * s_fac)),
            outline=(255, 255, 255, 190),
            width=max(2, int(round(3 * s_fac))),
        )
        font_top = _load_kr_font(max(16, int(round(28 * s_fac))), bold=True)
        if logo_disp:
            bbox_l = draw.textbbox((0, 0), logo_disp, font=font_top)
            lw = bbox_l[2] - bbox_l[0]
            draw.rounded_rectangle(
                [W - margin - lw - int(round(68 * s_fac)), margin + int(round(26 * s_fac)), W - margin - int(round(26 * s_fac)), margin + int(round(82 * s_fac))],
                radius=int(round(12 * s_fac)),
                fill=(15, 22, 38, 175),
                outline=(255, 255, 255, 210),
                width=max(1, int(round(2 * s_fac))),
            )
            draw.text(
                (W - margin - lw - int(round(47 * s_fac)), margin + int(round(38 * s_fac))),
                logo_disp,
                font=font_top,
                fill=(255, 242, 195, 255),
            )

        font_tag = _load_kr_font(max(16, int(round(26 * s_fac))), bold=True)
        font_title = _load_kr_font(max(34, int(round(78 * s_fac))), bold=True)
        font_sub = _load_kr_font(max(18, int(round(34 * s_fac))), bold=True)
        st_w = max(2, int(round(5 * s_fac)))

        tb = draw.textbbox((0, 0), main_title, font=font_title, stroke_width=st_w)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]

        card_x0 = int(round(92 * s_fac))
        card_y1 = H - int(round(96 * s_fac))
        card_h = th + int(round(165 * s_fac))
        card_y0 = card_y1 - card_h
        card_w = max(tw + int(round(140 * s_fac)), int(round(760 * s_fac)))
        card_x1 = min(W - int(round(92 * s_fac)), card_x0 + card_w)

        draw.rounded_rectangle(
            [card_x0, card_y0, card_x1, card_y1],
            radius=int(round(22 * s_fac)),
            fill=(12, 18, 32, 172),
            outline=(255, 255, 255, 220),
            width=max(2, int(round(3 * s_fac))),
        )
        accent_w = int(round(12 * s_fac))
        draw.rounded_rectangle(
            [card_x0 + int(round(28 * s_fac)), card_y0 + int(round(28 * s_fac)), card_x0 + int(round(28 * s_fac)) + accent_w, card_y1 - int(round(28 * s_fac))],
            radius=int(round(6 * s_fac)),
            fill=(56, 189, 248, 255),
        )
        tx = card_x0 + int(round(62 * s_fac))
        ty_tag = card_y0 + int(round(28 * s_fac))
        draw.text(
            (tx, ty_tag),
            "EDITORIAL VLOG  ·  MEMORY LOG",
            font=font_tag,
            fill=(56, 189, 248, 255),
        )
        ty_title = ty_tag + int(round(38 * s_fac))
        draw.text(
            (tx, ty_title),
            main_title,
            font=font_title,
            fill=(255, 255, 255, 255),
            stroke_width=st_w,
            stroke_fill=(10, 15, 28, 240),
        )
        ty_sub = ty_title + th + int(round(18 * s_fac))
        draw.text(
            (tx, ty_sub),
            f"📅  {sub_title}",
            font=font_sub,
            fill=(255, 240, 195, 255),
        )

    elif style_id == "minimal_glass":
        font_badge = _load_kr_font(max(16, int(round(25 * s_fac))), bold=True)
        font_title = _load_kr_font(max(34, int(round(78 * s_fac))), bold=True)
        font_sub = _load_kr_font(max(18, int(round(30 * s_fac))), bold=True)
        st_w = max(2, int(round(4 * s_fac)))

        tb = draw.textbbox((0, 0), main_title, font=font_title, stroke_width=st_w)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]

        card_w = max(tw + int(round(170 * s_fac)), int(round(780 * s_fac)))
        card_h = th + int(round(190 * s_fac))
        cx0 = (W - card_w) // 2
        cy0 = (H - card_h) // 2
        cx1 = cx0 + card_w
        cy1 = cy0 + card_h

        draw.rounded_rectangle(
            [cx0, cy0, cx1, cy1],
            radius=int(round(36 * s_fac)),
            fill=(18, 24, 42, 138),
            outline=(255, 255, 255, 230),
            width=max(2, int(round(3 * s_fac))),
        )

        badge_text = logo_disp if logo_disp else "AUTOCUT STUDIO"
        bb = draw.textbbox((0, 0), badge_text, font=font_badge)
        bw, bh = bb[2] - bb[0], bb[3] - bb[1]
        pill_w = bw + int(round(48 * s_fac))
        pill_h = bh + int(round(20 * s_fac))
        px0 = (W - pill_w) // 2
        py0 = cy0 + int(round(28 * s_fac))
        draw.rounded_rectangle(
            [px0, py0, px0 + pill_w, py0 + pill_h],
            radius=pill_h // 2,
            fill=(37, 99, 235, 215),
            outline=(147, 197, 253, 240),
            width=max(1, int(round(2 * s_fac))),
        )
        draw.text(
            ((W - bw) // 2, py0 + int(round(8 * s_fac))),
            badge_text,
            font=font_badge,
            fill=(255, 255, 255, 255),
        )

        tx = (W - tw) // 2
        ty = py0 + pill_h + int(round(20 * s_fac))
        draw.text(
            (tx, ty),
            main_title,
            font=font_title,
            fill=(255, 255, 255, 255),
            stroke_width=st_w,
            stroke_fill=(12, 18, 34, 220),
        )

        sb = draw.textbbox((0, 0), sub_title, font=font_sub)
        sw = sb[2] - sb[0]
        draw.text(
            ((W - sw) // 2, ty + th + int(round(20 * s_fac))),
            sub_title,
            font=font_sub,
            fill=(186, 230, 253, 255),
        )

    else:  # vlog_stamp (폴라로이드·다이어리 스탬프)
        border_side = int(round(52 * s_fac))
        border_top = int(round(48 * s_fac))
        border_bot = int(round(168 * s_fac))

        draw.rectangle([0, 0, W, border_top], fill=(250, 248, 242, 248))
        draw.rectangle([0, H - border_bot, W, H], fill=(250, 248, 242, 248))
        draw.rectangle([0, border_top, border_side, H - border_bot], fill=(250, 248, 242, 248))
        draw.rectangle([W - border_side, border_top, W, H - border_bot], fill=(250, 248, 242, 248))
        draw.rectangle(
            [border_side, border_top, W - border_side, H - border_bot],
            outline=(30, 41, 59, 220),
            width=max(2, int(round(4 * s_fac))),
        )

        font_stamp = _load_kr_font(max(16, int(round(26 * s_fac))), bold=True)
        stamp_txt = f"REC ●  {sub_title}"
        sb_box = draw.textbbox((0, 0), stamp_txt, font=font_stamp)
        stw, sth = sb_box[2] - sb_box[0], sb_box[3] - sb_box[1]
        sx0 = border_side + int(round(28 * s_fac))
        sy0 = border_top + int(round(24 * s_fac))
        draw.rounded_rectangle(
            [sx0, sy0, sx0 + stw + int(round(36 * s_fac)), sy0 + sth + int(round(20 * s_fac))],
            radius=int(round(12 * s_fac)),
            fill=(220, 38, 38, 225),
            outline=(255, 255, 255, 240),
            width=max(1, int(round(2 * s_fac))),
        )
        draw.text((sx0 + int(round(18 * s_fac)), sy0 + int(round(8 * s_fac))), stamp_txt, font=font_stamp, fill=(255, 255, 255, 255))

        font_title = _load_kr_font(max(30, int(round(64 * s_fac))), bold=True)
        font_logo = _load_kr_font(max(18, int(round(30 * s_fac))), bold=True)
        tb = draw.textbbox((0, 0), main_title, font=font_title)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]

        by_center = H - (border_bot // 2) - (th // 2) - int(round(6 * s_fac))
        draw.text(
            (border_side + int(round(24 * s_fac)), by_center),
            main_title,
            font=font_title,
            fill=(24, 34, 56, 255),
        )
        right_label = f"{logo_disp}   |   {sub_title}" if logo_disp else sub_title
        rb = draw.textbbox((0, 0), right_label, font=font_logo)
        rw, rh = rb[2] - rb[0], rb[3] - rb[1]
        draw.text(
            (W - border_side - rw - int(round(24 * s_fac)), H - (border_bot // 2) - (rh // 2)),
            right_label,
            font=font_logo,
            fill=(71, 85, 105, 255),
        )

    composed = Image.alpha_composite(base, overlay)
    return composed.convert("RGB"), style_id


def create_intro_segment(
    best_img_path: str,
    intro_mp4_path: str,
    title_text: str,
    date_name: str,
    logo_text: str,
    duration: float = 3.0,
    bgm_path: str = "",
    bgm_photo_vol: float = 0.38,
    out_width: int = 3840,
    out_height: int = 2160,
    out_fps: float = 60.0,
    video_bitrate_bps: int = 25000000,
    video_codec: str = "hevc_nvenc",
    intro_style: str = "classic_frame",
):
    """
    Create a 3-second Intro clip from the Best Shot using the selected intro_style:
    - Supports 5 distinct intro designs (classic_frame, cinematic_bar, magazine_lower, minimal_glass, vlog_stamp) + random
    - Renders 3.0s MP4 with HEVC/H.264, custom resolution/fps/bitrate, and normal-volume calm BGM
    """
    W, H = int(out_width), int(out_height)
    with Image.open(best_img_path) as raw_im:
        base = ImageOps.exif_transpose(raw_im).convert("RGBA").resize((W, H), Image.Resampling.LANCZOS)

    composed_rgb, _ = render_intro_frame_by_style(
        base_rgba=base,
        title_text=title_text,
        date_name=date_name,
        logo_text=logo_text,
        intro_style=intro_style,
    )
    intro_frame_png = intro_mp4_path + "_frame.png"
    composed_rgb.save(intro_frame_png, "PNG")

    fps_expr = _fps_to_ffmpeg_expr(out_fps)
    vcodec_args = _build_vcodec_args(video_codec, video_bitrate_bps)

    if bgm_path and os.path.isfile(bgm_path):
        fade_out_st = max(0.2, round(duration - 0.35, 2))
        cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-loop",
            "1",
            "-t",
            f"{duration:.2f}",
            "-i",
            intro_frame_png,
            "-stream_loop",
            "-1",
            "-ss",
            "0.00",
            "-t",
            f"{duration:.2f}",
            "-i",
            bgm_path,
            "-vf",
            f"fps={fps_expr},fade=t=in:st=0:d=0.45,format=yuv420p",
            "-af",
            f"volume={bgm_photo_vol:.3f},afade=t=in:st=0:d=0.45,afade=t=out:st={fade_out_st:.2f}:d=0.35,aresample=48000",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            *vcodec_args,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
            intro_mp4_path,
        ]
    else:
        cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-loop",
            "1",
            "-t",
            f"{duration:.2f}",
            "-i",
            intro_frame_png,
            "-f",
            "lavfi",
            "-t",
            f"{duration:.2f}",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-vf",
            f"fps={fps_expr},fade=t=in:st=0:d=0.45,format=yuv420p",
            *vcodec_args,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
            intro_mp4_path,
        ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        os.remove(intro_frame_png)
    except Exception:
        pass


def format_srt_time(seconds: float) -> str:
    ms = int(round((seconds % 1) * 1000))
    s = int(seconds)
    if ms == 1000:
        s += 1
        ms = 0
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_ass_time(seconds: float) -> str:
    cs = int(round((seconds % 1) * 100))
    s = int(seconds)
    if cs == 100:
        s += 1
        cs = 0
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def infer_scene_caption(
    idx: int,
    fname: str,
    creation_time: str,
    prev_time_hm: str,
    vision_records: List[Dict],
) -> Optional[Tuple[str, str]]:
    if fname in KNOWN_SCENE_CAPTIONS:
        return KNOWN_SCENE_CAPTIONS[fname]

    time_hm = ""
    if creation_time and "T" in creation_time and len(creation_time) >= 16:
        time_hm = creation_time[11:16]

    if idx == 0:
        return (time_hm or "START", "오늘의 기록 시작")

    if time_hm and prev_time_hm:
        try:
            h1, m1 = map(int, prev_time_hm.split(":"))
            h2, m2 = map(int, time_hm.split(":"))
            diff_min = (h2 * 60 + m2) - (h1 * 60 + m1)
            if diff_min >= 20:
                avg_mot = np.mean([r["motion"] for r in vision_records]) if vision_records else 5.0
                if avg_mot > 12.0:
                    label = "이동 및 드라이브"
                elif h2 in (12, 13, 18, 19):
                    label = "식사 및 휴식 시간"
                else:
                    label = "다음 장소 이동"
                return (time_hm, label)
        except Exception:
            pass
    return None


def _split_subtitle_chunks(text: str, max_chars: int = 26) -> List[str]:
    """Split a long subtitle sentence into natural chunks of <= max_chars."""
    t = text.strip()
    if len(t) <= max_chars or max_chars <= 0:
        return [t]
    words = t.split()
    if len(words) <= 1:
        return [t[i : i + max_chars] for i in range(0, len(t), max_chars)]
    chunks = []
    cur = ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= max_chars:
            cur += " " + w
        else:
            chunks.append(cur)
            cur = w
    if cur:
        chunks.append(cur)
    return chunks


def build_subtitles_for_timeline(
    analysis: dict,
    srt_full_path: str,
    srt_speech_path: str,
    ass_path: str,
    include_scene_captions: bool = True,
    intro_offset_sec: float = 3.0,
    max_sub_chars: int = 28,
    sub_font_size: int = 52,
) -> Tuple[int, int]:
    timeline_cursor = float(intro_offset_sec)
    srt_entries = []
    speech_only_entries = []
    ass_events = []
    prev_hm = ""

    for idx, clip in enumerate(analysis["clips"]):
        fname = clip["file"]
        kept_intervals = clip["kept_intervals"]
        speech_list = clip.get("speech", [])
        ct = clip.get("creation_time", "")
        if not kept_intervals:
            continue

        cur_hm = ct[11:16] if (ct and "T" in ct and len(ct) >= 16) else ""

        if include_scene_captions:
            sc = infer_scene_caption(idx, fname, ct, prev_hm, clip.get("vision_summary", []))
            if sc:
                time_str, desc = sc
                cap_start = timeline_cursor + 0.45
                first_iv_dur = kept_intervals[0][1] - kept_intervals[0][0]
                cap_end = min(timeline_cursor + 4.3, timeline_cursor + first_iv_dur)
                if cap_end - cap_start >= 1.4:
                    caption_text = f"[{time_str}] {desc}"
                    srt_entries.append((cap_start, cap_end, caption_text))
                    ass_events.append(
                        f"Dialogue: 1,{format_ass_time(cap_start)},{format_ass_time(cap_end)},SceneTag,,0,0,0,,{{\\fad(250,250)}}▶ {time_str} | {desc}"
                    )
        if cur_hm:
            prev_hm = cur_hm

        for iv_start, iv_end in kept_intervals:
            iv_dur = iv_end - iv_start
            for sp in speech_list:
                sp_s = max(iv_start, sp["start"])
                sp_e = min(iv_end, sp["end"])
                if sp_e - sp_s >= 0.35:
                    tl_s = timeline_cursor + (sp_s - iv_start)
                    tl_e = timeline_cursor + (sp_e - iv_start)
                    if tl_e - tl_s < 1.1:
                        tl_e = min(timeline_cursor + iv_dur, tl_s + 1.1)
                    chunks = _split_subtitle_chunks(sp["text"].strip(), max_chars=max_sub_chars)
                    chunk_dur = (tl_e - tl_s) / max(1, len(chunks))
                    for c_i, ch_text in enumerate(chunks):
                        cs = tl_s + c_i * chunk_dur
                        ce = tl_s + (c_i + 1) * chunk_dur
                        srt_entries.append((cs, ce, ch_text))
                        speech_only_entries.append((cs, ce, ch_text))
                        ass_events.append(
                            f"Dialogue: 0,{format_ass_time(cs)},{format_ass_time(ce)},Speech,,0,0,0,,{ch_text}"
                        )
            timeline_cursor += iv_dur

    srt_entries.sort(key=lambda x: (x[0], x[1]))
    with open(srt_full_path, "w", encoding="utf-8-sig") as f:
        for i, (s, e, text) in enumerate(srt_entries, 1):
            f.write(f"{i}\n{format_srt_time(s)} --> {format_srt_time(e)}\n{text}\n\n")

    speech_only_entries.sort(key=lambda x: (x[0], x[1]))
    with open(srt_speech_path, "w", encoding="utf-8-sig") as f:
        for i, (s, e, text) in enumerate(speech_only_entries, 1):
            f.write(f"{i}\n{format_srt_time(s)} --> {format_srt_time(e)}\n{text}\n\n")

    ass_header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Speech,Malgun Gothic,{sub_font_size},&H00FFFFFF,&H0000FFFF,&H00141414,&H80000000,-1,0,0,0,100,100,0,0,1,3.4,1.5,2,60,60,55,1
Style: SceneTag,Malgun Gothic,36,&H00A8F0FF,&H0000FFFF,&H00181818,&H96000000,-1,0,0,0,100,100,0,0,3,10,0,7,44,48,105,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(ass_path, "w", encoding="utf-8-sig") as f:
        f.write(ass_header)
        for ev in ass_events:
            f.write(ev + "\n")

    return len(srt_entries), len(speech_only_entries)


def _extract_last_frame_clip(
    seg_mp4_path: str,
    tail_mp4_path: str,
    trans_dur: float = 0.45,
    out_fps: float = 60.0,
    video_bitrate_bps: int = 25000000,
    video_codec: str = "hevc_nvenc",
):
    """Extract the last frame of seg_mp4_path and loop it for trans_dur seconds for seamless xfade into next clip."""
    last_jpg = tail_mp4_path + ".jpg"
    cmd_frame = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-sseof",
        "-0.15",
        "-i",
        seg_mp4_path,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        last_jpg,
    ]
    subprocess.run(cmd_frame, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not os.path.exists(last_jpg):
        return False

    fps_expr = _fps_to_ffmpeg_expr(out_fps)
    vcodec_args = _build_vcodec_args(video_codec, video_bitrate_bps)
    cmd_loop = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-loop",
        "1",
        "-t",
        f"{trans_dur:.2f}",
        "-i",
        last_jpg,
        "-vf",
        f"fps={fps_expr},format=yuv420p",
        *vcodec_args,
        "-an",
        tail_mp4_path,
    ]
    subprocess.run(cmd_loop, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        os.remove(last_jpg)
    except Exception:
        pass
    return os.path.exists(tail_mp4_path)


def run_autocut_pipeline(
    date_folder_path: str,
    output_dir: str,
    edit_mode: str = "travel",
    whisper_model_size: str = "small",
    denoise_audio: bool = True,
    include_scene_captions: bool = True,
    burn_subtitles: bool = True,
    export_filmora_clean: bool = True,
    include_photos: bool = True,
    enable_intro: bool = True,
    intro_title: str = "윰탱이네 제주도 여행",
    intro_style: str = "classic_frame",
    logo_text: str = "윰탱해윤해진이네",
    enable_hud: bool = True,
    enable_transitions: bool = True,
    enable_bgm: bool = True,
    custom_bgm_path: str = "",
    bgm_video_vol: float = 0.06,
    bgm_photo_vol: float = 0.38,
    style_preset_name: str = "기본 (표준 가족·여행 편집)",
    color_profile: str = "자동 감지 (일반 + DJI D-Log/D-Log M 자동 복원)",
    out_width: int = 3840,
    out_height: int = 2160,
    out_fps: float = 60.0,
    video_bitrate_bps: int = 25000000,
    video_codec: str = "hevc_nvenc",
    log_fn: Callable[[str], None] = print,
    progress_fn: Optional[Callable[[float, str], None]] = None,
    stop_check_fn: Optional[Callable[[], bool]] = None,
) -> Dict:
    """
    End-to-end AutoCut pipeline for a date folder with Chronological Video/Photo Metadata Sorting,
    Configurable Output Specs (Default: 3840x2160 60fps HEVC 25Mbps),
    DJI D-Log / D-Log M Color Restoration, 5 Intro Styles, Edit Modes (travel / talk / highlight / drive),
    YouTube Style Preset & Dynamic BGM Ducking support.
    """
    os.makedirs(output_dir, exist_ok=True)
    date_name = os.path.basename(os.path.normpath(date_folder_path))
    if edit_mode in EDIT_MODE_PRESETS:
        edit_mode = EDIT_MODE_PRESETS[edit_mode]
    if edit_mode == "normal":
        edit_mode = "travel"

    out_width = max(320, int(out_width))
    out_height = max(240, int(out_height))
    out_fps = float(out_fps)
    video_bitrate_bps = max(1_000_000, int(video_bitrate_bps))
    fps_expr = _fps_to_ffmpeg_expr(out_fps)
    vcodec_args = _build_vcodec_args(video_codec, video_bitrate_bps)
    scale_pad_vf = (
        f"scale={out_width}:{out_height}:force_original_aspect_ratio=decrease,"
        f"pad={out_width}:{out_height}:(ow-iw)/2:(oh-ih)/2,fps={fps_expr}"
    )

    all_presets = get_all_style_presets()
    style_cfg = all_presets.get(style_preset_name) or next(iter(all_presets.values()))
    eq_filter_str = style_cfg.get("eq_filter", "").strip()
    eq_chain = f",{eq_filter_str}" if eq_filter_str else ""
    max_sub_chars = int(style_cfg.get("max_sub_chars", 28))
    sub_font_size = int(style_cfg.get("sub_font_size", 52))
    trans_pool = style_cfg.get("transitions") or TRANSITION_POOL
    trans_prob = float(style_cfg.get("transition_prob", 1.0))

    codec_label = "HEVC (H.265 NVENC)" if ("hevc" in video_codec.lower() or "265" in video_codec.lower()) else "H.264 (AVC NVENC)"
    log_fn(
        f"⚙️ 출력 인코딩 설정: 해상도 {out_width}x{out_height} | {out_fps:g}fps | "
        f"화질 {video_bitrate_bps:,} bps ({video_bitrate_bps/1_000_000:.1f} Mbps) | 압축 코덱: {codec_label}"
    )
    log_fn(f"🎞️ 영상 편집 모드: {edit_mode} | 인트로 스타일: {intro_style} | 컬러 프로파일(DJI/D-Log): {color_profile}")

    bgm_file = ""
    if enable_bgm:
        if custom_bgm_path and os.path.isfile(custom_bgm_path):
            bgm_file = custom_bgm_path
        else:
            bgm_file = ensure_calm_bgm_file()
        log_fn(
            f"🎵 잔잔한 배경음악(BGM) 활성화: {os.path.basename(bgm_file)} "
            f"(영상 재생 시 아주 작게 {int(bgm_video_vol*100)}% / 사진·인트로 재생 시 보통 크기 {int(bgm_photo_vol*100)}%)"
        )

    video_files, raw_photo_files = scan_media_files_in_folder(date_folder_path)
    photo_files = raw_photo_files if include_photos else []

    total_items = len(video_files) + len(photo_files)
    if total_items == 0:
        raise ValueError(f"선택한 폴더에 영상이나 사진이 없습니다: {date_folder_path}")

    log_fn(f"🚀 [{date_name}] 자동 편집 시작 (영상 {len(video_files)}개, 사진 {len(photo_files)}개, 모드: {edit_mode})")
    log_fn(f"🎨 적용된 편집 스타일: {style_preset_name} ({style_cfg.get('desc', '')})")

    report_json = os.path.join(output_dir, f"{date_name}_편집분석리포트.json")
    analysis_summary = None
    cached_video_map: Dict[str, Dict] = {}

    if os.path.isfile(report_json):
        try:
            with open(report_json, "r", encoding="utf-8") as f:
                cached = json.load(f)
            cached_mode = cached.get("edit_mode", "travel")
            if cached_mode == "normal":
                cached_mode = "travel"
            if cached_mode == edit_mode:
                for c in cached.get("clips", []):
                    if c.get("type") == "video" and c.get("file"):
                        cached_video_map[c["file"]] = c

                current_file_set = {os.path.basename(p) for p in (video_files + photo_files)}
                cached_file_set = {c.get("file", "") for c in cached.get("clips", [])}
                if current_file_set == cached_file_set and cached.get("total_clips") == total_items:
                    analysis_summary = cached
                    clips_meta = cached["clips"]
                    total_orig_sec = cached["total_original_seconds"]
                    total_edit_sec = cached["total_edited_seconds"]
                    updated_cache = False
                    for c in clips_meta:
                        cpath = c.get("path", "")
                        ctype = c.get("type", "video")
                        ct_val = c.get("creation_time", "")
                        if (not ct_val or len(ct_val) < 19 or ct_val.endswith("Z")) and os.path.exists(cpath):
                            new_ct = get_photo_datetime_iso(cpath) if ctype == "photo" else get_video_datetime_iso(cpath)
                            if new_ct and new_ct != ct_val:
                                c["creation_time"] = new_ct
                                updated_cache = True
                    sorted_clips = sorted(clips_meta, key=lambda x: (x.get("creation_time") or "9999-99-99", x.get("file", "")))
                    if sorted_clips != clips_meta or updated_cache:
                        clips_meta = sorted_clips
                        analysis_summary["clips"] = clips_meta
                        with open(report_json, "w", encoding="utf-8") as fw:
                            json.dump(analysis_summary, fw, ensure_ascii=False, indent=2)
                    log_fn(f"⚡ 기존 분석 리포트({os.path.basename(report_json)})를 불러와 음성/비전 분석 단계를 즉시 통과합니다!")
                elif cached_video_map:
                    log_fn(
                        f"🔄 폴더 내 추가/변경된 미디어 감지! 기존 분석 완료된 영상 {len(cached_video_map)}개는 캐시를 재사용하고 새로 추가된 항목만 분석합니다."
                    )
        except Exception:
            analysis_summary = None

    with tempfile.TemporaryDirectory() as tmpdir:
        if analysis_summary is None:
            model = None
            clips_meta = []
            total_orig_sec = 0.0
            total_edit_sec = 0.0

            for idx, fpath in enumerate(video_files):
                if stop_check_fn and stop_check_fn():
                    raise RuntimeError("사용자에 의해 작업이 중단되었습니다.")

                fname = os.path.basename(fpath)
                ct = get_video_datetime_iso(fpath)

                if fname in cached_video_map:
                    c_entry = dict(cached_video_map[fname])
                    c_entry["path"] = fpath
                    c_entry["creation_time"] = ct
                    dur = float(c_entry.get("duration", 0.0))
                    kept_dur = float(c_entry.get("kept_duration", 0.0))
                    total_orig_sec += dur
                    total_edit_sec += kept_dur
                    clips_meta.append(c_entry)
                    log_fn(
                        f"  [{idx+1:02d}/{len(video_files)}] ⚡ {fname} (촬영: {ct.replace('T', ' ')}) | 기존 분석 캐시 재사용 (원본 {dur:5.1f}초 -> 채택 {kept_dur:5.1f}초)"
                    )
                    continue

                if model is None:
                    model = load_whisper_model(whisper_model_size, log_fn=log_fn)

                _, dur = get_video_info(fpath)

                if progress_fn:
                    progress_fn((idx / max(1, total_items)) * 45.0, f"분석 중 ({idx+1}/{len(video_files)}): {fname}")

                wav_path = os.path.join(tmpdir, f"{fname}.wav")
                speech_list = []
                try:
                    extract_denoised_wav(fpath, wav_path, denoise=denoise_audio)
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
                                }
                            )
                except Exception as e:
                    log_fn(f"  ⚠️ {fname} 음성 분석 건너뜀: {e}")

                vision_recs = analyze_video_keyframes(fpath, dur)
                kept_intervals = select_clip_cuts(dur, vision_recs, speech_list, edit_mode=edit_mode)
                kept_dur = sum(e - s for s, e in kept_intervals)

                total_orig_sec += dur
                total_edit_sec += kept_dur

                log_fn(
                    f"  [{idx+1:02d}/{len(video_files)}] 🎬 {fname} (촬영: {ct.replace('T', ' ')}) | "
                    f"원본 {dur:5.1f}초 -> 채택 {kept_dur:5.1f}초 (컷 {len(kept_intervals)}개, 음성자막 {len(speech_list)}개)"
                )
                for sp in speech_list:
                    log_fn(f"      💬 [{sp['start']:4.1f}s~{sp['end']:4.1f}s] {sp['text']}")

                clips_meta.append(
                    {
                        "type": "video",
                        "file": fname,
                        "path": fpath,
                        "creation_time": ct,
                        "duration": round(dur, 2),
                        "kept_duration": round(kept_dur, 2),
                        "kept_intervals": kept_intervals,
                        "speech": speech_list,
                    }
                )

            for ppath in photo_files:
                pname = os.path.basename(ppath)
                ct_photo = get_photo_datetime_iso(ppath)
                log_fn(f"  📷 사진 메타데이터 확인: {pname} (촬영: {ct_photo.replace('T', ' ')}, 3.0초 슬라이드)")
                clips_meta.append(
                    {
                        "type": "photo",
                        "file": pname,
                        "path": ppath,
                        "creation_time": ct_photo,
                        "duration": 3.0,
                        "kept_duration": 3.0,
                        "kept_intervals": [[0.0, 3.0]],
                        "speech": [],
                    }
                )
                total_orig_sec += 3.0
                total_edit_sec += 3.0

            # Sort all videos and photos chronologically by shooting metadata timestamp
            clips_meta.sort(key=lambda x: (x.get("creation_time") or "9999-99-99", x.get("file", "")))

            analysis_summary = {
                "date": date_name,
                "edit_mode": edit_mode,
                "style_preset": style_preset_name,
                "total_clips": len(clips_meta),
                "total_original_seconds": round(total_orig_sec, 2),
                "total_edited_seconds": round(total_edit_sec, 2),
                "clips": clips_meta,
            }

            with open(report_json, "w", encoding="utf-8") as f:
                json.dump(analysis_summary, f, ensure_ascii=False, indent=2)

        if clips_meta:
            first_ct = (clips_meta[0].get("creation_time") or "").replace("T", " ")
            last_ct = (clips_meta[-1].get("creation_time") or "").replace("T", " ")
            log_fn(
                f"📅 영상·사진 메타데이터(EXIF/FFprobe) 촬영 시간순 배치 완료: "
                f"총 {len(clips_meta)}개 클립 ({first_ct} ~ {last_ct})"
            )

        # Step 2: Best Shot Intro (3.0s) & Top-Left Date / Top-Right Logo HUD Overlay
        seg_files = []
        intro_dur = 3.0 if enable_intro else 0.0
        bgm_cursor = 0.0

        hud_png = os.path.join(tmpdir, "hud_overlay.png")
        if enable_hud:
            create_hud_overlay_png(
                hud_png,
                date_name=date_name,
                logo_text=logo_text,
                out_width=out_width,
                out_height=out_height,
            )

        prev_tail_mp4 = None
        trans_dur = 0.45

        if enable_intro:
            if progress_fn:
                progress_fn(47.0, "베스트 샷 탐색 및 3초 인트로 타이틀 생성 중...")
            best_img_path = find_best_shot_frame(
                clips_meta,
                tmpdir,
                log_fn=log_fn,
                out_width=out_width,
                out_height=out_height,
            )
            shutil.copyfile(best_img_path, os.path.join(output_dir, f"{date_name}_베스트샷.jpg"))

            intro_mp4 = os.path.join(tmpdir, "seg_0000_intro.mp4")
            create_intro_segment(
                best_img_path=best_img_path,
                intro_mp4_path=intro_mp4,
                title_text=intro_title,
                date_name=date_name,
                logo_text=logo_text,
                duration=intro_dur,
                bgm_path=bgm_file,
                bgm_photo_vol=bgm_photo_vol,
                out_width=out_width,
                out_height=out_height,
                out_fps=out_fps,
                video_bitrate_bps=video_bitrate_bps,
                video_codec=video_codec,
                intro_style=intro_style,
            )
            bgm_cursor = intro_dur
            seg_files.append(intro_mp4)
            log_fn(f"✨ 3초 인트로 생성 완료 (제목: '{intro_title}', 스타일: {intro_style}, 정방향 베스트 샷 적용)")

            if enable_transitions:
                tail_path = os.path.join(tmpdir, "tail_0000.mp4")
                if _extract_last_frame_clip(
                    intro_mp4,
                    tail_path,
                    trans_dur=trans_dur,
                    out_fps=out_fps,
                    video_bitrate_bps=video_bitrate_bps,
                    video_codec=video_codec,
                ):
                    prev_tail_mp4 = tail_path

        # Step 3: Build SRT and ASS subtitles (offset by intro_dur = 3.0s)
        srt_full = os.path.join(output_dir, f"{date_name}_전체자막(필모라용).srt")
        srt_speech = os.path.join(output_dir, f"{date_name}_음성자막(필모라용).srt")
        ass_tmp = os.path.join(tmpdir, "subtitles.ass")

        n_all, n_sp = build_subtitles_for_timeline(
            analysis_summary,
            srt_full,
            srt_speech,
            ass_tmp,
            include_scene_captions=include_scene_captions,
            intro_offset_sec=intro_dur,
            max_sub_chars=max_sub_chars,
            sub_font_size=sub_font_size,
        )
        log_fn(f"📝 자막 파일 생성 완료: 전체 자막 {n_all}개 (음성 대화 {n_sp}개) -> {os.path.basename(srt_full)}")

        flat_cuts = []
        for clip in clips_meta:
            for s, e in clip["kept_intervals"]:
                if round(e - s, 2) >= 0.8:
                    flat_cuts.append((clip, s, e))

        total_cuts = len(flat_cuts)
        rng = random.Random(42)
        color_filter_cache: Dict[str, str] = {}

        for cut_idx, (clip, s, e) in enumerate(flat_cuts, 1):
            if stop_check_fn and stop_check_fn():
                raise RuntimeError("사용자에 의해 작업이 중단되었습니다.")

            fpath = clip["path"]
            fname = clip["file"]
            ctype = clip.get("type", "video")
            dur = round(e - s, 2)
            is_last_cut = cut_idx == total_cuts

            if ctype == "video":
                if fpath not in color_filter_cache:
                    cf_str = get_color_profile_filter(color_profile, fpath, log_fn=log_fn)
                    color_filter_cache[fpath] = f",{cf_str}" if cf_str else ""
                clip_eq_chain = color_filter_cache[fpath] + eq_chain
            else:
                clip_eq_chain = eq_chain

            use_tr = enable_transitions and prev_tail_mp4 and (rng.random() <= trans_prob)
            chosen_trans = rng.choice(trans_pool) if use_tr else "none"
            if progress_fn:
                t_info = f" [전환: {chosen_trans}]" if chosen_trans != "none" else ""
                progress_fn(
                    50.0 + (cut_idx / max(1, total_cuts)) * 38.0,
                    f"컷 렌더링 중 ({cut_idx}/{total_cuts}): {fname} ({s:.1f}s~{e:.1f}s){t_info}",
                )

            raw_seg = os.path.join(tmpdir, f"raw_{cut_idx:04d}.mp4")
            seg_out = os.path.join(tmpdir, f"seg_{cut_idx:04d}.mp4")

            fade_out_v = ""
            fade_out_a = ""
            if is_last_cut and dur > 1.5:
                st_fade = max(0.2, round(dur - 1.2, 2))
                fade_out_v = f",fade=t=out:st={st_fade:.2f}:d=1.20:color=black"
                fade_out_a = f",afade=t=out:st={st_fade:.2f}:d=1.20"

            bgm_ss = round(bgm_cursor % 145.0, 2)
            bgm_cursor += dur

            if ctype == "video":
                if bgm_file and os.path.isfile(bgm_file):
                    # Mix original denoised video audio (100%) with very low-volume BGM (bgm_video_vol, ~6%)
                    if enable_hud and os.path.exists(hud_png):
                        fc_str = (
                            f"[0:v]{scale_pad_vf}{clip_eq_chain}[base];"
                            f"[base][1:v]overlay=0:0:format=auto{fade_out_v},format=yuv420p[v];"
                            f"[0:a]highpass=f=85,afftdn=nf=-25,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[va];"
                            f"[2:a]volume={bgm_video_vol:.3f},aresample=48000[ba];"
                            f"[va][ba]amix=inputs=2:duration=first:dropout_transition=0:weights=1 1:normalize=0{fade_out_a}[a]"
                        )
                        cmd_raw = [
                            "ffmpeg",
                            "-y",
                            "-v",
                            "error",
                            "-hwaccel",
                            "auto",
                            "-ss",
                            f"{s:.2f}",
                            "-to",
                            f"{e:.2f}",
                            "-i",
                            fpath,
                            "-i",
                            hud_png,
                            "-stream_loop",
                            "-1",
                            "-ss",
                            f"{bgm_ss:.2f}",
                            "-t",
                            f"{dur:.2f}",
                            "-i",
                            bgm_file,
                            "-filter_complex",
                            fc_str,
                            "-map",
                            "[v]",
                            "-map",
                            "[a]",
                            *vcodec_args,
                            "-c:a",
                            "aac",
                            "-b:a",
                            "192k",
                            "-ar",
                            "48000",
                            "-ac",
                            "2",
                            raw_seg,
                        ]
                    else:
                        fc_str = (
                            f"[0:v]{scale_pad_vf}{clip_eq_chain}{fade_out_v},format=yuv420p[v];"
                            f"[0:a]highpass=f=85,afftdn=nf=-25,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[va];"
                            f"[1:a]volume={bgm_video_vol:.3f},aresample=48000[ba];"
                            f"[va][ba]amix=inputs=2:duration=first:dropout_transition=0:weights=1 1:normalize=0{fade_out_a}[a]"
                        )
                        cmd_raw = [
                            "ffmpeg",
                            "-y",
                            "-v",
                            "error",
                            "-hwaccel",
                            "auto",
                            "-ss",
                            f"{s:.2f}",
                            "-to",
                            f"{e:.2f}",
                            "-i",
                            fpath,
                            "-stream_loop",
                            "-1",
                            "-ss",
                            f"{bgm_ss:.2f}",
                            "-t",
                            f"{dur:.2f}",
                            "-i",
                            bgm_file,
                            "-filter_complex",
                            fc_str,
                            "-map",
                            "[v]",
                            "-map",
                            "[a]",
                            *vcodec_args,
                            "-c:a",
                            "aac",
                            "-b:a",
                            "192k",
                            "-ar",
                            "48000",
                            "-ac",
                            "2",
                            raw_seg,
                        ]
                else:
                    if enable_hud and os.path.exists(hud_png):
                        cmd_raw = [
                            "ffmpeg",
                            "-y",
                            "-v",
                            "error",
                            "-hwaccel",
                            "auto",
                            "-ss",
                            f"{s:.2f}",
                            "-to",
                            f"{e:.2f}",
                            "-i",
                            fpath,
                            "-i",
                            hud_png,
                            "-filter_complex",
                            f"[0:v]{scale_pad_vf}{clip_eq_chain}[base];[base][1:v]overlay=0:0:format=auto{fade_out_v},format=yuv420p[v]",
                            "-map",
                            "[v]",
                            "-map",
                            "0:a:0?",
                            "-af",
                            f"highpass=f=85,afftdn=nf=-25,loudnorm=I=-16:TP=-1.5:LRA=11{fade_out_a}",
                            *vcodec_args,
                            "-c:a",
                            "aac",
                            "-b:a",
                            "192k",
                            "-ar",
                            "48000",
                            "-ac",
                            "2",
                            raw_seg,
                        ]
                    else:
                        cmd_raw = [
                            "ffmpeg",
                            "-y",
                            "-v",
                            "error",
                            "-hwaccel",
                            "auto",
                            "-ss",
                            f"{s:.2f}",
                            "-to",
                            f"{e:.2f}",
                            "-i",
                            fpath,
                            "-map",
                            "0:v:0",
                            "-map",
                            "0:a:0?",
                            "-vf",
                            f"{scale_pad_vf}{clip_eq_chain}{fade_out_v},format=yuv420p",
                            "-af",
                            f"highpass=f=85,afftdn=nf=-25,loudnorm=I=-16:TP=-1.5:LRA=11{fade_out_a}",
                            *vcodec_args,
                            "-c:a",
                            "aac",
                            "-b:a",
                            "192k",
                            "-ar",
                            "48000",
                            "-ac",
                            "2",
                            raw_seg,
                        ]
            else:
                # Photo segment: normalize orientation (EXIF + upright check + blurred frame) & play BGM at normal volume (bgm_photo_vol)
                norm_photo = os.path.join(tmpdir, f"photo_norm_{cut_idx:04d}.jpg")
                ok_p, _ = prepare_upright_photo_1080p(
                    fpath,
                    norm_photo,
                    cover_mode=False,
                    out_width=out_width,
                    out_height=out_height,
                )
                photo_src = norm_photo if (ok_p and os.path.exists(norm_photo)) else fpath

                photo_fade_end = max(0.2, round(dur - 0.35, 2))
                photo_bgm_af = (
                    f"volume={bgm_photo_vol:.3f},afade=t=in:st=0:d=0.35,"
                    f"afade=t=out:st={photo_fade_end:.2f}:d=0.35{fade_out_a},aresample=48000"
                )

                if bgm_file and os.path.isfile(bgm_file):
                    if enable_hud and os.path.exists(hud_png):
                        cmd_raw = [
                            "ffmpeg",
                            "-y",
                            "-v",
                            "error",
                            "-loop",
                            "1",
                            "-t",
                            f"{dur:.2f}",
                            "-i",
                            photo_src,
                            "-i",
                            hud_png,
                            "-stream_loop",
                            "-1",
                            "-ss",
                            f"{bgm_ss:.2f}",
                            "-t",
                            f"{dur:.2f}",
                            "-i",
                            bgm_file,
                            "-filter_complex",
                            f"[0:v]{scale_pad_vf}{eq_chain}[base];[base][1:v]overlay=0:0:format=auto{fade_out_v},format=yuv420p[v]",
                            "-map",
                            "[v]",
                            "-map",
                            "2:a:0",
                            "-af",
                            photo_bgm_af,
                            *vcodec_args,
                            "-c:a",
                            "aac",
                            "-b:a",
                            "192k",
                            "-ar",
                            "48000",
                            "-ac",
                            "2",
                            "-shortest",
                            raw_seg,
                        ]
                    else:
                        cmd_raw = [
                            "ffmpeg",
                            "-y",
                            "-v",
                            "error",
                            "-loop",
                            "1",
                            "-t",
                            f"{dur:.2f}",
                            "-i",
                            photo_src,
                            "-stream_loop",
                            "-1",
                            "-ss",
                            f"{bgm_ss:.2f}",
                            "-t",
                            f"{dur:.2f}",
                            "-i",
                            bgm_file,
                            "-vf",
                            f"{scale_pad_vf}{eq_chain}{fade_out_v},format=yuv420p",
                            "-map",
                            "0:v:0",
                            "-map",
                            "1:a:0",
                            "-af",
                            photo_bgm_af,
                            *vcodec_args,
                            "-c:a",
                            "aac",
                            "-b:a",
                            "192k",
                            "-ar",
                            "48000",
                            "-ac",
                            "2",
                            "-shortest",
                            raw_seg,
                        ]
                else:
                    if enable_hud and os.path.exists(hud_png):
                        cmd_raw = [
                            "ffmpeg",
                            "-y",
                            "-v",
                            "error",
                            "-loop",
                            "1",
                            "-t",
                            f"{dur:.2f}",
                            "-i",
                            photo_src,
                            "-i",
                            hud_png,
                            "-f",
                            "lavfi",
                            "-t",
                            f"{dur:.2f}",
                            "-i",
                            "anullsrc=channel_layout=stereo:sample_rate=48000",
                            "-filter_complex",
                            f"[0:v]{scale_pad_vf}{eq_chain}[base];[base][1:v]overlay=0:0:format=auto{fade_out_v},format=yuv420p[v]",
                            "-map",
                            "[v]",
                            "-map",
                            "2:a",
                            *vcodec_args,
                            "-c:a",
                            "aac",
                            "-b:a",
                            "192k",
                            "-shortest",
                            raw_seg,
                        ]
                    else:
                        cmd_raw = [
                            "ffmpeg",
                            "-y",
                            "-v",
                            "error",
                            "-loop",
                            "1",
                            "-t",
                            f"{dur:.2f}",
                            "-i",
                            photo_src,
                            "-f",
                            "lavfi",
                            "-t",
                            f"{dur:.2f}",
                            "-i",
                            "anullsrc=channel_layout=stereo:sample_rate=48000",
                            "-vf",
                            f"{scale_pad_vf}{eq_chain}{fade_out_v},format=yuv420p",
                            *vcodec_args,
                            "-c:a",
                            "aac",
                            "-b:a",
                            "192k",
                            "-shortest",
                            raw_seg,
                        ]

            subprocess.run(cmd_raw, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # Apply random xfade transition from previous clip's tail if enabled
            if enable_transitions and prev_tail_mp4 and os.path.exists(prev_tail_mp4) and dur > 1.2:
                cmd_xfade = [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    prev_tail_mp4,
                    "-i",
                    raw_seg,
                    "-filter_complex",
                    f"[0:v][1:v]xfade=transition={chosen_trans}:duration={trans_dur:.2f}:offset=0,format=yuv420p[v]",
                    "-map",
                    "[v]",
                    "-map",
                    "1:a?",
                    *vcodec_args,
                    "-c:a",
                    "copy",
                    seg_out,
                ]
                res = subprocess.run(cmd_xfade, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if res.returncode != 0 or not os.path.exists(seg_out):
                    shutil.copyfile(raw_seg, seg_out)
            else:
                shutil.copyfile(raw_seg, seg_out)

            # Extract tail of raw_seg for the NEXT clip's transition
            if enable_transitions and not is_last_cut:
                next_tail = os.path.join(tmpdir, f"tail_{cut_idx:04d}.mp4")
                if _extract_last_frame_clip(
                    raw_seg,
                    next_tail,
                    trans_dur=trans_dur,
                    out_fps=out_fps,
                    video_bitrate_bps=video_bitrate_bps,
                    video_codec=video_codec,
                ):
                    prev_tail_mp4 = next_tail
            try:
                os.remove(raw_seg)
            except Exception:
                pass

            seg_files.append(seg_out)

        if is_last_cut:
            log_fn("🎬 랜덤 클립 트랜지션 및 마지막 장면 블랙 페이드아웃 적용 완료")

        concat_txt = os.path.join(tmpdir, "concat_list.txt")
        with open(concat_txt, "w", encoding="utf-8") as f:
            for sf in seg_files:
                f.write(f"file '{sf.replace(chr(92), '/')}'\n")

        clean_mp4 = os.path.join(output_dir, f"{date_name}_무자막_필모라용.mp4")
        subbed_mp4 = os.path.join(output_dir, f"{date_name}_자막포함_완성본.mp4")

        if progress_fn:
            progress_fn(90.0, "클립 병합 및 무자막 MP4 생성 중...")
        log_fn("🎬 컷 편집 클립 고속 병합 중...")
        is_hevc = ("hevc" in video_codec.lower()) or ("265" in video_codec.lower())
        tag_args = ["-tag:v", "hvc1"] if is_hevc else []
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_txt,
                "-c",
                "copy",
                *tag_args,
                "-movflags",
                "+faststart",
                clean_mp4,
            ],
            check=True,
        )
        log_fn(f"✅ 필모라 연동용 무자막 영상 저장 완료: {clean_mp4}")

        if burn_subtitles:
            if progress_fn:
                progress_fn(94.0, "자막 하드코딩 완성본 MP4 렌더링 중...")
            log_fn(f"🎬 자막 입힌 완성형 MP4 렌더링 중 ({codec_label}, {video_bitrate_bps:,} bps)...")
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-hwaccel",
                    "auto",
                    "-i",
                    clean_mp4,
                    "-vf",
                    "ass=subtitles.ass",
                    *vcodec_args,
                    "-c:a",
                    "copy",
                    "-movflags",
                    "+faststart",
                    subbed_mp4,
                ],
                cwd=tmpdir,
                check=True,
            )
            log_fn(f"✅ 자막 포함 완성본 영상 저장 완료: {subbed_mp4}")

        if not export_filmora_clean and burn_subtitles and os.path.exists(clean_mp4):
            os.remove(clean_mp4)

        final_edit_sec = total_edit_sec + intro_dur
        if progress_fn:
            progress_fn(100.0, f"완료! 원본 {total_orig_sec/60:.1f}분 -> 편집본 {final_edit_sec/60:.1f}분")

        analysis_summary["output_dir"] = output_dir
        analysis_summary["subbed_mp4"] = subbed_mp4 if burn_subtitles else ""
        analysis_summary["clean_mp4"] = clean_mp4 if export_filmora_clean else ""
        analysis_summary["srt_full"] = srt_full
        analysis_summary["srt_speech"] = srt_speech
        return analysis_summary


# ==============================================================================
# YouTube Style Presets & YouTube URL Style Analyzer / Learner
# ==============================================================================

CUSTOM_STYLES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custom_styles.json")

BUILTIN_STYLE_PRESETS: Dict[str, Dict] = {
    "기본 (표준 가족·여행 편집)": {
        "id": "default",
        "avg_cut_sec": 6.0,
        "max_silent_sec": 6.0,
        "speech_pad_sec": 0.5,
        "max_sub_chars": 28,
        "sub_font_size": 52,
        "transition_prob": 1.0,
        "transitions": TRANSITION_POOL,
        "eq_filter": "",
        "desc": "균형 잡힌 6초 호흡 · 자연스러운 원본 색감 · 15종 랜덤 트랜지션",
    },
    "🏝️ 감성 가족/여행 브이로그풍 (따뜻한 필름톤 · 여유 있는 호흡)": {
        "id": "emotional_vlog",
        "avg_cut_sec": 7.5,
        "max_silent_sec": 8.0,
        "speech_pad_sec": 0.7,
        "max_sub_chars": 24,
        "sub_font_size": 48,
        "transition_prob": 0.85,
        "transitions": ["fade", "dissolve", "smoothleft", "smoothright", "smoothup", "smoothdown"],
        "eq_filter": "eq=saturation=1.12:contrast=1.04:brightness=0.02,colorbalance=rs=0.04:gs=0.01:bs=-0.04",
        "desc": "따뜻한 감성 필름 색감 · 7.5초 여유로운 컷 호흡 · 부드러운 디졸브/스무스 전환 위주",
    },
    "⚡ 빠른 텐션 여행 유튜버풍 (빠른 점프컷 · 쨍한 색감 · 짧은 자막)": {
        "id": "fast_youtuber",
        "avg_cut_sec": 3.5,
        "max_silent_sec": 3.0,
        "speech_pad_sec": 0.25,
        "max_sub_chars": 16,
        "sub_font_size": 56,
        "transition_prob": 0.45,
        "transitions": ["wipeleft", "wiperight", "slideleft", "slideright", "circleopen", "diagtl"],
        "eq_filter": "eq=saturation=1.18:contrast=1.08:brightness=0.01",
        "desc": "침묵 즉시 컷(3.5초 빠른 호흡) · 선명하고 쨍한 색감 · 짧은 한 줄 예능형 자막(16자)",
    },
    "🌊 제주 청량 시네마틱 & 드라이브풍 (푸른 바다톤 · 풍경 강조)": {
        "id": "jeju_cinematic",
        "avg_cut_sec": 8.0,
        "max_silent_sec": 9.0,
        "speech_pad_sec": 0.6,
        "max_sub_chars": 26,
        "sub_font_size": 48,
        "transition_prob": 0.90,
        "transitions": ["fade", "dissolve", "circleopen", "radial", "smoothleft", "smoothright"],
        "eq_filter": "eq=saturation=1.22:contrast=1.09:brightness=0.01,colorbalance=rs=-0.03:gs=0.02:bs=0.06",
        "desc": "청량한 제주 바다/하늘 색감 강조 · 8초 시네마틱 호흡 · 부드러운 오버랩 전환",
    },
    "👶 육아·아이 중심 하이라이트풍 (화사한 피부톤 · 큼직한 자막)": {
        "id": "kids_family",
        "avg_cut_sec": 4.8,
        "max_silent_sec": 4.5,
        "speech_pad_sec": 0.5,
        "max_sub_chars": 18,
        "sub_font_size": 58,
        "transition_prob": 0.80,
        "transitions": ["circleopen", "circleclose", "fade", "smoothup", "smoothdown", "radial"],
        "eq_filter": "eq=saturation=1.10:contrast=1.03:brightness=0.04,colorbalance=rs=0.03:gs=0.01:bs=-0.02",
        "desc": "화사하고 밝은 피부톤 · 아이 대화/표정 위주 4.8초 호흡 · 큼직한 가독성 자막",
    },
}


def get_all_style_presets() -> Dict[str, Dict]:
    presets = dict(BUILTIN_STYLE_PRESETS)
    if os.path.isfile(CUSTOM_STYLES_FILE):
        try:
            with open(CUSTOM_STYLES_FILE, "r", encoding="utf-8") as f:
                custom = json.load(f)
            if isinstance(custom, dict):
                presets.update(custom)
        except Exception:
            pass
    return presets


def analyze_youtube_style(
    url_or_path: str,
    log_fn: Callable[[str], None] = print,
    progress_fn: Optional[Callable[[float, str], None]] = None,
) -> Tuple[str, Dict]:
    """
    Stream a 60~75s sample from a YouTube URL (or read a local video file),
    analyze its scene cut rhythm, motion energy, color grading (saturation/contrast/warmth),
    and transition style, then save and return (preset_name, style_dict).
    """
    url_or_path = url_or_path.strip()
    if not url_or_path:
        raise ValueError("유튜브 URL 또는 샘플 영상 경로를 입력해주세요.")

    w, h = 240, 135
    frame_bytes = w * h * 3
    raw = b""
    title = "유튜브 커스텀 스타일"

    if os.path.isfile(url_or_path):
        title = os.path.splitext(os.path.basename(url_or_path))[0][:24]
        log_fn(f"🔍 로컬 레퍼런스 영상 분석 시작: {title}")
        cmd_dec = [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            url_or_path,
            "-t",
            "75",
            "-vf",
            f"fps=4,scale={w}:{h}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-",
        ]
        proc = subprocess.Popen(cmd_dec, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        raw, _ = proc.communicate()
    else:
        log_fn(f"🌐 유튜브 영상 정보 및 스트림 연결 중: {url_or_path}")
        if progress_fn:
            progress_fn(15.0, "유튜브 영상 메타데이터 및 고속 스트림 추출 중...")
        try:
            import yt_dlp

            ydl_opts = {
                "format": "160/133/134/229/230/worstvideo[ext=mp4]/worstvideo/worst",
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url_or_path, download=False)
                raw_title = info.get("title", "YouTube 스타일")
                title = "".join(c for c in raw_title if c.isalnum() or c in " _-[]()")[:26].strip() or "YouTube 스타일"
                vid_dur = float(info.get("duration") or 120.0)
                stream_url = info.get("url", "")
                if not stream_url and info.get("requested_formats"):
                    stream_url = info["requested_formats"][0].get("url", "")

            start_s = min(20.0, max(0.0, vid_dur * 0.10))
            sample_dur = min(40.0, max(15.0, vid_dur - start_s))
            log_fn(f"  -> 영상 제목: '{raw_title}' (샘플 분석 구간: {start_s:.0f}초 ~ {start_s + sample_dur:.0f}초)")

            if progress_fn:
                progress_fn(45.0, "유튜브 스트림에서 샘플 프레임 고속 추출 중...")

            cmd_dec = [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                f"{start_s:.1f}",
                "-t",
                f"{sample_dur:.1f}",
                "-i",
                stream_url,
                "-vf",
                f"fps=4,scale={w}:{h}",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "bgr24",
                "-",
            ]
            proc = subprocess.Popen(cmd_dec, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            raw, _ = proc.communicate()
        except Exception as e:
            raise RuntimeError(f"유튜브 스트림 분석 실패: {e}")

    if progress_fn:
        progress_fn(70.0, "유튜브 영상의 컷 전환 호흡 및 색감/모션 패턴 계산 중...")
    log_fn("📊 레퍼런스 영상의 장면 전환(Cut) 간격, 색감(채도/대비/색온도), 모션 에너지 측정 중...")

    n_frames = len(raw) // frame_bytes
    if n_frames < 12:
        raise RuntimeError("분석할 프레임이 부족합니다. URL을 확인해주세요.")

    cut_timestamps = [0.0]
    sats, contrasts, brights, warmths = [], [], [], []
    prev_hist = None
    gradual_transitions = 0
    sharp_cuts = 0
    recent_diffs = []

    for idx in range(n_frames):
        bgr = np.frombuffer(raw[idx * frame_bytes : (idx + 1) * frame_bytes], dtype=np.uint8).reshape((h, w, 3))
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        sats.append(float(np.mean(hsv[:, :, 1])))
        brights.append(float(np.mean(gray)))
        contrasts.append(float(np.std(gray)))
        r_mean = float(np.mean(bgr[:, :, 2]))
        b_mean = float(np.mean(bgr[:, :, 0]))
        warmths.append(r_mean - b_mean)

        hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
        cv2.normalize(hist, hist)
        if prev_hist is not None:
            diff = float(cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA))
            recent_diffs.append(diff)
            t_sec = idx / 4.0
            if diff > 0.45 and (t_sec - cut_timestamps[-1]) >= 1.2:
                cut_timestamps.append(t_sec)
                if len(recent_diffs) >= 3 and 0.20 < recent_diffs[-2] <= 0.45:
                    gradual_transitions += 1
                else:
                    sharp_cuts += 1
        prev_hist = hist

    if len(cut_timestamps) >= 3:
        intervals = np.diff(cut_timestamps)
        avg_cut = float(np.clip(np.median(intervals), 2.5, 10.0))
    else:
        avg_cut = 5.5

    mean_sat = float(np.mean(sats))
    mean_con = float(np.mean(contrasts))
    mean_warm = float(np.mean(warmths))

    sat_mult = round(float(np.clip(mean_sat / 95.0, 0.95, 1.25)), 2)
    con_mult = round(float(np.clip(mean_con / 52.0, 0.96, 1.14)), 2)
    rs_val = round(float(np.clip(mean_warm / 350.0, -0.06, 0.06)), 2)
    bs_val = round(-rs_val, 2)

    eq_filter = f"eq=saturation={sat_mult}:contrast={con_mult},colorbalance=rs={rs_val}:bs={bs_val}"

    if avg_cut <= 4.2:
        max_sub_chars = 16
        max_silent_sec = 3.2
        speech_pad = 0.3
        style_tag = "빠른 점프컷 호흡"
    elif avg_cut <= 6.5:
        max_sub_chars = 22
        max_silent_sec = 5.5
        speech_pad = 0.45
        style_tag = "표준 브이로그 호흡"
    else:
        max_sub_chars = 26
        max_silent_sec = 8.0
        speech_pad = 0.65
        style_tag = "여유로운 감성 호흡"

    total_tr = max(1, sharp_cuts + gradual_transitions)
    trans_prob = round(float(np.clip((gradual_transitions / total_tr) + 0.35, 0.35, 0.95)), 2)

    preset_key = f"🎯 [유튜브 학습] {title}"
    learned_preset = {
        "id": f"yt_{int(datetime.now().timestamp())}",
        "source_url": url_or_path,
        "avg_cut_sec": round(avg_cut, 1),
        "max_silent_sec": round(max_silent_sec, 1),
        "speech_pad_sec": speech_pad,
        "max_sub_chars": max_sub_chars,
        "sub_font_size": 54 if avg_cut <= 4.5 else 50,
        "transition_prob": trans_prob,
        "transitions": TRANSITION_POOL,
        "eq_filter": eq_filter,
        "desc": f"{style_tag}(평균 컷 {avg_cut:.1f}초) · 채도 x{sat_mult} / 대비 x{con_mult} / 색온도 {rs_val:+.2f} · 자막 {max_sub_chars}자",
    }

    existing = {}
    if os.path.isfile(CUSTOM_STYLES_FILE):
        try:
            with open(CUSTOM_STYLES_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}
    existing[preset_key] = learned_preset
    with open(CUSTOM_STYLES_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    if progress_fn:
        progress_fn(100.0, f"스타일 학습 완료: {preset_key}")
    log_fn(f"✅ 유튜브 편집 스타일 학습 완료! -> [{preset_key}]")
    log_fn(f"   • {learned_preset['desc']}")
    return preset_key, learned_preset
