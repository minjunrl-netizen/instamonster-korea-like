"""
xProxy 연결 테스트 스크립트

실전 투입 전에 반드시 통과해야 하는 것:
  1. 모든 유심 슬롯이 온라인이고 각각 다른 IP를 쓴다
  2. IP 로테이션 API가 실제로 IP를 바꾼다
  3. 슬롯 여러 개를 동시에 로테이션해도 서로 방해하지 않는다

실행: python test_xproxy.py
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from xproxy_manager import XProxyManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def build() -> XProxyManager:
    with open("config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    xp = cfg["xproxy"]
    return XProxyManager(
        host=xp["host"],
        api_port=xp["api_port"],
        proxy_type=xp.get("proxy_type", "socks5"),
        slots=xp["slots"],
        api_pattern=xp.get("api_pattern"),
        username=xp.get("username"),
        password=xp.get("password"),
    )


def check_slots(xproxy: XProxyManager) -> dict:
    logger.info("=== [1] 슬롯 상태 체크 ===")
    health = xproxy.health_check()

    online = 0
    for name, info in health.items():
        mark = "✅" if info["status"] == "online" else "❌"
        logger.info(f"  {mark} {name}: IP={info['ip']}  Proxy={info['proxy']}")
        if info["status"] == "online":
            online += 1

    ips = [i["ip"] for i in health.values() if i["status"] == "online"]
    unique = len(set(ips))
    logger.info(f"  → 온라인 {online}/{len(health)}개, 고유 IP {unique}개")
    if online and unique < online:
        logger.error("  ❌ 슬롯끼리 IP가 겹친다 - 유심/모뎀 매핑 확인 필요")

    return health


def check_rotation(xproxy: XProxyManager) -> None:
    logger.info("\n=== [2] IP 로테이션 테스트 (슬롯 0) ===")
    old_ip = xproxy.get_current_ip(0)
    logger.info(f"  변경 전: {old_ip}")

    started = time.time()
    ok = xproxy.rotate_ip(0, wait_seconds=15)
    took = time.time() - started

    new_ip = xproxy.get_current_ip(0)
    logger.info(f"  변경 후: {new_ip}  ({took:.1f}초 소요)")

    if not ok:
        logger.error("  ❌ 로테이션 API 호출 실패 - config의 xproxy.api_pattern 확인")
    elif old_ip == new_ip:
        logger.warning("  ⚠️ IP가 그대로 - 통신사가 같은 IP를 재할당했거나 로테이션이 안 먹음")
    elif new_ip == "error":
        logger.error("  ❌ 로테이션 후 프록시 응답 없음")
    else:
        logger.info("  ✅ IP 로테이션 성공")


def check_parallel(xproxy: XProxyManager) -> None:
    """워커가 슬롯별로 동시에 로테이션하는 실제 상황을 재현"""
    n = len(xproxy.slots)
    logger.info(f"\n=== [3] {n}개 슬롯 동시 로테이션 ===")

    before = {i: xproxy.get_current_ip(i) for i in range(n)}

    started = time.time()
    results = xproxy.rotate_all(wait_seconds=15)
    took = time.time() - started

    with ThreadPoolExecutor(max_workers=n) as ex:
        after = dict(zip(range(n), ex.map(xproxy.get_current_ip, range(n))))

    changed = sum(1 for i in range(n) if before[i] != after[i] and after[i] != "error")
    logger.info(f"  소요: {took:.1f}초 (순차였다면 약 {n * 15}초)")
    logger.info(f"  IP 변경됨: {changed}/{n}개")
    logger.info(f"  API 성공: {sum(1 for v in results.values() if v)}/{n}개")

    unique_after = len({ip for ip in after.values() if ip != "error"})
    if unique_after < changed:
        logger.error("  ❌ 로테이션 후 IP 중복 발생")
    else:
        logger.info("  ✅ 슬롯별 IP 독립성 유지")


def main():
    xproxy = build()
    check_slots(xproxy)
    check_rotation(xproxy)
    check_parallel(xproxy)


if __name__ == "__main__":
    main()
