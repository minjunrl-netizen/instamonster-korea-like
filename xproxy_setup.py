"""
xProxy 장비 연결 준비 도구

장비를 랜에 연결한 뒤, 실전 세팅 전에 이걸로 준비를 끝낸다.

  python xproxy_setup.py scan     # 랜에서 xProxy 장비 찾기
  python xproxy_setup.py probe    # config의 장비에 연결되는지 + 슬롯 포트 확인
  python xproxy_setup.py apicheck # IP 로테이션 API 경로 자동 감지
  python xproxy_setup.py checklist# 연결 전 준비 체크리스트

준비가 끝나면 test_xproxy.py로 최종 검증한다.
"""

import sys
import json
import socket
import logging
from concurrent.futures import ThreadPoolExecutor

import requests

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

CONFIG = "config.json"

# xProxy 웹 대시보드/API가 흔히 쓰는 포트
COMMON_WEB_PORTS = [8080, 80, 8888, 3000, 5000, 8000]

# 장비 버전별 IP 변경 API 후보 (test 시 자동 학습)
API_PATTERNS = [
    "/api/changeIP/{pos}",
    "/api/rotate/{pos}",
    "/rotating?modem={pos}",
    "/api/changeip?port={port}",
    "/reboot?port={port}",
]


def load_cfg() -> dict:
    with open(CONFIG, "r", encoding="utf-8") as f:
        return json.load(f)


def local_subnet() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ".".join(ip.split(".")[:3])


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


# ─────────────────────────── scan ───────────────────────────

def scan():
    """랜에서 웹 포트가 열린 장비를 찾는다 (xProxy 대시보드 후보)"""
    subnet = local_subnet()
    logger.info(f"서브넷 {subnet}.0/24 스캔 중... (웹 포트 {COMMON_WEB_PORTS})")
    logger.info("이 PC와 xProxy 장비가 같은 공유기/스위치에 물려 있어야 잡힙니다.\n")

    candidates = []

    def check_host(i: int):
        host = f"{subnet}.{i}"
        for port in COMMON_WEB_PORTS:
            if _port_open(host, port):
                return (host, port)
        return None

    with ThreadPoolExecutor(max_workers=64) as ex:
        for result in ex.map(check_host, range(1, 255)):
            if result:
                candidates.append(result)

    if not candidates:
        logger.info("❌ 웹 포트가 열린 장비를 못 찾았습니다.")
        logger.info("   - 장비 전원/랜선 확인")
        logger.info("   - 장비가 이 PC와 같은 공유기에 연결됐는지 확인")
        logger.info("   - 장비 대시보드 포트가 특이하면 config에 직접 입력")
        return

    logger.info(f"발견된 웹 장비 {len(candidates)}개:")
    for host, port in candidates:
        title = _http_title(host, port)
        mark = " ← xProxy 후보!" if _looks_like_xproxy(title) else ""
        logger.info(f"  http://{host}:{port}   {title}{mark}")

    logger.info("\n다음 단계:")
    logger.info("  1. 위 주소를 브라우저로 열어 xProxy 대시보드인지 확인")
    logger.info("  2. config.json의 xproxy.host / api_port를 그 값으로 수정")
    logger.info("  3. python xproxy_setup.py probe")


def _http_title(host: str, port: int) -> str:
    try:
        r = requests.get(f"http://{host}:{port}/", timeout=2)
        text = r.text[:2000].lower()
        import re
        m = re.search(r"<title>(.*?)</title>", text)
        return f"[{m.group(1)[:40]}]" if m else f"[HTTP {r.status_code}]"
    except Exception:
        return "[응답없음]"


def _looks_like_xproxy(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in ["xproxy", "proxy", "modem", "lte", "4g", "5g", "router"])


# ─────────────────────────── probe ───────────────────────────

def probe():
    """config의 장비 + 슬롯 포트에 실제로 붙는지 확인"""
    cfg = load_cfg()
    xp = cfg["xproxy"]
    host = xp["host"]
    api_port = xp["api_port"]
    slots = xp["slots"]

    logger.info(f"대상 장비: {host}")
    logger.info(f"API 포트:  {api_port}")
    logger.info(f"슬롯:      {len(slots)}개\n")

    # 1. 장비 도달 확인
    logger.info("=== [1] 장비 도달 확인 ===")
    if _port_open(host, api_port, timeout=2):
        logger.info(f"  ✅ {host}:{api_port} 열림 (API 포트 도달)")
    else:
        logger.info(f"  ❌ {host}:{api_port} 닫힘/도달 불가")
        logger.info("     → 장비 IP가 맞는지, 같은 랜인지 확인. scan으로 다시 찾기.")
        return

    # 2. 각 슬롯 프록시 포트 열림 확인
    logger.info("\n=== [2] 슬롯 프록시 포트 확인 ===")
    proxy_type = xp.get("proxy_type", "socks5")
    open_ports = 0
    for slot in slots:
        port = slot["port"]
        name = slot.get("name", f"port-{port}")
        if _port_open(host, port, timeout=1.5):
            logger.info(f"  ✅ {name}: {host}:{port} 열림")
            open_ports += 1
        else:
            logger.info(f"  ❌ {name}: {host}:{port} 닫힘 (유심 미장착/모뎀 오프?)")
    logger.info(f"  → {open_ports}/{len(slots)}개 슬롯 포트 열림")

    # 3. 실제 프록시로 나가는 IP 확인 (한 슬롯만)
    logger.info("\n=== [3] 첫 슬롯 실제 IP 확인 ===")
    first = slots[0]
    purl = f"{proxy_type}://{host}:{first['port']}"
    ip = _ip_via_proxy(purl)
    home = _direct_ip()
    logger.info(f"  집 IP:        {home}")
    logger.info(f"  슬롯 IP:      {ip}")
    if ip == "error":
        logger.info("  ❌ 프록시로 나가지 못함 (유심 데이터 확인)")
    elif ip == home:
        logger.info("  🚨 슬롯 IP가 집 IP와 같음 - 프록시가 안 걸림!")
    else:
        logger.info("  ✅ 모바일 IP로 정상 출구")

    logger.info("\n다음 단계: python xproxy_setup.py apicheck")


