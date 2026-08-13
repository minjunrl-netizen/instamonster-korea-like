"""
SQLite 데이터 레이어

계정 4,000개를 파일 글롭으로 관리하는 건 불가능하다.
계정 상태(신규/준비완료/챌린지/밴), 유심 슬롯 고정, 일일 사용량,
작업 이력을 전부 여기서 관리한다.

스레드 안전:
  워커가 슬롯마다 동시에 붙으므로 커넥션을 스레드별로 따로 연다.
  WAL 모드라 읽기는 서로 막지 않고, 쓰기는 짧은 트랜잭션으로 끝낸다.
"""

import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from devices import pick_device

DB_PATH = Path("instamonster.db")

# ─── 계정 상태 ───
NEW = "new"                # 등록만 됨, 로그인 전
READY = "ready"            # 로그인 성공, 세션 있음 → 실전 투입 가능
WARMING = "warming"        # 신규 계정 워밍업 진행 중 → 아직 실전 투입 불가
CHALLENGE = "challenge"    # 인증 챌린지 - 수동 해결 필요
TWO_FACTOR = "2fa"         # 2단계 인증 - 코드 필요
BAD_PASSWORD = "bad_pw"    # 비번 틀림
NOT_EXIST = "not_exist"    # 계정이 인스타에서 삭제/비활성화됨 — 재시도 무의미
BANNED = "banned"          # 밴/스팸 감지 - 폐기
RATE_LIMITED = "rate_limit"  # 레이트리밋 - 시간 지나면 재시도
FAILED = "failed"          # 기타 일시적 실패 - 재시도 가능

# ── 사용 불가 (교체 필요) ──
DEAD_STATUSES = (BAD_PASSWORD, NOT_EXIST, BANNED)

ACTIVE_STATUSES = (READY,)
RETRYABLE_STATUSES = (NEW, FAILED, RATE_LIMITED)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS accounts (
    username        TEXT PRIMARY KEY,
    password        TEXT NOT NULL,
    totp_seed       TEXT,
    backup_codes    TEXT,
    status          TEXT NOT NULL DEFAULT 'new',
    session_file    TEXT,
    proxy_slot      INTEGER,
    device_model    TEXT,
    last_login      TEXT,
    last_error      TEXT,
    login_attempts  INTEGER NOT NULL DEFAULT 0,
    likes_today     INTEGER NOT NULL DEFAULT 0,
    likes_total     INTEGER NOT NULL DEFAULT 0,
    counter_date    TEXT,
    warmup_day      INTEGER NOT NULL DEFAULT 0,
    note            TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
CREATE INDEX IF NOT EXISTS idx_accounts_slot   ON accounts(proxy_slot);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    total       INTEGER NOT NULL DEFAULT 0,
    processed   INTEGER NOT NULL DEFAULT 0,
    succeeded   INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT,
    finished_at TEXT,
    message     TEXT
);

