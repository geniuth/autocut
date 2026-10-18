"""
Premiere-style clip timeline for AutoCut Studio:
- Shows every video/photo of a date folder as a thumbnail clip on one track (V1), in shooting-time order.
- Drag & drop (or Ctrl+←/→) to fix the order when cameras disagree on time (GoPro clock never set, UTC vs local, ...).
- Per-device time correction: enter the real time of one clip and every clip from that device shifts with it.
- The arranged order is saved to 편집결과_<date>/<date>_타임라인순서.json and handed to run_autocut_pipeline(manual_order=...).
"""

import os
import re
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
from PIL import Image, ImageTk

from autocut_engine import (
    scan_media_files_in_folder,
    probe_media_item,
    extract_media_thumbnail,
    apply_manual_clip_order,
)

DEVICE_COLORS = ["#89b4fa", "#a6e3a1", "#fab387", "#f5c2e7", "#94e2d5", "#f9e2af", "#cba6f7", "#eba0ac"]
SUSPECT_DAYS = 7.0  # clips this far from the folder's median shooting time get a ⚠ (likely wrong camera clock)

_META_CACHE: Dict[Tuple[str, float, int], Dict] = {}
_THUMB_CACHE: Dict[Tuple[str, float, int], Optional[Image.Image]] = {}


def _file_key(path: str) -> Tuple[str, float, int]:
    st = os.stat(path)
    return (path, st.st_mtime, st.st_size)


def _probe_cached(path: str) -> Dict:
    key = _file_key(path)
    if key not in _META_CACHE:
        _META_CACHE[key] = probe_media_item(path)
    return dict(_META_CACHE[key])


def _device_key(label: str) -> str:
    return re.sub(r"[\s_\-]+", "", (label or "").lower())


def _parse_dt(s: str) -> Optional[datetime]:
    s = (s or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:19] if fmt.endswith("%S") else s[:16], fmt)
        except ValueError:
            continue
    return None


