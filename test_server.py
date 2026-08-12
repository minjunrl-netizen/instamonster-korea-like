"""
웹 서버 + 대량 로그인 검증 (네트워크 없이 실행)

인스타 로그인 계층만 가짜로 갈아끼우고 DB/병렬/웹 라우팅을 실제로 돌린다.

검증 항목:
  1. 계정 텍스트 파싱 (구분자 5종, 헤더/주석 무시)
  2. 등록 시 유심 슬롯 균등 분배 + 계정-슬롯 고정
  3. 로그인 결과가 상태별로 정확히 분류된다
  4. 계정은 자기 슬롯으로만 로그인한다
  5. 슬롯 수만큼 실제 병렬 실행
  6. 중단 요청이 먹는다
  7. 웹 라우트 전부 200
"""

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

import db
import bulk_login
from bulk_login import BulkLogin, parse_account_text


# ─── 가짜 로그인 계층 ───

class Ledger:
    def __init__(self):
        self.lock = threading.Lock()
        self.logins: list[tuple[str, str]] = []   # (username, proxy_url)
        self.codes: list[tuple[str, str]] = []    # (username, verification_code)
        self.concurrent = 0
        self.peak = 0

    def enter(self):
        with self.lock:
            self.concurrent += 1
            self.peak = max(self.peak, self.concurrent)

    def leave(self):
        with self.lock:
            self.concurrent -= 1


LEDGER = Ledger()

# username → 통과시킬 코드 집합. 값이 None이면 어떤 코드든 거부.
TFA_ACCOUNTS: dict[str, set] = {}

# username 접두사로 결과를 강제한다
FORCE = {
    "chal": "ChallengeRequired",
    "tfa": "TwoFactorRequired",
    "badpw": "BadPassword",
    "spam": "FeedbackRequired",
    "err": "PleaseWaitFewMinutes",
}


class FakeSession:
    """requests.Session 대역 — proxies 속성만 있으면 된다"""
    def __init__(self):
        self.proxies = {}

    def mount(self, prefix, adapter):
        pass

    def close(self):
        pass


class FakeClient:
    def __init__(self):
        self.proxy = None
        self.delay_range = [0, 0]
        self.request_timeout = 20
        self._user = None
        # 실제 Client와 동일하게 전송 경로 3개를 갖는다
        self.private = FakeSession()
        self.public = FakeSession()
        self.graphql = FakeSession()

    def load_settings(self, path): pass

    def set_proxy(self, url):
        # instagrapi와 동일한 동작: falsy 값이면 프록시를 '해제'한다
        self.proxy = url
        proxies = {"http": url, "https": url} if url else {}
        self.private.proxies = self.public.proxies = self.graphql.proxies = proxies

    def set_device(self, device=None, reset=False): pass
    def set_country(self, c): pass
    def set_country_code(self, c): pass
    def set_locale(self, c): pass
    def set_timezone_offset(self, c): pass

    def login(self, username, password, relogin=False, verification_code=""):
        from instagrapi import exceptions as E
        LEDGER.enter()
        try:
            time.sleep(0.004)
            self._user = username

            for prefix, exc in FORCE.items():
                if username.startswith(prefix):
                    raise getattr(E, exc)(f"forced {exc}")

            # 2FA 계정: 올바른 코드가 있어야만 통과
            if username in TFA_ACCOUNTS:
                code = (verification_code or "").strip()
                if not code:
                    raise E.TwoFactorRequired("two factor required")
                with LEDGER.lock:
                    LEDGER.codes.append((username, code))
                allowed = TFA_ACCOUNTS[username]
                if not allowed or code not in allowed:
                    raise E.TwoFactorRequired("invalid code")

            with LEDGER.lock:
                LEDGER.logins.append((username, self.proxy))
            return True
        finally:
            LEDGER.leave()

    def get_timeline_feed(self, reason="pull_to_refresh"):
        return {"items": []}

    def dump_settings(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({"user": self._user}), encoding="utf-8")


