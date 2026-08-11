"""
xProxy 장비 IP 로테이션 관리 모듈

xProxy는 각 유심 슬롯마다 SOCKS5/HTTP 프록시 포트를 제공하고,
API를 통해 IP 로테이션(모뎀 비행기모드 토글)을 트리거할 수 있다.

슬롯끼리는 물리적으로 독립된 모뎀이므로 동시 로테이션이 가능하다.
이 모듈은 워커 스레드가 슬롯별로 동시에 호출해도 안전하다.

xProxy API 문서: https://xproxy.io/document/changing-ip-by-api-link
"""

import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

logger = logging.getLogger(__name__)


# 장비 버전마다 엔드포인트가 다르다. 첫 성공한 패턴을 학습해서 이후 재사용한다.
API_PATTERNS = (
    "/api/changeIP/{pos}",
    "/api/rotate/{pos}",
    "/rotating?modem={pos}",
)


class XProxyManager:
    """xProxy 하드웨어 장비의 IP 로테이션과 프록시 관리 (스레드 세이프)"""

    def __init__(
        self,
        host: str,
        api_port: int,
        proxy_type: str = "socks5",
        slots: list | None = None,
        api_pattern: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ):
        self.host = host
        self.api_port = api_port
        self.proxy_type = proxy_type  # socks5 or http
        self.slots = slots or []
        self.username = username
        self.password = password

        self._lock = threading.Lock()
        self._pattern = api_pattern  # 학습/고정된 API 경로
        self._session = requests.Session()

    # ─── 프록시 주소 ───

    def get_proxy_url(self, slot_index: int) -> str:
        """
        특정 슬롯의 프록시 URL 반환.

        호스트나 포트가 비면 절대 빈 문자열을 돌려주지 않는다.
        instagrapi의 set_proxy는 falsy 값을 받으면 프록시를 '해제'하므로,
        빈 값이 흘러가면 실제 IP로 직결된다.
        """
        slot = self.slots[slot_index]
        host = str(self.host or "").strip()
        port = slot.get("port")

        if not host:
            raise ValueError("xproxy.host가 비어있다 - 프록시 없이 접속할 위험")
        if not port:
            raise ValueError(f"슬롯 {slot_index}의 port가 없다")

        auth = ""
        user = slot.get("username", self.username)
        pw = slot.get("password", self.password)
        if user and pw:
            auth = f"{user}:{pw}@"
        return f"{self.proxy_type}://{auth}{host}:{port}"

    def slot_name(self, slot_index: int) -> str:
        return self.slots[slot_index].get("name", f"slot-{slot_index}")

    # ─── IP 로테이션 ───

    def rotate_ip(self, slot_index: int, wait_seconds: int = 12) -> bool:
        """
        특정 슬롯의 IP를 로테이션한다.

        슬롯마다 독립된 모뎀이라 다른 슬롯의 로테이션을 막지 않는다.
        실패하면 False를 반환한다 — 호출자는 같은 IP로 계속 쏘는 위험을 인지해야 한다.
        """
        name = self.slot_name(slot_index)
        position = self.slots[slot_index].get("modem", slot_index + 1)

        patterns = [self._pattern] if self._pattern else list(API_PATTERNS)

        for pattern in patterns:
            url = f"http://{self.host}:{self.api_port}{pattern.format(pos=position)}"
            try:
                resp = self._session.get(url, timeout=10)
            except requests.RequestException:
                continue

            if resp.status_code != 200:
                continue

            with self._lock:
                if self._pattern is None:
                    self._pattern = pattern
                    logger.info(f"xProxy API 엔드포인트 확정: {pattern}")

            logger.debug(f"[{name}] IP 로테이션 요청 성공, {wait_seconds}초 대기")
            time.sleep(wait_seconds)  # 새 IP 할당 대기
            return True

        logger.warning(
            f"[{name}] IP 로테이션 실패 - xProxy API 경로 확인 필요 "
            f"(config의 xproxy.api_pattern에 직접 지정 가능)"
        )
        return False

    def rotate_all(self, wait_seconds: int = 15) -> dict[str, bool]:
        """모든 슬롯 IP 동시 로테이션"""
        if not self.slots:
            return {}

        with ThreadPoolExecutor(max_workers=len(self.slots)) as ex:
            results = list(ex.map(lambda i: self.rotate_ip(i, wait_seconds), range(len(self.slots))))

        out = {self.slot_name(i): ok for i, ok in enumerate(results)}
        ok_count = sum(1 for v in out.values() if v)
        logger.info(f"전체 {len(self.slots)}개 슬롯 로테이션: {ok_count}개 성공")
        return out

    # ─── 상태 확인 ───

    def get_current_ip(self, slot_index: int) -> str:
        """특정 슬롯의 현재 외부 IP 확인"""
        proxy_url = self.get_proxy_url(slot_index)
        proxies = {"http": proxy_url, "https": proxy_url}
        try:
            resp = requests.get(
                "https://api.ipify.org?format=json", proxies=proxies, timeout=10
            )
            resp.raise_for_status()
            return resp.json().get("ip", "unknown")
        except Exception as e:
            logger.error(f"IP 확인 실패 ({self.slot_name(slot_index)}): {e}")
            return "error"

    def health_check(self) -> dict:
        """전체 슬롯 상태 동시 체크"""
        if not self.slots:
            return {}

        with ThreadPoolExecutor(max_workers=len(self.slots)) as ex:
            ips = list(ex.map(self.get_current_ip, range(len(self.slots))))

        results = {}
        seen: dict[str, str] = {}
        for i, ip in enumerate(ips):
            name = self.slot_name(i)
            online = ip not in ("error", "unknown")
            results[name] = {
                "proxy": self.get_proxy_url(i),
                "ip": ip,
                "status": "online" if online else "offline",
            }
            if online:
                if ip in seen:
                    logger.warning(f"⚠️ IP 중복: {name} 와 {seen[ip]} 가 같은 IP({ip}) 사용 중")
                seen[ip] = name

        return results

    def direct_ip(self) -> str:
        """프록시를 거치지 않은 실제 IP (누출 판정 기준값)"""
        try:
            resp = requests.get("https://api.ipify.org?format=json", timeout=10)
            resp.raise_for_status()
            return resp.json().get("ip", "unknown")
        except Exception as e:
            logger.warning(f"실제 IP 확인 실패: {e}")
            return "unknown"

    def preflight(self) -> dict:
        """
        실전 투입 전 안전 점검.

        가장 위험한 실패는 프록시가 조용히 풀려서 집 IP로 접속하는 것이다.
        슬롯의 외부 IP가 실제 IP와 같으면 그 슬롯은 절대 쓰면 안 된다.
        """
        home = self.direct_ip()
        health = self.health_check()

        leaking, offline, ips = [], [], []
        for name, info in health.items():
            if info["status"] != "online":
                offline.append(name)
                continue
            ips.append(info["ip"])
            if home != "unknown" and info["ip"] == home:
                leaking.append(name)
                info["status"] = "LEAK"

        duplicates = len(ips) != len(set(ips))
        safe = not leaking and not offline and not duplicates and bool(ips)

        if leaking:
            logger.error(
                f"🚨 IP 누출: {', '.join(leaking)} 가 실제 IP({home})로 나간다. 즉시 중단 필요."
            )
        if offline:
            logger.error(f"❌ 오프라인 슬롯: {', '.join(offline)}")
        if duplicates:
            logger.error("❌ 슬롯끼리 IP 중복 - 유심/모뎀 매핑 확인 필요")

        return {
            "safe": safe,
            "home_ip": home,
            "slots": health,
            "online": len(ips),
            "total": len(health),
            "unique_ips": len(set(ips)),
            "leaking": leaking,
            "offline": offline,
            "duplicate": duplicates,
        }
