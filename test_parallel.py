"""
병렬 좋아요 엔진 검증 (네트워크 없이 실행)

인스타 API 계층만 가짜로 갈아끼우고 실제 스레딩/배정/집계 로직을 그대로 돌린다.

검증 항목:
  1. 같은 계정이 같은 게시물에 두 번 좋아요를 누르지 않는다
  2. 주문 수량을 초과 발사하지 않는다
  3. 유심 슬롯 수만큼 실제로 동시 실행된다
  4. 밴/레이트리밋 계정이 풀에서 올바르게 빠진다
  5. 계정이 부족하면 초과분만 미납으로 남는다
  6. 합산된 게시물 결과가 주문별로 정확히 분배된다
"""

import json
import time
import shutil
import tempfile
import threading
from pathlib import Path

from instagrapi.utils.ids import InstagramIdCodec

import order_processor
from order_processor import OrderProcessor


# ─── 가짜 인스타 클라이언트 ───

class FakeLedger:
    """모든 워커가 공유하는 호출 기록"""

    def __init__(self):
        self.lock = threading.Lock()
        self.likes: list[tuple[str, str]] = []      # (username, media_id)
        self.like_proxies: list[tuple[str, str]] = []  # (username, proxy_url)
        self.attempts: set[str] = set()            # 좋아요를 시도한 계정
        self.concurrent = 0
        self.peak_concurrent = 0
        self.burn_users: set[str] = set()           # 이 계정은 밴 예외를 던진다
        self.cooldown_users: set[str] = set()       # 이 계정은 레이트리밋을 던진다
        self.dead_media: set[str] = set()           # 이 게시물은 MediaNotFound

    def enter(self):
        with self.lock:
            self.concurrent += 1
            self.peak_concurrent = max(self.peak_concurrent, self.concurrent)

    def leave(self):
        with self.lock:
            self.concurrent -= 1


LEDGER = FakeLedger()


class FakeClient:
    """instagrapi.Client 대역 - 네트워크 호출 없음"""

    def __init__(self):
        self.username = "<unloaded>"
        self.user_id = None
        self.delay_range = [0, 0]
        self.proxy = None

    # 실제 구현과 동일하게 로컬 디코딩
    def media_pk_from_url(self, url: str) -> str:
        code = url.rstrip("/").split("/")[-1]
        return str(InstagramIdCodec.decode(code[:11]))

    def media_id(self, media_pk: str) -> str:
        return f"{media_pk}_777"

    def load_settings(self, path: str) -> None:
        self.username = Path(path).stem
        self.user_id = "777"

    def set_proxy(self, url: str) -> None:
        self.proxy = url

    def media_like(self, media_id: str) -> bool:
        from instagrapi.exceptions import (
            FeedbackRequired, PleaseWaitFewMinutes, MediaNotFound,
        )

        LEDGER.enter()
        with LEDGER.lock:
            LEDGER.attempts.add(self.username)
        try:
            if self.username in LEDGER.burn_users:
                raise FeedbackRequired("spam detected")
            if self.username in LEDGER.cooldown_users:
                raise PleaseWaitFewMinutes("slow down")
            if media_id in LEDGER.dead_media:
                raise MediaNotFound("gone")

            with LEDGER.lock:
                LEDGER.likes.append((self.username, media_id))
                LEDGER.like_proxies.append((self.username, self.proxy))
            # 실제 네트워크 왕복 대신 짧은 지연 — 스레드 겹침을 관측 가능하게
            time.sleep(0.003)
            return True
        finally:
            LEDGER.leave()

    # 인간 행동 시뮬레이션이 호출하는 것들
    def get_timeline_feed(self, reason="pull_to_refresh"):
        return {"items": []}

    def get_reels_tray_feed(self, reason="pull_to_refresh"):
        return {"tray": []}

    def media_info(self, pk, use_cache=True):
        return None

    def user_stories(self, user_id, amount=None):
        return []

    def user_info(self, user_id, use_cache=True):
        return None


# ─── 테스트 환경 구성 ───

CODES = [
    "DaNf3SCBoWH", "DaNFOGIxvyY", "DaNkQSNEfOE", "DaNrj7Rj2ft",
    "DaNqAC3BJJ8", "DaNhdnVz6AB", "DaNlauMSBjz", "DaN2HcDmrTi",
]
URL = "https://www.instagram.com/p/{code}/"


