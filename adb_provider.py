"""
ADB 테더링 프로바이더

xProxy 장비가 오기 전, USB로 연결한 폰 1대를 슬롯 1개짜리 프록시처럼 써서
웹 시스템(대량 로그인/좋아요)의 전체 흐름을 실제로 테스트할 수 있게 한다.

xProxy와의 차이:
  - 프록시 포트가 없다. 트래픽은 기본 라우트(USB 테더링)로 나간다.
  - IP 변경은 ADB로 비행기모드를 껐다 켜서 한다.
  - 슬롯이 1개뿐이라 동시 병렬이 아니라 순차 처리다.

XProxyManager와 같은 메서드를 노출해서 bulk_login / order_processor가
프로바이더 종류를 몰라도 그대로 동작한다. is_direct=True 로 구분한다.
"""

import time
import logging
import threading
import subprocess

import requests

logger = logging.getLogger(__name__)

IP_CHECK_URL = "https://api.ipify.org?format=json"


def list_adb_devices() -> list[str]:
    """연결된 실기기 목록 (에뮬레이터 제외)"""
    try:
        r = subprocess.run("adb devices", shell=True, capture_output=True,
                           text=True, timeout=15)
    except Exception as e:
        logger.warning(f"adb 실행 실패: {e}")
        return []
    out = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device" and "emulator" not in parts[0]:
            out.append(parts[0])
    return out


class ADBProvider:
    """USB 테더링 폰을 슬롯 1개짜리 프록시처럼 다룬다 (테스트/장비 대체용)"""

    is_direct = True  # 프록시 없이 기본 라우트(테더링) 사용

    def __init__(self, device: str = "", home_ip: str = "", name: str = "ADB-테더링"):
        # device 비어있으면 자동 감지
        self.device = device or (list_adb_devices()[0] if list_adb_devices() else "")
        self.home_ip = str(home_ip or "").strip()
        self.slots = [{"port": 0, "name": name, "modem": 1, "device": self.device}]
        self._lock = threading.Lock()

    # ─── ADB 헬퍼 ───

    def _adb(self, cmd: str) -> str:
        if not self.device:
            return ""
        try:
            r = subprocess.run(f"adb -s {self.device} {cmd}", shell=True,
                               capture_output=True, text=True, timeout=20)
            return r.stdout.strip()
        except Exception as e:
            logger.warning(f"adb 명령 실패({cmd}): {e}")
            return ""

    @staticmethod
    def _current_ip() -> str:
        for _ in range(3):
            try:
                return requests.get(IP_CHECK_URL, timeout=10).json()["ip"]
            except Exception:
                time.sleep(2)
        return "error"

    # ─── XProxyManager 호환 인터페이스 ───

    def get_proxy_url(self, slot_index: int) -> str:
        """테더링은 프록시가 없다 → 빈 문자열(직결=테더링 라우트)"""
        return ""

    def slot_name(self, slot_index: int = 0) -> str:
        return self.slots[slot_index].get("name", "ADB-테더링")

    def rotate_ip(self, slot_index: int = 0, wait_seconds: int = 12) -> bool:
        """
        비행기모드 껐다 켜서 IP를 바꾼다.
        테더링이 다시 살아나 새 IP가 잡힐 때까지 기다린다.
        """
        if not self.device:
            logger.error("ADB 기기가 없다 - IP 로테이션 불가")
            return False

        with self._lock:  # 슬롯 1개라 로테이션은 항상 단독 실행
            prev = self._current_ip()
            logger.info("✈️  비행기모드 ON...")
            self._adb("shell cmd connectivity airplane-mode enable")
            time.sleep(4)
            logger.info("✈️  비행기모드 OFF...")
            self._adb("shell cmd connectivity airplane-mode disable")

            for attempt in range(30):
                time.sleep(3)
                ip = self._current_ip()
                if ip == "error":
                    continue
                if self.home_ip and ip == self.home_ip:
                    continue  # 가정망으로 빠짐 — 테더링 아직 안 살아남
                if ip != prev:
                    logger.info(f"✅ IP 변경: {prev} → {ip} ({(attempt+1)*3}초)")
                    return True
                if attempt >= 5:  # 통신사가 같은 IP 재할당한 경우
                    logger.info(f"⚠️ 같은 IP 재할당: {ip} (진행)")
                    return True

            logger.warning("❌ IP 변경 실패 - 테더링 확인 필요")
            return False

    def rotate_all(self, wait_seconds: int = 15) -> dict[str, bool]:
        return {self.slot_name(0): self.rotate_ip(0, wait_seconds)}

    def get_current_ip(self, slot_index: int = 0) -> str:
        return self._current_ip()

    def direct_ip(self) -> str:
        """
        테더링 모드에선 '집 IP'를 프록시 없이 따로 잴 방법이 없다
        (직결 요청도 테더링으로 나가므로). config에 home_ip를 주면 그걸 쓴다.
        """
        return self.home_ip or "unknown"

    def health_check(self) -> dict:
        ip = self._current_ip()
        online = ip not in ("error", "unknown") and bool(self.device)
        return {
            self.slot_name(0): {
                "proxy": f"ADB:{self.device}" if self.device else "기기 없음",
                "ip": ip,
                "status": "online" if online else "offline",
            }
        }

    def preflight(self) -> dict:
        """
        테더링 안전 점검.
          - ADB 기기가 연결돼 있는지
          - 외부 IP가 잡히는지
          - (home_ip를 알면) 지금 IP가 집 IP가 아닌지
        """
        health = self.health_check()
        info = health[self.slot_name(0)]
        ip = info["ip"]

        offline, leaking = [], []
        if not self.device:
            offline.append(self.slot_name(0))
        elif ip in ("error", "unknown"):
            offline.append(self.slot_name(0))
        elif self.home_ip and ip == self.home_ip:
            leaking.append(self.slot_name(0))
            info["status"] = "LEAK"

        online = 1 if (self.device and ip not in ("error", "unknown")) else 0
        safe = not offline and not leaking and online > 0

        if not self.device:
            logger.error("❌ ADB 기기 없음 - USB 디버깅 켜고 폰 연결")
        elif leaking:
            logger.error(f"🚨 집 IP({ip})로 나감 - 테더링을 켜라")
        elif offline:
            logger.error("❌ 외부 IP 확인 불가 - 테더링/인터넷 확인")

        return {
            "safe": safe,
            "home_ip": self.direct_ip(),
            "slots": health,
            "online": online,
            "total": 1,
            "unique_ips": 1 if online else 0,
            "leaking": leaking,
            "offline": offline,
            "duplicate": False,
            "mode": "adb",
            "device": self.device,
        }
