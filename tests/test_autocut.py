"""
AutoCut Studio regression tests (stdlib unittest, synthetic footage only).

    python -m unittest discover -s tests -v            # fast + render tests (about a minute, needs ffmpeg/NVENC)
    set AUTOCUT_E2E=1 & python -m unittest discover -s tests -v   # also the full pipeline (loads the AI models)

Every source clip is generated with ffmpeg's test sources into a temp folder; nothing outside it is touched.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autocut_engine as E  # noqa: E402


def _ffmpeg(*args) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def _has_nvenc() -> bool:
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=256x144:d=0.2", "-c:v", "hevc_nvenc", "-f", "null", "-"],
        capture_output=True,
    )
    return r.returncode == 0


def _make_clip(path: str, seconds: float, size: str = "1280x720", fps: int = 30, tone: int = 440) -> None:
    _ffmpeg("-f", "lavfi", "-i", f"testsrc2=s={size}:r={fps}:d={seconds}", "-f", "lavfi", "-i", f"sine=f={tone}:d={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path)


class Units(unittest.TestCase):
    def test_cut_intervals_are_clamped_merged_and_filtered(self):
        self.assertEqual(E.normalize_cut_intervals([[2.0, 6.0], [5.5, 8.0], [9.0, 9.3], [-1.0, 1.5]], 7.5), [[0.0, 1.5], [2.0, 7.5]])
        self.assertEqual(E.normalize_cut_intervals([], 10.0), [])

    def test_cut_overrides_replace_only_listed_video_clips(self):
        clips = [
            {"type": "video", "file": "a.mp4", "duration": 10.0, "kept_intervals": [[0, 5]], "kept_duration": 5.0},
            {"type": "video", "file": "b.mp4", "duration": 10.0, "kept_intervals": [[1, 4]], "kept_duration": 3.0},
            {"type": "photo", "file": "a.jpg", "duration": 3.0, "kept_intervals": [[0, 3]], "kept_duration": 3.0},
        ]
        self.assertEqual(E.apply_cut_overrides(clips, {"a.mp4": [[2, 4]], "a.jpg": [], "zzz.mp4": [[0, 1]]}), 1)
        self.assertEqual(clips[0]["kept_intervals"], [[2.0, 4.0]])
        self.assertEqual(clips[0]["kept_duration"], 2.0)
        self.assertEqual(clips[1]["kept_intervals"], [[1, 4]])
        self.assertEqual(clips[2]["kept_intervals"], [[0, 3]])

    def test_bgm_ducks_under_speech_and_rises_in_pauses(self):
        self.assertEqual(E.bgm_duck_expr([], 0.06, 20), "0.1500")  # no speech: pause level throughout
        expr = E.bgm_duck_expr([(2.0, 4.0), (4.8, 6.0), (12.0, 13.0)], 0.06, 20)
        self.assertTrue(expr.startswith("0.1500-0.0900*"))
        self.assertEqual(expr.count("clip("), 2)  # the first two sentences are one span (gap < 1.5 s)
        self.assertIn("1.70-t", expr)
        self.assertIn("t-6.30", expr)

    def test_nvenc_never_passes_source_timecode_or_captions(self):
        for codec, dr in (("hevc_nvenc", "sdr"), ("h264_nvenc", "sdr"), ("hevc_nvenc", "hlg")):
            args = E._build_vcodec_args(codec, 8_000_000, dr)
            self.assertEqual(args[args.index("-s12m_tc") + 1], "0", (codec, dr))
            self.assertEqual(args[args.index("-a53cc") + 1], "0", (codec, dr))

    def test_resume_signature_ignores_the_temp_folder(self):
        a = E._strip_workdir([["ffmpeg", "-i", r"C:\Temp\autocut_render_aaa\hud.png", "lut3d=file='C\\:/Temp/autocut_render_aaa/x.cube'"]], r"C:\Temp\autocut_render_aaa")
        b = E._strip_workdir([["ffmpeg", "-i", r"C:\Temp\autocut_render_bbb\hud.png", "lut3d=file='C\\:/Temp/autocut_render_bbb/x.cube'"]], r"C:\Temp\autocut_render_bbb")
        self.assertEqual(a, b)
        self.assertEqual(E._sig(a), E._sig(b))
        self.assertNotEqual(E._sig(a), E._sig(a, "other"))

    def test_color_match_lut_is_identity_for_a_zero_correction(self):
        with tempfile.TemporaryDirectory() as d:
            for domain in ("sdr", "hlg"):
                lut = os.path.join(d, f"{domain}.cube")
                E.write_color_match_lut(lut, {"da": [0.0] * 3, "db": [0.0] * 3, "k": 1.0}, domain)
                with open(lut) as f:
                    rows = [ln.split() for ln in f if ln[:1].isdigit()]
                n = E._CM_LUT_SIZE
                self.assertEqual(len(rows), n ** 3)
                r, g, b = (float(x) for x in rows[n * n * 5 + n * 7 + 9])  # red fastest: (r=9, g=7, b=5)
                self.assertAlmostEqual(r, 9 / (n - 1), places=4)
                self.assertAlmostEqual(g, 7 / (n - 1), places=4)
                self.assertAlmostEqual(b, 5 / (n - 1), places=4)

    def test_ass_event_spans(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.ass")
            with open(p, "w", encoding="utf-8-sig") as f:
                f.write("[Events]\nDialogue: 0,0:00:03.17,0:00:05.17,Speech,,0,0,0,,안녕\nDialogue: 1,0:01:02.50,0:01:04.00,SceneTag,,0,0,0,,x\n")
            self.assertEqual(E._ass_event_spans(p), [(3.17, 5.17), (62.5, 64.0)])


class Watchdog(unittest.TestCase):
    def setUp(self):
        self._stall = E.FFMPEG_STALL_SEC
        E.FFMPEG_STALL_SEC = 2.0

    def tearDown(self):
        E.FFMPEG_STALL_SEC = self._stall

    def test_idle_process_is_killed_as_stalled(self):
        t = time.time()
        with self.assertRaises(E.FfmpegStalled):
            E._run_ffmpeg_watched([sys.executable, "-c", "import time; time.sleep(60)"])
        self.assertLess(time.time() - t, 15)

    def test_busy_process_is_left_alone(self):
        rc, _ = E._run_ffmpeg_watched([sys.executable, "-c", "import time\nt=time.time()\nwhile time.time()-t<4: pass"])
        self.assertEqual(rc, 0)

    def test_stop_request_ends_the_process_at_once(self):
        t = time.time()
        with self.assertRaises(RuntimeError):
            E._run_ffmpeg_watched([sys.executable, "-c", "import time; time.sleep(60)"], stop_check_fn=lambda: time.time() - t > 1.0)
        self.assertLess(time.time() - t, 6)


class OutputCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="autocut_test_")
        src = ["-f", "lavfi", "-i", "testsrc2=s=320x180:r=60:d=12", "-f", "lavfi", "-i", "sine=d=12"]
        enc = ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"]
        _ffmpeg(*src, *enc, os.path.join(cls.dir, "good.mp4"))
        _ffmpeg(*src, "-vf", "select='not(between(t,3,7))'", "-fps_mode", "passthrough", *enc, os.path.join(cls.dir, "gap.mp4"))
        _ffmpeg(*src, "-vf", "select='mod(n,4)'", "-fps_mode", "passthrough", *enc, os.path.join(cls.dir, "drop.mp4"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def check(self, name, expected=12.0):
        return E.verify_output_video(os.path.join(self.dir, name), expected, 60.0)

    def test_good_file_passes(self):
        r = self.check("good.mp4")
        self.assertTrue(r["ok"], r["problems"])
        self.assertAlmostEqual(r["fps"], 60.0, delta=0.5)
        self.assertIsNotNone(r["lufs"])

    def test_stretch_without_video_is_reported(self):
        r = self.check("gap.mp4")
        self.assertFalse(r["ok"])
        self.assertTrue(any("영상이 없습니다" in p for p in r["problems"]), r["problems"])

    def test_dropped_frames_are_reported(self):
        r = self.check("drop.mp4")
        self.assertFalse(r["ok"])
        self.assertTrue(any("프레임이 빠져" in p for p in r["problems"]), r["problems"])

    def test_wrong_length_is_reported(self):
        self.assertTrue(any("길이가 예상과" in p for p in self.check("good.mp4", expected=20.0)["problems"]))


@unittest.skipUnless(_has_nvenc(), "needs an NVIDIA GPU with NVENC")
class CutRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="autocut_test_")
        cls.src = os.path.join(cls.dir, "src.mp4")
        _make_clip(cls.src, 30, size="1920x1080")
        cls.bgm = os.path.join(cls.dir, "bgm.wav")
        _ffmpeg("-f", "lavfi", "-i", "sine=f=220:d=40", "-ac", "2", "-ar", "48000", cls.bgm)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def render(self, dur, handle, **kw):
        cmds = E._build_cut_render_cmd(
            src=self.src, is_photo=False, s=1.0, dur=dur, handle=handle, pre_vf="",
            scale_pad_vf="scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=60000/1001",
            post_vf="", hud_png="", bgm_file=self.bgm, bgm_ss=2.0, bgm_vol=0.06, photo_bgm_af="", hw_args=[],
            decode_args=E._default_decode_args(), fade_out_v="", fade_out_a="",
            vcodec_args=E._build_vcodec_args("hevc_nvenc", 8_000_000, "sdr"), out_main=os.path.join(self.dir, "raw.mp4"),
            out_handle=os.path.join(self.dir, "handle.mp4"), tail_vf="format=yuv420p", audio_gain_db=0.0, **kw,
        )
        for c in cmds:
            rc, err = E._run_ffmpeg_watched(c)
            self.assertEqual(rc, 0, err[-400:])
        return cmds

    def test_long_cut_with_handle_is_two_runs_with_exact_frame_counts(self):
        # v3.1.1: one ffmpeg writing cut + handle queued every frame of the cut in RAM and stalled on long cuts
        cmds = self.render(24.0, 0.5, bgm_speech=[(3.0, 6.0)])
        self.assertEqual(len(cmds), 2)
        fps = 60000 / 1001
        self.assertAlmostEqual(E._video_packet_count(os.path.join(self.dir, "raw.mp4")), 24.0 * fps, delta=2)
        self.assertAlmostEqual(E._video_packet_count(os.path.join(self.dir, "handle.mp4")), 0.5 * fps, delta=2)

    def test_lower_third_overlay_renders(self):
        lt = os.path.join(self.dir, "lt.png")
        E.create_lower_third_png(lt, "15:12", "공항에서 가족이 함께 기다리는 모습", 1920, 1080)
        self.render(5.0, 0.0, lower_third=(lt, 0.45, 4.3))
        self.assertAlmostEqual(E._video_packet_count(os.path.join(self.dir, "raw.mp4")), 5.0 * 60000 / 1001, delta=2)

    def test_end_credits_segment(self):
        out = os.path.join(self.dir, "credits.mp4")
        ok = E.create_end_credits_segment(out, [("title", "테스트 여행"), ("gap", ""), ("head", "촬영"), ("item", "카메라 · 3개")],
                                          bgm_path=self.bgm, out_width=1280, out_height=720, out_fps=30, video_bitrate_bps=4_000_000)
        self.assertTrue(ok)
        self.assertTrue(E.verify_output_video(out, E.END_CREDITS_SEC, 30.0)["ok"])


@unittest.skipUnless(os.environ.get("AUTOCUT_E2E") == "1" and _has_nvenc(), "set AUTOCUT_E2E=1 to run the full pipeline")
class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="autocut_test_")
        cls.folder = os.path.join(cls.dir, "2024-05-05")
        os.makedirs(cls.folder)
        for i, (sec, tone) in enumerate(((9, 330), (26, 440), (7, 550))):
            _make_clip(os.path.join(cls.folder, f"clip_{i}.mp4"), sec, tone=tone)
        cls.out = os.path.join(cls.dir, "편집결과_2024-05-05")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def run_edit(self, logs=None, **kw):
        opts = dict(out_width=1280, out_height=720, out_fps=30, video_bitrate_bps=4_000_000, intro_title="테스트",
                    speaker_diarization=False, max_edit_ratio=1.0, log_fn=(logs.append if logs is not None else (lambda m: None)))
        opts.update(kw)
        return E.run_autocut_pipeline(self.folder, self.out, **opts)

    def test_1_full_edit_passes_its_own_check(self):
        s = self.run_edit(intro_typewriter=True, lower_third_captions=True, end_credits=True, burn_subtitles=True)
        self.assertTrue(s["verify"]["ok"], s["verify"]["problems"])
        self.assertTrue(os.path.exists(s["clean_mp4"]))
        self.assertFalse(os.path.isdir(os.path.join(self.out, E.RESUME_DIR_NAME)))  # removed after a finished edit

    def test_2_stopped_edit_resumes_from_the_finished_cuts(self):
        state = {"stop": False}

        def prog(pct, msg):
            if "컷 렌더링 중 (2/" in msg:
                state["stop"] = True

        with self.assertRaises(RuntimeError):
            self.run_edit(progress_fn=prog, stop_check_fn=lambda: state["stop"])
        self.assertTrue(os.path.exists(os.path.join(self.out, E.RESUME_DIR_NAME, "seg_0001.mp4")))
        logs = []
        s = self.run_edit(logs=logs)
        self.assertTrue(any("이어서 편집" in m for m in logs), logs[-15:])
        self.assertTrue(s["verify"]["ok"], s["verify"]["problems"])

    def test_3_cut_edit_and_preview(self):
        full = self.run_edit()["verify"]["duration"]
        plan = E.planned_cuts_for_folder(self.out, "2024-05-05", 1.0)
        self.assertIn("clip_1.mp4", plan)
        E.save_cut_overrides(self.out, "2024-05-05", {"clip_1.mp4": [[2.0, 6.0]], "clip_2.mp4": []})
        try:
            s = self.run_edit(preview=True)
        finally:
            E.save_cut_overrides(self.out, "2024-05-05", {})
        self.assertTrue(s["clean_mp4"].endswith("_미리보기.mp4"))
        self.assertTrue(s["verify"]["ok"], s["verify"]["problems"])
        self.assertLess(s["verify"]["duration"], full - 3.0)
        with open(os.path.join(self.out, "2024-05-05_편집분석리포트.json"), encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(report["total_clips"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