def _mix(c1: str, c2: str, t: float) -> str:
    a = [int(c1[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{int(x + (y - x) * t):02x}" for x, y in zip(a, b))


def order_file_path(output_dir: str, date_name: str) -> str:
    return os.path.join(output_dir, f"{date_name}_타임라인순서.json")


def read_timeline_file(folder: str, output_dir: str) -> Optional[Dict]:
    """Raw saved timeline JSON for a date folder (order, device offsets, rotations), or None."""
    fp = order_file_path(output_dir, os.path.basename(os.path.normpath(folder)))
    if not os.path.isfile(fp):
        return None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_timeline_file(folder: str, output_dir: str, data: Optional[Dict]):
    """Write (or with data=None remove) a date folder's saved timeline."""
    fp = order_file_path(output_dir, os.path.basename(os.path.normpath(folder)))
    if data is None:
        if os.path.isfile(fp):
            os.remove(fp)
        return
    os.makedirs(output_dir, exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_saved_timeline(folder: str, output_dir: str) -> Tuple[Optional[List[str]], Dict[str, float]]:
    """Return (absolute file paths in the saved order or None, per-device time offsets in seconds)."""
    data = read_timeline_file(folder, output_dir)
    if not data:
        return None, {}
    order = [os.path.normpath(os.path.join(folder, rel)) for rel in data.get("order", [])]
    order = [p for p in order if os.path.exists(p)]
    offsets = {str(k): float(v) for k, v in (data.get("device_offsets_sec") or {}).items()}
    return (order or None), offsets


def load_manual_order(folder: str, output_dir: str) -> Optional[List[str]]:
    """Order to pass to run_autocut_pipeline(manual_order=...), or None to keep shooting-time order."""
    return load_saved_timeline(folder, output_dir)[0]


def load_clip_rotations(folder: str, output_dir: str) -> Dict[str, int]:
    """{absolute path: clockwise degrees} to pass to run_autocut_pipeline(clip_rotations=...)."""
    data = read_timeline_file(folder, output_dir) or {}
    return {
        os.path.normpath(os.path.join(folder, rel)): int(deg) % 360
        for rel, deg in (data.get("rotations") or {}).items()
        if int(deg) % 360
    }


class TimelinePanel(tk.Frame):
    def __init__(self, master, log_fn: Callable[[str], None], bg: str = "#242436", font_family: str = "맑은 고딕"):
        super().__init__(master, bg=bg, highlightthickness=1, highlightbackground="#313244", padx=14, pady=8)
        # Canvas geometry is in raw pixels while fonts follow DPI, so scale layout constants by the Tk DPI factor
        self.k = max(1.0, float(self.tk.call("tk", "scaling")) * 72.0 / 96.0)
        self.THUMB_W, self.THUMB_H = self._px(80), self._px(45)
        self.CLIP_Y = self._px(14)
        self.CLIP_H = self._px(76)
        self.GAP = self._px(5)
        self.CANVAS_H = self._px(94)
        self.log_fn = log_fn
        self.bg = bg
        self.ff = font_family
        self.folder = ""
        self.output_dir = ""
        self.include_photos = True
        self.items: List[Dict] = []
        self.offsets: Dict[str, float] = {}
        self.dev_colors: Dict[str, str] = {}
        self.sel = -1
        self._gen = 0
        self._xs: List[Tuple[int, int]] = []
        self._tk_thumbs: Dict[str, ImageTk.PhotoImage] = {}
        self._thumb_src: Dict[str, Image.Image] = {}
        self._drag: Optional[Dict] = None
        self._redraw_pending = False
        self._pool = ThreadPoolExecutor(max_workers=4)

        # One compact header row (buttons packed first so they never get clipped; the device legend clips first)
        head = tk.Frame(self, bg=bg)
        head.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(head, text="▶ 뒤로", command=lambda: self.move_selected(+1)).pack(side=tk.RIGHT)
        ttk.Button(head, text="◀ 앞으로", command=lambda: self.move_selected(-1)).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(head, text="⟲ 촬영시각순 정렬", command=self.reset_to_time_order).pack(side=tk.RIGHT, padx=(0, 6))
        self.state_lbl = tk.Label(head, text="", bg=bg, fg="#a6adc8", font=(font_family, 9, "bold"))
        self.state_lbl.pack(side=tk.RIGHT, padx=(0, 12))
        tk.Label(head, text="🎞️ 4. 타임라인", bg=bg, fg="#f9e2af", font=(font_family, 11, "bold")).pack(side=tk.LEFT)
        self.folder_lbl = tk.Label(head, text="", bg=bg, fg="#cdd6f4", font=(font_family, 10, "bold"))
        self.folder_lbl.pack(side=tk.LEFT, padx=(10, 0))
        self.info_lbl = tk.Label(head, text="", bg=bg, fg="#a6e3a1", font=(font_family, 9), anchor="w")
        self.info_lbl.pack(side=tk.LEFT, padx=(12, 0))
        self.legend = tk.Frame(head, bg=bg)
        self.legend.pack(side=tk.LEFT)

        body = tk.Frame(self, bg=bg)
        body.pack(fill=tk.X)
        tk.Label(
            body,
            text="V1\n🎬📷\n\n드래그\n우클릭\nR 회전",
            bg="#1e1e2e",
            fg="#b4befe",
            font=(font_family, 8, "bold"),
            width=6,
        ).pack(side=tk.LEFT, fill=tk.Y, padx=(0, 4))
        cwrap = tk.Frame(body, bg=bg)
        cwrap.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.canvas = tk.Canvas(
            cwrap,
            height=self.CANVAS_H,
            bg="#14141f",
            highlightthickness=1,
            highlightbackground="#45475a",
            xscrollincrement=self._px(30),
        )
        self.canvas.pack(fill=tk.X)
        hs = ttk.Scrollbar(cwrap, orient=tk.HORIZONTAL, command=self.canvas.xview)
        hs.pack(fill=tk.X)
        self.canvas.configure(xscrollcommand=hs.set)

        c = self.canvas
        c.bind("<ButtonPress-1>", self._on_press)
        c.bind("<B1-Motion>", self._on_motion)
        c.bind("<ButtonRelease-1>", self._on_release)
        c.bind("<Double-Button-1>", self._on_double)
        c.bind("<Button-3>", self._on_right_click)
        c.bind("<MouseWheel>", lambda e: c.xview_scroll(int(-e.delta / 120) * 2, "units"))
        c.bind("<Left>", lambda e: self._select(self.sel - 1))
        c.bind("<Right>", lambda e: self._select(self.sel + 1))
        c.bind("<Control-Left>", lambda e: self.move_selected(-1))
        c.bind("<Control-Right>", lambda e: self.move_selected(+1))
        c.bind("<r>", lambda e: self.rotate_selected(90))
        c.bind("<R>", lambda e: self.rotate_selected(-90))
        c.bind("<Configure>", lambda e: self._schedule_redraw())

        self._set_placeholder("왼쪽에서 날짜 폴더를 선택하면 영상·사진 클립이 이곳에 배치됩니다.")

    # ── Loading ──────────────────────────────────────────────────────────────
    def load_folder(self, folder: str, output_dir: str, include_photos: bool = True, force: bool = False):
        if not force and folder == self.folder and include_photos == self.include_photos and self.items:
            return
        self.folder, self.output_dir, self.include_photos = folder, output_dir, include_photos
        self._gen += 1
        gen = self._gen
        self.items = []
        self.sel = -1
        self.folder_lbl.configure(text=f"📁 {os.path.basename(folder)}")
        self.state_lbl.configure(text="")
        self.info_lbl.configure(text="메타데이터(촬영 시각·기기·HDR) 분석 중...")
        for w in self.legend.winfo_children():
            w.destroy()
        self._set_placeholder("클립 불러오는 중...")
        threading.Thread(target=self._load_worker, args=(gen, folder, include_photos), daemon=True).start()

    def _load_worker(self, gen: int, folder: str, include_photos: bool):
        v_files, p_files = scan_media_files_in_folder(folder)
        paths = v_files + (p_files if include_photos else [])
        metas = []
        for m in self._pool.map(self._safe_probe, paths):
            if m:
                metas.append(m)
        if gen == self._gen:
            self.after(0, self._on_meta, gen, metas)

    @staticmethod
    def _safe_probe(path: str) -> Optional[Dict]:
        try:
            return _probe_cached(path)
        except Exception:
            return None

    def _on_meta(self, gen: int, metas: List[Dict]):
        if gen != self._gen:
            return
        saved_order, self.offsets = load_saved_timeline(self.folder, self.output_dir)
        rotations = load_clip_rotations(self.folder, self.output_dir)
        for m in metas:
            m["dev_key"] = _device_key(m["device"])
            m["rotation"] = rotations.get(os.path.normpath(m["path"]), 0)
        self.dev_colors = {}
        for m in sorted(metas, key=lambda x: (x["creation_time"], x["file"])):
            if m["dev_key"] not in self.dev_colors:
                self.dev_colors[m["dev_key"]] = DEVICE_COLORS[len(self.dev_colors) % len(DEVICE_COLORS)]
        self._recompute_times(metas)
        if saved_order:
            self.items = apply_manual_clip_order(metas, saved_order)
        else:
            self.items = sorted(metas, key=lambda x: (x["eff_time"], x["file"]))
        self.sel = 0 if self.items else -1
        self._update_info()
        self._update_state_label()
        self._schedule_redraw()
        if not self.items:
            self._set_placeholder("이 폴더에는 영상·사진이 없습니다.")
            return
        for it in self.items:
            self._pool.submit(self._thumb_job, gen, it["path"], it["type"], it["duration"])

    def _thumb_job(self, gen: int, path: str, mtype: str, duration: float):
        if gen != self._gen:
            return
        try:
            key = _file_key(path)
            if key not in _THUMB_CACHE:
                _THUMB_CACHE[key] = extract_media_thumbnail(path, mtype, duration)
            img = _THUMB_CACHE[key]
        except Exception:
            img = None
        if img is not None and gen == self._gen:
            self.after(0, self._on_thumb, gen, path, img)

    def _on_thumb(self, gen: int, path: str, img: Image.Image):
        if gen != self._gen:
            return
        self._thumb_src[path] = img
        self._build_tile(path)
        self._schedule_redraw()

    def _build_tile(self, path: str):
        img = self._thumb_src.get(path)
        if img is None:
            return
        rot = next((it.get("rotation", 0) for it in self.items if it["path"] == path), 0)
        im = img.rotate(-rot, expand=True) if rot else img.copy()
        im.thumbnail((self.THUMB_W, self.THUMB_H), Image.Resampling.LANCZOS)
        tile = Image.new("RGB", (self.THUMB_W, self.THUMB_H), (17, 17, 27))
        tile.paste(im, ((self.THUMB_W - im.width) // 2, (self.THUMB_H - im.height) // 2))
        self._tk_thumbs[path] = ImageTk.PhotoImage(tile)

    def rotate_selected(self, delta: int):
        """Rotate the selected clip clockwise by delta degrees (±90 / 180); 0 resets it."""
        if not (0 <= self.sel < len(self.items)):
            return
        it = self.items[self.sel]
        it["rotation"] = 0 if delta == 0 else (it.get("rotation", 0) + delta) % 360
        self._build_tile(it["path"])
        self._commit(f"↻ 클립 회전: '{it['file']}' -> {it['rotation']}°" if it["rotation"] else f"↻ 클립 회전 해제: '{it['file']}'")

    # ── Time correction & ordering ───────────────────────────────────────────
    def _recompute_times(self, items: List[Dict]):
        for it in items:
            off = self.offsets.get(it["dev_key"], 0.0)
            dt = _parse_dt(it["creation_time"])
            it["eff_time"] = (dt + timedelta(seconds=off)).strftime("%Y-%m-%dT%H:%M:%S") if (dt and off) else it["creation_time"]
        # Plain arithmetic instead of datetime.timestamp(): that raises on Windows for pre-1970 dates (QuickTime's 1904 "zero")
        epoch = datetime(1970, 1, 1)
        secs = {id(it): (d - epoch).total_seconds() for it in items for d in [_parse_dt(it["eff_time"])] if d}
        stamps = sorted(secs.values())
        median = stamps[len(stamps) // 2] if stamps else 0.0
        for it in items:
            s = secs.get(id(it))
            it["suspect"] = s is not None and abs(s - median) > SUSPECT_DAYS * 86400

    def _is_default_order(self) -> bool:
        raw = sorted(self.items, key=lambda x: (x["creation_time"], x["file"]))
        return not self.offsets and [i["path"] for i in raw] == [i["path"] for i in self.items]

    def _rotations(self) -> Dict[str, int]:
        return {os.path.relpath(it["path"], self.folder): it["rotation"] for it in self.items if it.get("rotation")}

    def _commit(self, log_msg: str = ""):
        try:
            if self._is_default_order() and not self._rotations():
                write_timeline_file(self.folder, self.output_dir, None)
            else:
                write_timeline_file(
                    self.folder,
                    self.output_dir,
                    {
                        "version": 2,
                        "saved_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                        "order": [os.path.relpath(it["path"], self.folder) for it in self.items],
                        "device_offsets_sec": self.offsets,
                        "device_labels": {it["dev_key"]: it["device"] for it in self.items},
                        "rotations": self._rotations(),
                    },
                )
        except Exception as e:
            self.log_fn(f"⚠️ 타임라인 순서 저장 실패: {e}")
        if log_msg:
            self.log_fn(log_msg)
        self._update_info()
        self._update_state_label()
        self._schedule_redraw()

    def _update_state_label(self):
        n_rot = len(self._rotations()) if self.items else 0
        rot_txt = f" · 회전 {n_rot}개" if n_rot else ""
        if not self.items:
            self.state_lbl.configure(text="")
        elif self._is_default_order():
            self.state_lbl.configure(text=f"● 촬영시각순 (자동){rot_txt}", fg="#f9e2af" if n_rot else "#a6adc8")
        else:
            self.state_lbl.configure(text=f"✎ 사용자 순서{rot_txt} (저장됨)", fg="#f9e2af")

    def _update_info(self):
        n_v = sum(1 for it in self.items if it["type"] == "video")
        n_p = len(self.items) - n_v
        total_min = sum(it["duration"] for it in self.items if it["type"] == "video") / 60.0
        parts = [f"영상 {n_v} · 사진 {n_p} · {total_min:.1f}분"]
        n_hlg = sum(1 for it in self.items if it.get("dynamic_range") == "HLG")
        n_pq = sum(1 for it in self.items if it.get("dynamic_range") == "HDR10")
        if n_hlg or n_pq:
            parts.append(f"🌈 HDR {n_hlg + n_pq} (HLG {n_hlg}·PQ {n_pq})→SDR")
        n_sus = sum(1 for it in self.items if it.get("suspect"))
        if n_sus:
            parts.append(f"⚠ 시간 이상 {n_sus} (우클릭→보정)")
        self.info_lbl.configure(text="  ·  ".join(parts), fg="#f38ba8" if n_sus else "#a6e3a1")

        for w in self.legend.winfo_children():
            w.destroy()
        counts: Dict[str, int] = {}
        labels: Dict[str, str] = {}
        for it in self.items:
            counts[it["dev_key"]] = counts.get(it["dev_key"], 0) + 1
            labels.setdefault(it["dev_key"], it["device"])
        for key, col in self.dev_colors.items():
            if key not in counts:
                continue
            off = self.offsets.get(key, 0.0)
            off_txt = f" {off / 3600:+.1f}h" if off else ""
            tk.Label(
                self.legend,
                text=f"■ {labels[key]} {counts[key]}{off_txt}",
                bg=self.bg,
                fg=col,
                font=(self.ff, 9, "bold"),
            ).pack(side=tk.LEFT, padx=(8, 0))

    def reset_to_time_order(self):
        if not self.items:
            return
        self.items.sort(key=lambda x: (x["eff_time"], x["file"]))
        self.sel = min(max(self.sel, 0), len(self.items) - 1)
        self._commit("⟲ 타임라인을 촬영 시각순(기기 시간 보정 반영)으로 다시 정렬했습니다.")

    def move_selected(self, delta: int):
        i = self.sel
        j = i + delta
        if not (0 <= i < len(self.items)) or not (0 <= j < len(self.items)):
            return
        self._move(i, j)

    def _move(self, i: int, j: int):
        it = self.items.pop(i)
        self.items.insert(j, it)
        self.sel = j
        self._commit(f"🧲 타임라인 순서 변경: '{it['file']}' -> {j + 1}번째")
        self._ensure_visible(j)

    def _set_device_offset(self, dev_key: str, seconds: float, msg: str):
        if abs(seconds) < 0.5:
            self.offsets.pop(dev_key, None)
        else:
            self.offsets[dev_key] = seconds
        self._recompute_times(self.items)
        self.items.sort(key=lambda x: (x["eff_time"], x["file"]))
        self._commit(msg)

    def _ask_device_time(self, i: int):
        it = self.items[i]
        cur = it["eff_time"].replace("T", " ")
        n_same = sum(1 for x in self.items if x["dev_key"] == it["dev_key"])
        s = simpledialog.askstring(
            "기기 시간 보정",
            f"'{it['device']}'(으)로 찍은 클립 {n_same}개의 시각을 한꺼번에 보정합니다.\n\n"
            f"선택한 클립: {it['file']}\n기록된 시각: {cur}\n\n"
            "이 클립의 실제 촬영 시각을 입력하세요 (YYYY-MM-DD HH:MM[:SS]):",
            initialvalue=cur,
            parent=self,
        )
        if not s:
            return
        new_dt = _parse_dt(s)
        orig_dt = _parse_dt(it["creation_time"])
        if not new_dt or not orig_dt:
            messagebox.showerror("형식 오류", "예: 2022-04-01 09:30 처럼 입력해주세요.", parent=self)
            return
        off = (new_dt - orig_dt).total_seconds()
        self._set_device_offset(
            it["dev_key"],
            off,
            f"🕒 기기 시간 보정: '{it['device']}' 클립 {n_same}개를 {off / 3600:+.2f}시간 이동 후 촬영시각순으로 재정렬",
        )

    # ── Drawing ──────────────────────────────────────────────────────────────
    def _px(self, v: float) -> int:
        return int(round(v * self.k))

    def _set_placeholder(self, text: str):
        c = self.canvas
        c.delete("all")
        c.create_text(self._px(12), self.CANVAS_H // 2, text=text, anchor="w", fill="#7f849c", font=(self.ff, 10))
        c.configure(scrollregion=(0, 0, max(1, c.winfo_width()), self.CANVAS_H))

    def _clip_w(self, it: Dict) -> int:
        base = self.THUMB_W + self._px(8)
        if it["type"] != "video":
            return base
        return base + self._px(min(70, 16 * math.log2(1.0 + max(0.0, it["duration"]) / 4.0)))

    def _schedule_redraw(self):
        if not self._redraw_pending:
            self._redraw_pending = True
            self.after(40, self._redraw)

    def _redraw(self):
        self._redraw_pending = False
        if not self.items:
            return
        c = self.canvas
        P = self._px
        c.delete("all")
        self._xs = []
        x = P(4)
        prev_date = ""
        y0, y1 = self.CLIP_Y, self.CLIP_Y + self.CLIP_H
        for i, it in enumerate(self.items):
            w = self._clip_w(it)
            x0, x1 = x, x + w
            self._xs.append((x0, x1))
            col = self.dev_colors.get(it["dev_key"], "#89b4fa")
            selected = i == self.sel

            # Ruler: tick per clip, date label when the day changes
            day = it["eff_time"][:10]
            c.create_line(x0, P(9), x0, P(13), fill="#45475a")
            if day != prev_date:
                c.create_text(x0 + P(2), P(5), text=day[5:].replace("-", "/"), anchor="w", fill="#7f849c", font=(self.ff, 7))
                prev_date = day

            c.create_rectangle(
                x0, y0, x1, y1,
                fill=_mix(col, "#14141f", 0.78),
                outline="#f9e2af" if selected else _mix(col, "#14141f", 0.35),
                width=P(2) if selected else 1,
            )
            c.create_rectangle(x0 + 1, y0 + 1, x1 - 1, y0 + P(3), fill=col, outline="")

            tx = x0 + (w - self.THUMB_W) // 2
            ty = y0 + P(5)
            tkimg = self._tk_thumbs.get(it["path"])
            if tkimg is not None:
                c.create_image(tx, ty, anchor="nw", image=tkimg)
            else:
                c.create_rectangle(tx, ty, tx + self.THUMB_W, ty + self.THUMB_H, fill="#11111b", outline="")
                c.create_text(
                    tx + self.THUMB_W // 2, ty + self.THUMB_H // 2,
                    text="🎬" if it["type"] == "video" else "📷", fill="#585b70", font=(self.ff, 12),
                )

            if it.get("dynamic_range", "SDR") != "SDR":
                badge = "HLG" if it["dynamic_range"] == "HLG" else "PQ"
                c.create_rectangle(tx + P(1), ty + P(1), tx + P(5 + 6 * len(badge)), ty + P(11), fill="#fab387", outline="")
                c.create_text(tx + P(3), ty + P(6), text=badge, anchor="w", fill="#11111b", font=(self.ff, 6, "bold"))
            if it.get("rotation"):
                rx = tx + self.THUMB_W // 2
                c.create_rectangle(rx - P(12), ty + self.THUMB_H - P(11), rx + P(12), ty + self.THUMB_H - P(1), fill="#89b4fa", outline="")
                c.create_text(rx, ty + self.THUMB_H - P(6), text=f"↻{it['rotation']}°", fill="#11111b", font=(self.ff, 6, "bold"))
            if it.get("suspect"):
                c.create_rectangle(
                    tx + self.THUMB_W - P(12), ty + P(1), tx + self.THUMB_W - P(1), ty + P(12), fill="#f38ba8", outline=""
                )
                c.create_text(tx + self.THUMB_W - P(6.5), ty + P(6.5), text="!", fill="#11111b", font=(self.ff, 7, "bold"))

            shifted = "*" if self.offsets.get(it["dev_key"]) else ""
            c.create_text(
                x0 + P(4), ty + self.THUMB_H + P(7),
                text=f"{i + 1}. {it['eff_time'][11:19]}{shifted}",
                anchor="w", fill="#f5f7ff" if selected else "#cdd6f4", font=(self.ff, 7, "bold"),
            )
            if it["type"] == "video":
                d = int(round(it["duration"]))
                sub = f"{d // 60}:{d % 60:02d} {it['file']}"
            else:
                sub = f"📷 {it['file']}"
            max_chars = max(6, int((w - P(8)) / P(4.6)))
            c.create_text(
                x0 + P(4), ty + self.THUMB_H + P(18),
                text=sub if len(sub) <= max_chars else sub[: max_chars - 1] + "…",
                anchor="w", fill="#a6adc8", font=(self.ff, 6),
            )
            x = x1 + self.GAP
        c.configure(scrollregion=(0, 0, x + P(4), self.CANVAS_H))

    def _ensure_visible(self, i: int):
        if not (0 <= i < len(self._xs)):
            return
        c = self.canvas
        total = max(1, self._xs[-1][1] + self.GAP + 8)
        left = c.canvasx(0)
        right = left + c.winfo_width()
        x0, x1 = self._xs[i]
        if x0 < left:
            c.xview_moveto(max(0.0, (x0 - 20) / total))
        elif x1 > right:
            c.xview_moveto(max(0.0, (x1 - c.winfo_width() + 20) / total))

    # ── Mouse / keyboard ─────────────────────────────────────────────────────
    def _index_at(self, cx: float) -> int:
        for i, (x0, x1) in enumerate(self._xs):
            if x0 <= cx <= x1:
                return i
        return -1

    def _gap_at(self, cx: float) -> int:
        for i, (x0, x1) in enumerate(self._xs):
            if cx < (x0 + x1) / 2:
                return i
        return len(self._xs)

    def _select(self, i: int):
        if 0 <= i < len(self.items):
            self.sel = i
            self._schedule_redraw()
            self._ensure_visible(i)

    def _on_press(self, e):
        self.canvas.focus_set()
        i = self._index_at(self.canvas.canvasx(e.x))
        self._drag = {"idx": i, "x": e.x, "active": False} if i >= 0 else None
        if i >= 0:
            self.sel = i
            self._schedule_redraw()

    def _on_motion(self, e):
        d = self._drag
        if not d:
            return
        if not d["active"] and abs(e.x - d["x"]) < self._px(5):
            return
        d["active"] = True
        c = self.canvas
        edge = self._px(24)
        if e.x < edge:
            c.xview_scroll(-1, "units")
        elif e.x > c.winfo_width() - edge:
            c.xview_scroll(1, "units")
        cx = c.canvasx(e.x)
        g = self._gap_at(cx)
        c.delete("dnd")
        gx = (self._xs[g][0] - self.GAP / 2) if g < len(self._xs) else (self._xs[-1][1] + self.GAP / 2)
        c.create_line(gx, 2, gx, self.CANVAS_H - 2, fill="#f9e2af", width=self._px(2.5), tags="dnd")
        w = self._clip_w(self.items[d["idx"]])
        c.create_rectangle(
            cx - w / 2, self.CLIP_Y + self._px(4), cx + w / 2, self.CLIP_Y + self.CLIP_H - self._px(4),
            outline="#f9e2af", dash=(4, 3), width=self._px(1.5), tags="dnd",
        )
        d["gap"] = g

    def _on_release(self, e):
        d, self._drag = self._drag, None
        self.canvas.delete("dnd")
        if not d or not d["active"] or "gap" not in d:
            return
        i, g = d["idx"], d["gap"]
        if g > i:
            g -= 1
        if g != i:
            self._move(i, g)

    def _on_double(self, e):
        i = self._index_at(self.canvas.canvasx(e.x))
        if i >= 0:
            try:
                os.startfile(self.items[i]["path"])
            except Exception:
                pass

    def _on_right_click(self, e):
        i = self._index_at(self.canvas.canvasx(e.x))
        if i < 0:
            return
        self.sel = i
        self._schedule_redraw()
        it = self.items[i]
        dev = it["device"]
        key = it["dev_key"]
        cur_off = self.offsets.get(key, 0.0)
        m = tk.Menu(self, tearoff=0, font=(self.ff, 10))
        m.add_command(label=f"▶ 재생 / 열기: {it['file']}", command=lambda: self._on_double(e))
        m.add_separator()
        m.add_command(label="⏮ 맨 앞으로 이동", command=lambda: self._move(i, 0) if i != 0 else None)
        m.add_command(
            label="⏭ 맨 뒤로 이동",
            command=lambda: self._move(i, len(self.items) - 1) if i != len(self.items) - 1 else None,
        )
        m.add_separator()
        m.add_command(label="↻ 시계방향 90° 회전 (R)", command=lambda: self.rotate_selected(90))
        m.add_command(label="↺ 반시계방향 90° 회전 (Shift+R)", command=lambda: self.rotate_selected(-90))
        m.add_command(label="⟳ 180° 회전 (거꾸로 찍힌 영상)", command=lambda: self.rotate_selected(180))
        if it.get("rotation"):
            m.add_command(label=f"회전 초기화 (현재 {it['rotation']}°)", command=lambda: self.rotate_selected(0))
        m.add_separator()
        m.add_command(label=f"🕒 '{dev}' 시간 보정 — 이 클립의 실제 촬영 시각 입력...", command=lambda: self._ask_device_time(i))
        m.add_command(
            label=f"'{dev}' 전체 +9시간 (UTC로 기록된 기기)",
            command=lambda: self._set_device_offset(key, cur_off + 9 * 3600, f"🕒 '{dev}' 클립 시각 +9시간 보정"),
        )
        m.add_command(
            label=f"'{dev}' 전체 -9시간",
            command=lambda: self._set_device_offset(key, cur_off - 9 * 3600, f"🕒 '{dev}' 클립 시각 -9시간 보정"),
        )
        if cur_off:
            m.add_command(
                label=f"'{dev}' 시간 보정 해제 ({cur_off / 3600:+.2f}h)",
                command=lambda: self._set_device_offset(key, 0.0, f"🕒 '{dev}' 시간 보정 해제"),
            )
        m.add_separator()
        m.add_command(label="⟲ 전체 촬영시각순으로 다시 정렬", command=self.reset_to_time_order)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()
