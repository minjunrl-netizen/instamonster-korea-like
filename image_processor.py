"""
이미지 처리 — 업로드 전 안티탐지 파이프라인

목적:
  - EXIF 메타데이터 제거 (기기/GPS/편집프로그램 흔적 삭제)
  - 미세 변형 (같은 원본이어도 파일마다 고유해짐 → 중복 감지 회피)
  - 계정 기종에 맞춘 자연스러운 가짜 EXIF 삽입 (진짜 폰 사진처럼)

같은 사진을 여러 계정에 재활용할 때 각 계정마다 다른 파일로 나가게 해서
인스타의 이미지 중복/봇 연관 감지를 피한다.

동영상(mp4/mov)은 EXIF 개념이 다르므로 그대로 둔다 (원본 재활용 시 주의).
"""

import io
import random
import logging
from pathlib import Path
from datetime import datetime, timedelta

from PIL import Image, ImageEnhance
import piexif

logger = logging.getLogger(__name__)

IMAGE_EXT = {".jpg", ".jpeg", ".png"}

# 기종별 EXIF Make/Model (계정 디바이스와 맞추기 위함)
DEVICE_EXIF = {
    "Samsung": [("samsung", "SM-S928N"), ("samsung", "SM-S921N"),
                ("samsung", "SM-S918N"), ("samsung", "SM-A546N")],
    "Google/google": [("Google", "Pixel 8 Pro"), ("Google", "Pixel 8")],
}

# 흔한 촬영 설정 값 (자연스러운 범위)
FOCAL_LENGTHS = [(24, 10), (26, 10), (52, 10)]   # (분자, 분모) = mm
F_NUMBERS = [(18, 10), (20, 10), (24, 10)]       # f/1.8, f/2.0, f/2.4
ISO_VALUES = [50, 64, 80, 100, 125, 200, 400]


def _rational(value: tuple[int, int]) -> tuple[int, int]:
    return value


def _now_exif(days_ago_max: int = 30) -> str:
    """최근 N일 내 랜덤 촬영 시각 (EXIF 형식)"""
    dt = datetime.now() - timedelta(
        days=random.randint(0, days_ago_max),
        hours=random.randint(0, 23),
        minutes=random.randint(0, 59),
    )
    return dt.strftime("%Y:%m:%d %H:%M:%S")


def build_fake_exif(device_model: str | None = None) -> bytes:
    """
    계정 기종에 맞는 자연스러운 가짜 EXIF를 만든다.
    GPS는 넣지 않는다 (위치 노출 위험 + 요즘 폰은 기본 off 많음).
    """
    # 기종 결정
    make, model = "samsung", "SM-S921N"
    if device_model:
        for mk, models in DEVICE_EXIF.items():
            for m_make, m_model in models:
                if m_model == device_model:
                    make, model = m_make, m_model
                    break
    else:
        make, model = random.choice(DEVICE_EXIF["Samsung"])

    shot_time = _now_exif()
    focal = random.choice(FOCAL_LENGTHS)
    fnum = random.choice(F_NUMBERS)
    iso = random.choice(ISO_VALUES)

    zeroth = {
        piexif.ImageIFD.Make: make,
        piexif.ImageIFD.Model: model,
        piexif.ImageIFD.Software: f"{model} Camera",
        piexif.ImageIFD.DateTime: shot_time,
        piexif.ImageIFD.Orientation: 1,
    }
    exif = {
        piexif.ExifIFD.DateTimeOriginal: shot_time,
        piexif.ExifIFD.DateTimeDigitized: shot_time,
        piexif.ExifIFD.FNumber: fnum,
        piexif.ExifIFD.ISOSpeedRatings: iso,
        piexif.ExifIFD.FocalLength: focal,
        piexif.ExifIFD.ExposureProgram: 2,   # Normal program
        piexif.ExifIFD.WhiteBalance: 0,       # Auto
        piexif.ExifIFD.Flash: 16,             # off, did not fire
    }
    return piexif.dump({"0th": zeroth, "Exif": exif, "GPS": {}, "1st": {}, "thumbnail": None})