CREATE TABLE IF NOT EXISTS job_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER NOT NULL,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id, id);
"""

_local = threading.local()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    """스레드별 커넥션 (워커가 동시에 붙어도 안전)"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """기존 DB에 없는 컬럼을 채운다"""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
    cols = (
        ("totp_seed", "TEXT"), ("backup_codes", "TEXT"), ("device_model", "TEXT"),
        ("follower_count", "INTEGER"), ("following_count", "INTEGER"),
        ("media_count", "INTEGER"), ("age_class", "TEXT"),
        ("warmup_started", "TEXT"), ("last_warmup", "TEXT"), ("last_post", "TEXT"),
        ("total_posts", "INTEGER"),
        ("health_status", "TEXT"), ("last_health_check", "TEXT"),
        ("ig_user_id", "TEXT"), ("email", "TEXT"), ("email_password", "TEXT"),
    )
    for col, ddl in cols:
        if col not in have:
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {col} {ddl}")


# ─── 2FA 시드 ───

BASE32_RE = re.compile(r"^[A-Z2-7]+=*$")


def totp_code(seed: str) -> str:
    """
    시드로 현재 TOTP 코드 생성.

    순수 계산이라 HTTP 클라이언트와 무관하다. instagrapi의 Client에 붙어있지만
    staticmethod이므로 여기서 직접 부른다.
    """
    from instagrapi import Client as _IGClient
    return _IGClient.totp_generate_code(seed)


def normalize_totp_seed(raw: str | None) -> str | None:
    """
    TOTP 시드 정규화 + 검증.

    공백/하이픈을 지우고 대문자로 맞춘 뒤 base32인지 확인한다.
    instagrapi의 totp_generate_code는 빈 문자열로도 그럴싸한 6자리를 뱉으므로,
    형식 검증을 여기서 확실히 해두지 않으면 계정이 무한 실패한다.
    """
    if not raw:
        return None

    seed = re.sub(r"[\s\-_]", "", str(raw)).upper()
    if len(seed) < 16 or not BASE32_RE.match(seed):
        return None

    try:
        code = totp_code(seed)
    except Exception:
        return None

    return seed if code and code.isdigit() and len(code) == 6 else None


def parse_backup_codes(raw: str | None) -> list[str]:
    """백업코드 목록 파싱 (쉼표/공백/슬래시 구분)"""
    if not raw:
        return []
    parts = re.split(r"[,\s/;]+", str(raw))
    return [p.strip() for p in parts if p.strip()]


# ─────────────────────────── 계정 ───────────────────────────

def add_accounts(records: list[dict], slot_count: int) -> dict:
    """
    계정 등록. 각 레코드는 {username, password, totp_seed?, backup_codes?}.

    계정마다 유심 슬롯을 고정한다. 인스타는 계정-디바이스-IP 대역의 일관성을 보므로,
    같은 계정이 매번 다른 통신사에서 접속하면 그 자체로 신호가 된다.
    """
    added = updated = with_2fa = bad_seed = 0
    now = _now()

    with tx() as conn:
        # 슬롯을 고르게 채우기 위해 현재 분포를 먼저 본다
        rows = conn.execute(
            "SELECT proxy_slot, COUNT(*) c FROM accounts GROUP BY proxy_slot"
        ).fetchall()
        load = {i: 0 for i in range(slot_count)}
        for r in rows:
            if r["proxy_slot"] is not None and r["proxy_slot"] < slot_count:
                load[r["proxy_slot"]] = r["c"]

        for rec in records:
            username = str(rec.get("username", "")).strip().lstrip("@")
            password = str(rec.get("password", "")).strip()
            if not username or not password:
                continue

            raw_seed = rec.get("totp_seed")
            seed = normalize_totp_seed(raw_seed)
            if raw_seed and not seed:
                bad_seed += 1
            if seed:
                with_2fa += 1

            codes = parse_backup_codes(rec.get("backup_codes"))
            codes_json = ",".join(codes) if codes else None

            exists = conn.execute(
                "SELECT username FROM accounts WHERE username=?", (username,)
            ).fetchone()

            if exists:
                # 슬롯은 절대 바꾸지 않는다. 시드/백업코드는 준 값이 있을 때만 덮어쓴다.
                conn.execute(
                    """UPDATE accounts SET
                         password=?,
                         totp_seed=COALESCE(?, totp_seed),
                         backup_codes=COALESCE(?, backup_codes),
                         updated_at=?
                       WHERE username=?""",
                    (password, seed, codes_json, now, username),
                )
                updated += 1
                continue

            slot = min(load, key=load.get)
            load[slot] += 1

            device = pick_device(username)
            device_model = device.get("model", "")

            conn.execute(
                """INSERT INTO accounts
                   (username, password, totp_seed, backup_codes,
                    status, proxy_slot, device_model, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (username, password, seed, codes_json, NEW, slot, device_model, now, now),
            )
            added += 1

    return {"added": added, "updated": updated,
            "with_2fa": with_2fa, "bad_seed": bad_seed}


