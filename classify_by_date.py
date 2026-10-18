import os
import glob
import json
import shutil
import subprocess
from collections import defaultdict


def get_media_date(filepath: str) -> str:
    """Extract YYYY-MM-DD from ffprobe creation_time, falling back to file mtime."""
    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_entries",
            "format_tags=creation_time:format=duration",
            filepath,
        ]
        out = subprocess.check_output(cmd).decode("utf-8", "ignore")
        data = json.loads(out)
        ct = data.get("format", {}).get("tags", {}).get("creation_time", "")
        if ct and len(ct) >= 10:
            return ct[:10]
    except Exception:
        pass
    from datetime import datetime
    return datetime.fromtimestamp(os.path.getmtime(filepath)).strftime("%Y-%m-%d")


def classify_folder(target_dir: str, move_files: bool = True):
    entries = [
        f for f in os.listdir(target_dir)
        if os.path.isfile(os.path.join(target_dir, f))
    ]
    if not entries:
        print("No top-level files to classify (already classified or empty).")
        return {}

    # Map base name (e.g. GX011864) to date using the video file first
    stem_to_date = {}
    video_exts = {".mp4", ".mov", ".avi", ".mkv", ".insv", ".lrv"}
    for fname in sorted(entries):
        stem, ext = os.path.splitext(fname)
        if ext.lower() in video_exts:
            full_path = os.path.join(target_dir, fname)
            stem_to_date[stem] = get_media_date(full_path)

    grouped = defaultdict(list)
    for fname in sorted(entries):
        stem, ext = os.path.splitext(fname)
        full_path = os.path.join(target_dir, fname)
        date_str = stem_to_date.get(stem) or get_media_date(full_path)
        grouped[date_str].append(fname)

        if move_files:
            date_dir = os.path.join(target_dir, date_str)
            os.makedirs(date_dir, exist_ok=True)
            dst_path = os.path.join(date_dir, fname)
            shutil.move(full_path, dst_path)

    for date_str, fnames in sorted(grouped.items()):
        mp4s = [f for f in fnames if f.lower().endswith(".mp4")]
        print(f"[{date_str}] Total files moved: {len(fnames)} (MP4 videos: {len(mp4s)})")
        if mp4s:
            print(f"  Range: {mp4s[0]} ~ {mp4s[-1]}")

    return grouped


if __name__ == "__main__":
    folder = r"D:\편집할영상\2023_제주도여행"
    classify_folder(folder, move_files=True)
