"""
한국 시장 안드로이드 디바이스 풀

4,000개 계정이 전부 같은 Pixel 8 Pro로 나가면 비현실적이다.
한국에서 실제로 쓰는 기종을 가중치 기반으로 뽑아서 계정마다 다른 폰을 배정한다.

배정 규칙:
  - 계정 등록 시 1회 배정, 이후 불변
  - 세션 파일에 디바이스가 저장됨 → load_settings가 복원
  - 첫 로그인에만 set_device 적용, 이후는 세션이 우선
"""

import random

# weight = 한국 시장 점유율 기반 상대 가중치
DEVICES = [
    # ── 갤럭시 S24 시리즈 (2024, 최신) ──
    {
        "model": "SM-S928N", "device": "e3q", "manufacturer": "Samsung",
        "android_version": 34, "android_release": "14",
        "dpi": "510dpi", "resolution": "1440x3120", "cpu": "s5e9945",
        "weight": 12,
    },
    {
        "model": "SM-S926N", "device": "e2q", "manufacturer": "Samsung",
        "android_version": 34, "android_release": "14",
        "dpi": "390dpi", "resolution": "1080x2340", "cpu": "s5e9945",
        "weight": 8,
    },
    {
        "model": "SM-S921N", "device": "e1q", "manufacturer": "Samsung",
        "android_version": 34, "android_release": "14",
        "dpi": "420dpi", "resolution": "1080x2340", "cpu": "s5e9945",
        "weight": 10,
    },

    # ── 갤럭시 S23 시리즈 (2023) ──
    {
        "model": "SM-S918N", "device": "dm3q", "manufacturer": "Samsung",
        "android_version": 34, "android_release": "14",
        "dpi": "500dpi", "resolution": "1440x3088", "cpu": "s5e9925",
        "weight": 10,
    },
    {
        "model": "SM-S916N", "device": "dm2q", "manufacturer": "Samsung",
        "android_version": 34, "android_release": "14",
        "dpi": "390dpi", "resolution": "1080x2340", "cpu": "s5e9925",
        "weight": 6,
    },
    {
        "model": "SM-S911N", "device": "dm1q", "manufacturer": "Samsung",
        "android_version": 34, "android_release": "14",
        "dpi": "420dpi", "resolution": "1080x2340", "cpu": "s5e9925",
        "weight": 8,
    },

    # ── 갤럭시 S22 시리즈 (2022) ──
    {
        "model": "SM-S908N", "device": "b0q", "manufacturer": "Samsung",
        "android_version": 34, "android_release": "14",
        "dpi": "500dpi", "resolution": "1440x3088", "cpu": "s5e9925",
        "weight": 6,
    },
    {
        "model": "SM-S901N", "device": "r0q", "manufacturer": "Samsung",
        "android_version": 34, "android_release": "14",
        "dpi": "420dpi", "resolution": "1080x2340", "cpu": "s5e9925",
        "weight": 5,
    },

    # ── 갤럭시 S21 시리즈 (2021) ──
    {
        "model": "SM-G998N", "device": "o1s", "manufacturer": "Samsung",
        "android_version": 33, "android_release": "13",
        "dpi": "515dpi", "resolution": "1440x3200", "cpu": "exynos2100",
        "weight": 4,
    },
    {
        "model": "SM-G991N", "device": "o1s", "manufacturer": "Samsung",
        "android_version": 33, "android_release": "13",
        "dpi": "420dpi", "resolution": "1080x2400", "cpu": "exynos2100",
        "weight": 4,
    },

    # ── 갤럭시 A 시리즈 (보급형, 점유율 높음) ──
    {
        "model": "SM-A546N", "device": "a54x", "manufacturer": "Samsung",
        "android_version": 34, "android_release": "14",
        "dpi": "403dpi", "resolution": "1080x2340", "cpu": "s5e8835",
        "weight": 8,
    },
    {
        "model": "SM-A346N", "device": "a34x", "manufacturer": "Samsung",
        "android_version": 34, "android_release": "14",
        "dpi": "403dpi", "resolution": "1080x2340", "cpu": "s5e8835",
        "weight": 5,
    },
    {
        "model": "SM-A556N", "device": "a55x", "manufacturer": "Samsung",
        "android_version": 34, "android_release": "14",
        "dpi": "390dpi", "resolution": "1080x2340", "cpu": "s5e8845",
        "weight": 6,
    },

    # ── 갤럭시 Z 시리즈 (폴더블) ──
    {
        "model": "SM-F946N", "device": "q5q", "manufacturer": "Samsung",
        "android_version": 34, "android_release": "14",
        "dpi": "373dpi", "resolution": "1812x2176", "cpu": "s5e9925",
        "weight": 3,
    },
    {
        "model": "SM-F731N", "device": "b5q", "manufacturer": "Samsung",
        "android_version": 34, "android_release": "14",
        "dpi": "426dpi", "resolution": "1080x2640", "cpu": "s5e9925",
        "weight": 3,
    },

    # ── Pixel (소수지만 있어야 자연스러움) ──
    {
        "model": "Pixel 8 Pro", "device": "husky", "manufacturer": "Google/google",
        "android_version": 34, "android_release": "14",
        "dpi": "480dpi", "resolution": "1344x2992", "cpu": "husky",
        "weight": 1,
    },
    {
        "model": "Pixel 8", "device": "shiba", "manufacturer": "Google/google",
        "android_version": 34, "android_release": "14",
        "dpi": "420dpi", "resolution": "1080x2400", "cpu": "shiba",
        "weight": 1,
    },
    {
        "model": "Pixel 7", "device": "panther", "manufacturer": "Google/google",
        "android_version": 34, "android_release": "14",
        "dpi": "420dpi", "resolution": "1080x2400", "cpu": "panther",
        "weight": 1,
    },
]

# instagrapi 앱 버전은 전 기종 공통
APP_VERSION = "428.0.0.47.67"
VERSION_CODE = "961145276"


def pick_device(seed: str | None = None) -> dict:
    """
    디바이스를 가중치 기반으로 뽑는다.

    seed를 주면 같은 seed에 대해 항상 같은 디바이스가 나온다 (결정적).
    계정 username을 seed로 쓰면 재등록해도 같은 기종을 받는다.
    """
    if seed:
        rng = random.Random(seed)
    else:
        rng = random

    weights = [d["weight"] for d in DEVICES]
    chosen = rng.choices(DEVICES, weights=weights, k=1)[0]

    return {
        "android_version": chosen["android_version"],
        "android_release": chosen["android_release"],
        "dpi": chosen["dpi"],
        "resolution": chosen["resolution"],
        "manufacturer": chosen["manufacturer"],
        "device": chosen["device"],
        "model": chosen["model"],
        "cpu": chosen["cpu"],
        "app_version": APP_VERSION,
        "version_code": VERSION_CODE,
    }


def device_label(device: dict) -> str:
    """사람이 읽을 수 있는 기종명"""
    return f"{device.get('manufacturer', '?')} {device.get('model', '?')}"


def distribution_stats(models: list[str]) -> dict[str, int]:
    """기종 분포 통계"""
    counts: dict[str, int] = {}
    for m in models:
        counts[m] = counts.get(m, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))