def consume_backup_code(username: str) -> str | None:
    """백업코드 1개를 꺼내 쓰고 목록에서 제거한다 (1회용)"""
    with tx() as conn:
        row = conn.execute(
            "SELECT backup_codes FROM accounts WHERE username=?", (username,)
        ).fetchone()
        codes = parse_backup_codes(row["backup_codes"] if row else None)
        if not codes:
            return None
        used, rest = codes[0], codes[1:]
        conn.execute(
            "UPDATE accounts SET backup_codes=?, updated_at=? WHERE username=?",
            (",".join(rest) if rest else None, _now(), username),
        )
        return used


def set_totp_seed(username: str, raw_seed: str) -> bool:
    seed = normalize_totp_seed(raw_seed)
    if not seed:
        return False
    with tx() as conn:
        conn.execute(
            "UPDATE accounts SET totp_seed=?, updated_at=? WHERE username=?",
            (seed, _now(), username),
        )
    return True


def preview_totp(username: str) -> dict | None:
    """
    저장된 시드로 현재 TOTP 코드를 생성.
    인증앱 화면과 대조해서 시드가 맞는지 확인하는 용도.
    """
    import time

    row = connect().execute(
        "SELECT totp_seed FROM accounts WHERE username=?", (username,)
    ).fetchone()
    if not row or not row["totp_seed"]:
        return None

    try:
        code = totp_code(row["totp_seed"])
    except Exception:
        return None

    return {"code": code, "expires_in": int(30 - (time.time() % 30))}


def accounts_for_login(statuses: tuple[str, ...] = RETRYABLE_STATUSES,
                       limit: int | None = None) -> list[dict]:
    q = ("SELECT * FROM accounts WHERE status IN (%s) ORDER BY proxy_slot, username"
         % ",".join("?" * len(statuses)))
    params = list(statuses)
    if limit:
        q += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in connect().execute(q, params).fetchall()]


def mark_login_result(username: str, status: str, session_file: str | None = None,
                      error: str | None = None) -> None:
    with tx() as conn:
        conn.execute(
            """UPDATE accounts SET
                 status=?, session_file=COALESCE(?, session_file),
                 last_error=?, login_attempts=login_attempts+1,
                 last_login=CASE WHEN ?=? THEN ? ELSE last_login END,
                 updated_at=?
               WHERE username=?""",
            (status, session_file, error, status, READY, _now(), _now(), username),
        )


def set_status(username: str, status: str, error: str | None = None) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE accounts SET status=?, last_error=?, updated_at=? WHERE username=?",
            (status, error, _now(), username),
        )


def save_metrics(username: str, metrics: dict) -> None:
    """로그인 후 수집한 나이/활동 지표를 저장한다"""
    with tx() as conn:
        conn.execute(
            """UPDATE accounts SET
                 ig_user_id=?, follower_count=?, following_count=?,
                 media_count=?, age_class=?, updated_at=?
               WHERE username=?""",
            (
                str(metrics.get("user_id", "")),
                int(metrics.get("follower_count", 0)),
                int(metrics.get("following_count", 0)),
                int(metrics.get("media_count", 0)),
                str(metrics.get("age_class", "")),
                _now(), username,
            ),
        )


def start_warmup(username: str) -> None:
    """계정을 워밍업 상태로 전환하고 시작일을 기록"""
    with tx() as conn:
        conn.execute(
            """UPDATE accounts SET
                 status=?, warmup_day=1, warmup_started=?, updated_at=?
               WHERE username=?""",
            (WARMING, _now(), _now(), username),
        )