def _direct_ip() -> str:
    try:
        return requests.get("https://api.ipify.org?format=json", timeout=8).json()["ip"]
    except Exception:
        return "unknown"


def _ip_via_proxy(purl: str) -> str:
    try:
        r = requests.get("https://api.ipify.org?format=json",
                         proxies={"http": purl, "https": purl}, timeout=12)
        return r.json()["ip"]
    except Exception:
        return "error"


# ─────────────────────────── apicheck ───────────────────────────

def apicheck():
    """IP 로테이션 API 경로를 자동 감지한다 (장비 버전마다 다름)"""
    cfg = load_cfg()
    xp = cfg["xproxy"]
    host, api_port = xp["host"], xp["api_port"]
    first = xp["slots"][0]
    pos = first.get("modem", 1)
    port = first["port"]

    logger.info(f"IP 로테이션 API 경로 자동 감지 (슬롯1, modem={pos})\n")

    # 로테이션 전 IP
    proxy_type = xp.get("proxy_type", "socks5")
    purl = f"{proxy_type}://{host}:{port}"
    before = _ip_via_proxy(purl)
    logger.info(f"로테이션 전 IP: {before}\n")

    found = None
    for pattern in API_PATTERNS:
        path = pattern.format(pos=pos, port=port)
        url = f"http://{host}:{api_port}{path}"
        try:
            r = requests.get(url, timeout=8)
            status = r.status_code
        except Exception as e:
            logger.info(f"  {path:32} → 요청실패 ({type(e).__name__})")
            continue

        if status == 200:
            logger.info(f"  {path:32} → HTTP 200 ✅ 응답옴")
            if found is None:
                found = pattern
        else:
            logger.info(f"  {path:32} → HTTP {status}")

    if found:
        logger.info(f"\n✅ 작동하는 API 패턴: {found}")
        logger.info(f"   config.json의 xproxy.api_pattern에 넣으세요:")
        logger.info(f'   "api_pattern": "{found}"')
    else:
        logger.info("\n❌ 표준 패턴이 안 먹힘.")
        logger.info("   장비 대시보드 → API 문서/링크 확인 후 수동 입력 필요.")
        logger.info("   흔한 형태: http://장비IP:포트/api/changeIP/모뎀번호")


# ─────────────────────────── checklist ───────────────────────────

def checklist():
    cfg = load_cfg()
    xp = cfg["xproxy"]
    subnet = local_subnet()

    print("=" * 60)
    print("  xProxy 연결 준비 체크리스트")
    print("=" * 60)
    print(f"""
[하드웨어]
  □ xProxy 장비 전원 켜짐
  □ 장비 랜선 → 이 PC와 같은 공유기/스위치에 연결
    (이 PC 대역: {subnet}.x)
  □ USB 4G/5G 동글 {len(xp['slots'])}개 장착
  □ 각 동글에 데이터 유심 삽입 (SKT/KT/LGU 섞기 권장)
  □ 유심 데이터 요금제 활성 상태

[네트워크 확인]
  □ python xproxy_setup.py scan     → 장비 IP 찾기
  □ config.json 수정:
      "host": "{xp['host']}"   ← 실제 장비 IP로
      "api_port": {xp['api_port']}         ← 실제 API 포트로
  □ python xproxy_setup.py probe    → 슬롯 포트 열림 확인
  □ python xproxy_setup.py apicheck → 로테이션 API 경로 감지
  □ config.json의 api_pattern 채우기

[슬롯 매핑]
  □ 슬롯 {len(xp['slots'])}개 이름/포트/모뎀번호가 실제 장비와 일치
  □ 유심 통신사 이름을 슬롯 name에 반영 (유심01-SKT 등)

[최종 검증]
  □ python test_xproxy.py
      → 슬롯 전부 온라인
      → 고유 IP (중복 없음)
      → 집 IP 누출 없음
      → IP 로테이션 작동
      → 동시 로테이션 독립성

[대시보드 확인]
  □ 서버 켜고 http://localhost:8000 접속
  □ 대시보드 → "xProxy 장비" 토글 선택 (ADB 아님)
  □ "연결 상태 확인" → 슬롯 {len(xp['slots'])}개 전부 ✅ 초록
""")
    print("=" * 60)
    print("  위 순서대로 하면 실전 세팅 준비 완료.")
    print("=" * 60)


# ─────────────────────────── main ───────────────────────────

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "checklist"
    if cmd == "scan":
        scan()
    elif cmd == "probe":
        probe()
    elif cmd == "apicheck":
        apicheck()
    elif cmd == "checklist":
        checklist()
    else:
        print("사용법: python xproxy_setup.py [scan|probe|apicheck|checklist]")


if __name__ == "__main__":
    main()