def build_processor(tmp: Path, account_count: int, slots: int, human: bool = False) -> OrderProcessor:
    sessions = tmp / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    for i in range(account_count):
        (sessions / f"acc{i+1:05d}.json").write_text("{}", encoding="utf-8")

    config = {
        "xproxy": {
            "host": "127.0.0.1",
            "api_port": 8080,
            "proxy_type": "socks5",
            "slots": [{"port": 30000 + i, "name": f"sim{i+1}", "modem": i + 1} for i in range(slots)],
        },
        "settings": {
            "sessions_dir": str(sessions),
            "likes_per_account": 10,
            "max_likes_per_post": 3000,
            "delay_between_likes_min": 0,
            "delay_between_likes_max": 0,
            "delay_between_accounts_min": 0,
            "delay_between_accounts_max": 0,
            "ip_rotate_wait_seconds": 0,
            "human_simulation": human,
            "view_media_probability": 0.25,
            "random_action_probability": 0.2,
        },
    }
    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps(config), encoding="utf-8")

    proc = OrderProcessor(str(cfg_path))
    # 실제 xProxy 장비 대신 즉시 성공 처리
    proc.xproxy.rotate_ip = lambda slot, wait_seconds=0: True
    return proc


def reset_ledger():
    global LEDGER
    LEDGER = FakeLedger()
    order_processor.Client = FakeClient
    import test_parallel
    test_parallel.LEDGER = LEDGER


# ─── 검증 ───

def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "✅" if condition else "❌"
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))
    return condition