def advance_warmup(username: str, graduated: bool = False) -> None:
    """워밍업 하루 진행. graduated면 ready로 전환."""
    with tx() as conn:
        if graduated:
            conn.execute(
                "UPDATE accounts SET status=?, last_warmup=?, updated_at=? WHERE username=?",
                (READY, _now(), _now(), username),
            )
        else:
            conn.execute(
                """UPDATE accounts SET
                     warmup_day=warmup_day+1, last_warmup=?, updated_at=?
                   WHERE username=?""",
                (_now(), _now(), username),
            )


def warming_accounts(due_only: bool = True) -> list[dict]:
    """워밍업 중인 계정 목록. due_only면 오늘 아직 워밍업 안 한 것만."""
    today = date.today().isoformat()
    q = "SELECT * FROM accounts WHERE status=?"
    params = [WARMING]
    if due_only:
        q += " AND (last_warmup IS NULL OR substr(last_warmup,1,10)<>?)"
        params.append(today)
    q += " ORDER BY warmup_day ASC, username"
    return [dict(r) for r in connect().execute(q, params).fetchall()]


def set_email(username: str, email: str, email_password: str) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE accounts SET email=?, email_password=?, updated_at=? WHERE username=?",
            (email, email_password, _now(), username),
        )


def get_account(username: str) -> dict | None:
    r = connect().execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
    return dict(r) if r else None


def record_post(username: str) -> None:
    """포스팅 완료 기록 (마지막 포스팅일 갱신 + 카운트 증가)"""
    with tx() as conn:
        conn.execute(
            """UPDATE accounts SET
                 last_post=?, total_posts=COALESCE(total_posts,0)+1, updated_at=?
               WHERE username=?""",
            (_now(), _now(), username),
        )


def record_health(username: str, health: str, new_status: str | None = None) -> None:
    """
    헬스체크 결과 기록. new_status가 있으면 계정 상태도 갱신
    (죽은 계정을 banned/challenge 등으로 전환).
    """
    with tx() as conn:
        if new_status:
            conn.execute(
                """UPDATE accounts SET
                     health_status=?, last_health_check=?, status=?, updated_at=?
                   WHERE username=?""",
                (health, _now(), new_status, _now(), username),
            )
        else:
            conn.execute(
                """UPDATE accounts SET
                     health_status=?, last_health_check=?, updated_at=?
                   WHERE username=?""",
                (health, _now(), _now(), username),
            )


def monitorable_accounts() -> list[dict]:
    """헬스체크 대상: 세션이 있는 계정 (ready/warming)"""
    rows = connect().execute(
        "SELECT * FROM accounts WHERE status IN (?, ?) AND session_file IS NOT NULL",
        (READY, WARMING),
    ).fetchall()
    return [dict(r) for r in rows]


def health_summary() -> dict:
    """헬스체크 현황 요약 (대시보드용)"""
    rows = connect().execute(
        "SELECT username, status, health_status, last_health_check "
        "FROM accounts WHERE session_file IS NOT NULL ORDER BY last_health_check DESC"
    ).fetchall()
    by_health: dict[str, int] = {}
    last_check = None
    attention = []
    for r in rows:
        h = r["health_status"] or "미점검"
        by_health[h] = by_health.get(h, 0) + 1
        if r["last_health_check"] and (last_check is None or r["last_health_check"] > last_check):
            last_check = r["last_health_check"]
        if h in ("banned", "not_exist", "challenge", "session_expired"):
            attention.append({"username": r["username"], "health": h,
                              "status": r["status"]})
    return {
        "total": len(rows),
        "alive": by_health.get("alive", 0),
        "by_health": by_health,
        "last_check": last_check,
        "attention": attention,
    }


def bump_likes(username: str, n: int = 1) -> None:
    """일일 카운터는 날짜가 바뀌면 자동 리셋된다"""
    today = date.today().isoformat()
    with tx() as conn:
        conn.execute(
            """UPDATE accounts SET
                 likes_today = CASE WHEN counter_date=? THEN likes_today+? ELSE ? END,
                 likes_total = likes_total+?,
                 counter_date=?, updated_at=?
               WHERE username=?""",
            (today, n, n, n, today, _now(), username),
        )