def check(label, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {label}" + (f" — {detail}" if detail else ""))
    return cond


def build_env(tmp: Path, slots: int = 5) -> str:
    cfg = {
        "xproxy": {
            "host": "127.0.0.1", "api_port": 8080, "proxy_type": "socks5",
            "slots": [{"port": 30000 + i, "name": f"sim{i+1}", "modem": i + 1}
                      for i in range(slots)],
        },
        "settings": {"sessions_dir": str(tmp / "sessions")},
        "login": {"delay_min": 0, "delay_max": 0, "ip_rotate_wait_seconds": 0},
        "admin": {
            "username": "bjdlclrh",
            "password_sha256": "ee3e16303edba1ae73b8c76ea8d74c92df9f38fe951a0220a9f1bc35b23efdcc",
        },
    }
    p = tmp / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return str(p)


def fresh_db(tmp: Path):
    db.DB_PATH = tmp / "test.db"
    db._local = threading.local()
    db.init()


# ─── 테스트 ───

SEED_A = "JBSWY3DPEHPK3PXP"          # 표준 base32 시드
SEED_B = "ABCD EFGH IJKL MNOP"       # 공백 포함


def test_parse():
    print("\n[1] 계정 텍스트 파싱 (2FA 포함)")
    text = f"""
# 주석은 무시
username:password
acc0001:pw1
acc0002,pw2
acc0003\tpw3
acc0004|pw4
acc0005 pw5
@acc0006:pw6
acc0007:pw7:{SEED_A}
acc0008:pw8:{SEED_B}
acc0009:pw9:{SEED_A}:11223344 55667788 99001122
acc0010:pw10:11223344 55667788
깨진줄
"""
    recs = parse_account_text(text)
    by = {r["username"]: r for r in recs}
    ok = True

    ok &= check("10건 인식", len(recs) == 10, f"{len(recs)}건 → {sorted(by)}")
    ok &= check("@ 접두사 제거", "acc0006" in by and by["acc0006"]["password"] == "pw6")
    ok &= check("헤더/주석/불량줄 제외", all(u.startswith("acc") for u in by))

    ok &= check("2FA 시드 인식", by["acc0007"].get("totp_seed") == SEED_A,
                str(by["acc0007"].get("totp_seed")))
    ok &= check("공백 포함 시드도 인식", by["acc0008"].get("totp_seed") == SEED_B,
                str(by["acc0008"].get("totp_seed")))
    ok &= check("시드 + 백업코드 동시 인식",
                by["acc0009"].get("totp_seed") == SEED_A
                and "11223344" in by["acc0009"].get("backup_codes", ""),
                str(by["acc0009"]))
    ok &= check("base32 아니면 백업코드로 분류",
                by["acc0010"].get("totp_seed") is None
                and "11223344" in by["acc0010"].get("backup_codes", ""),
                str(by["acc0010"]))
    ok &= check("2FA 없는 계정은 시드 없음", by["acc0001"].get("totp_seed") is None)
    return ok


def test_totp():
    print("\n[2] TOTP 시드 검증 / 코드 생성")
    ok = True

    # 정규화
    ok &= check("공백/하이픈 제거 + 대문자화",
                db.normalize_totp_seed("jbswy 3dpe-hpk3_pxp") == SEED_A,
                str(db.normalize_totp_seed("jbswy 3dpe-hpk3_pxp")))

    # instagrapi가 빈 문자열로도 코드를 뱉는 함정을 막아야 한다
    ok &= check("빈 시드 거부", db.normalize_totp_seed("") is None)
    ok &= check("None 거부", db.normalize_totp_seed(None) is None)
    ok &= check("짧은 시드 거부", db.normalize_totp_seed("JBSWY3DP") is None)
    ok &= check("base32 아닌 문자 거부", db.normalize_totp_seed("1234567890ABCDEF!!") is None)
    ok &= check("숫자만 있는 백업코드 거부", db.normalize_totp_seed("1122334455667788") is None)

    # 생성된 코드가 실제 TOTP와 일치하는지
    code = db.totp_code(SEED_A)
    ok &= check("6자리 숫자 코드 생성", code.isdigit() and len(code) == 6, code)

    tmp = Path(tempfile.mkdtemp())
    try:
        fresh_db(tmp)
        db.add_accounts([
            {"username": "seeded", "password": "pw", "totp_seed": "jbswy 3dpe hpk3 pxp"},
            {"username": "backup", "password": "pw", "backup_codes": "111,222,333"},
            {"username": "badseed", "password": "pw", "totp_seed": "not-base32!!"},
            {"username": "plain", "password": "pw"},
        ], slot_count=3)

        rows = {a["username"]: a for a in db.list_accounts(limit=100)[0]}
        ok &= check("시드 정규화 저장", rows["seeded"]["totp_seed"] == SEED_A,
                    str(rows["seeded"]["totp_seed"]))
        ok &= check("잘못된 시드는 저장 안 함", rows["badseed"]["totp_seed"] is None)
        ok &= check("백업코드 저장", rows["backup"]["backup_codes"] == "111,222,333")

        st = db.stats()
        ok &= check("통계에 2FA 집계", st["totp_seeds"] == 1 and st["backup_sets"] == 1,
                    f"seeds={st['totp_seeds']} backups={st['backup_sets']}")

        # 백업코드는 1회용
        ok &= check("백업코드 순서대로 소모", db.consume_backup_code("backup") == "111")
        ok &= check("소모 후 목록에서 제거",
                    db.list_accounts(q="backup")[0][0]["backup_codes"] == "222,333")
        db.consume_backup_code("backup")
        db.consume_backup_code("backup")
        ok &= check("전부 쓰면 None 반환", db.consume_backup_code("backup") is None)
        ok &= check("시드 없는 계정도 None", db.consume_backup_code("plain") is None)

        # 코드 미리보기
        pv = db.preview_totp("seeded")
        ok &= check("미리보기 코드 = 실제 TOTP",
                    pv and pv["code"] == db.totp_code(SEED_A), str(pv))
        ok &= check("시드 없으면 미리보기 None", db.preview_totp("plain") is None)

        # 개별 시드 등록
        ok &= check("개별 시드 등록 성공", db.set_totp_seed("plain", SEED_B))
        ok &= check("잘못된 시드 등록 거부", not db.set_totp_seed("plain", "nope"))
        ok &= check("거부돼도 기존 시드 유지",
                    db.list_accounts(q="plain")[0][0]["totp_seed"] == SEED_B.replace(" ", ""))
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_auto_2fa_login():
    print("\n[3] 2FA 자동 해제 로그인")
    tmp = Path(tempfile.mkdtemp())
    orig_wait = BulkLogin._wait_next_totp_window
    try:
        fresh_db(tmp)
        global LEDGER
        LEDGER = Ledger()
        bulk_login.Client = FakeClient
        TFA_ACCOUNTS.clear()

        cfg = build_env(tmp, slots=3)

        db.add_accounts([
            # 시드 보유 → 자동 통과해야 한다
            {"username": "seed01", "password": "pw", "totp_seed": SEED_A},
            {"username": "seed02", "password": "pw", "totp_seed": SEED_B},
            # 백업코드만 보유 → 1개 소모하고 통과
            {"username": "bkup01", "password": "pw", "backup_codes": "AAA111,BBB222"},
            # 아무것도 없음 → 수동 대상으로 남아야 한다
            {"username": "none01", "password": "pw"},
            # 시드가 틀림 → 거부되고 2fa로 남아야 한다
            {"username": "wrong1", "password": "pw", "totp_seed": "ZZZZZZZZZZZZZZZZ"},
            # 2FA 자체가 없는 평범한 계정
            {"username": "plain1", "password": "pw"},
        ], slot_count=3)

        TFA_ACCOUNTS["seed01"] = {db.totp_code(SEED_A)}
        TFA_ACCOUNTS["seed02"] = {db.totp_code(SEED_B.replace(" ", ""))}
        TFA_ACCOUNTS["bkup01"] = {"AAA111"}
        TFA_ACCOUNTS["none01"] = {"무엇이든"}
        TFA_ACCOUNTS["wrong1"] = {"정답코드"}

        engine = BulkLogin(cfg)
        engine.xproxy.rotate_ip = lambda slot, wait_seconds=0: True
        engine.xproxy.preflight = lambda: {
            "safe": True, "home_ip": "1.2.3.4", "slots": {}, "online": 3,
            "total": 3, "unique_ips": 3, "leaking": [], "offline": [], "duplicate": False,
        }
        # 창 경계 재시도가 30초를 잡아먹지 않게 한다
        BulkLogin._wait_next_totp_window = staticmethod(lambda: None)

        accounts = db.accounts_for_login()
        r = engine.run(accounts, job_id=db.create_job("bulk_login", len(accounts)))

        rows = {a["username"]: a for a in db.list_accounts(limit=100)[0]}
        ok = True

        ok &= check("TOTP 시드로 자동 통과 (seed01)", rows["seed01"]["status"] == db.READY,
                    f"{rows['seed01']['status']} / {rows['seed01']['last_error']}")
        ok &= check("공백 포함 시드도 자동 통과 (seed02)", rows["seed02"]["status"] == db.READY,
                    rows["seed02"]["status"])
        ok &= check("백업코드로 통과 (bkup01)", rows["bkup01"]["status"] == db.READY,
                    f"{rows['bkup01']['status']} / {rows['bkup01']['last_error']}")
        ok &= check("2FA 없는 계정 정상 (plain1)", rows["plain1"]["status"] == db.READY)

        ok &= check("시드/백업 없으면 수동 대상 (none01)",
                    rows["none01"]["status"] == db.TWO_FACTOR, rows["none01"]["status"])
        ok &= check("틀린 시드는 2fa로 남음 (wrong1)",
                    rows["wrong1"]["status"] == db.TWO_FACTOR, rows["wrong1"]["status"])

        ok &= check("사용한 백업코드 1개만 소모",
                    rows["bkup01"]["backup_codes"] == "BBB222",
                    str(rows["bkup01"]["backup_codes"]))

        codes = {u: c for u, c in LEDGER.codes}
        ok &= check("TOTP 코드를 실제로 전송",
                    codes.get("seed01") == db.totp_code(SEED_A),
                    str(codes.get("seed01")))
        ok &= check("백업코드를 실제로 전송", codes.get("bkup01") == "AAA111",
                    str(codes.get("bkup01")))

        ok &= check("성공 4건", r["ready"] == 4, str(r["ready"]))
        ok &= check("2FA 미해결 2건", r["2fa"] == 2, str(r["2fa"]))
        ok &= check("자동 인증 사유 기록",
                    "TOTP" in (rows["seed01"]["last_error"] or ""),
                    str(rows["seed01"]["last_error"]))
        return ok
    finally:
        BulkLogin._wait_next_totp_window = orig_wait
        TFA_ACCOUNTS.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def test_import_and_slots():
    print("\n[4] 계정 등록 + 슬롯 균등 분배")
    tmp = Path(tempfile.mkdtemp())
    try:
        fresh_db(tmp)
        recs = [{"username": f"acc{i:05d}", "password": f"pw{i}"} for i in range(1, 101)]
        r = db.add_accounts(recs, slot_count=5)

        rows, total = db.list_accounts(limit=1000)
        per_slot = {}
        for a in rows:
            per_slot[a["proxy_slot"]] = per_slot.get(a["proxy_slot"], 0) + 1

        ok = True
        ok &= check("100건 등록", r["added"] == 100 and total == 100)
        ok &= check("슬롯 5개에 균등 분배",
                    sorted(per_slot.values()) == [20] * 5, str(sorted(per_slot.values())))

        # 재등록 시 슬롯이 바뀌면 안 된다 (계정-IP 일관성)
        before = {a["username"]: a["proxy_slot"] for a in rows}
        db.add_accounts([{"username": "acc00001", "password": "newpw"}], slot_count=5)
        after_row = db.list_accounts(q="acc00001")[0][0]
        ok &= check("재등록해도 슬롯 고정", after_row["proxy_slot"] == before["acc00001"])
        ok &= check("비밀번호는 갱신", after_row["password"] == "newpw")
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bulk_login():
    print("\n[5] 대량 로그인 — 결과 분류 / 슬롯 고정 / 병렬성")
    tmp = Path(tempfile.mkdtemp())
    try:
        fresh_db(tmp)
        global LEDGER
        LEDGER = Ledger()
        bulk_login.Client = FakeClient
        import test_server
        test_server.LEDGER = LEDGER

        cfg = build_env(tmp, slots=5)

        def R(prefix, n):
            return [{"username": f"{prefix}{i:04d}", "password": "pw"} for i in range(n)]

        recs = R("ok", 60) + R("chal", 8) + R("tfa", 5) + R("badpw", 4) + R("spam", 3) + R("err", 5)
        db.add_accounts(recs, slot_count=5)

        engine = BulkLogin(cfg)
        engine.xproxy.rotate_ip = lambda slot, wait_seconds=0: True

        accounts = db.accounts_for_login()
        job_id = db.create_job("bulk_login", total=len(accounts))
        result = engine.run(accounts, job_id=job_id, skip_preflight=True)

        ok = True
        ok &= check("성공 60건", result["ready"] == 60, str(result["ready"]))
        ok &= check("챌린지 8건", result["challenge"] == 8, str(result["challenge"]))
        ok &= check("2FA 5건", result["2fa"] == 5, str(result["2fa"]))
        ok &= check("비번오류 4건", result["bad_pw"] == 4, str(result["bad_pw"]))
        ok &= check("밴 3건", result["banned"] == 3, str(result["banned"]))
        ok &= check("레이트리밋 5건", result.get("rate_limit", 0) == 5,
                    f"rate_limit={result.get('rate_limit', 0)} failed={result.get('failed', 0)}")

        ok &= check("유심 병렬 동작", LEDGER.peak > 1, f"최대 동시 {LEDGER.peak}개")

        # 계정이 자기 슬롯 포트로만 접속했는지
        slot_of = {a["username"]: a["proxy_slot"] for a in accounts}
        mismatched = [
            u for u, proxy in LEDGER.logins
            if proxy and int(proxy.rsplit(":", 1)[1]) != 30000 + slot_of[u]
        ]
        ok &= check("계정-슬롯 고정 준수", not mismatched, f"{len(mismatched)}건 불일치")

        # DB 상태 반영
        st = db.stats()
        ok &= check("DB 상태 반영", st["ready"] == 60, str(st["by_status"]))

        # 세션 파일 생성
        sessions = list((tmp / "sessions").glob("*.json"))
        ok &= check("성공 계정만 세션 생성", len(sessions) == 60, f"{len(sessions)}개")

        # 실패 계정은 재시도 대상으로 남는다
        retry = db.accounts_for_login()
        ok &= check("실패 5건만 재시도 대상", len(retry) == 5, f"{len(retry)}건")

        # 준비된 계정 조회
        ready = db.ready_accounts(max_daily=10)
        ok &= check("실전 투입 가능 60건", len(ready) == 60, f"{len(ready)}건")
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stop():
    print("\n[6] 중단 요청")
    tmp = Path(tempfile.mkdtemp())
    try:
        fresh_db(tmp)
        global LEDGER
        LEDGER = Ledger()
        bulk_login.Client = FakeClient

        cfg = build_env(tmp, slots=3)
        db.add_accounts([{"username": f"ok{i:04d}", "password": "pw"} for i in range(300)],
                        slot_count=3)

        engine = BulkLogin(cfg)
        engine.xproxy.rotate_ip = lambda slot, wait_seconds=0: True
        engine.gap_min = engine.gap_max = 0.02

        accounts = db.accounts_for_login()
        job_id = db.create_job("bulk_login", total=len(accounts))

        holder = {}
        t = threading.Thread(
            target=lambda: holder.update(engine.run(accounts, job_id, skip_preflight=True))
        )
        t.start()
        time.sleep(0.6)
        engine.stop()
        t.join(timeout=15)

        ok = True
        ok &= check("스레드 정상 종료", not t.is_alive())
        ok &= check("중단 플래그 기록", holder.get("stopped") is True)
        done = sum(v for k, v in holder.items() if k in
                   ("ready", "challenge", "2fa", "bad_pw", "banned", "failed"))
        ok &= check("전량 처리 전에 멈춤", 0 < done < 300, f"{done}/300건 처리 후 중단")
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_web_routes():
    print("\n[7] 웹 라우트")
    tmp = Path(tempfile.mkdtemp())
    try:
        fresh_db(tmp)
        bulk_login.Client = FakeClient

        import server
        server.CONFIG_PATH = build_env(tmp, slots=5)
        client = TestClient(server.app)

        with client:
            ok = True
            # 인증 미들웨어 통과를 위해 먼저 로그인
            r = client.post("/signin", data={"username": "bjdlclrh", "password": "wnsrl1019"},
                            follow_redirects=False)
            ok &= check("관리자 로그인 성공", r.status_code == 302, f"HTTP {r.status_code}")

            for path in ["/", "/accounts", "/login"]:
                r = client.get(path)
                ok &= check(f"GET {path}", r.status_code == 200, f"HTTP {r.status_code}")

            # 계정 등록
            r = client.post("/api/accounts/import",
                            data={"text": "webacc1:pw1\nwebacc2,pw2\nwebacc3\tpw3"})
            d = r.json()
            ok &= check("POST /api/accounts/import",
                        r.status_code == 200 and d["added"] == 3, str(d))

            # 잘못된 입력 거부
            r = client.post("/api/accounts/import", data={"text": "쓰레기"})
            ok &= check("빈 입력 400 거부", r.status_code == 400, f"HTTP {r.status_code}")

            # 통계
            d = client.get("/api/stats").json()
            ok &= check("GET /api/stats", d["total"] == 3, str(d))

            # 필터 조회
            r = client.get("/accounts?status=new&q=webacc")
            ok &= check("계정 필터 조회", r.status_code == 200 and "webacc1" in r.text)

            # 내보내기
            r = client.get("/api/accounts/export")
            ok &= check("CSV 내보내기",
                        r.status_code == 200 and "webacc1" in r.text)

            # 삭제
            r = client.post("/api/accounts/delete", data={"usernames": "webacc3"})
            ok &= check("계정 삭제", r.json()["deleted"] == 1)

            # 로그인 대상 없을 때
            client.post("/api/accounts/delete", data={"usernames": "webacc1,webacc2"})
            r = client.post("/api/login/start", data={"scope": "retryable", "limit": 0})
            ok &= check("대상 없으면 400", r.status_code == 400, f"HTTP {r.status_code}")

            # 없는 작업 조회
            ok &= check("없는 작업 404", client.get("/api/job/9999").status_code == 404)
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_web_login_flow():
    print("\n[8] 웹에서 로그인 작업 전체 흐름")
    tmp = Path(tempfile.mkdtemp())
    try:
        fresh_db(tmp)
        global LEDGER
        LEDGER = Ledger()
        bulk_login.Client = FakeClient

        import server
        server.CONFIG_PATH = build_env(tmp, slots=5)
        server.runner = server.Runner()

        _orig = BulkLogin.__init__

        def patched(self, path=server.CONFIG_PATH):
            _orig(self, server.CONFIG_PATH)
            self.xproxy.rotate_ip = lambda slot, wait_seconds=0: True
            self.xproxy.preflight = lambda: {
                "safe": True, "home_ip": "1.2.3.4", "slots": {},
                "online": 5, "total": 5, "unique_ips": 5,
                "leaking": [], "offline": [], "duplicate": False,
            }
            self.gap_min = self.gap_max = 0

        BulkLogin.__init__ = patched
        try:
            client = TestClient(server.app)
            with client:
                client.post("/signin", data={"username": "bjdlclrh", "password": "wnsrl1019"})
                lines = "\n".join(f"ok{i:04d}:pw{i}" for i in range(40))
                lines += "\n" + "\n".join(f"chal{i:02d}:pw" for i in range(5))
                client.post("/api/accounts/import", data={"text": lines})

                r = client.post("/api/login/start", data={"scope": "retryable", "limit": 0})
                ok = check("작업 시작", r.status_code == 200, f"HTTP {r.status_code}")
                job_id = r.json()["job_id"]

                # 중복 실행 차단
                r2 = client.post("/api/login/start", data={"scope": "all", "limit": 0})
                ok &= check("중복 실행 409 차단", r2.status_code == 409, f"HTTP {r2.status_code}")

                deadline = time.time() + 40
                job = None
                while time.time() < deadline:
                    job = client.get(f"/api/job/{job_id}").json()["job"]
                    if job["status"] != "running":
                        break
                    time.sleep(0.3)

                ok &= check("작업 완료", job and job["status"] == "done", str(job and job["status"]))
                ok &= check("성공 40건", job["succeeded"] == 40, str(job["succeeded"]))
                ok &= check("실패 5건", job["failed"] == 5, str(job["failed"]))

                d = client.get(f"/api/job/{job_id}?after=0").json()
                ok &= check("작업 로그 기록됨", len(d["events"]) > 10, f"{len(d['events'])}줄")
                ok &= check("작업 페이지 렌더", client.get(f"/job/{job_id}").status_code == 200)
                ok &= check("최종 통계 반영", d["stats"]["ready"] == 40, str(d["stats"]["by_status"]))
            return ok
        finally:
            BulkLogin.__init__ = _orig
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_proxy_enforcement():
    print("\n[9] 프록시 강제 적용 / IP 누출 차단")
    tmp = Path(tempfile.mkdtemp())
    try:
        fresh_db(tmp)
        global LEDGER
        LEDGER = Ledger()
        bulk_login.Client = FakeClient

        cfg = build_env(tmp, slots=3)
        db.add_accounts([{"username": f"ok{i:04d}", "password": "pw"} for i in range(9)],
                        slot_count=3)
        accounts = db.accounts_for_login()

        engine = BulkLogin(cfg)
        engine.xproxy.rotate_ip = lambda slot, wait_seconds=0: True
        ok = True

        def fake_preflight(**kw):
            base = {"safe": True, "home_ip": "1.2.3.4", "slots": {},
                    "online": 3, "total": 3, "unique_ips": 3,
                    "leaking": [], "offline": [], "duplicate": False}
            base.update(kw)
            return lambda: base

        # ── 실제 IP 누출이면 시작 자체가 막혀야 한다 ──
        engine.xproxy.preflight = fake_preflight(safe=False, leaking=["sim1"])
        blocked = False
        try:
            engine.run(accounts, job_id=db.create_job("bulk_login", len(accounts)))
        except RuntimeError as e:
            blocked = "누출" in str(e)
        ok &= check("IP 누출 시 로그인 전면 차단", blocked)
        ok &= check("누출 상태에서 발사 0건", len(LEDGER.logins) == 0, f"{len(LEDGER.logins)}건")

        # ── 오프라인 슬롯도 차단 ──
        engine.xproxy.preflight = fake_preflight(safe=False, offline=["sim3"], online=2)
        blocked = False
        try:
            engine.run(accounts, job_id=db.create_job("bulk_login", len(accounts)))
        except RuntimeError as e:
            blocked = "오프라인" in str(e)
        ok &= check("오프라인 슬롯 있으면 차단", blocked)

        # ── 정상이면 통과 ──
        engine.xproxy.preflight = fake_preflight()
        r = engine.run(accounts, job_id=db.create_job("bulk_login", len(accounts)))
        ok &= check("안전 확인 후 정상 실행", r["ready"] == 9, str(r["ready"]))

        # ── 세션에 프록시가 안 걸리면 개별 계정도 차단 ──
        class NoProxyClient(FakeClient):
            def set_proxy(self, url):
                pass  # 프록시 설정이 먹지 않는 상황 재현

        bulk_login.Client = NoProxyClient
        db.add_accounts([{"username": "leak0001", "password": "pw"}], slot_count=3)
        leak_acc = [a for a in db.accounts_for_login() if a["username"] == "leak0001"]
        before = len(LEDGER.logins)
        r2 = engine.run(leak_acc, job_id=db.create_job("bulk_login", 1))
        ok &= check("프록시 미적용 계정은 접속 차단",
                    r2["ready"] == 0 and r2["failed"] == 1, str(r2))
        ok &= check("차단된 계정은 패킷 미발사",
                    len(LEDGER.logins) == before, f"{len(LEDGER.logins) - before}건 유출")

        row = db.list_accounts(q="leak0001")[0][0]
        ok &= check("차단 사유 DB 기록",
                    "프록시" in (row["last_error"] or ""), str(row["last_error"]))
        return ok
    finally:
        bulk_login.Client = FakeClient
        shutil.rmtree(tmp, ignore_errors=True)


def test_proxy_url_validation():
    print("\n[10] 프록시 URL 검증")
    from xproxy_manager import XProxyManager
    slots = [{"port": 30001, "name": "sim1"}]
    ok = True

    mgr = XProxyManager(host="192.168.0.100", api_port=8080, slots=slots)
    ok &= check("정상 URL 생성",
                mgr.get_proxy_url(0) == "socks5://192.168.0.100:30001", mgr.get_proxy_url(0))

    # 빈 host → instagrapi가 프록시를 '해제'하므로 반드시 예외여야 한다
    for bad_host in ("", "   ", None):
        raised = False
        try:
            XProxyManager(host=bad_host, api_port=8080, slots=slots).get_proxy_url(0)
        except ValueError:
            raised = True
        ok &= check(f"빈 host({bad_host!r}) 거부", raised)

    raised = False
    try:
        XProxyManager(host="1.2.3.4", api_port=8080, slots=[{"name": "s"}]).get_proxy_url(0)
    except ValueError:
        raised = True
    ok &= check("포트 없으면 거부", raised)

    auth = XProxyManager(host="1.2.3.4", api_port=8080, slots=slots,
                         username="u", password="p").get_proxy_url(0)
    ok &= check("인증정보 포함", auth == "socks5://u:p@1.2.3.4:30001", auth)

    # 누출 판정 로직
    mgr2 = XProxyManager(host="1.2.3.4", api_port=8080,
                         slots=[{"port": 30001, "name": "s1"}, {"port": 30002, "name": "s2"}])
    mgr2.direct_ip = lambda: "203.0.113.9"
    mgr2.health_check = lambda: {
        "s1": {"proxy": "", "ip": "203.0.113.9", "status": "online"},   # 집 IP = 누출
        "s2": {"proxy": "", "ip": "10.20.30.40", "status": "online"},
    }
    pf = mgr2.preflight()
    ok &= check("실제 IP와 같은 슬롯을 누출로 판정",
                pf["leaking"] == ["s1"] and not pf["safe"], str(pf["leaking"]))

    mgr2.health_check = lambda: {
        "s1": {"proxy": "", "ip": "10.0.0.1", "status": "online"},
        "s2": {"proxy": "", "ip": "10.0.0.1", "status": "online"},      # 중복
    }
    pf = mgr2.preflight()
    ok &= check("슬롯 간 IP 중복 탐지", pf["duplicate"] and not pf["safe"])

    mgr2.health_check = lambda: {
        "s1": {"proxy": "", "ip": "10.0.0.1", "status": "online"},
        "s2": {"proxy": "", "ip": "10.0.0.2", "status": "online"},
    }
    ok &= check("정상 구성은 safe", mgr2.preflight()["safe"])
    return ok


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)

    print("=" * 62)
    print("  웹 서버 + 대량 로그인 검증")
    print("=" * 62)

    results = [
        test_parse(),
        test_totp(),
        test_auto_2fa_login(),
        test_import_and_slots(),
        test_bulk_login(),
        test_stop(),
        test_web_routes(),
        test_web_login_flow(),
        test_proxy_enforcement(),
        test_proxy_url_validation(),
    ]

    print("\n" + "=" * 62)
    if all(results):
        print(f"  ✅ 전체 통과 ({len(results)}/{len(results)})")
    else:
        print(f"  ❌ 실패 {results.count(False)}건 / 전체 {len(results)}건")
    print("=" * 62)
    raise SystemExit(0 if all(results) else 1)
