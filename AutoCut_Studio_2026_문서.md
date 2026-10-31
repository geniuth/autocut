# 🎬 AutoCut Studio 2026 — AI 가족·여행·주행 영상 자동 편집 스튜디오

> **★ 윰탱해윤해진이네 에디션**  
> 고프로(GoPro)·아이폰(iOS)·일반 카메라로 촬영한 가족 여행 영상과 사진을 **촬영 시각(메타데이터) 순서대로 자동 정렬**하고, **AI 비전(얼굴/선명도/흔들림) + AI 한국어 음성인식(Faster-Whisper STT)**으로 베스트 구간만 컷편집·자막 생성·배경음악(BGM) 믹싱·HEVC(H.265) 하드웨어 가속 렌더링까지 원클릭으로 완성하는 데스크톱 스튜디오 프로그램입니다.

---

## 📁 1. 프로젝트 파일 구성

| 파일 / 폴더 | 역할 및 설명 |
| :--- | :--- |
| [`autocut_gui.py`](file:///d:/workspace/autocut/autocut_gui.py) | **메인 데스크톱 GUI 애플리케이션** (`1920x1080` 풀HD 다크 스튜디오 테마, 프리미어 스타일 스플래시 화면, 3단 카드형 UI, 실시간 CPU/GPU/VRAM 모니터 그래프, 유튜브 URL 학습기) |
| [`autocut_engine.py`](file:///d:/workspace/autocut/autocut_engine.py) | **핵심 AI 편집 엔진** (날짜별 분류, 메타데이터 시간순 정렬, OpenCV 비전/모션 스코어링, Faster-Whisper 한국어 STT, 베스트 샷 인트로 생성, 동적 BGM 더킹, HDR→SDR 톤매핑, 상황별 트랜지션·오디오 크로스페이드, FFmpeg HEVC NVENC 렌더링) |
| [`autocut_timeline.py`](file:///d:/workspace/autocut/autocut_timeline.py) | **프리미어식 하단 타임라인** (썸네일 클립 배치, 드래그 순서 변경, 기기 시간 보정, 클립 회전, `편집결과_<날짜>/<날짜>_타임라인순서.json` 저장) |
| `autocut_session.json` (자동 생성, git 제외) | 마지막 작업 상태(설정·선택 폴더) — 다음 실행 때 자동 복원 |
| `assets/autocut_icon.ico` / `.png` | 시네마 카메라 프로그램 아이콘 (창·작업 표시줄·스플래시) |
| `assets/face_detection_yunet_2023mar.onnx` | 세로 쇼츠 리프레임용 얼굴 검출 모델 (OpenCV Zoo YuNet, MIT 라이선스, 230KB) |
| [`AutoCut_Studio.vbs`](file:///d:/workspace/autocut/AutoCut_Studio.vbs) | **무콘솔(Windowless) 백그라운드 런처** (검은색 `cmd` 창 없이 즉시 GUI만 깔끔하게 실행) |
| [`AutoCut_Studio.bat`](file:///d:/workspace/autocut/AutoCut_Studio.bat) | 배치 파일 실행 런처 (`AutoCut_Studio.vbs` 호출) |
| [`custom_styles.json`](file:///d:/workspace/autocut/custom_styles.json) | 유튜브 URL 또는 로컬 레퍼런스 영상을 분석해 학습된 **맞춤형 편집 스타일 프리셋** 저장소 |
| [`assets/calm_youtube_free_bgm.wav`](file:///d:/workspace/autocut/assets/calm_youtube_free_bgm.wav) | 자체 합성된 **저작권 무료(Royalty-Free) 잔잔한 어쿠스틱 피아노 & 웜 패드 배경음악** (`48kHz`, 16-bit Stereo, 약 170초 루프) |

---

## 🔄 2. 전체 자동 편집 파이프라인 구조

```mermaid
flowchart TD
    A["📂 원본 미디어 폴더 (고프로 MP4 + 아이폰 MOV/JPG)"] --> B["📅 1. 날짜별 자동 분류 (classify_media_by_date)"]
    B --> C["🕒 2. 메타데이터 촬영 시각 추출 및 시간순 교차 정렬<br/>(타임라인에서 순서·기기 시간·회전 수동 조정)"]
    C --> D["🧠 3. AI 멀티모달 분석 (스마트 캐시 지원)"]
    D --> D1["🎙️ FFT 노이즈 제거 + Faster-Whisper 한국어 음성인식(STT)"]
    D --> D2["👁️ OpenCV 키프레임 비전 분석 (얼굴 검출 · 선명도 · 색감 · 흔들림 감점)"]
    D1 & D2 --> E["✂️ 4. 편집 모드별 최적 컷 구간 선택 + 편집 길이 상한 (원본의 60% 이하)"]
    E --> E2["🎨 HDR 자동 색 균형 · 기기 간 색 맞춤 · 클립 음량 측정 · 화자 구분(옵션)"]
    E2 --> F["🌟 5. 정방향 베스트 샷 추출 & 3초 인트로 생성 (반투명 타이틀 + 하얀 테두리)"]
    F --> G["🎵 6. 클립별 렌더링 (HDR 톤매핑·회전·손떨림 보정·색 맞춤 LUT) + 상단 날짜/로고 HUD + 클립 고정 게인 음량 + 동적 BGM 믹싱 (영상 6% / 사진 38%)"]
    G --> H["✨ 7. 상황별 xfade 트랜지션 (실제 영상 겹침) + 오디오 크로스페이드 + 마지막 블랙 페이드아웃"]
    H --> I["🎬 8. 최종 출력 (HEVC H.265 NVENC / 지정 해상도·FPS·bps)"]
    I --> O1["✅ 자막 포함 완성본 MP4"]
    I --> O2["✅ 필모라(Filmora) 연동용 무자막 MP4 + 전체/음성 SRT 자막"]
    I --> O3["✅ 베스트샷 JPG + 편집분석리포트 JSON"]
    I --> O4["📱 세로 쇼츠 9:16 (옵션) + 화자 자막 SRT (옵션)"]
```

---

## ✨ 3. 핵심 기능 상세 정리

### ① 촬영 날짜별 자동 분류 및 메타데이터 시간순 배치
* **날짜별 자동 폴더 정리 ([`classify_media_by_date`](file:///d:/workspace/autocut/autocut_engine.py#L254-L300))**:
  * 최상위 폴더에 흩어진 영상(`.mp4`, `.mov` 등), 사진(`.jpg`, `.png`, `.heic` 등), 부속 파일(`.thm`, `.lrv`)을 촬영 날짜(`YYYY-MM-DD`) 하위 폴더로 자동 이동·정리합니다.
* **영상 + 사진 촬영 시각(메타데이터) 기준 시간순 교차 배치**:
  * **동영상 ([`get_video_datetime_iso`](file:///d:/workspace/autocut/autocut_engine.py#L153-L177))**: 아이폰 `.MOV`의 Apple QuickTime 현지 시각 태그(`com.apple.quicktime.creationdate`, KST `+0900`)와 고프로 `.MP4`의 `creation_time`을 읽어 정확한 촬영 시각을 추출합니다.
  * **사진 ([`get_photo_datetime_iso`](file:///d:/workspace/autocut/autocut_engine.py#L180-L207))**: EXIF 메타데이터의 `DateTimeOriginal` (Tag `36867`, `36868`, `306`)을 읽어 촬영 시각을 추출합니다.
  * 영상과 사진을 촬영 시각(`YYYY-MM-DDTHH:MM:SS`) 오름차순으로 정렬하여, 여행 동선 그대로 영상과 사진(3초 슬라이드)이 자연스럽게 이어집니다.
* **실시간 폴더 감지 & 새로고침 ([`refresh_date_folders`](file:///d:/workspace/autocut/autocut_gui.py#L949-L1028))**:
  * 윈도우 탐색기에서 사진을 추가하거나 압축을 풀면 **2초 이내에 자동으로 파일 개수를 감지해 갱신**하며, `🔄 개수 새로고침` 버튼 및 `F5` 단축키도 지원합니다.
  * 하위 폴더에 들어있는 미디어까지 재귀 스캔([`scan_media_files_in_folder`](file:///d:/workspace/autocut/autocut_engine.py#L55-L76))하며, 재편집 시 이미 분석된 영상은 캐시를 재사용하고 새로 추가된 사진/영상만 즉시 반영합니다.

---

### ② AI 음성인식(STT) & 비전/모션 하이라이트 컷편집
* **주행·바람 소리 노이즈 제거 ([`extract_denoised_wav`](file:///d:/workspace/autocut/autocut_engine.py#L314-L338))**:
  * FFmpeg `highpass=f=100, lowpass=f=7500, afftdn=nf=-25` 필터로 차량 주행음과 제주도 바람 소리를 억제한 뒤 음성을 추출합니다.
* **한국어 대화 인식 및 환각 필터링 ([`load_whisper_model`](file:///d:/workspace/autocut/autocut_engine.py#L303-L311), [`is_hallucination`](file:///d:/workspace/autocut/autocut_engine.py#L289-L300))**:
  * `faster-whisper`와 VAD(음성 활동 감지)로 실제 가족 대화 구간을 추출하고, 유튜브 자막 환각 문구(`"구독과 좋아요"`, `"시청해주셔서 감사합니다"` 등)를 완벽히 차단합니다.
  * **대화 보호 규칙**: 말하고 있는 도중에는 절대 컷이 잘리지 않도록 인접 대화를 병합하고 앞뒤 여백(`0.35초` / `0.55초`)을 확보합니다.
* **비전/모션 키프레임 분석 ([`analyze_video_keyframes`](file:///d:/workspace/autocut/autocut_engine.py#L341-L407))**:
  * OpenCV Haar Cascade 얼굴 검출(가족 클로즈업 가산점), 라플라시안 선명도, 색채 생동감, 옵티컬 플로우(Optical Flow) 카메라 흔들림 감점을 종합해 최고의 장면을 선별합니다.
* **편집 길이 상한 ([`enforce_edit_length_budget`](file:///d:/workspace/autocut/autocut_engine.py), v2.6.0)**:
  * 편집된 영상 길이를 원본 영상 전체 길이의 **60% 이하**(기본, GUI에서 40~80%·제한 없음)로 맞춥니다. 무음 구간 2.5초로 축소 → 대화 앞뒤 무음 여백 0.4초로 축소 → 영상별 최고 구간만 남기기 → 저점수(말소리 없는) 영상부터 제외 순으로 줄이며, 점수는 말소리 비율×3 + 미학 + VLM 하이라이트입니다.

---

### ③ 정방향 베스트 샷 3초 인트로 & 상단 날짜/로고 HUD
* **상하 반전 방지 정방향 보정 ([`_ensure_upright_bgr`](file:///d:/workspace/autocut/autocut_engine.py#L695-L738), [`prepare_upright_photo_1080p`](file:///d:/workspace/autocut/autocut_engine.py#L741-L802))**:
  * 사진 EXIF 방향 태그 자동 회전(`ImageOps.exif_transpose`) 및 정방향/180도 회전 얼굴·하늘 밝기 비교를 통해 베스트 샷이나 사진이 절대 위아래로 뒤집히지 않도록 보정합니다.
* **3초 베스트 샷 인트로 ([`find_best_shot_frame`](file:///d:/workspace/autocut/autocut_engine.py#L805-L942), [`create_intro_segment`](file:///d:/workspace/autocut/autocut_engine.py#L1035-L1200))**:
  * 해당 날짜의 모든 영상·사진 중 가장 선명하고 얼굴이 잘 나온 베스트 샷을 뽑아 **바깥쪽 하얀 액자 테두리**와 **은은한 반투명 중앙 타이틀 박스**(예: `윰탱이네 제주도 여행`, 불필요한 영문 배지 제거)를 합성하여 3초 인트로 영상으로 만듭니다.
* **상단 날짜 · 로고 상시 표시 ([`create_hud_overlay_png`](file:///d:/workspace/autocut/autocut_engine.py#L945-L1032))**:
  * 좌측 상단에는 촬영 날짜(`📅 2023-09-21`), 우측 상단에는 가족 로고(`★ 윰탱해윤해진이네`)를 깔끔한 반투명 배지로 표시합니다.
* **클립 수동 회전 (`rotation_vf`, v2.4.0)**:
  * 옆으로·거꾸로 찍힌 클립은 타임라인에서 `R` / `Shift+R` / 우클릭(180°)으로 돌립니다. 영상은 `transpose`, 사진은 회전 후 블러 배경을 다시 만들며, 수동 회전한 클립은 자동 상하반전 보정을 건너뜁니다.
* **HDR 자동 감지 & SDR 톤매핑 (`detect_dynamic_range`, `get_hdr_tonemap_vf`, v2.3.0)**:
  * HLG(아이폰 Dolby Vision 8.4 포함)·HDR10/HDR10+ 클립을 감지해 `libplacebo`(Vulkan GPU, 없으면 `zscale` CPU)로 Rec.709 SDR에 맞춥니다. BT.709 색 태그를 비트스트림에 기록합니다.
* **HDR 자동 색 균형 (`analyze_auto_color`, `ensure_auto_color`, v2.9.0)**:
  * HDR 클립마다 키프레임 6장(키프레임만 디코딩, 1초 미만) → ① 검정(brightness)×대비 12개 후보를 libplacebo HDR→SDR 보기로 렌더링해 하위 2% 휘도≈12·하이라이트 미클리핑·밝기 목표로 선택 → ② 채도(목표 65로 수렴)×감마 9개 + 원본을 CLIP 미학 점수 − 벗어남 벌점으로 선택. 클립당 고정값(깜빡임 없음), `<날짜>_자동색보정.json` 캐시, HLG 출력은 HLG→HLG libplacebo로 적용(실측: 예상치와 출력 일치), 인트로 베스트샷에도 반영. libplacebo가 없으면 건너뜀.
* **출력 다이내믹 레인지 선택 (`output_dynamic_range`, `sdr_to_output_vf`, `hdr_to_hlg_vf`, v2.8.0)**:
  * **SDR(기본)** 또는 **HLG HDR**(HEVC Main10 · BT.2020 · ARIB STD-B67). HLG에서는 SDR 영상·사진·인트로를 SDR 그래프 끝에서 HDR 기준 백색 203nit로 변환하고(실측: HDR TV처럼 다시 보면 SDR 마스터와 밝기 일치), HLG 원본은 그대로, PQ는 HLG로 변환합니다. HDR 원본에는 8-bit 전용 `eq`를 생략하고 날짜/로고 HUD를 75%로 낮춰 합성, 화이트 딥→블랙 딥, 라이트 릭은 10-bit 값으로, 자막 입히기는 생략(SRT 제공).

---

### ④ 저작권 무료 잔잔한 배경음악(BGM) & 동적 볼륨 더킹(Ducking)
* **유튜브 무료 감성 BGM 자동 생성 ([`ensure_calm_bgm_file`](file:///d:/workspace/autocut/autocut_engine.py#L561-L692))**:
  * 저작권 걱정 없는 어쿠스틱 펠트 피아노 아르페지오 & 따뜻한 스테레오 패드 사운드(`calm_youtube_free_bgm.wav`)를 내장하고 있습니다.
* **클립 종류별 자동 볼륨 조절**:
  * **동영상 클립 재생 시**: 가족의 대화 목소리가 또렷하게 들리도록 배경음악을 **아주 작게(`6%`)** 깔아줍니다.
  * **사진 슬라이드 & 3초 인트로 재생 시**: 현장음이 없는 구간이므로 배경음악을 **보통 크기(`38%`, 부드러운 페이드 인/아웃 포함)**로 자연스럽게 키워줍니다.

---

### ④-2 상황별 트랜지션 & 오디오 크로스페이드 (v2.5.0)
![상황별 트랜지션 미리보기](docs/transitions_preview.png)
* **실제 영상 겹침 ([`_build_cut_render_cmd`](file:///d:/workspace/autocut/autocut_engine.py), [`_join_cut`](file:///d:/workspace/autocut/autocut_engine.py))**:
  * 각 컷 뒤 원본 0.25~0.7초(핸들)를 같은 필터(색·톤매핑·회전·HUD·음량·BGM)로 렌더링해 다음 컷 시작부와 `xfade`로 겹칩니다. 전체 길이는 그대로라 자막 싱크가 유지됩니다. (사진·원본 끝 컷은 마지막 프레임 정지 방식으로 대체)
  * **핸들은 별도 ffmpeg 실행 (`_one_cut_cmd`, v3.1.1)**: 예전엔 한 그래프에서 `split` + `trim`으로 본 컷과 핸들을 함께 출력했는데, 핸들 출력의 첫 프레임(컷 끝)이 나올 때까지 ffmpeg가 컷 전체 프레임을 RAM에 쌓아(4K 10-bit 프레임당 약 25MB) 20초 이상 컷에서 수십 GB 후 정지 (실측: 23초 컷에서 53.8GB, 진행률 51.3%에서 멈춤). 이제 본 컷은 `-ss s -t dur`, 핸들은 `-ss s+dur -t handle`로 따로 (최대 RAM 약 3.9GB, 핸들 2.5초). vid.stab이 걸린 컷은 변환이 프레임 순서에 묶여 있어 핸들을 컷 시작부터 디코딩해 `trim=start=dur`로 잘라 씀. 출력에 `-t`를 지정해 컷마다 1프레임씩 늘어 자막이 밀리는 것을 방지.
* **상황별 선택 ([`plan_transition`](file:///d:/workspace/autocut/autocut_engine.py))**:
  * 점프컷 = 짧은 디졸브/컷, 같은 장면 = 필름 디졸브, 장면 전환(20분 이상·다른 날) = 화이트 딥·라이트 릭·휩 팬(그냥 대화 = 블랙 딥). 프리셋 스타일(빠른 텐션/시네마틱)과 `transition_prob`를 반영합니다.
* **오디오 크로스페이드**: 영상 컷마다 0.25초 선형 크로스페이드로 '틱' 소리와 뚝 끊김을 없앱니다.

### ④-3 마무리 보정 (v3.0.0 · FilmCraft 아이디어를 ffmpeg + numpy로 재구현)
* **기기 간 색 맞춤 ([`ensure_color_match`](file:///d:/workspace/autocut/autocut_engine.py), [`write_color_match_lut`](file:///d:/workspace/autocut/autocut_engine.py))**:
  * 측정: 키프레임 6장을 시청 화면 기준(SDR은 그대로·D-Log 복원 후, HDR은 `_ac_render`로 libplacebo 톤매핑 + 자동 색 균형 후)으로 뽑아 96×54 Oklab으로 변환 → 어두운(`smoothstep(0.55,0.15,L)`)·중간·밝은(`smoothstep(0.45,0.85,L)`) 영역별 평균 a/b와 전체 채도. 피부 픽셀(Oklab 색상 20°~75°)은 가중치 0.2.
  * 기준: 촬영 시각 ±15분 안의 클립들의 길이 가중 중간값(혼자 찍힌 클립은 보정 안 함). 영역별 a/b를 70%만큼(최대 0.02) 옮기고 채도 배율은 0.85~1.2. 피부색이 평균 8°를 넘게 돌면 이분 탐색으로 강도를 줄임. 순수 검정·흰색은 중립 유지.
  * 적용: 33³ `.cube` LUT를 `lut3d`로 (색 프로파일 복원 → 색 맞춤 → 스타일 eq 순). HLG 마스터의 HDR 클립은 HLG 신호를 SDR 화면에 대응(기준 백색 75%, BT.2020↔709)시킨 LUT로 10-bit 그대로 처리. LUT는 `원본 + (보정 − 왕복)`이라 보정 0이면 정확히 원본 (실측: yuv↔rgb 왕복 평균 차이 0).
* **클립 음량 맞춤 ([`ensure_loudness`](file:///d:/workspace/autocut/autocut_engine.py), [`clip_audio_chain`](file:///d:/workspace/autocut/autocut_engine.py))**: `highpass,afftdn,ebur128`로 파일별 통합 음량 측정 → `volume=(−16−I)dB`(−12~+10 dB, −60 LUFS 미만은 0) + `alimiter=limit=0.84:level=0:latency=1`. 핸들(겹침 구간)도 같은 게인이라 크로스페이드에서 음량이 튀지 않음. 끄면 예전 컷별 `loudnorm`. 실측: 5클립 편집본 −16.6 LUFS.
* **손떨림 보정 ([`prepare_cut_stabilization`](file:///d:/workspace/autocut/autocut_engine.py))**: 기기 이름에 DJI·Osmo·GoPro·Insta360·Action이 있으면 제외. 컷(+핸들) 구간을 회전 적용 후 960px로 `vidstabdetect`(ASCII) → 프레임별 국소 움직임 중간값 경로의 고주파 RMS가 화면 폭 0.3% 이상일 때만, LM 좌표·크기를 원본 해상도 배율로 키운 `.trf`로 `vidstabtransform`(smoothing = fps×0.5, optzoom=1, bicubic)을 톤매핑·회전 뒤·스케일 앞에 적용. 실측: 아이폰 걷는 컷 흔들림 2.52% → 1.36%.
* **세로 쇼츠 ([`create_vertical_reframe`](file:///d:/workspace/autocut/autocut_engine.py))**: 완성본을 5fps·480px로 디코딩 → YuNet 얼굴(면적×점수 가중 중심), 없으면 스펙트럼 잔차 saliency 중심을 60%만 반영 → 샷별 1초 가우시안 + 샷·프레임별 실제 화면 범위(검은 여백·HUD 띠 제외) 안으로 제한 → `sendcmd`로 0.1초마다 `crop x` 갱신 → 1080×1920 lanczos + 세로용 HUD(`ui_scale=1`, HLG면 75%). 비트레이트는 화소 비율×1.3(최소 8Mbps). 실측(제주 75초): 얼굴 검출 93% 프레임.
* **화자 구분 ([`diarize_speakers`](file:///d:/workspace/autocut/autocut_engine.py), 실험적)**: 0.6초 이상 문장의 16kHz 음성(긴 문장은 가운데 8초) → `microsoft/wavlm-base-plus-sv` x-vector(정규화, float16 base64 캐시) → 코사인 거리 평균 연결 군집(임계 0.42, 최대 6명) → 말 비중 5% 미만 묶음은 가장 가까운 화자로 → 말한 시간순으로 화자 1, 2… 짧은 문장은 같은 클립의 가장 가까운 문장 화자를 따름. ASS에서 화자별 `\c` 색. (FilmCraft식 MFCC 벡터는 클립마다 장소·마이크가 달라 화자보다 환경으로 묶여 채택하지 않음.)

---

### ⑤ 출력 인코딩 사양 (HEVC 기본 · 해상도 · 프레임 · bps 화질)
* **압축 코덱 ([`_build_vcodec_args`](file:///d:/workspace/autocut/autocut_engine.py#L219-L250))**:
  * 고화질·고효율 압축을 위해 **NVIDIA 하드웨어 가속 `HEVC (H.265 - hevc_nvenc, -tag:v hvc1)`**를 기본 코덱으로 사용하며, `H.264 (h264_nvenc)`로도 전환할 수 있습니다.
* **사용자 지정 해상도 · 프레임 · 비트레이트(`bps`)**:
  * **출력 해상도**: `3840x2160 (4K UHD)`, `2560x1440 (QHD)`, `1920x1080 (FHD, 기본)`, `1280x720 (HD)`
  * **프레임 레이트**: `60 fps` (기본), `30 fps`, `24 fps`
  * **화질 (`bps` 단위)**: 기본 `25,000,000 bps` (`25 Mbps`), 프리셋 선택(`12,000,000` ~ `80,000,000 bps`) 또는 원하는 숫자 직접 입력 지원.

---

### ⑥ 유튜브 URL 편집 스타일 학습기 & 프리셋
* **유튜브 영상 스타일 자동 분석 ([`analyze_youtube_style`](file:///d:/workspace/autocut/autocut_engine.py#L2300-L2555))**:
  * 원하는 유튜브 영상 URL(예: 가족 여행 브이로그)을 입력하면 `yt-dlp`로 샘플 구간을 받아 **컷 전환 속도(평균 컷 길이), 색감(밝기·채도·따뜻한 톤), 화면 전환 비율**을 분석해 `custom_styles.json`에 프리셋으로 자동 등록합니다.
  * 기본 제공 5종 프리셋(표준 가족·여행, 감성 브이로그, 다이내믹 하이라이트, 시네마틱 드라이브, 도란도란 가족 토크) + 학습된 유튜브 스타일 프리셋을 즉시 선택해 적용할 수 있습니다.

---

### ⑦ 스튜디오급 다크 UI & 하드웨어 모니터
* **깔끔한 3단 카드 레이아웃 ([`AutoCutStudioApp._build_ui`](file:///d:/workspace/autocut/autocut_gui.py#L510-L855))**:
  * **좌측 카드**: 날짜별 폴더 목록, 실시간 미디어 개수 배지, `🔄 개수 새로고침 (F5)`, 사진 포함 옵션
  * **폴더별 편집 모드 (`folder_edit_modes`, `_show_mode_for_folder`, v3.1.0)**: 편집 모드 라디오는 타임라인에 열린 날짜 폴더의 모드를 보여 주고 바꾸면 그 폴더에만 저장(기본 모드와 같으면 폴더 설정 삭제). 기본 모드는 예전 설정 키 `edit_mode`(`default_edit_mode`)로 저장돼 이전 작업 상태와 호환. 목록 항목에 모드 표시(`✎` = 폴더 전용), `↳ 모든 폴더에 적용` = 기본 모드 변경 + 폴더 설정 초기화. 편집 시작 시 UI 스레드에서 폴더별 모드를 고정해 `_worker_run`에 넘기고 폴더마다 `run_autocut_pipeline(edit_mode=…)`. 작업 상태 JSON의 `folder_edit_modes`({폴더 이름: 모드})에 저장.
  * **중앙 카드**: 편집 모드(일반/하이라이트), 인트로 제목·로고 입력창(글자 겹침 없는 2열 다크 폼), 큼직한 `✔` (v-체크) 커스텀 체크박스 옵션
  * **우측 카드**: 출력 인코딩 사양(`HEVC`, 해상도, FPS, `bps`), 스타일 프리셋 및 유튜브 URL 학습기
  * **하단 상태 줄**: 실시간 **CPU · GPU0(iGPU) · GPU1(NVIDIA, NVENC·VRAM) · NPU 사용률 숫자**([`SystemResourceMonitor`](file:///d:/workspace/autocut/autocut_gui.py) — 작업 관리자와 같은 Windows `GPU Engine` 성능 카운터, GPU 번호도 작업 관리자 순서)를 상태 줄 오른쪽에 표시하고, **사용률 그래프와 실시간 로그는 각각 플로팅 창**(체크박스로 표시/숨김, 창 ✕ = 체크 해제, 위치 저장 — 로그 v2.7.0, 그래프 v2.9.1). 비워진 공간은 위쪽 날짜 폴더 목록이 사용.
  * **Ollama 연결 표시 (v3.0.1)**: 시작 시 `detect_ollama_vlm_model`(서버 자동 실행, 15초 대기) 후에도 서버가 안 뜨면 3초마다 `_ollama_list_models`로 최대 2분 더 확인 (부팅 직후 Ollama 콜드 스타트가 20초 이상 걸려 `미연결`로 고정되던 문제). 라벨 클릭 = 다시 확인.
  * **좌측 상단 날짜 (`display_date`, `resolve_display_date`, v2.9.1)**: 입력값(날짜 형식이면 `YYYY. MM. DD`로 정리) → 폴더 이름 날짜 → 폴더 안 가장 이른 촬영 날짜 순으로 정해 HUD와 인트로에 표시.
  * **최하단 타임라인 ([`TimelinePanel`](file:///d:/workspace/autocut/autocut_timeline.py))**: 선택한 날짜 폴더의 영상·사진을 썸네일 클립으로 배치. 드래그·`Ctrl+←/→`로 순서 변경, 우클릭으로 기기 시간 보정·회전, ⚠ 시각 이상 표시, HDR 배지. `Ctrl/Shift+클릭`·`Ctrl+A` 다중 선택 후 함께 이동·회전 (v2.6.0), 굵고 대비 높은 스크롤 바 + `보는 중 a–b / N` 위치 표시.
* **프로그램 아이콘 (v2.6.0 · v2.8.1)**: 시네마 카메라 아이콘(`assets/autocut_icon.ico`, 16~256px 15가지 크기)이 창 제목줄·작업 표시줄·스플래시에 표시됩니다. 창 아이콘은 `WM_SETICON`으로 DPI에 맞는 정확한 크기(200%: 64/32px)를 넣어 선명하게 하고, 작업 표시줄 고정은 앱 ID `YumTaeng.AutoCutStudio.2026`을 가진 바로가기로 해야 카메라 아이콘으로 묶입니다.
* **종료 시 인코딩 정리 (`enable_kill_children_on_exit`, `terminate_child_processes`, v2.7.1)**: 앱 프로세스를 KILL_ON_JOB_CLOSE 작업 개체에 넣어 강제 종료·오류 시에도 ffmpeg가 함께 종료되고(Ollama는 job 이탈), 편집 중 창을 닫으면 확인 후 즉시 중단 + `autocut_render_*` 임시 폴더 삭제(남은 폴더는 다음 실행 때 30분 이상 방치된 것만 정리). 최종 영상은 `*.partial.mp4` → 완료 시 이름 변경.
* **작업 상태 저장 (v2.4.0 · v2.8.0)**: 설정·선택 폴더를 자동 저장/복원. `Ctrl+S`(수동 저장) 시 **작업 폴더에 `AutoCut_작업저장.autocut.json`**(설정 + 모든 폴더 타임라인)을 생성하고, `폴더 찾기`로 그 폴더를 고르면 불러올지 확인 (폴더 위치가 바뀌어도 그 폴더 기준으로 복원). 메뉴에서 임의 위치로 내보내기/불러오기도 가능.

---

## 📦 4. 편집 완료 시 생성되는 결과물 (`편집결과_YYYY-MM-DD` 폴더)

1. **`YYYY-MM-DD_무자막_필모라용.mp4`** (항상 생성): 3초 인트로 + 상단 날짜/로고 + 상황별 트랜지션(실제 영상 겹침) + 오디오 크로스페이드 + 동적 BGM이 합성된 완성 영상 (자막은 SRT로 별도 제공)
2. **`YYYY-MM-DD_자막포함_완성본.mp4`** (옵션 · 기본 꺼짐, v2.7.0): `자막 입힌 완성본도 만들기`를 켠 경우에만 생성. 완성 영상 전체를 다시 인코딩하며 `ass` 자막 렌더링이 CPU 위주라(실측: CPU 100% · NVENC 0%) 긴 영상은 수십 분이 추가됩니다.
3. **`YYYY-MM-DD_전체자막(필모라용).srt`**: 필모라 타임라인에 바로 드래그 앤 드롭할 수 있는 음성 대화 + 장면 캡션 자막 파일
4. **`YYYY-MM-DD_음성자막(필모라용).srt`**: 순수 한국어 음성 대화만 담긴 SRT 자막 파일
5. **`YYYY-MM-DD_베스트샷.jpg`**: AI가 추출한 해당 날짜의 베스트 하이라이트 원본 사진
6. **`YYYY-MM-DD_편집분석리포트.json`**: 클립별 촬영 시각, 채택 구간, 음성 대화 타임스탬프가 기록된 분석 캐시 리포트
7. **`YYYY-MM-DD_타임라인순서.json`** (타임라인에서 손댄 경우만): 사용자 지정 클립 순서, 기기별 시간 보정, 클립 회전 — [자동 편집 시작] 시 그대로 적용
8. **`YYYY-MM-DD_세로쇼츠_9x16.mp4`** (옵션, v3.0.0): 얼굴을 따라가는 1080×1920 세로 버전
9. **`YYYY-MM-DD_화자자막(필모라용).srt`** (옵션, v3.0.0): `[화자 N]` 음성 자막
10. **분석 캐시** `_자동색보정.json` · `_컬러매치.json` · `_음량.json` · `_화자분석.json`

---

## 🚀 5. 실행 방법

* **바탕화면/폴더에서 바로 실행**: [`AutoCut_Studio.vbs`](file:///d:/workspace/autocut/AutoCut_Studio.vbs) 또는 [`AutoCut_Studio.bat`](file:///d:/workspace/autocut/AutoCut_Studio.bat) 더블클릭 (검은색 콘솔 창 없이 Adobe Premiere 스타일 스플래시 로고 후 메인 스튜디오 창 실행)
* **터미널에서 실행**:
  ```powershell
  pythonw d:\workspace\autocut\autocut_gui.py
  ```