def ready_accounts(max_daily: int, slot: int | None = None) -> list[dict]:
    """실전 투입 가능한 계정 (오늘 한도 미달)"""
    today = date.today().isoformat()
    q = """SELECT * FROM accounts
           WHERE status=? AND session_file IS NOT NULL
             AND (counter_date IS NULL OR counter_date<>? OR likes_today<?)"""
    params = [READY, today, max_daily]
    if slot is not None:
        q += " AND proxy_slot=?"
        params.append(slot)
    q += " ORDER BY likes_today ASC, username"
    return [dict(r) for r in connect().execute(q, params).fetchall()]


def stats() -> dict:
    conn = connect()
    rows = conn.execute("SELECT status, COUNT(*) c FROM accounts GROUP BY status").fetchall()
    by_status = {r["status"]: r["c"] for r in rows}
    total = sum(by_status.values())

    slots = conn.execute(
        "SELECT proxy_slot, COUNT(*) c FROM accounts WHERE status=? GROUP BY proxy_slot",
        (READY,),
    ).fetchall()

    today = date.today().isoformat()
    likes = conn.execute(
        "SELECT COALESCE(SUM(likes_today),0) s FROM accounts WHERE counter_date=?", (today,)
    ).fetchone()["s"]

    tfa = conn.execute(
        """SELECT
             COUNT(*) FILTER (WHERE totp_seed IS NOT NULL)    seeds,
             COUNT(*) FILTER (WHERE backup_codes IS NOT NULL) backups
           FROM accounts"""
    ).fetchone()

    device_rows = conn.execute(
        "SELECT device_model, COUNT(*) c FROM accounts WHERE device_model IS NOT NULL GROUP BY device_model ORDER BY c DESC"
    ).fetchall()


    return {
        "total": total,
        "by_status": by_status,
        "ready": by_status.get(READY, 0),
        "per_slot": {r["proxy_slot"]: r["c"] for r in slots},
        "likes_today": likes,
        "totp_seeds": tfa["seeds"],
        "backup_sets": tfa["backups"],
        "devices": {r["device_model"]: r["c"] for r in device_rows},
    }


