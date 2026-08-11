"""
9개 계정 자동 로그인 — ADB 비행기모드 토글로 IP 자동 변경

흐름:
  계정마다:
    1. ADB로 비행기모드 ON → OFF (IP 변경)
    2. 새 IP가 잡힐 때까지 대기
    3. 로그인 + 세션 저장 + 세션 재사용 검증
    4. 결과 기록
"""

import os
import json
import subprocess
import time
import logging
import requests
from pathlib import Path

import login_test

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ADB 디바이스 ID와 가정망 IP는 환경변수로 지정 (없으면 자동 감지)
DEVICE = os.environ.get("ADB_DEVICE", "")
HOME_IP = os.environ.get("HOME_IP", "")


def detect_device() -> str:
    """연결된 첫 번째 실기기 ID 자동 감지"""
    if DEVICE:
        return DEVICE
    r = subprocess.run("adb devices", shell=True, capture_output=True, text=True, timeout=15)
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device" and "emulator" not in parts[0]:
            return parts[0]
    return ""


def adb(cmd: str) -> str:
    r = subprocess.run(
        f"adb -s {DEVICE} {cmd}",
        shell=True, capture_output=True, text=True, timeout=15,
    )
    return r.stdout.strip()


def get_ip() -> str:
    for _ in range(3):
        try:
            return requests.get(
                "https://api.ipify.org?format=json", timeout=10
            ).json()["ip"]
        except Exception:
            time.sleep(2)
    return "error"


def rotate_ip(prev_ip: str) -> str:
    """ADB 비행기모드 토글 → 새 IP 반환"""
    logger.info("✈️  비행기모드 ON...")
    adb("shell cmd connectivity airplane-mode enable")
    time.sleep(4)

    logger.info("✈️  비행기모드 OFF...")
    adb("shell cmd connectivity airplane-mode disable")

    # USB 테더링이 살아날 때까지 대기
    for attempt in range(30):
        time.sleep(3)
        ip = get_ip()
        if ip == "error":
            continue
        if ip == HOME_IP:
            # 가정망으로 빠졌다 — 테더링이 아직 안 살아남
            continue
        if ip != prev_ip:
            logger.info(f"✅ IP 변경: {prev_ip} → {ip} ({(attempt+1)*3}초)")
            return ip
        # 같은 IP가 나올 수도 있다 (통신사가 재할당)
        # 3번 연속 같으면 통과
        if attempt >= 5:
            logger.info(f"⚠️  같은 IP 재할당: {ip} (진행)")
            return ip

    logger.warning(f"❌ IP 변경 실패 — 현재: {get_ip()}")
    return get_ip()


def main():
    global DEVICE, HOME_IP
    DEVICE = detect_device()
    if not DEVICE:
        logger.error("연결된 ADB 실기기가 없다. USB 디버깅을 켜고 폰을 연결해라.")
        return

    accounts = login_test.ACCOUNTS
    if not accounts:
        logger.error("login_accounts.txt 에 계정을 넣어라 (아이디:비번 한 줄에 하나씩)")
        return

    results = []
    current_ip = get_ip()

    print("=" * 62)
    print(f"  자동 로그인 테스트 — {len(accounts)}개 계정")
    print(f"  디바이스: {DEVICE}")
    print(f"  현재 IP: {current_ip}")
    print("=" * 62)

    if HOME_IP and current_ip == HOME_IP:
        logger.error("가정망 IP로 나가고 있다 — USB 테더링을 켜라")
        return

    for i, (username, password) in enumerate(accounts, 1):
        print(f"\n{'─'*62}")
        print(f"  [{i}/{len(accounts)}] {username}")
        print(f"{'─'*62}")

        # 첫 계정은 현재 IP로, 이후부터 비행기모드 토글
        if i > 1:
            current_ip = rotate_ip(current_ip)
            if current_ip == "error":
                logger.error("인터넷 연결 불가 — 중단")
                break
            if current_ip == HOME_IP:
                logger.error("가정망 IP로 전환됨 — 테더링 확인 필요, 중단")
                break

        # 로그인
        result = login_test.login_account(username, password, i, len(accounts))
        results.append(result)

        # 결과 즉시 출력
        icon = {
            "ready": "✅", "challenge": "🔐", "2fa": "🔑",
            "bad_pw": "❌", "banned": "⛔", "failed": "⚠️",
        }.get(result["status"], "•")
        logger.info(
            f"{icon} {username} → {result['status']} "
            f"({result['message'][:50]}) "
            f"[IP:{result['ip']}] [{result['took_sec']:.1f}초]"
        )

        if result.get("session_reload_ok"):
            logger.info(f"   📁 세션 저장+복원 검증 완료")

        # 중간 저장
        login_test.save_results(results)

        # 계정 사이 안전 대기
        if i < len(accounts):
            wait = 5
            logger.info(f"   ⏳ {wait}초 대기 후 다음 계정...")
            time.sleep(wait)

    # 최종 요약
    login_test.print_summary(results)
    login_test.save_results(results)

    print(f"\n결과 파일: login_test_results.json")
    print(f"세션 파일: sessions/ 디렉토리")


if __name__ == "__main__":
    main()