def test_no_duplicate_and_no_overshoot():
    print("\n[1] 중복 방지 + 초과 발사 방지 + 병렬성")
    tmp = Path(tempfile.mkdtemp())
    try:
        reset_ledger()
        proc = build_processor(tmp, account_count=200, slots=10)

        orders = [{"user": "u", "link": URL.format(code=c), "quantity": 150} for c in CODES[:4]]
        proc.load_orders_list(orders)
        stats = proc.process_all()

        pairs = LEDGER.likes
        ok = True
        ok &= check("중복 좋아요 없음", len(pairs) == len(set(pairs)),
                    f"발사 {len(pairs)} / 고유 {len(set(pairs))}")
        ok &= check("목표 정확히 달성", stats["success"] == 600,
                    f"성공 {stats['success']} / 목표 600")

        per_media = {}
        for _, mid in pairs:
            per_media[mid] = per_media.get(mid, 0) + 1
        ok &= check("게시물별 초과 없음", all(v == 150 for v in per_media.values()),
                    str(sorted(per_media.values())))

        ok &= check("계정당 한도 준수",
                    max(len([1 for u, _ in pairs if u == acc]) for acc in {u for u, _ in pairs}) <= 10,
                    f"최대 {max(len([1 for u, _ in pairs if u == acc]) for acc in {u for u, _ in pairs})}개")

        ok &= check("유심 병렬 동작", LEDGER.peak_concurrent > 1,
                    f"최대 동시 {LEDGER.peak_concurrent}개")

        ok &= check("주문 전량 납품", all(o.status == "done" for o in proc.orders),
                    f"{[o.status for o in proc.orders]}")
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_order_merge_and_split():
    print("\n[2] 같은 게시물 주문 합산 → 결과 분배")
    tmp = Path(tempfile.mkdtemp())
    try:
        reset_ledger()
        proc = build_processor(tmp, account_count=120, slots=5)

        same = URL.format(code=CODES[0])
        proc.load_orders_list([
            {"user": "a", "link": same, "quantity": 30},
            {"user": "a", "link": same + "?igsh=xyz", "quantity": 20},
            {"user": "b", "link": same, "quantity": 50},
        ])
        proc.process_all()

        ok = True
        ok &= check("게시물 1개로 합산", len(proc.media_tasks) == 1, f"{len(proc.media_tasks)}개")
        ok &= check("합산 수량 100", sum(t.total_likes_needed for t in proc.media_tasks.values()) == 100)
        ok &= check("주문별 분배 정확",
                    [o.delivered for o in proc.orders] == [30, 20, 50],
                    str([o.delivered for o in proc.orders]))
        ok &= check("전부 done", all(o.status == "done" for o in proc.orders))
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_account_shortage():
    print("\n[3] 계정 부족 시 미납 처리")
    tmp = Path(tempfile.mkdtemp())
    try:
        reset_ledger()
        proc = build_processor(tmp, account_count=40, slots=5)

        proc.load_orders_list([
            {"user": "a", "link": URL.format(code=CODES[0]), "quantity": 100},
        ])
        stats = proc.process_all()

        ok = True
        ok &= check("계정 수만큼만 납품", stats["success"] == 40, f"성공 {stats['success']} / 계정 40")
        ok &= check("중복 없음", len(LEDGER.likes) == len(set(LEDGER.likes)))
        ok &= check("주문 상태 processing", proc.orders[0].status == "processing",
                    proc.orders[0].status)
        ok &= check("납품 수량 기록", proc.orders[0].delivered == 40, str(proc.orders[0].delivered))
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_burn_and_cooldown():
    print("\n[4] 밴/레이트리밋 계정 격리")
    tmp = Path(tempfile.mkdtemp())
    try:
        reset_ledger()
        proc = build_processor(tmp, account_count=100, slots=5)

        LEDGER.burn_users = {f"acc{i:05d}" for i in range(1, 11)}      # 10개 밴
        LEDGER.cooldown_users = {f"acc{i:05d}" for i in range(11, 21)}  # 10개 쿨다운

        proc.load_orders_list([
            {"user": "a", "link": URL.format(code=CODES[0]), "quantity": 80},
        ])
        stats = proc.process_all()

        liked_users = {u for u, _ in LEDGER.likes}
        ok = True
        ok &= check("밴 계정 좋아요 0건", not (liked_users & LEDGER.burn_users))
        ok &= check("쿨다운 계정 좋아요 0건", not (liked_users & LEDGER.cooldown_users))
        ok &= check("정상 계정으로 목표 달성", stats["success"] == 80, f"성공 {stats['success']}")

        # 목표를 채우면 남은 계정은 건드리지 않으므로, 실제로 시도된 밴 계정만 세야 한다
        tried_burn = LEDGER.attempts & LEDGER.burn_users
        ok &= check("시도된 밴 계정은 전부 폐기",
                    proc.pool.burned_count == len(tried_burn),
                    f"폐기 {proc.pool.burned_count} / 시도된 밴 계정 {len(tried_burn)}")
        ok &= check("밴 계정이 실제로 배정됐음", len(tried_burn) >= 9, f"{len(tried_burn)}개")
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_dead_media():
    print("\n[5] 삭제된 게시물 조기 차단")
    tmp = Path(tempfile.mkdtemp())
    try:
        reset_ledger()
        proc = build_processor(tmp, account_count=100, slots=5)

        dead_url = URL.format(code=CODES[1])
        dead_pk = FakeClient().media_pk_from_url(dead_url)
        LEDGER.dead_media = {f"{dead_pk}_777"}

        proc.load_orders_list([
            {"user": "a", "link": URL.format(code=CODES[0]), "quantity": 50},
            {"user": "b", "link": dead_url, "quantity": 50},
        ])
        proc.process_all()

        ok = True
        ok &= check("정상 게시물은 완납", proc.orders[0].delivered == 50, str(proc.orders[0].delivered))
        ok &= check("죽은 게시물은 즉시 중단", proc.orders[1].delivered == 0,
                    f"{proc.orders[1].delivered}개 발사됨")
        ok &= check("죽은 게시물 재시도 없음",
                    sum(1 for _, m in LEDGER.likes if m in LEDGER.dead_media) == 0)
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_human_simulation_on():
    print("\n[6] 인간 행동 시뮬레이션 켠 상태")
    tmp = Path(tempfile.mkdtemp())
    try:
        reset_ledger()
        proc = build_processor(tmp, account_count=60, slots=5, human=True)

        proc.load_orders_list([
            {"user": "a", "link": URL.format(code=c), "quantity": 40} for c in CODES[:3]
        ])
        stats = proc.process_all()

        ok = True
        ok &= check("시뮬레이션 켜도 목표 달성", stats["success"] == 120, f"성공 {stats['success']}")
        ok &= check("중복 없음", len(LEDGER.likes) == len(set(LEDGER.likes)))
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_csv_and_cap():
    print("\n[7] 실제 주문 CSV 파싱 + 게시물당 상한")
    tmp = Path(tempfile.mkdtemp())
    try:
        reset_ledger()
        proc = build_processor(tmp, account_count=50, slots=5)
        n = proc.load_orders_csv("orders_sample.csv")

        ok = True
        ok &= check("CSV 28건 로드", n == 28, f"{n}건")
        ok &= check("빈 Start count 처리", any(o.start_count == 0 for o in proc.orders))

        proc._build_media_tasks()
        ok &= check("게시물 17개로 합산", len(proc.media_tasks) == 17, f"{len(proc.media_tasks)}개")

        merged = {t.link: t.total_likes_needed for t in proc.media_tasks.values()}
        ok &= check("총 좋아요 1,022개", sum(merged.values()) == 1022, f"{sum(merged.values())}개")

        # DaNlauMSBjz 는 CSV에 5건(20+10+10+20+20)
        target = "https://www.instagram.com/reel/DaNlauMSBjz"
        ok &= check("동일 게시물 5건 합산 = 80", merged.get(target) == 80, str(merged.get(target)))

        # 같은 게시물에 igsh 파라미터가 붙어도 하나로 합쳐진다
        dupes = "https://www.instagram.com/p/DaN2HcDmrTi"
        ok &= check("DaN2HcDmrTi 2건 합산 = 150", merged.get(dupes) == 150, str(merged.get(dupes)))

        # 상한 초과 주문은 잘려야 한다
        proc.max_likes_per_post = 100
        proc.load_orders_list([{"user": "x", "link": URL.format(code=CODES[0]), "quantity": 5000}])
        ok &= check("주문 수량 상한 적용", proc.orders[0].quantity == 100, str(proc.orders[0].quantity))
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_real_panel_hazards():
    print("\n[8] 실제 패널 CSV 위험 입력 방어")
    tmp = Path(tempfile.mkdtemp())
    try:
        reset_ledger()
        proc = build_processor(tmp, account_count=60, slots=5)

        rows = [
            # 정상
            {"ID": 1, "User": "a", "Link": URL.format(code=CODES[0]),
             "Quantity": 20, "Status": "Pending"},
            # 프로필 URL — media_pk_from_url이 조용히 가짜 pk를 만드는 케이스
            {"ID": 2, "User": "b", "Link": "https://www.instagram.com/xxuqlsxx/",
             "Quantity": 50, "Status": "Pending"},
            {"ID": 3, "User": "c", "Link": "https://www.instagram.com/oooooookkjooor?igsh=MXp",
             "Quantity": 50, "Status": "Pending"},
            # 계정명만
            {"ID": 4, "User": "d", "Link": "o_n_yoo", "Quantity": 50, "Status": "Pending"},
            # 계좌번호 / 해시태그 텍스트
            {"ID": 5, "User": "e", "Link": "IBK기업은행 495-055224-02-010",
             "Quantity": 50, "Status": "Pending"},
            {"ID": 6, "User": "f", "Link": "#광고 #거당벌 #사양벌꿀",
             "Quantity": 70, "Status": "Pending"},
            # 취소 주문 — 링크가 멀쩡해도 처리하면 안 됨
            {"ID": 7, "User": "g", "Link": URL.format(code=CODES[1]),
             "Quantity": 100, "Status": "Cancel"},
            # 이미 완료된 주문
            {"ID": 8, "User": "h", "Link": URL.format(code=CODES[2]),
             "Quantity": 100, "Status": "Completed", "Remains": 0},
            # 부분 처리 — Remains 만큼만 채워야 함
            {"ID": 9, "User": "i", "Link": URL.format(code=CODES[3]),
             "Quantity": 100, "Status": "Partial", "Remains": 12},
            # &amp; 엔티티 + img_index
            {"ID": 10, "User": "j",
             "Link": "https://www.instagram.com/p/DaNkQSNEfOE/?img_index=2&amp;igsh=MzB5",
             "Quantity": 7, "Status": "In progress", "Remains": 7},
            # www 없는 도메인
            {"ID": 11, "User": "k", "Link": "https://instagram.com/p/DbcPbWMEpUW/?utm_source=qr",
             "Quantity": 15, "Status": "Pending"},
        ]
        n = proc.load_orders_list(rows)

        ids = [o.order_id for o in proc.orders]
        ok = True
        # 11건 중 차단: 쓰레기 링크 5건(ID 2~6) + 취소 1건(7) + 완료 1건(8) = 7건
        ok &= check("쓰레기/취소/완료 7건 차단", n == 4, f"{n}건 통과 (기대 4)")
        ok &= check("통과한 주문 = 정상 링크만", ids == [1, 9, 10, 11], str(ids))

        partial = next(o for o in proc.orders if o.order_id == 9)
        ok &= check("부분 주문은 Remains만 처리", partial.quantity == 12, str(partial.quantity))

        entity = next(o for o in proc.orders if o.order_id == 10)
        ok &= check("&amp; 엔티티 URL 정규화",
                    entity.link == "https://www.instagram.com/p/DaNkQSNEfOE", entity.link)

        stats = proc.process_all()
        ok &= check("정상 주문만 발사", stats["success"] == 54, f"성공 {stats['success']} (기대 54)")
        ok &= check("가짜 pk로 발사된 것 없음", len(LEDGER.likes) == 54, f"{len(LEDGER.likes)}건")
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_real_csv_end_to_end():
    print("\n[9] 실제 패널 CSV 전체 로드")
    real = Path(r"C:/Users/oc/Downloads/instamonster.co.kr_panel_orders_11-08-2026.csv")
    if not real.exists():
        print("  ⏭  실제 CSV 없음, 스킵")
        return True

    tmp = Path(tempfile.mkdtemp())
    try:
        reset_ledger()
        proc = build_processor(tmp, account_count=10, slots=5)
        n = proc.load_orders_csv(str(real))

        ok = True
        ok &= check("완료/취소 주문 전량 제외", 0 < n < 200, f"{n}건 로드")
        ok &= check("모든 링크가 게시물 URL",
                    all("/p/" in o.link or "/reel/" in o.link for o in proc.orders))
        ok &= check("수량 전부 양수", all(o.quantity > 0 for o in proc.orders))
        ok &= check("3,000 상한 이내", all(o.quantity <= 3000 for o in proc.orders))
        ok &= check("패널 주문번호 보존", all(o.order_id > 1000000 for o in proc.orders))

        proc._build_media_tasks()
        total = sum(t.total_likes_needed for t in proc.media_tasks.values())
        biggest = max(t.total_likes_needed for t in proc.media_tasks.values())
        print(f"     → 미처리 {n}건 / 게시물 {len(proc.media_tasks)}개 "
              f"/ 총 {total:,}개 / 최대 게시물 {biggest:,}개")
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_db_processor(tmp: Path, account_count: int, slots: int) -> OrderProcessor:
    """DB에 계정을 등록하고 ready 상태로 만든 뒤 프로세서를 만든다"""
    import db as _db
    _db.DB_PATH = tmp / "test.db"
    _db._local = threading.local()
    _db.init()

    sessions = tmp / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)

    recs = [{"username": f"acc{i+1:05d}", "password": "pw"} for i in range(account_count)]
    _db.add_accounts(recs, slot_count=slots)   # 슬롯 균등 배정 + 디바이스 배정

    # 전부 로그인 성공(ready)으로 만들고 세션 파일 생성
    for r in recs:
        u = r["username"]
        sf = sessions / f"{u}.json"
        sf.write_text("{}", encoding="utf-8")
        _db.mark_login_result(u, _db.READY, str(sf))

    config = {
        "xproxy": {
            "host": "127.0.0.1", "api_port": 8080, "proxy_type": "socks5",
            "slots": [{"port": 30000 + i, "name": f"sim{i+1}", "modem": i + 1} for i in range(slots)],
        },
        "settings": {
            "sessions_dir": str(sessions),
            "likes_per_account": 10, "max_likes_per_post": 3000,
            "delay_between_likes_min": 0, "delay_between_likes_max": 0,
            "delay_between_accounts_min": 0, "delay_between_accounts_max": 0,
            "ip_rotate_wait_seconds": 0, "human_simulation": False,
        },
    }
    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps(config), encoding="utf-8")

    proc = OrderProcessor(str(cfg_path))
    proc.xproxy.rotate_ip = lambda slot, wait_seconds=0: True
    return proc, _db


