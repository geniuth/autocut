import os
import sys
import glob
import time
import ctypes
import threading
import subprocess
from collections import deque

# 0. Ensure GUI always runs on the user's interactive 'WinSta0\Default' desktop
if os.name == "nt":
    try:
        import ctypes.wintypes as _wt

        _u32 = ctypes.windll.user32
        _k32 = ctypes.windll.kernel32
        _hdesk = _u32.GetThreadDesktop(_k32.GetCurrentThreadId())
        _buf = ctypes.create_unicode_buffer(256)
        _u32.GetUserObjectInformationW(_hdesk, 2, _buf, 512, None)
        if _buf.value and _buf.value.lower() != "default" and "--on-default-desktop" not in sys.argv:
            class _STARTUPINFOW(ctypes.Structure):
                _fields_ = [
                    ("cb", _wt.DWORD), ("lpReserved", _wt.LPWSTR), ("lpDesktop", _wt.LPWSTR), ("lpTitle", _wt.LPWSTR),
                    ("dwX", _wt.DWORD), ("dwY", _wt.DWORD), ("dwXSize", _wt.DWORD), ("dwYSize", _wt.DWORD),
                    ("dwXCountChars", _wt.DWORD), ("dwYCountChars", _wt.DWORD), ("dwFillAttribute", _wt.DWORD),
                    ("dwFlags", _wt.DWORD), ("wShowWindow", _wt.WORD), ("cbReserved2", _wt.WORD),
                    ("lpReserved2", ctypes.POINTER(_wt.BYTE)), ("hStdInput", _wt.HANDLE),
                    ("hStdOutput", _wt.HANDLE), ("hStdError", _wt.HANDLE),
                ]

            class _PROCESS_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("hProcess", _wt.HANDLE), ("hThread", _wt.HANDLE),
                    ("dwProcessId", _wt.DWORD), ("dwThreadId", _wt.DWORD),
                ]

            _si = _STARTUPINFOW()
            _si.cb = ctypes.sizeof(_si)
            _si.lpDesktop = "WinSta0\\Default"
            _si.dwFlags = 1
            _si.wShowWindow = 1
            _pi = _PROCESS_INFORMATION()
            _pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.exists(_pyw):
                _pyw = sys.executable
            _cmd_str = f'"{_pyw}" "{os.path.abspath(__file__)}" --on-default-desktop'
            _cmd_buf = ctypes.create_unicode_buffer(_cmd_str)
            if _k32.CreateProcessW(
                None, _cmd_buf, None, None, False, 0, None,
                os.path.dirname(os.path.abspath(__file__)),
                ctypes.byref(_si), ctypes.byref(_pi)
            ):
                sys.exit(0)
    except Exception:
        pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("YumTaeng.AutoCutStudio.2026")
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont
from PIL import Image, ImageDraw, ImageFont, ImageTk

from autocut_engine import (
    VIDEO_EXTS,
    PHOTO_EXTS,
    EDIT_MODE_PRESETS,
    INTRO_STYLE_PRESETS,
    COLOR_PROFILE_PRESETS,
    scan_media_files_in_folder,
    classify_media_by_date,
    run_autocut_pipeline,
    get_all_style_presets,
    analyze_youtube_style,
    _load_kr_font,
    AUTOCUT_VERSION,
    DEFAULT_AI_MODELS,
)


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


def _ft_to_int(ft: _FILETIME) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


class SystemResourceMonitor:
    """
    Zero-popup real-time CPU & NVIDIA GPU monitor:
    - CPU % via Windows kernel32.GetSystemTimes
    - GPU % & VRAM via a single persistent 'nvidia-smi -l 1' background stream with CREATE_NO_WINDOW
    """

    def __init__(self, history_len: int = 60):
        self.history_len = history_len
        self.cpu_history = deque([0.0] * history_len, maxlen=history_len)
        self.gpu_history = deque([0.0] * history_len, maxlen=history_len)
        self.cpu_pct = 0.0
        self.gpu_pct = 0.0
        self.vram_used_mb = 0.0
        self.vram_total_mb = 8192.0
        self.gpu_name = "NVIDIA GPU"

        self._prev_idle = 0
        self._prev_kernel = 0
        self._prev_user = 0
        self._running = True
        self._smi_proc = None

        self._init_cpu_times()
        self._start_gpu_stream()

    def _init_cpu_times(self):
        idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
        if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            self._prev_idle = _ft_to_int(idle)
            self._prev_kernel = _ft_to_int(kernel)
            self._prev_user = _ft_to_int(user)

    def sample_cpu(self) -> float:
        idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
        if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            i = _ft_to_int(idle)
            k = _ft_to_int(kernel)
            u = _ft_to_int(user)
            d_idle = i - self._prev_idle
            d_total = (k - self._prev_kernel) + (u - self._prev_user)
            self._prev_idle, self._prev_kernel, self._prev_user = i, k, u
            if d_total > 0:
                usage = max(0.0, min(100.0, (1.0 - d_idle / d_total) * 100.0))
                self.cpu_pct = usage
        self.cpu_history.append(self.cpu_pct)
        return self.cpu_pct

    def _start_gpu_stream(self):
        def _reader():
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
                cmd = [
                    "nvidia-smi",
                    "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                    "-l",
                    "1",
                ]
                self._smi_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=flags,
                )
                for line in self._smi_proc.stdout:
                    if not self._running:
                        break
                    parts = [p.strip() for p in line.strip().split(",")]
                    if len(parts) >= 4:
                        self.gpu_name = parts[0].replace("NVIDIA GeForce ", "")
                        self.gpu_pct = float(parts[1])
                        self.vram_used_mb = float(parts[2])
                        self.vram_total_mb = max(1.0, float(parts[3]))
            except Exception:
                pass

        threading.Thread(target=_reader, daemon=True).start()

    def sample_all(self):
        self.sample_cpu()
        self.gpu_history.append(self.gpu_pct)

    def stop(self):
        self._running = False
        if self._smi_proc:
            try:
                self._smi_proc.terminate()
            except Exception:
                pass


def create_premiere_splash_image(width: int = 820, height: int = 460) -> Image.Image:
    """Render an Adobe Premiere Pro style splash screen artwork using PIL."""
    img = Image.new("RGBA", (width, height), (14, 12, 28, 255))
    draw = ImageDraw.Draw(img, "RGBA")

    for y in range(height):
        r = int(14 + (y / height) * 18)
        g = int(12 + (y / height) * 12)
        b = int(32 + (y / height) * 40)
        draw.line([(0, y), (width, y)], fill=(r, g, b, 255))

    track_colors = [
        ((52, 74, 126, 255), (137, 180, 250, 255)),
        ((78, 54, 122, 255), (203, 166, 247, 255)),
        ((46, 92, 76, 255), (166, 227, 161, 255)),
        ((96, 82, 54, 255), (249, 226, 175, 255)),
    ]
    for idx, (fill_col, border_col) in enumerate(track_colors):
        ty = 172 + idx * 34
        draw.rounded_rectangle([585 + (idx % 2) * 20, ty, width - 38, ty + 22], radius=6, fill=fill_col, outline=border_col, width=1)
        draw.rounded_rectangle([530, ty, 575 + (idx % 2) * 20, ty + 22], radius=6, fill=fill_col, outline=border_col, width=1)

    draw.rounded_rectangle([2, 2, width - 3, height - 3], radius=16, outline=(180, 190, 254, 255), width=3)
    draw.rounded_rectangle([8, 8, width - 9, height - 9], radius=12, outline=(88, 91, 112, 180), width=1)

    bx, by, bs = 54, 48, 92
    draw.rounded_rectangle([bx, by, bx + bs, by + bs], radius=20, fill=(30, 22, 68, 255), outline=(203, 166, 247, 255), width=4)
    font_badge = _load_kr_font(44, bold=True)
    draw.text((bx + 17, by + 16), "Ac", font=font_badge, fill=(216, 180, 254, 255))

    font_brand = _load_kr_font(38, bold=True)
    font_sub = _load_kr_font(16, bold=True)
    font_desc = _load_kr_font(15, bold=True)
    font_ed = _load_kr_font(16, bold=True)

    draw.text((166, 50), "AutoCut Studio 2026", font=font_brand, fill=(255, 255, 255, 255))
    draw.text((168, 104), "AI VISION · MOTION & YOUTUBE STYLE AUTO-EDITING SUITE", font=font_sub, fill=(137, 180, 250, 255))

    draw.text((56, 172), "★  스마트 비전/모션 컷편집 & 베스트 샷 인트로 생성", font=font_desc, fill=(225, 230, 250, 255))
    draw.text((56, 206), "★  유튜브 URL 스타일 학습기 & 장르별 편집 프리셋 탑재", font=font_desc, fill=(225, 230, 250, 255))
    draw.text((56, 240), "★  클립 간 랜덤 트랜지션 & 필모라(Filmora) 완벽 호환", font=font_desc, fill=(225, 230, 250, 255))
    draw.text((56, 274), "★  NVIDIA GeForce RTX CUDA & NVENC 하드웨어 가속", font=font_desc, fill=(225, 230, 250, 255))

    draw.rounded_rectangle([54, 322, 415, 362], radius=10, fill=(40, 36, 72, 255), outline=(249, 226, 175, 255), width=2)
    draw.text((74, 331), "★  Creative Edition · 윰탱해윤해진이네", font=font_ed, fill=(249, 226, 175, 255))

    return img


class AutoCutStudioApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()

        self.title(f"AutoCut Studio 2026 v{AUTOCUT_VERSION} — 비전/모션 & 유튜브 스타일 AI 자동 영상 편집기 (Filmora 연동)")

        self.geometry("1920x1080+0+0")
        self.minsize(1360, 820)
        self.configure(bg="#181825")

        base_family = "맑은 고딕"
        for fname, sz in [
            ("TkDefaultFont", 10),
            ("TkTextFont", 10),
            ("TkMenuFont", 10),
            ("TkHeadingFont", 11),
            ("TkFixedFont", 10),
        ]:
            try:
                f = tkfont.nametofont(fname)
                f.configure(family=base_family, size=sz)
            except Exception:
                pass

        # Dark dropdown listbox colors for Combobox popups
        self.option_add("*TCombobox*Listbox.background", "#181825")
        self.option_add("*TCombobox*Listbox.foreground", "#cdd6f4")
        self.option_add("*TCombobox*Listbox.selectBackground", "#89b4fa")
        self.option_add("*TCombobox*Listbox.selectForeground", "#11111b")
        self.option_add("*TCombobox*Listbox.font", (base_family, 10))

        self.target_dir = tk.StringVar(value=r"D:\편집할영상\2023_제주도여행")
        self.edit_mode = tk.StringVar(value="travel")
        self.whisper_model = tk.StringVar(value="large-v3-turbo")
        self.denoise_audio = tk.BooleanVar(value=True)
        self.include_scene_captions = tk.BooleanVar(value=True)
        self.burn_subtitles = tk.BooleanVar(value=True)
        self.export_filmora_clean = tk.BooleanVar(value=True)
        self.include_photos = tk.BooleanVar(value=True)

        # Branding, Intro (5 styles + random), Transition, BGM & Post-Edit Folder Open options
        self.enable_intro = tk.BooleanVar(value=True)
        self.intro_style_var = tk.StringVar(value=next(iter(INTRO_STYLE_PRESETS.keys())))
        self.intro_title = tk.StringVar(value="윰탱이네 제주도 여행")
        self.enable_hud = tk.BooleanVar(value=True)
        self.logo_text = tk.StringVar(value="윰탱해윤해진이네")
        self.enable_transitions = tk.BooleanVar(value=True)
        self.enable_bgm = tk.BooleanVar(value=True)
        self.custom_bgm_path = tk.StringVar(value="")
        self.auto_open_folder_on_done = tk.BooleanVar(value=False)

        # Default Output Encoding Specifications: 3840x2160 (4K UHD), 60fps, HEVC, 25Mbps (25,000,000 bps)
        self.out_resolution_var = tk.StringVar(value="3840x2160 (4K UHD)")
        self.out_fps_var = tk.StringVar(value="60 fps")
        self.out_bitrate_bps_var = tk.StringVar(value="25,000,000")
        self.out_codec_var = tk.StringVar(value="HEVC (H.265 - hevc_nvenc 권장)")
        self.color_profile_var = tk.StringVar(value=next(iter(COLOR_PROFILE_PRESETS.keys())))

        # YouTube Style Analyzer & Preset variables
        self.all_presets = get_all_style_presets()
        preset_keys = list(self.all_presets.keys())
        default_preset_key = preset_keys[-1] if len(preset_keys) > 5 else preset_keys[0]
        self.style_preset_var = tk.StringVar(value=default_preset_key)
        self.style_desc_var = tk.StringVar(value=self.all_presets[default_preset_key].get("desc", ""))
        self.yt_url_var = tk.StringVar(value="https://youtu.be/jxqq9VGnCbs")

        self.progress_var = tk.DoubleVar(value=0.0)
        self.status_var = tk.StringVar(value="준비 완료 — 날짜 폴더를 확인하고 [자동 컷편집 시작]을 눌러주세요.")
        self.cpu_label_var = tk.StringVar(value="CPU: 0.0%")
        self.gpu_label_var = tk.StringVar(value="GPU: 0.0% | VRAM: 0.0 / 8.0 GB")
        self.folder_summary_var = tk.StringVar(value="폴더 스캔 중...")

        self._stop_requested = False
        self._last_output_dir = ""
        self._last_video_path = ""
        self._date_folders = []
        self._last_folder_snapshot = None
        self._tick_count = 0

        self.monitor = SystemResourceMonitor(history_len=60)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<F5>", lambda e: self.refresh_date_folders(manual=True))

        self._build_custom_indicator_icons()
        self._build_menubar()
        self._build_ui()
        self.refresh_date_folders(manual=False)
        self._show_splash_and_launch()

    def _build_custom_indicator_icons(self):
        """Create large, high-DPI anti-aliased 24x24 V-checkmark (✔) and radio button icons."""
        S = 96  # 4x supersampling -> 24x24 final

        # 1. Unchecked box
        im_off = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d_off = ImageDraw.Draw(im_off)
        d_off.rounded_rectangle([8, 8, S - 8, S - 8], radius=18, fill=(24, 24, 37, 255), outline=(108, 112, 134, 255), width=6)
        self._img_chk_off = ImageTk.PhotoImage(im_off.resize((22, 22), Image.Resampling.LANCZOS))

        # 2. Checked box with bold V-checkmark (✔)
        im_on = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d_on = ImageDraw.Draw(im_on)
        d_on.rounded_rectangle([8, 8, S - 8, S - 8], radius=18, fill=(137, 180, 250, 255), outline=(180, 190, 254, 255), width=5)
        # Draw crisp V-checkmark: left-mid -> bottom-center -> top-right
        pts = [(24, 50), (41, 67), (73, 29)]
        d_on.line(pts, fill=(17, 17, 27, 255), width=11, joint="curve")
        for px, py in pts:
            d_on.ellipse([px - 5, py - 5, px + 5, py + 5], fill=(17, 17, 27, 255))
        self._img_chk_on = ImageTk.PhotoImage(im_on.resize((22, 22), Image.Resampling.LANCZOS))

        # 3. Radio unselected
        im_roff = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d_roff = ImageDraw.Draw(im_roff)
        d_roff.ellipse([8, 8, S - 8, S - 8], fill=(24, 24, 37, 255), outline=(108, 112, 134, 255), width=6)
        self._img_rad_off = ImageTk.PhotoImage(im_roff.resize((22, 22), Image.Resampling.LANCZOS))

        # 4. Radio selected
        im_ron = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d_ron = ImageDraw.Draw(im_ron)
        d_ron.ellipse([8, 8, S - 8, S - 8], fill=(24, 24, 37, 255), outline=(137, 180, 250, 255), width=7)
        d_ron.ellipse([26, 26, S - 26, S - 26], fill=(137, 180, 250, 255))
        self._img_rad_on = ImageTk.PhotoImage(im_ron.resize((22, 22), Image.Resampling.LANCZOS))

    def _make_vcheck(self, parent, text: str, variable: tk.BooleanVar, bg: str = "#242436", fg: str = "#cdd6f4"):
        """Create a clean, large V-checkmark (✔) checkbox row that never uses 'X' or overlaps text."""
        row = tk.Frame(parent, bg=bg, cursor="hand2")
        icon_lbl = tk.Label(row, bg=bg, bd=0, highlightthickness=0, cursor="hand2")
        icon_lbl.pack(side=tk.LEFT, padx=(0, 8))
        txt_lbl = tk.Label(
            row,
            text=text,
            bg=bg,
            fg=fg,
            font=("맑은 고딕", 10),
            anchor="w",
            justify=tk.LEFT,
            cursor="hand2",
        )
        txt_lbl.pack(side=tk.LEFT, fill=tk.X)

        def _sync(*_):
            is_on = bool(variable.get())
            icon_lbl.configure(image=self._img_chk_on if is_on else self._img_chk_off)
            txt_lbl.configure(fg="#f5f7ff" if is_on else "#a6adc8")

        def _toggle(_event=None):
            variable.set(not bool(variable.get()))

        variable.trace_add("write", _sync)
        _sync()

        row.bind("<Button-1>", _toggle)
        icon_lbl.bind("<Button-1>", _toggle)
        txt_lbl.bind("<Button-1>", _toggle)
        return row

    def _make_vradio(self, parent, text: str, variable: tk.StringVar, value: str, bg: str = "#242436", fg: str = "#cdd6f4"):
        """Create a clean, large circular radio button row."""
        row = tk.Frame(parent, bg=bg, cursor="hand2")
        icon_lbl = tk.Label(row, bg=bg, bd=0, highlightthickness=0, cursor="hand2")
        icon_lbl.pack(side=tk.LEFT, padx=(0, 8))
        txt_lbl = tk.Label(
            row,
            text=text,
            bg=bg,
            fg=fg,
            font=("맑은 고딕", 10),
            anchor="w",
            justify=tk.LEFT,
            cursor="hand2",
        )
        txt_lbl.pack(side=tk.LEFT, fill=tk.X)

        def _sync(*_):
            is_on = variable.get() == value
            icon_lbl.configure(image=self._img_rad_on if is_on else self._img_rad_off)
            txt_lbl.configure(
                fg="#f9e2af" if is_on else "#a6adc8",
                font=("맑은 고딕", 10, "bold" if is_on else "normal"),
            )

        def _select(_event=None):
            variable.set(value)

        variable.trace_add("write", _sync)
        _sync()

        row.bind("<Button-1>", _select)
        icon_lbl.bind("<Button-1>", _select)
        txt_lbl.bind("<Button-1>", _select)
        return row

    def _make_dark_entry(self, parent, textvariable: tk.StringVar, width: int = 24):
        """Create a sleek dark-themed text entry with subtle border (no jarring white box)."""
        border = tk.Frame(parent, bg="#45475a", padx=1, pady=1)
        ent = tk.Entry(
            border,
            textvariable=textvariable,
            font=("맑은 고딕", 10),
            bg="#181825",
            fg="#f5f7ff",
            insertbackground="#89b4fa",
            selectbackground="#89b4fa",
            selectforeground="#11111b",
            relief=tk.FLAT,
            bd=5,
            width=width,
        )
        ent.pack(fill=tk.X, expand=True)
        return border, ent

    def _build_menubar(self):
        menubar = tk.Menu(self, font=("맑은 고딕", 10))

        # 1. 폴더 및 결과물 열기 메뉴
        folder_menu = tk.Menu(menubar, tearoff=0, font=("맑은 고딕", 10))
        folder_menu.add_command(label="🔄 폴더 및 영상/사진 개수 새로고침 (F5)", command=lambda: self.refresh_date_folders(manual=True))
        folder_menu.add_separator()
        folder_menu.add_command(label="📂 편집 결과 폴더 열기", command=self.open_output_folder)
        folder_menu.add_command(label="📂 원본 작업 폴더 열기", command=self.open_source_folder)
        folder_menu.add_separator()
        folder_menu.add_command(label="▶️ 방금 완성된 영상 바로 재생", command=self.play_last_video)
        folder_menu.add_separator()
        folder_menu.add_command(label="작업 폴더 변경...", command=self.browse_folder)
        folder_menu.add_separator()
        folder_menu.add_command(label="종료 (Exit)", command=self._on_close)
        menubar.add_cascade(label="📂 폴더 및 결과물 열기(F)", menu=folder_menu)

        # 2. 유튜브 스타일 프리셋 메뉴
        style_menu = tk.Menu(menubar, tearoff=0, font=("맑은 고딕", 10))
        for k in self.all_presets.keys():
            style_menu.add_command(label=k, command=lambda preset_name=k: self._select_preset_from_menu(preset_name))
        style_menu.add_separator()
        style_menu.add_command(label="🔍 로컬 레퍼런스 영상으로 스타일 학습...", command=self.browse_reference_video)
        menubar.add_cascade(label="🎨 편집 스타일 프리셋(S)", menu=style_menu)

        self.config(menu=menubar)

    def _select_preset_from_menu(self, preset_name: str):
        self.style_preset_var.set(preset_name)
        self._on_preset_changed()

    def _show_splash_and_launch(self):
        splash = tk.Toplevel(self)
        splash.overrideredirect(True)
        splash.attributes("-topmost", True)
        sw, sh = 820, 460
        scr_w = self.winfo_screenwidth()
        scr_h = self.winfo_screenheight()
        x = max(0, (scr_w - sw) // 2)
        y = max(0, (scr_h - sh) // 2)
        splash.geometry(f"{sw}x{sh}+{x}+{y}")

        canvas = tk.Canvas(splash, width=sw, height=sh, highlightthickness=0, bg="#0e0c1c")
        canvas.pack(fill=tk.BOTH, expand=True)

        pil_img = create_premiere_splash_image(sw, sh)
        self._splash_tk_img = ImageTk.PhotoImage(pil_img)
        canvas.create_image(0, 0, anchor="nw", image=self._splash_tk_img)

        bar_x0, bar_y0, bar_x1, bar_y1 = 54, 412, sw - 54, 422
        canvas.create_rectangle(bar_x0, bar_y0, bar_x1, bar_y1, fill="#242038", outline="")
        prog_rect = canvas.create_rectangle(bar_x0, bar_y0, bar_x0 + 10, bar_y1, fill="#cba6f7", outline="")
        status_item = canvas.create_text(
            54,
            390,
            anchor="w",
            text="AI 비전/모션 엔진 및 FFmpeg 파이프라인 초기화 중...",
            fill="#cdd6f4",
            font=("맑은 고딕", 10, "bold"),
        )

        steps = [
            (0.25, "AI 비전/모션 분석 모듈 및 OpenCV 얼굴/샤프니스 검출기 로드 중..."),
            (0.55, "유튜브 편집 스타일 분석기(yt-dlp) 및 컬러 그레이딩 프리셋 로드 중..."),
            (0.85, "NVIDIA GeForce RTX CUDA & HEVC NVENC 하드웨어 가속기 준비 중..."),
            (1.00, "AutoCut Studio 2026 워크스페이스 실행 완료!"),
        ]

        def _animate_step(idx=0):
            if idx < len(steps):
                ratio, msg = steps[idx]
                cur_x = bar_x0 + int((bar_x1 - bar_x0) * ratio)
                canvas.coords(prog_rect, bar_x0, bar_y0, cur_x, bar_y1)
                canvas.itemconfig(status_item, text=msg)
                self.after(420, lambda: _animate_step(idx + 1))
            else:
                splash.destroy()
                self.deiconify()
                try:
                    self.state("zoomed")
                except Exception:
                    pass
                try:
                    self.lift()
                    self.attributes("-topmost", True)
                    self.focus_force()
                    self.after(300, lambda: self.attributes("-topmost", False))
                except Exception:
                    pass
                self._tick_monitor()

        self.after(150, lambda: _animate_step(0))

    def _on_close(self):
        self.monitor.stop()
        self.destroy()

    def _build_ui(self):
        f_family = "맑은 고딕"
        bg_main = "#181825"
        bg_card = "#242436"

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=bg_main)
        style.configure("Card.TFrame", background=bg_card)
        style.configure("TLabel", background=bg_main, foreground="#cdd6f4", font=(f_family, 10))
        style.configure("Card.TLabel", background=bg_card, foreground="#cdd6f4", font=(f_family, 10))
        style.configure("FormLabel.TLabel", background=bg_card, foreground="#b4befe", font=(f_family, 10, "bold"))
        style.configure("Desc.TLabel", background=bg_card, foreground="#a6e3a1", font=(f_family, 9))
        style.configure("Header.TLabel", background=bg_main, foreground="#89b4fa", font=(f_family, 14, "bold"))
        style.configure("SubHeader.TLabel", background=bg_card, foreground="#f9e2af", font=(f_family, 11, "bold"))
        style.configure("Status.TLabel", background=bg_main, foreground="#a6e3a1", font=(f_family, 10, "bold"))
        style.configure("CPU.TLabel", background=bg_card, foreground="#89b4fa", font=(f_family, 9, "bold"))
        style.configure("GPU.TLabel", background=bg_card, foreground="#a6e3a1", font=(f_family, 9, "bold"))

        # Dark-themed Combobox (no bright white box)
        style.configure(
            "TCombobox",
            fieldbackground="#181825",
            background="#313244",
            foreground="#f5f7ff",
            arrowcolor="#89b4fa",
            bordercolor="#45475a",
            lightcolor="#45475a",
            darkcolor="#45475a",
            padding=5,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", "#181825"), ("!disabled", "#181825")],
            foreground=[("readonly", "#f5f7ff"), ("!disabled", "#f5f7ff")],
            selectbackground=[("readonly", "#181825")],
            selectforeground=[("readonly", "#f5f7ff")],
            background=[("active", "#45475a")],
        )

        style.configure(
            "TButton",
            font=(f_family, 10, "bold"),
            padding=(12, 6),
            background="#313244",
            foreground="#cdd6f4",
            bordercolor="#45475a",
        )
        style.map("TButton", background=[("active", "#45475a"), ("disabled", "#1e1e2e")])
        style.configure(
            "Folder.TButton",
            font=(f_family, 10, "bold"),
            padding=(12, 6),
            background="#f9e2af",
            foreground="#11111b",
        )
        style.map("Folder.TButton", background=[("active", "#f5e0dc")])
        style.configure(
            "Accent.TButton",
            font=(f_family, 11, "bold"),
            padding=(18, 9),
            background="#89b4fa",
            foreground="#11111b",
        )
        style.map("Accent.TButton", background=[("active", "#b4befe"), ("disabled", "#45475a")])
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor="#11111b",
            background="#a6e3a1",
            bordercolor="#313244",
            lightcolor="#a6e3a1",
            darkcolor="#a6e3a1",
            thickness=14,
        )

        main = ttk.Frame(self, padding=(18, 12))
        main.pack(fill=tk.BOTH, expand=True)

        # ── Top Header & Folder Bar ──────────────────────────────────────────
        header_row = ttk.Frame(main)
        header_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            header_row,
            text="🎬 AutoCut Studio 2026",
            style="Header.TLabel",
        ).pack(side=tk.LEFT)
        ttk.Label(
            header_row,
            text="  |  가족·여행·주행 AI 자동 컷편집 & 자막 스튜디오",
            font=(f_family, 11),
            foreground="#a6adc8",
            background=bg_main,
        ).pack(side=tk.LEFT)
        ttk.Label(
            header_row,
            text="★ 윰탱해윤해진이네 에디션",
            font=(f_family, 10, "bold"),
            foreground="#f9e2af",
            background=bg_main,
        ).pack(side=tk.RIGHT)
        ttk.Label(
            header_row,
            text=f"v{AUTOCUT_VERSION}   ",
            font=(f_family, 10, "bold"),
            foreground="#a6e3a1",
            background=bg_main,
        ).pack(side=tk.RIGHT)

        ttk.Label(
            main,
            text=(
                f"🤖 AI 엔진: {DEFAULT_AI_MODELS['stt']}  ·  CLIP Aesthetic (LAION V2.5)  ·  "
                f"{DEFAULT_AI_MODELS['shot_detector']}  ·  {DEFAULT_AI_MODELS['vlm']}"
            ),
            font=(f_family, 9),
            foreground="#7f849c",
            background=bg_main,
        ).pack(fill=tk.X, anchor="w", pady=(0, 6))

        folder_card = tk.Frame(main, bg=bg_card, highlightthickness=1, highlightbackground="#313244", padx=14, pady=9)
        folder_card.pack(fill=tk.X, pady=(0, 12))
        ttk.Label(folder_card, text="📂 작업 폴더", style="FormLabel.TLabel").pack(side=tk.LEFT, padx=(0, 10))
        dir_border, dir_ent = self._make_dark_entry(folder_card, self.target_dir, width=40)
        dir_border.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        dir_ent.bind("<Return>", lambda e: self.refresh_date_folders(manual=True))
        ttk.Button(folder_card, text="폴더 찾기...", command=self.browse_folder).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            folder_card,
            text="🔄 새로고침 (F5)",
            command=lambda: self.refresh_date_folders(manual=True),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(folder_card, text="📅 날짜별 자동 분류", command=self.run_classification).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(folder_card, text="📂 원본 폴더 열기", command=self.open_source_folder).pack(side=tk.LEFT)

        # ── Center 3-Column Studio Grid ──────────────────────────────────────
        grid_area = ttk.Frame(main)
        grid_area.pack(fill=tk.BOTH, expand=False, pady=(0, 12))
        grid_area.columnconfigure(0, weight=10, uniform="col")
        grid_area.columnconfigure(1, weight=11, uniform="col")
        grid_area.columnconfigure(2, weight=11, uniform="col")
        grid_area.rowconfigure(0, weight=1)

        # =====================================================================
        # COLUMN 1: 날짜별 폴더 목록
        # =====================================================================
        col1 = tk.Frame(grid_area, bg=bg_card, highlightthickness=1, highlightbackground="#313244", padx=16, pady=14)
        col1.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        c1_head = ttk.Frame(col1, style="Card.TFrame")
        c1_head.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(c1_head, text="📅 1. 촬영 날짜 폴더 선택", style="SubHeader.TLabel").pack(side=tk.LEFT)
        ttk.Button(
            c1_head,
            text="🔄 개수 새로고침",
            style="Folder.TButton",
            command=lambda: self.refresh_date_folders(manual=True),
        ).pack(side=tk.RIGHT)

        ttk.Label(
            col1,
            textvariable=self.folder_summary_var,
            style="Desc.TLabel",
        ).pack(anchor="w", pady=(0, 8))

        list_container = tk.Frame(col1, bg="#181825", highlightthickness=1, highlightbackground="#45475a")
        list_container.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        scrollbar = ttk.Scrollbar(list_container, orient=tk.VERTICAL)
        self.date_listbox = tk.Listbox(
            list_container,
            selectmode=tk.EXTENDED,
            yscrollcommand=scrollbar.set,
            bg="#181825",
            fg="#cdd6f4",
            selectbackground="#89b4fa",
            selectforeground="#11111b",
            font=(f_family, 10),
            height=9,
            relief=tk.FLAT,
            bd=6,
            highlightthickness=0,
            activestyle="none",
        )
        scrollbar.config(command=self.date_listbox.yview)
        self.date_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        c1_btns = ttk.Frame(col1, style="Card.TFrame")
        c1_btns.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(c1_btns, text="첫째 날만 선택", command=self.select_first_day).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(c1_btns, text="전체 날짜 선택", command=self.select_all_days).pack(side=tk.LEFT)

        self._make_vcheck(
            col1,
            "폴더 내 사진(JPG/PNG)도 촬영 시간순으로 함께 배치",
            self.include_photos,
            bg=bg_card,
        ).pack(anchor="w", pady=(2, 0))

        # =====================================================================
        # COLUMN 2: 편집 모드(여행/대화/하이라이트/드라이브) · 인트로 5종 · 자동 편집 옵션
        # =====================================================================
        col2 = tk.Frame(grid_area, bg=bg_card, highlightthickness=1, highlightbackground="#313244", padx=16, pady=12)
        col2.grid(row=0, column=1, sticky="nsew", padx=8)

        ttk.Label(col2, text="✂️ 2. 영상 편집 모드 & 인트로·자막 옵션", style="SubHeader.TLabel").pack(anchor="w", pady=(0, 6))

        mode_box = tk.Frame(col2, bg="#1e1e2e", highlightthickness=1, highlightbackground="#313244", padx=12, pady=6)
        mode_box.pack(fill=tk.X, pady=(0, 8))
        self._make_vradio(
            mode_box,
            "여행 (기본값 — 풍경·인물 베스트컷 + 대화 조화)",
            self.edit_mode,
            "travel",
            bg="#1e1e2e",
        ).pack(anchor="w", pady=1)
        self._make_vradio(
            mode_box,
            "그냥 대화 (일상 토크 중심 — 말소리 위주 촘촘 연결)",
            self.edit_mode,
            "talk",
            bg="#1e1e2e",
        ).pack(anchor="w", pady=1)
        self._make_vradio(
            mode_box,
            "하이라이트 요약 (핵심 대화 + 베스트 장면 빠른 템포)",
            self.edit_mode,
            "highlight",
            bg="#1e1e2e",
        ).pack(anchor="w", pady=1)
        self._make_vradio(
            mode_box,
            "드라이브·풍경 감상 (이동·풍경 롱테이크 비중 확대)",
            self.edit_mode,
            "drive",
            bg="#1e1e2e",
        ).pack(anchor="w", pady=1)

        # Clean 2-column grid for Intro Style (5 types + random), Intro Title & Top-Right Logo
        form_brand = ttk.Frame(col2, style="Card.TFrame")
        form_brand.pack(fill=tk.X, pady=(0, 8))
        form_brand.columnconfigure(1, weight=1)

        ttk.Label(form_brand, text="인트로 종류 선택", style="FormLabel.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 14), pady=4
        )
        self.intro_style_combo = ttk.Combobox(
            form_brand,
            textvariable=self.intro_style_var,
            values=list(INTRO_STYLE_PRESETS.keys()),
            state="readonly",
            font=(f_family, 10),
        )
        self.intro_style_combo.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(form_brand, text="인트로 영상 제목", style="FormLabel.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 14), pady=4
        )
        t_border, _ = self._make_dark_entry(form_brand, self.intro_title)
        t_border.grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(form_brand, text="우측 상단 로고", style="FormLabel.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 14), pady=4
        )
        l_border, _ = self._make_dark_entry(form_brand, self.logo_text)
        l_border.grid(row=2, column=1, sticky="ew", pady=4)

        tk.Frame(col2, bg="#313244", height=1).pack(fill=tk.X, pady=(2, 6))

        # Large V-Checkboxes with clean spacing
        chk_list = [
            ("베스트 샷 3초 인트로 생성 (선택한 인트로 디자인 적용)", self.enable_intro),
            ("좌측 상단 촬영 날짜 · 우측 상단 로고 상시 표시", self.enable_hud),
            ("클립 간 랜덤 트랜지션 효과 + 마지막 페이드아웃", self.enable_transitions),
            ("잔잔한 무료 배경음악(BGM) 자동 볼륨 조절 믹싱", self.enable_bgm),
            ("주행·바람 노이즈 제거 및 AI 한국어 음성 자막", self.denoise_audio),
            ("자막 완성본 + 필모라(Filmora)용 무자막 동시 생성", self.burn_subtitles),
            ("편집 완료 후 결과 폴더 자동으로 열기", self.auto_open_folder_on_done),
        ]
        for lbl_txt, var_obj in chk_list:
            self._make_vcheck(col2, lbl_txt, var_obj, bg=bg_card).pack(anchor="w", pady=2)

        # =====================================================================
        # COLUMN 3: 출력 인코딩 화질(4K 60fps HEVC 25Mbps) · DJI D-Log · 유튜브 스타일
        # =====================================================================
        col3 = tk.Frame(grid_area, bg=bg_card, highlightthickness=1, highlightbackground="#313244", padx=16, pady=12)
        col3.grid(row=0, column=2, sticky="nsew", padx=(8, 0))

        ttk.Label(col3, text="🎞️ 3. 출력 화질(4K 60fps HEVC) & DJI D-Log 설정", style="SubHeader.TLabel").pack(
            anchor="w", pady=(0, 6)
        )

        enc_grid = ttk.Frame(col3, style="Card.TFrame")
        enc_grid.pack(fill=tk.X, pady=(0, 8))
        enc_grid.columnconfigure(1, weight=1)

        ttk.Label(enc_grid, text="압축 코덱", style="FormLabel.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 14), pady=4
        )
        self.codec_combo = ttk.Combobox(
            enc_grid,
            textvariable=self.out_codec_var,
            values=["HEVC (H.265 - hevc_nvenc 권장)", "H.264 (AVC - h264_nvenc 호환)"],
            state="readonly",
            font=(f_family, 10),
        )
        self.codec_combo.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(enc_grid, text="출력 해상도", style="FormLabel.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 14), pady=4
        )
        self.res_combo = ttk.Combobox(
            enc_grid,
            textvariable=self.out_resolution_var,
            values=["3840x2160 (4K UHD)", "2560x1440 (QHD)", "1920x1080 (FHD)", "1280x720 (HD)"],
            state="readonly",
            font=(f_family, 10),
        )
        self.res_combo.grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(enc_grid, text="프레임 레이트", style="FormLabel.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 14), pady=4
        )
        self.fps_combo = ttk.Combobox(
            enc_grid,
            textvariable=self.out_fps_var,
            values=["60 fps", "30 fps", "24 fps"],
            state="readonly",
            font=(f_family, 10),
        )
        self.fps_combo.grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Label(enc_grid, text="화질 (bps)", style="FormLabel.TLabel").grid(
            row=3, column=0, sticky="w", padx=(0, 14), pady=4
        )
        bps_row = ttk.Frame(enc_grid, style="Card.TFrame")
        bps_row.grid(row=3, column=1, sticky="ew", pady=4)
        self.bps_combo = ttk.Combobox(
            bps_row,
            textvariable=self.out_bitrate_bps_var,
            values=["25,000,000", "35,000,000", "50,000,000", "80,000,000", "20,000,000", "12,000,000"],
            font=(f_family, 10),
        )
        self.bps_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(bps_row, text=" bps (25Mbps 기본)", style="Desc.TLabel").pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(enc_grid, text="DJI / D-Log 색감", style="FormLabel.TLabel").grid(
            row=4, column=0, sticky="w", padx=(0, 14), pady=4
        )
        self.dlog_combo = ttk.Combobox(
            enc_grid,
            textvariable=self.color_profile_var,
            values=list(COLOR_PROFILE_PRESETS.keys()),
            state="readonly",
            font=(f_family, 10),
        )
        self.dlog_combo.grid(row=4, column=1, sticky="ew", pady=4)

        tk.Frame(col3, bg="#313244", height=1).pack(fill=tk.X, pady=(4, 8))

        ttk.Label(col3, text="🎨 편집 스타일 프리셋 & 유튜브 학습", style="SubHeader.TLabel").pack(
            anchor="w", pady=(0, 5)
        )
        self.preset_combo = ttk.Combobox(
            col3,
            textvariable=self.style_preset_var,
            values=list(self.all_presets.keys()),
            state="readonly",
            font=(f_family, 10),
        )
        self.preset_combo.pack(fill=tk.X, pady=(0, 5))
        self.preset_combo.bind("<<ComboboxSelected>>", self._on_preset_changed)

        tk.Label(
            col3,
            textvariable=self.style_desc_var,
            bg="#1e1e2e",
            fg="#a6e3a1",
            font=(f_family, 9),
            anchor="w",
            justify=tk.LEFT,
            wraplength=460,
            padx=10,
            pady=5,
        ).pack(fill=tk.X, pady=(0, 8))

        ttk.Label(col3, text="유튜브 레퍼런스 URL 또는 영상 경로", style="FormLabel.TLabel").pack(
            anchor="w", pady=(0, 4)
        )
        yt_border, _ = self._make_dark_entry(col3, self.yt_url_var)
        yt_border.pack(fill=tk.X, pady=(0, 6))

        yt_btns = ttk.Frame(col3, style="Card.TFrame")
        yt_btns.pack(fill=tk.X)
        self.yt_analyze_btn = ttk.Button(
            yt_btns,
            text="🔍 유튜브 URL 스타일 학습",
            command=self.start_youtube_style_analysis,
        )
        self.yt_analyze_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(yt_btns, text="샘플 파일 선택...", command=self.browse_reference_video).pack(side=tk.LEFT)

        # ── Bottom Split Area: Monitor Graph (Left) + Real-time Log (Right) ──
        bot_split = ttk.Frame(main)
        bot_split.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        bot_split.columnconfigure(0, weight=4)
        bot_split.columnconfigure(1, weight=6)
        bot_split.rowconfigure(0, weight=1)

        mon_card = tk.Frame(bot_split, bg=bg_card, highlightthickness=1, highlightbackground="#313244", padx=14, pady=10)
        mon_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        mon_header = ttk.Frame(mon_card, style="Card.TFrame")
        mon_header.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(mon_header, text="📊 하드웨어 가속 모니터", style="SubHeader.TLabel").pack(side=tk.LEFT)
        ttk.Label(mon_header, textvariable=self.gpu_label_var, style="GPU.TLabel").pack(side=tk.RIGHT, padx=(12, 0))
        ttk.Label(mon_header, textvariable=self.cpu_label_var, style="CPU.TLabel").pack(side=tk.RIGHT)

        self.graph_canvas = tk.Canvas(
            mon_card,
            height=110,
            bg="#14141f",
            highlightthickness=1,
            highlightbackground="#45475a",
        )
        self.graph_canvas.pack(fill=tk.BOTH, expand=True)

        log_card = tk.Frame(bot_split, bg=bg_card, highlightthickness=1, highlightbackground="#313244", padx=14, pady=10)
        log_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        log_header = ttk.Frame(log_card, style="Card.TFrame")
        log_header.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(log_header, text="📋 실시간 분석 및 편집 로그", style="SubHeader.TLabel").pack(side=tk.LEFT)

        log_frame = tk.Frame(log_card, bg="#11111b", highlightthickness=1, highlightbackground="#45475a")
        log_frame.pack(fill=tk.BOTH, expand=True)
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL)
        self.log_text = tk.Text(
            log_frame,
            height=6,
            yscrollcommand=log_scroll.set,
            bg="#11111b",
            fg="#a6e3a1",
            font=(f_family, 10),
            state=tk.DISABLED,
            relief=tk.FLAT,
            padx=10,
            pady=6,
        )
        log_scroll.config(command=self.log_text.yview)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Footer Progress Bar & Action Buttons ─────────────────────────────
        footer = ttk.Frame(main)
        footer.pack(fill=tk.X)

        prog_row = ttk.Frame(footer)
        prog_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(prog_row, textvariable=self.status_var, style="Status.TLabel").pack(anchor="w", pady=(0, 4))
        self.progress_bar = ttk.Progressbar(
            prog_row,
            variable=self.progress_var,
            maximum=100.0,
            style="Horizontal.TProgressbar",
        )
        self.progress_bar.pack(fill=tk.X)

        btn_frame = ttk.Frame(footer)
        btn_frame.pack(fill=tk.X)
        self.start_btn = ttk.Button(
            btn_frame,
            text="🚀 선택한 날짜 자동 컷편집 & 자막 생성 시작",
            style="Accent.TButton",
            command=self.start_auto_edit,
        )
        self.start_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        self.stop_btn = ttk.Button(btn_frame, text="⏹ 중단", command=self.request_stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(
            btn_frame,
            text="📂 편집 결과 폴더 열기",
            style="Folder.TButton",
            command=self.open_output_folder,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="▶️ 완성 영상 재생", command=self.play_last_video).pack(side=tk.LEFT)

    def _on_preset_changed(self, event=None):
        self.all_presets = get_all_style_presets()
        sel = self.style_preset_var.get()
        info = self.all_presets.get(sel, {})
        self.style_desc_var.set(info.get("desc", ""))
        self.log(f"🎨 편집 스타일 프리셋 선택됨: {sel}")

    def browse_reference_video(self):
        fp = filedialog.askopenfilename(
            title="스타일을 학습할 레퍼런스 영상 파일 선택",
            filetypes=[("Video files", "*.mp4 *.mkv *.mov *.avi *.webm"), ("All files", "*.*")],
        )
        if fp:
            self.yt_url_var.set(fp)
            self.start_youtube_style_analysis()

    def start_youtube_style_analysis(self):
        url_or_path = self.yt_url_var.get().strip()
        if not url_or_path:
            messagebox.showinfo(
                "유튜브 URL 입력",
                "학습하고 싶은 유튜브 영상 URL 주소를 입력하거나 [샘플 영상 파일 선택...]을 눌러주세요.",
            )
            return

        self.yt_analyze_btn.configure(state=tk.DISABLED)

        def _worker():
            try:
                preset_key, preset_data = analyze_youtube_style(
                    url_or_path,
                    log_fn=lambda m: self.after(0, self.log, m),
                    progress_fn=lambda p, s: self.after(0, self.update_progress, p, s),
                )

                def _apply_ui():
                    self.all_presets = get_all_style_presets()
                    self.preset_combo.configure(values=list(self.all_presets.keys()))
                    self.style_preset_var.set(preset_key)
                    self.style_desc_var.set(preset_data.get("desc", ""))
                    self._build_menubar()
                    messagebox.showinfo(
                        "유튜브 스타일 학습 완료",
                        f"레퍼런스 영상 스타일 분석이 완료되어 프리셋으로 등록되었습니다!\n\n"
                        f"• 프리셋 이름: {preset_key}\n"
                        f"• 분석 결과: {preset_data.get('desc', '')}",
                    )

                self.after(0, _apply_ui)
            except Exception as e:
                self.after(0, self.log, f"❌ 유튜브 스타일 분석 오류: {e}")
                self.after(0, lambda: messagebox.showerror("스타일 분석 실패", f"유튜브/영상 스타일 분석 중 오류가 발생했습니다:\n{e}"))
            finally:
                self.after(0, lambda: self.yt_analyze_btn.configure(state=tk.NORMAL))

        threading.Thread(target=_worker, daemon=True).start()

    def _tick_monitor(self):
        if not self.winfo_exists():
            return
        self.monitor.sample_all()
        cpu = self.monitor.cpu_pct
        gpu = self.monitor.gpu_pct
        v_used = self.monitor.vram_used_mb / 1024.0
        v_tot = self.monitor.vram_total_mb / 1024.0

        self.cpu_label_var.set(f"● CPU 사용률: {cpu:4.1f}%")
        self.gpu_label_var.set(f"● GPU ({self.monitor.gpu_name}): {gpu:4.1f}%  |  VRAM: {v_used:.1f} / {v_tot:.1f} GB")

        self._draw_resource_graph()

        # Auto-detect file additions/removals in folders every 2 seconds
        self._tick_count += 1
        if self._tick_count % 2 == 0:
            try:
                base = self.target_dir.get().strip()
                if os.path.isdir(base):
                    snap = self._compute_folder_snapshot(base)
                    if self._last_folder_snapshot is not None and snap != self._last_folder_snapshot:
                        self.refresh_date_folders(manual=False, auto_triggered=True)
            except Exception:
                pass

        self.after(1000, self._tick_monitor)

    def _compute_folder_snapshot(self, base: str):
        if not os.path.isdir(base):
            return ()
        items = []
        subdirs = sorted(
            d for d in os.listdir(base)
            if os.path.isdir(os.path.join(base, d)) and not d.startswith("편집결과") and not d.startswith(".")
        )
        for d in subdirs:
            full_d = os.path.join(base, d)
            v_files, p_files = scan_media_files_in_folder(full_d)
            if v_files or p_files:
                items.append((d, len(v_files), len(p_files)))
        top_files = [f for f in os.listdir(base) if os.path.isfile(os.path.join(base, f))]
        top_v = sum(1 for f in top_files if os.path.splitext(f)[1].lower() in VIDEO_EXTS)
        top_p = sum(1 for f in top_files if os.path.splitext(f)[1].lower() in PHOTO_EXTS)
        items.append(("__ROOT__", top_v, top_p))
        return tuple(items)

    def _draw_resource_graph(self):
        c = self.graph_canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 50 or h < 30:
            return

        pad_l, pad_r, pad_t, pad_b = 46, 14, 10, 10
        plot_w = max(10, w - pad_l - pad_r)
        plot_h = max(10, h - pad_t - pad_b)

        for pct in (100, 50, 0):
            y = pad_t + plot_h * (1.0 - pct / 100.0)
            c.create_line(pad_l, y, w - pad_r, y, fill="#2a2b3d", dash=(3, 3))
            c.create_text(pad_l - 8, y, text=f"{pct}%", anchor="e", fill="#7f849c", font=("맑은 고딕", 8))

        n = self.monitor.history_len
        step_x = plot_w / max(1, n - 1)

        def draw_series(data, color, width=2):
            pts = []
            for i, val in enumerate(data):
                x = pad_l + i * step_x
                y = pad_t + plot_h * (1.0 - max(0.0, min(100.0, val)) / 100.0)
                pts.extend([x, y])
            if len(pts) >= 4:
                c.create_line(*pts, fill=color, width=width, smooth=True)

        draw_series(self.monitor.cpu_history, "#89b4fa", width=2)
        draw_series(self.monitor.gpu_history, "#a6e3a1", width=2)

    def log(self, msg: str):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def update_progress(self, pct: float, status_msg: str):
        self.progress_var.set(pct)
        self.status_var.set(f"[{pct:5.1f}%] {status_msg}")

    def browse_folder(self):
        selected = filedialog.askdirectory(initialdir=self.target_dir.get())
        if selected:
            self.target_dir.set(selected)
            self.refresh_date_folders(manual=True)

    def refresh_date_folders(self, manual: bool = False, auto_triggered: bool = False):
        prev_sel_indices = self.date_listbox.curselection()
        prev_sel_paths = {
            self._date_folders[i]
            for i in prev_sel_indices
            if 0 <= i < len(self._date_folders)
        }

        self.date_listbox.delete(0, tk.END)
        self._date_folders = []
        base = self.target_dir.get().strip()
        if not os.path.isdir(base):
            self.folder_summary_var.set("⚠️ 유효한 작업 폴더를 선택해주세요.")
            self._last_folder_snapshot = ()
            return

        subdirs = sorted(
            d for d in os.listdir(base)
            if os.path.isdir(os.path.join(base, d)) and not d.startswith("편집결과") and not d.startswith(".")
        )
        total_v = 0
        total_p = 0
        folder_summaries = []

        for d in subdirs:
            full_d = os.path.join(base, d)
            v_files, p_files = scan_media_files_in_folder(full_d)
            v_cnt = len(v_files)
            p_cnt = len(p_files)
            if v_cnt > 0 or p_cnt > 0:
                self._date_folders.append(full_d)
                total_v += v_cnt
                total_p += p_cnt
                folder_summaries.append(f"{d}(영상 {v_cnt}·사진 {p_cnt})")
                self.date_listbox.insert(tk.END, f"📁 {d}   (영상 {v_cnt}개 / 사진 {p_cnt}개 — 시간순 자동배치)")

        top_files = [f for f in os.listdir(base) if os.path.isfile(os.path.join(base, f))]
        top_v = sum(1 for f in top_files if os.path.splitext(f)[1].lower() in VIDEO_EXTS)
        top_p = sum(1 for f in top_files if os.path.splitext(f)[1].lower() in PHOTO_EXTS)

        self._last_folder_snapshot = self._compute_folder_snapshot(base)

        if self._date_folders:
            self.folder_summary_var.set(
                f"✓ 총 {len(self._date_folders)}개 날짜 폴더  |  전체 영상 {total_v}개 · 전체 사진 {total_p}개  |  촬영 시각(EXIF/메타데이터) 시간순 자동 배치"
            )
        else:
            self.folder_summary_var.set("ℹ️ 날짜별 하위 폴더가 없습니다. [1. 날짜별 자동 분류]를 실행하거나 폴더를 확인해주세요.")

        if top_v > 0 or top_p > 0:
            self.log(f"ℹ️ 최상위 폴더에 미분류 미디어(영상 {top_v}개, 사진 {top_p}개)가 있습니다. [1. 날짜별 자동 분류]를 눌러주세요.")

        # Restore previous selection if possible, otherwise select first item
        restored = False
        if prev_sel_paths:
            for idx, p in enumerate(self._date_folders):
                if p in prev_sel_paths:
                    self.date_listbox.selection_set(idx)
                    restored = True
        if not restored and self.date_listbox.size() > 0:
            self.date_listbox.selection_set(0)

        if manual:
            detail_str = ", ".join(folder_summaries) if folder_summaries else "미디어 없음"
            self.log(f"🔄 [새로고침 완료] 총 영상 {total_v}개 / 사진 {total_p}개 확인됨 ({detail_str})")
            self.status_var.set(f"새로고침 완료 — 총 {len(self._date_folders)}개 날짜 폴더 (영상 {total_v}개, 사진 {total_p}개)")
        elif auto_triggered:
            detail_str = ", ".join(folder_summaries) if folder_summaries else "미디어 없음"
            self.log(f"🔄 [폴더 변경 자동 감지] 파일 추가/삭제가 반영되어 개수가 갱신되었습니다 -> {detail_str}")
            self.status_var.set(f"폴더 변경 감지됨 — 총 영상 {total_v}개, 사진 {total_p}개로 자동 새로고침 완료!")

    def select_first_day(self):
        self.date_listbox.selection_clear(0, tk.END)
        if self.date_listbox.size() > 0:
            self.date_listbox.selection_set(0)

    def select_all_days(self):
        if self.date_listbox.size() > 0:
            self.date_listbox.selection_set(0, tk.END)

    def run_classification(self):
        base = self.target_dir.get().strip()
        if not os.path.isdir(base):
            messagebox.showerror("폴더 오류", "유효한 작업 폴더를 선택해주세요.")
            return
        self.log(f"📂 [{base}] 날짜별 자동 분류 실행 중...")
        grouped = classify_media_by_date(base, move_files=True, log_fn=self.log)
        if not grouped:
            self.log("✅ 이미 날짜별 하위 폴더로 모두 분류되어 있습니다.")
        self.refresh_date_folders()

    def request_stop(self):
        self._stop_requested = True
        self.status_var.set("중단 요청됨 — 현재 작업 단계가 끝나면 안전하게 멈춥니다...")

    def open_source_folder(self):
        base = self.target_dir.get().strip()
        if os.path.isdir(base):
            os.startfile(base)

    def open_output_folder(self):
        base = self.target_dir.get().strip()
        if self._last_output_dir and os.path.isdir(self._last_output_dir):
            os.startfile(self._last_output_dir)
            return
        # Find the most recent 편집결과_* folder if available
        if os.path.isdir(base):
            out_dirs = sorted(
                [
                    os.path.join(base, d)
                    for d in os.listdir(base)
                    if os.path.isdir(os.path.join(base, d)) and d.startswith("편집결과")
                ],
                key=lambda x: os.path.getmtime(x),
                reverse=True,
            )
            if out_dirs:
                os.startfile(out_dirs[0])
                return
            os.startfile(base)

    def play_last_video(self):
        if self._last_video_path and os.path.isfile(self._last_video_path):
            os.startfile(self._last_video_path)
        else:
            messagebox.showinfo("알림", "아직 생성된 완성 영상이 없거나 경로를 찾을 수 없습니다.")

    def _parse_encoding_specs(self):
        res_str = self.out_resolution_var.get().split()[0].strip()
        try:
            w_s, h_s = res_str.lower().split("x")
            out_w, out_h = int(w_s), int(h_s)
        except Exception:
            out_w, out_h = 3840, 2160

        fps_str = self.out_fps_var.get().split()[0].strip()
        try:
            out_fps = float(fps_str)
        except Exception:
            out_fps = 60.0

        bps_clean = (
            self.out_bitrate_bps_var.get()
            .replace(",", "")
            .replace("_", "")
            .replace("bps", "")
            .replace("BPS", "")
            .strip()
        )
        try:
            bitrate_bps = max(1_000_000, int(float(bps_clean)))
        except Exception:
            bitrate_bps = 25_000_000

        codec_raw = self.out_codec_var.get()
        v_codec = "hevc_nvenc" if ("HEVC" in codec_raw.upper() or "265" in codec_raw) else "h264_nvenc"
        return out_w, out_h, out_fps, bitrate_bps, v_codec

    def _show_completion_dialog(self, output_dir: str, orig_m: float, edit_m: float, preset_name: str):
        if self.auto_open_folder_on_done.get() and os.path.isdir(output_dir):
            os.startfile(output_dir)

        dlg = tk.Toplevel(self)
        dlg.title("🎉 자동 편집 완료 — 결과물 확인 및 폴더 열기")
        dlg.configure(bg="#28283d")
        dlg.transient(self)
        dlg.grab_set()

        dw, dh = 560, 310
        x = self.winfo_rootx() + (self.winfo_width() - dw) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dh) // 2
        dlg.geometry(f"{dw}x{dh}+{max(0, x)}+{max(0, y)}")

        frame = ttk.Frame(dlg, style="Card.TFrame", padding=20)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame,
            text="🎉 영상 자동 컷편집 및 자막 생성이 완료되었습니다!",
            font=("맑은 고딕", 13, "bold"),
            foreground="#a6e3a1",
            background="#28283d",
        ).pack(anchor="w", pady=(0, 12))

        info_text = (
            f"• 분량 요약: 원본 {orig_m:.1f}분  ➔  편집본 {edit_m:.1f}분\n"
            f"• 적용 스타일: {preset_name}\n"
            f"• 저장 경로: {output_dir}"
        )
        ttk.Label(
            frame,
            text=info_text,
            font=("맑은 고딕", 10),
            foreground="#cdd6f4",
            background="#28283d",
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 20))

        btn_row = ttk.Frame(frame, style="Card.TFrame")
        btn_row.pack(fill=tk.X, pady=(10, 0))

        ttk.Button(
            btn_row,
            text="📂 편집 결과 폴더 열기",
            style="Folder.TButton",
            command=lambda: (os.startfile(output_dir) if os.path.isdir(output_dir) else None),
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        ttk.Button(
            btn_row,
            text="▶️ 완성 영상 바로 재생",
            style="Accent.TButton",
            command=self.play_last_video,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        ttk.Button(btn_row, text="닫기", command=dlg.destroy).pack(side=tk.RIGHT)

    def start_auto_edit(self):
        sel = self.date_listbox.curselection()
        if not sel:
            messagebox.showinfo("날짜 선택", "왼쪽 목록에서 자동 편집할 날짜 폴더를 선택해주세요.")
            return

        selected_paths = [self._date_folders[i] for i in sel]
        self._stop_requested = False
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.progress_var.set(0.0)

        threading.Thread(
            target=self._worker_run,
            args=(selected_paths,),
            daemon=True,
        ).start()

    def _worker_run(self, selected_paths):
        base_dir = self.target_dir.get().strip()
        chosen_preset = self.style_preset_var.get()
        chosen_intro_style = INTRO_STYLE_PRESETS.get(self.intro_style_var.get(), "classic_frame")
        chosen_color_profile = self.color_profile_var.get()
        out_w, out_h, out_fps, bitrate_bps, v_codec = self._parse_encoding_specs()
        try:
            last_orig_m = 0.0
            last_edit_m = 0.0
            for idx, folder_path in enumerate(selected_paths, 1):
                date_name = os.path.basename(folder_path)
                out_dir = os.path.join(base_dir, f"편집결과_{date_name}")
                self._last_output_dir = out_dir

                self.after(0, self.log, f"\n==========================================")
                self.after(0, self.log, f"🎬 [{idx}/{len(selected_paths)}] {date_name} 자동 편집 시작")
                self.after(0, self.log, f"==========================================")

                summary = run_autocut_pipeline(
                    date_folder_path=folder_path,
                    output_dir=out_dir,
                    edit_mode=self.edit_mode.get(),
                    whisper_model_size=self.whisper_model.get(),
                    denoise_audio=self.denoise_audio.get(),
                    include_scene_captions=self.include_scene_captions.get(),
                    burn_subtitles=self.burn_subtitles.get(),
                    export_filmora_clean=self.export_filmora_clean.get(),
                    include_photos=self.include_photos.get(),
                    enable_intro=self.enable_intro.get(),
                    intro_title=self.intro_title.get(),
                    intro_style=chosen_intro_style,
                    logo_text=self.logo_text.get(),
                    enable_hud=self.enable_hud.get(),
                    enable_transitions=self.enable_transitions.get(),
                    enable_bgm=self.enable_bgm.get(),
                    custom_bgm_path=self.custom_bgm_path.get(),
                    style_preset_name=chosen_preset,
                    color_profile=chosen_color_profile,
                    out_width=out_w,
                    out_height=out_h,
                    out_fps=out_fps,
                    video_bitrate_bps=bitrate_bps,
                    video_codec=v_codec,
                    log_fn=lambda m: self.after(0, self.log, m),
                    progress_fn=lambda p, s: self.after(0, self.update_progress, p, s),
                    stop_check_fn=lambda: self._stop_requested,
                )

                self._last_video_path = summary.get("subbed_mp4") or summary.get("clean_mp4") or ""
                last_orig_m = summary["total_original_seconds"] / 60.0
                last_edit_m = (summary["total_edited_seconds"] + (3.0 if self.enable_intro.get() else 0.0)) / 60.0
                self.after(
                    0,
                    self.log,
                    f"🎉 [{date_name}] 편집 완료! (원본 {last_orig_m:.1f}분 -> 편집본 {last_edit_m:.1f}분)\n   출력 폴더: {out_dir}",
                )

            self.after(
                0,
                lambda: self._show_completion_dialog(
                    self._last_output_dir,
                    last_orig_m,
                    last_edit_m,
                    chosen_preset,
                ),
            )
        except Exception as e:
            self.after(0, self.log, f"❌ 작업 중단/오류: {e}")
            self.after(0, lambda: self.status_var.set(f"오류/중단: {e}"))
        finally:
            self.after(0, lambda: self.start_btn.configure(state=tk.NORMAL))
            self.after(0, lambda: self.stop_btn.configure(state=tk.DISABLED))


if __name__ == "__main__":
    app = AutoCutStudioApp()
    app.mainloop()