def process_image(src: str, dst: str | None = None,
                  device_model: str | None = None,
                  add_fake_exif: bool = True) -> str:
    """
    이미지 1장을 안티탐지 처리한다.

    1. 로드 + RGB 변환 (EXIF 통째로 사라짐)
    2. 미세 변형: 랜덤 1~3px 크롭 + 밝기/대비/채도 미세 조정
       → 픽셀 데이터가 원본과 미세하게 달라져 파일 해시/이미지 유사도가 바뀜
    3. (선택) 계정 기종에 맞는 가짜 EXIF 삽입
    4. JPEG로 저장 (품질 랜덤 88~96 → 파일 크기도 매번 다름)

    dst 없으면 원본 옆에 _p 붙여 저장. 반환값은 결과 경로.
    """
    src_path = Path(src)
    if src_path.suffix.lower() not in IMAGE_EXT:
        # 이미지가 아니면 그대로 반환 (동영상 등)
        return src

    img = Image.open(src_path)
    img = img.convert("RGB")  # EXIF/알파/팔레트 제거

    # ── 미세 변형 ──
    w, h = img.size
    # 가장자리 랜덤 1~3px 크롭
    cl = random.randint(1, 3); ct = random.randint(1, 3)
    cr = random.randint(1, 3); cb = random.randint(1, 3)
    img = img.crop((cl, ct, w - cr, h - cb))

    # 밝기/대비/채도 미세 조정 (±0.5~1.5%)
    def jitter(enhancer_cls, amount):
        factor = 1.0 + random.uniform(-amount, amount)
        return enhancer_cls(img).enhance(factor)

    img = jitter(ImageEnhance.Brightness, 0.015)
    img = jitter(ImageEnhance.Contrast, 0.015)
    img = jitter(ImageEnhance.Color, 0.02)

    # ── 저장 ──
    out = Path(dst) if dst else src_path.with_name(src_path.stem + "_p.jpg")
    quality = random.randint(88, 96)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    data = buf.getvalue()

    if add_fake_exif:
        try:
            exif_bytes = build_fake_exif(device_model)
            # piexif로 EXIF를 삽입한 새 바이트 생성
            out_buf = io.BytesIO()
            piexif.insert(exif_bytes, data, out_buf)
            data = out_buf.getvalue()
        except Exception as e:
            logger.debug(f"가짜 EXIF 삽입 실패(무시): {e}")

    out.write_bytes(data)
    logger.info(f"이미지 처리 완료: {out.name} (품질 {quality}, 크롭 {cl},{ct},{cr},{cb})")
    return str(out)


def process_for_upload(paths: list[str], device_model: str | None = None,
                       add_fake_exif: bool = True) -> tuple[list[str], list[str]]:
    """
    업로드할 파일 목록을 처리한다.
    이미지는 안티탐지 처리, 동영상은 그대로.

    반환: (처리된 경로 목록, 임시 생성된 파일 목록[정리용])
    """
    processed = []
    temps = []
    for p in paths:
        path = Path(p)
        if path.suffix.lower() in IMAGE_EXT:
            out = process_image(p, device_model=device_model, add_fake_exif=add_fake_exif)
            processed.append(out)
            if out != p:
                temps.append(out)
        else:
            processed.append(p)  # 동영상 등 원본 유지
    return processed, temps


def exif_summary(path: str) -> dict:
    """사진의 EXIF 요약 (처리 전후 확인용)"""
    try:
        exif = piexif.load(path)
        zeroth = exif.get("0th", {})
        return {
            "has_exif": bool(zeroth or exif.get("Exif")),
            "make": (zeroth.get(piexif.ImageIFD.Make, b"") or b"").decode("utf-8", "ignore") if isinstance(zeroth.get(piexif.ImageIFD.Make), bytes) else zeroth.get(piexif.ImageIFD.Make, ""),
            "model": (zeroth.get(piexif.ImageIFD.Model, b"") or b"").decode("utf-8", "ignore") if isinstance(zeroth.get(piexif.ImageIFD.Model), bytes) else zeroth.get(piexif.ImageIFD.Model, ""),
            "has_gps": bool(exif.get("GPS")),
        }
    except Exception:
        return {"has_exif": False, "make": "", "model": "", "has_gps": False}