def test_db_backed_pool():
    print("\n[10] DB 계정풀 연동 — 슬롯 고정 + likes_today 영구 기록")
    tmp = Path(tempfile.mkdtemp())
    try:
        reset_ledger()
        proc, _db = build_db_processor(tmp, account_count=100, slots=5)

        ok = True
        ok &= check("DB 모드로 로드됨", proc.pool.db_backed, str(proc.pool.db_backed))
        ok &= check("슬롯 5개에 균등 분배",
                    sorted(len(proc.pool.by_slot[i]) for i in range(5)) == [20]*5,
                    str([len(proc.pool.by_slot[i]) for i in range(5)]))

        # 각 계정의 DB상 배정 슬롯 기록
        slot_of = {}
        for i in range(5):
            for a in proc.pool.by_slot[i]:
                slot_of[a["username"]] = i

        orders = [{"user": "u", "link": URL.format(code=c), "quantity": 60} for c in CODES[:3]]
        proc.load_orders_list(orders)
        stats = proc.process_all()

        ok &= check("목표 달성", stats["success"] == 180, f"성공 {stats['success']} / 목표 180")
        ok &= check("중복 없음", len(LEDGER.likes) == len(set(LEDGER.likes)))

        # ── 핵심: 계정이 자기 슬롯 포트로만 발사했는지 ──
        mismatched = []
        for username, proxy in LEDGER.like_proxies:
            expected_port = 30000 + slot_of[username]
            actual_port = int(proxy.rsplit(":", 1)[1])
            if actual_port != expected_port:
                mismatched.append((username, actual_port, expected_port))
        ok &= check("계정-슬롯 고정 준수 (자기 유심으로만 발사)",
                    not mismatched, f"{len(mismatched)}건 불일치")

        # ── likes_today가 DB에 기록됐는지 ──
        rows, _ = _db.list_accounts(limit=1000)
        used = {r["username"]: r["likes_today"] for r in rows}
        total_recorded = sum(used.values())
        ok &= check("likes_today DB 기록 = 발사 수",
                    total_recorded == 180, f"DB기록 {total_recorded} / 발사 180")
        ok &= check("계정당 한도(10) 초과 없음",
                    max(used.values()) <= 10, f"최대 {max(used.values())}")

        # ── 재시작해도 카운터 유지: 새 풀 만들면 used_count가 DB값부터 시작 ──
        from order_processor import AccountPool
        pool2 = AccountPool(str(tmp / "sessions"), max_daily_use=10, slot_count=5)
        restart_used = sum(a["used_count"] for a in pool2.accounts)
        ok &= check("재시작 후 카운터 유지 (DB에서 복원)",
                    restart_used == 180, f"복원된 사용량 {restart_used}")
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_db_slot_pinning_isolation():
    print("\n[11] DB 모드 — 죽은 계정 발사 시 DB 상태 banned 갱신")
    tmp = Path(tempfile.mkdtemp())
    try:
        reset_ledger()
        proc, _db = build_db_processor(tmp, account_count=50, slots=5)

        # 특정 계정이 발사 때 스팸(밴) 예외를 던지게
        banned_targets = {a["username"] for i in range(5) for a in proc.pool.by_slot[i][:1]}
        LEDGER.burn_users = set(banned_targets)

        orders = [{"user": "u", "link": URL.format(code=CODES[0]), "quantity": 45}]
        proc.load_orders_list(orders)
        proc.process_all()

        rows, _ = _db.list_accounts(status="banned", limit=100)
        banned_in_db = {r["username"] for r in rows}

        ok = True
        ok &= check("밴 계정 좋아요 0건",
                    not ({u for u, _ in LEDGER.likes} & banned_targets))
        ok &= check("밴 계정 DB 상태 banned로 갱신",
                    banned_targets <= banned_in_db,
                    f"밴 대상 {len(banned_targets)} / DB banned {len(banned_in_db)}")
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)

    print("=" * 60)
    print("  병렬 좋아요 엔진 검증")
    print("=" * 60)

    results = [
        test_no_duplicate_and_no_overshoot(),
        test_order_merge_and_split(),
        test_account_shortage(),
        test_burn_and_cooldown(),
        test_dead_media(),
        test_human_simulation_on(),
        test_csv_and_cap(),
        test_db_backed_pool(),
        test_db_slot_pinning_isolation(),
        test_real_panel_hazards(),
        test_real_csv_end_to_end(),
    ]

    print("\n" + "=" * 60)
    if all(results):
        print(f"  ✅ 전체 통과 ({len(results)}/{len(results)})")
    else:
        print(f"  ❌ 실패 {results.count(False)}건 / 전체 {len(results)}건")
    print("=" * 60)
    raise SystemExit(0 if all(results) else 1)
