# 📦 AutoCut Studio 2026 — 버전 관리 기록 (CHANGELOG)

All notable changes to **AutoCut Studio 2026** are automatically versioned and tracked in this file.

---

## [v2.2.0] - 2026-10-18
### 🚀 SOTA AI 모델 4대 엔진 기본값 탑재 & 자동 버전 관리 시스템 도입
- **AI 음성 인식 & VAD 엔진 기본값 업그레이드 (`Faster-Whisper large-v3-turbo`)**:
  - 기존 `small` 모델에서 **`large-v3-turbo` (`mobiuslabsgmbh/faster-whisper-large-v3-turbo`)**를 기본 모델로 격상하여 속도는 유지하면서 아이들 목소리·식당/여행지 소음 환경의 한국어 대화 인식률을 극대화했습니다.
- **비전 미학 & 베스트 샷 평가 엔진 기본값 탑재 (`CLIP Aesthetic Predictor — SigLIP / LAION-Aesthetics V2.5`)**:
  - `SigLIP / CLIP ViT-B-32 + LAION-Aesthetics V2.5` 미학 예측기를 기본 탑재하여, 인트로 3초 베스트 샷 선정 및 하이라이트/여행 컷 추출 시 구도·색감·조명·인물 표정의 미학 점수(`0~10점`)를 딥러닝으로 채점합니다.
- **장면 전환 & 샷 경계 검출기 기본값 탑재 (`TransNetV2 / PySceneDetect ContentDetector`)**:
  - `PySceneDetect ContentDetector / AdaptiveDetector` 및 `TransNetV2` 기반 샷 경계 검출을 기본 적용하여, 컷 편집 구간이 카메라 전환점을 자연스럽게 따라가고 유튜브 레퍼런스 영상의 하드 컷/디졸브 전환 호흡을 정밀하게 학습합니다.
- **멀티모달 영상 맥락 이해 & 하이라이트 엔진 기본값 탑재 (`Qwen2.5-VL (7B) / InternVL 2.5`)**:
  - `Qwen2.5-VL (7B) / InternVL 2.5` (및 고속 멀티모달 시맨틱 비전 분류기 연동)를 기본 탑재하여 영상 장면을 이해하고 한국어 상황 캡션(예: *"푸른 제주 해변 산책"*, *"맛집 식사 및 가족 대화"*)과 하이라이트 가중치를 자동 부여합니다.
- **GPU VRAM 스마트 순차 메모리 관리 (RTX 5070 8GB 최적화)**:
  - 음성 인식(Whisper) ➔ 비전/샷 검출(TransNetV2/SceneDetect) ➔ 미학/멀티모달 분석(SigLIP/Qwen2.5-VL) 단계별로 모델 사용 직후 `gc.collect()` 및 `torch.cuda.empty_cache()`를 자동 수행하여 VRAM 충돌(OOM) 없이 최고 속도로 동작합니다.
- **SW 버전 관리 자동화 (`VERSION`, `CHANGELOG.md`, Git Tag `v2.2.0`, GitHub Release)**:
  - 코드 내 `AUTOCUT_VERSION = "2.2.0"` 상수, GUI 상단/타이틀바 버전 표기, 편집 분석 리포트 JSON 버전 기록 및 Git 태그/릴리즈 자동 관리를 적용했습니다.

---

## [v2.1.0] - 2026-10-18
### ✨ 신규 편집 기능 & 4K 60fps HEVC 기본 사양 업그레이드
- **DJI & D-Log / D-Log M (10-bit) 색감 자동 감지 및 Rec.709 복원** (`detect_dji_or_dlog_clip`, `get_color_profile_filter`)
- **베스트 샷 3초 인트로 디자인 5종 + 랜덤 선택 추가** (`classic_frame`, `cinematic_bar`, `magazine_lower`, `minimal_glass`, `vlog_stamp`, `random`)
- **4가지 영상 편집 모드 지원**: `여행 (기본값, travel)`, `그냥 대화 (talk)`, `하이라이트 요약 (highlight)`, `드라이브·풍경 감상 (drive)`
- **출력 기본값 상향**: `3840x2160 (4K UHD)`, `60fps`, `HEVC (hevc_nvenc)`, `25,000,000 bps (25 Mbps)`

---

## [v2.0.0] - 2026-10-17
### 🎨 다크 스튜디오 UI 리디자인 & 메타데이터 시간순 통합 편집
- 3열 다크 스튜디오 레이아웃, `22x22` `✔` 체크박스, 2초 주기 폴더 변경 자동 감지 및 `F5` 새로고침
- 동영상(`QuickTime creationdate` / `creation_time`)과 사진(`EXIF DateTimeOriginal`) 메타데이터 기반 촬영 시간순 자동 정렬
- 상하 반전 자동 보정(`_ensure_upright_bgr`) 및 잔잔한 무료 배경음악(BGM) 다이내믹 볼륨 더킹(영상 `6%` / 사진·인트로 `38%`)
