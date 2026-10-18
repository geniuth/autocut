import os
import json
import shutil
import subprocess
import tempfile

# Scene context captions based on visual inspection of the 27 clips on Day 1 (2023-09-20)
SCENE_CAPTIONS = {
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


def format_srt_time(seconds: float) -> str:
    ms = int(round((seconds % 1) * 1000))
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_ass_time(seconds: float) -> str:
    cs = int(round((seconds % 1) * 100))
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def build_subtitles(analysis: dict, srt_path: str, srt_speech_only_path: str, ass_path: str):
    """
    Map original clip timestamps to edited timeline timestamps and generate:
    1) Full SRT (scene captions + speech subtitles) for Filmora
    2) Speech-only SRT for Filmora
    3) Styled ASS subtitle file for burning into the final MP4
    """
    timeline_cursor = 0.0
    srt_entries = []
    speech_only_entries = []
    ass_events = []

    for clip in analysis["clips"]:
        fname = clip["file"]
        kept_intervals = clip["kept_intervals"]
        speech_list = clip["speech"]
        if not kept_intervals:
            continue

        # Add scene transition caption at the start of the first kept interval of key clips
        if fname in SCENE_CAPTIONS:
            time_str, desc = SCENE_CAPTIONS[fname]
            cap_start = timeline_cursor + 0.3
            cap_end = min(timeline_cursor + 4.2, timeline_cursor + (kept_intervals[0][1] - kept_intervals[0][0]))
            if cap_end - cap_start >= 1.5:
                caption_text = f"[{time_str}] {desc}"
                srt_entries.append((cap_start, cap_end, caption_text))
                ass_events.append(
                    f"Dialogue: 1,{format_ass_time(cap_start)},{format_ass_time(cap_end)},SceneTag,,0,0,0,,{{\\fad(250,250)}}📍 {time_str} | {desc}"
                )

        # Map each kept interval and project overlapping speech segments
        for iv_start, iv_end in kept_intervals:
            iv_dur = iv_end - iv_start
            for sp in speech_list:
                sp_s = max(iv_start, sp["start"])
                sp_e = min(iv_end, sp["end"])
                if sp_e - sp_s >= 0.35:
                    tl_s = timeline_cursor + (sp_s - iv_start)
                    tl_e = timeline_cursor + (sp_e - iv_start)
                    # Ensure readable minimum duration
                    if tl_e - tl_s < 1.1:
                        tl_e = min(timeline_cursor + iv_dur, tl_s + 1.1)
                    text = sp["text"].strip()
                    srt_entries.append((tl_s, tl_e, text))
                    speech_only_entries.append((tl_s, tl_e, text))
                    ass_events.append(
                        f"Dialogue: 0,{format_ass_time(tl_s)},{format_ass_time(tl_e)},Speech,,0,0,0,,{text}"
                    )

            timeline_cursor += iv_dur

    # Sort and write SRT (full)
    srt_entries.sort(key=lambda x: (x[0], x[1]))
    with open(srt_path, "w", encoding="utf-8-sig") as f:
        for idx, (s, e, text) in enumerate(srt_entries, 1):
            f.write(f"{idx}\n{format_srt_time(s)} --> {format_srt_time(e)}\n{text}\n\n")

    # Write speech-only SRT
    speech_only_entries.sort(key=lambda x: (x[0], x[1]))
    with open(srt_speech_only_path, "w", encoding="utf-8-sig") as f:
        for idx, (s, e, text) in enumerate(speech_only_entries, 1):
            f.write(f"{idx}\n{format_srt_time(s)} --> {format_srt_time(e)}\n{text}\n\n")

    # Write ASS file with clean Korean typography (Malgun Gothic)
    ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Speech,Malgun Gothic,52,&H00FFFFFF,&H0000FFFF,&H00141414,&H80000000,-1,0,0,0,100,100,0,0,1,3.2,1.5,2,60,60,55,1
Style: SceneTag,Malgun Gothic,38,&H00A8F0FF,&H0000FFFF,&H00181818,&H96000000,-1,0,0,0,100,100,0,0,3,10,0,7,48,48,42,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(ass_path, "w", encoding="utf-8-sig") as f:
        f.write(ass_header)
        for ev in ass_events:
            f.write(ev + "\n")

    return len(srt_entries), len(speech_only_entries)


def render_videos(analysis: dict, target_out_dir: str, workspace_out_dir: str):
    os.makedirs(target_out_dir, exist_ok=True)
    os.makedirs(workspace_out_dir, exist_ok=True)

    srt_full = os.path.join(target_out_dir, "2023-09-20_제주도여행_1일차_전체자막.srt")
    srt_speech = os.path.join(target_out_dir, "2023-09-20_제주도여행_1일차_음성자막.srt")
    ass_rel = "output/day1_subtitles.ass"
    ass_abs = os.path.join(workspace_out_dir, "day1_subtitles.ass")

    n_all, n_speech = build_subtitles(analysis, srt_full, srt_speech, ass_abs)
    print(f"Generated subtitles: {n_all} total entries ({n_speech} speech entries)")

    clean_mp4 = os.path.join(target_out_dir, "2023-09-20_제주도여행_1일차_무자막_필모라용.mp4")
    subbed_mp4 = os.path.join(target_out_dir, "2023-09-20_제주도여행_1일차_자막포함.mp4")

    with tempfile.TemporaryDirectory() as tmpdir:
        seg_files = []
        seg_idx = 0
        total_cuts = sum(len(c["kept_intervals"]) for c in analysis["clips"])

        for clip in analysis["clips"]:
            fpath = clip["path"]
            fname = clip["file"]
            for s, e in clip["kept_intervals"]:
                seg_idx += 1
                dur = round(e - s, 2)
                if dur < 0.5:
                    continue
                seg_out = os.path.join(tmpdir, f"seg_{seg_idx:04d}.mp4")
                print(f"  Encoding cut [{seg_idx}/{total_cuts}] {fname} ({s:.1f}s ~ {e:.1f}s, {dur:.1f}s)...")
                cmd = [
                    "ffmpeg",
                    "-y",
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
                    "0:a:0",
                    "-vf",
                    "scale=1920:1080,fps=60000/1001,format=yuv420p",
                    "-af",
                    "highpass=f=85,afftdn=nf=-20,loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-c:v",
                    "h264_nvenc",
                    "-preset",
                    "p4",
                    "-rc",
                    "vbr",
                    "-cq",
                    "21",
                    "-b:v",
                    "0",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    seg_out,
                ]
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                seg_files.append(seg_out)

        concat_txt = os.path.join(tmpdir, "concat_list.txt")
        with open(concat_txt, "w", encoding="utf-8") as f:
            for sf in seg_files:
                safe_path = sf.replace("\\", "/")
                f.write(f"file '{safe_path}'\n")

        print("Concatenating segments into clean MP4...")
        cmd_concat = [
            "ffmpeg",
            "-y",
            "-v",
            "quiet",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_txt,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            clean_mp4,
        ]
        subprocess.run(cmd_concat, check=True)
        print(f"Saved clean MP4: {clean_mp4}")

        print("Burning styled subtitles into final MP4...")
        cmd_sub = [
            "ffmpeg",
            "-y",
            "-hwaccel",
            "auto",
            "-v",
            "warning",
            "-i",
            clean_mp4,
            "-vf",
            f"ass={ass_rel}",
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p4",
            "-rc",
            "vbr",
            "-cq",
            "21",
            "-b:v",
            "0",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            subbed_mp4,
        ]
        subprocess.run(cmd_sub, cwd=r"d:\workspace\autocut", check=True)
        print(f"Saved subtitled MP4: {subbed_mp4}")


if __name__ == "__main__":
    analysis_path = r"d:\workspace\autocut\output\day1_analysis.json"
    with open(analysis_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    target_out = r"D:\편집할영상\2023_제주도여행\편집결과_1일차"
    workspace_out = r"d:\workspace\autocut\output"
    render_videos(data, target_out, workspace_out)