def list_accounts(status: str | None = None, q: str | None = None,
                  limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
    where, params = [], []
    if status:
        where.append("status=?")
        params.append(status)
    if q:
        where.append("username LIKE ?")
        params.append(f"%{q}%")
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    conn = connect()
    total = conn.execute(f"SELECT COUNT(*) c FROM accounts{clause}", params).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM accounts{clause} ORDER BY username LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [dict(r) for r in rows], total


def delete_accounts(usernames: list[str]) -> int:
    if not usernames:
        return 0
    with tx() as conn:
        cur = conn.execute(
            "DELETE FROM accounts WHERE username IN (%s)" % ",".join("?" * len(usernames)),
            usernames,
        )
        return cur.rowcount


def dead_accounts() -> list[dict]:
    """교체가 필요한 계정 (삭제/비번오류/밴)"""
    placeholders = ",".join("?" * len(DEAD_STATUSES))
    rows = connect().execute(
        f"SELECT * FROM accounts WHERE status IN ({placeholders}) ORDER BY status, username",
        list(DEAD_STATUSES),
    ).fetchall()
    return [dict(r) for r in rows]


def replace_account(old_username: str, new_username: str, new_password: str,
                    totp_seed: str | None = None, backup_codes: str | None = None) -> bool:
    """
    죽은 계정을 새 계정으로 교체.

    슬롯은 그대로 유지한다 — 같은 유심 대역에서 계속 쓰기 위해.
    죽은 계정의 세션 파일도 정리한다.
    """
    now = _now()
    seed = normalize_totp_seed(totp_seed)
    codes = ",".join(parse_backup_codes(backup_codes)) if backup_codes else None

    with tx() as conn:
        old = conn.execute(
            "SELECT proxy_slot, session_file FROM accounts WHERE username=?",
            (old_username,),
        ).fetchone()
        if not old:
            return False

        slot = old["proxy_slot"]

        # 기존 세션 파일 삭제
        if old["session_file"]:
            Path(old["session_file"]).unlink(missing_ok=True)

        # 새 계정이 이미 있으면 업데이트
        exists = conn.execute(
            "SELECT 1 FROM accounts WHERE username=?", (new_username,)
        ).fetchone()

        if exists:
            conn.execute(
                """UPDATE accounts SET
                     password=?, totp_seed=COALESCE(?,totp_seed),
                     backup_codes=COALESCE(?,backup_codes),
                     proxy_slot=?, status=?, session_file=NULL,
                     last_error=NULL, login_attempts=0,
                     likes_today=0, likes_total=0,
                     updated_at=?
                   WHERE username=?""",
                (new_password, seed, codes, slot, NEW, now, new_username),
            )
        else:
            conn.execute(
                """INSERT INTO accounts
                   (username, password, totp_seed, backup_codes,
                    status, proxy_slot, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (new_username, new_password, seed, codes, NEW, slot, now, now),
            )

        # 죽은 계정 삭제
        conn.execute("DELETE FROM accounts WHERE username=?", (old_username,))
    return True


def account_diagnosis() -> dict:
    """
    전체 계정 건강 진단 리포트.

    각 상태별 개수 + 교체 필요한 계정 목록 + 재시도 가능한 계정 목록.
    """
    conn = connect()
    total = conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]

    by_status = {}
    for r in conn.execute("SELECT status, COUNT(*) c FROM accounts GROUP BY status"):
        by_status[r["status"]] = r["c"]

    dead_list = dead_accounts()

    challenge_list = [
        dict(r) for r in conn.execute(
            "SELECT username, last_error, last_login FROM accounts WHERE status=?",
            (CHALLENGE,),
        ).fetchall()
    ]

    tfa_list = [
        dict(r) for r in conn.execute(
            "SELECT username, totp_seed IS NOT NULL as has_seed, backup_codes FROM accounts WHERE status=?",
            (TWO_FACTOR,),
        ).fetchall()
    ]

    return {
        "total": total,
        "by_status": by_status,
        "usable": by_status.get(READY, 0),
        "dead_count": len(dead_list),
        "dead": dead_list,
        "challenge": challenge_list,
        "tfa": tfa_list,
        "retryable": by_status.get(FAILED, 0) + by_status.get(RATE_LIMITED, 0),
    }

# ─────────────────────────── 작업 ───────────────────────────

def create_job(kind: str, total: int = 0) -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (kind, status, total, started_at) VALUES (?,?,?,?)",
            (kind, "running", total, _now()),
        )
        return cur.lastrowid


def update_job(job_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with tx() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", [*fields.values(), job_id])


def finish_job(job_id: int, status: str, message: str = "") -> None:
    update_job(job_id, status=status, finished_at=_now(), message=message)


def get_job(job_id: int) -> dict | None:
    r = connect().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(r) if r else None


def running_job(kind: str | None = None) -> dict | None:
    q = "SELECT * FROM jobs WHERE status='running'"
    params = []
    if kind:
        q += " AND kind=?"
        params.append(kind)
    r = connect().execute(q + " ORDER BY id DESC LIMIT 1", params).fetchone()
    return dict(r) if r else None


def recent_jobs(limit: int = 20) -> list[dict]:
    rows = connect().execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def log_event(job_id: int, level: str, message: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO job_events (job_id, ts, level, message) VALUES (?,?,?,?)",
            (job_id, _now(), level, message),
        )


def job_events(job_id: int, after_id: int = 0, limit: int = 300) -> list[dict]:
    rows = connect().execute(
        "SELECT * FROM job_events WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
        (job_id, after_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
