"""
인스타몬스터 관리 서버 (로컬 웹)

실행:  python server.py
접속:  http://localhost:8000

계정 4,000개를 CLI로 관리하는 건 불가능하다.
등록 → 대량 로그인 → 상태 확인 → 실전 발사를 브라우저에서 처리한다.
"""

import json
import hmac
import hashlib
import secrets
import logging
import threading
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import db
from bulk_login import BulkLogin, parse_account_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = "config.json"
BASE = Path(__file__).parent
_SERVE_MODE = False  # python server.py로 직접 실행할 때만 True → 테스트/임포트 시 스케줄러 안 뜸

app = FastAPI(title="인스타몬스터 관리", lifespan=lambda _app: _lifespan(_app))
templates = Jinja2Templates(directory=str(BASE / "templates"))

(BASE / "static").mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


def _session_secret() -> str:
    """세션 서명 키 — 재시작해도 로그인이 유지되도록 파일에 영구 저장"""
    f = BASE / ".session_secret"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    s = secrets.token_hex(32)
    f.write_text(s, encoding="utf-8")
    return s


# 인증 없이 접근 가능한 경로
PUBLIC_PATHS = {"/signin"}
PUBLIC_PREFIXES = ("/static",)


@app.middleware("http")
async def require_login(request: Request, call_next):
    """로그인 안 하면 모든 페이지를 /signin으로 돌린다"""
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)

    if request.session.get("auth"):
        return await call_next(request)

    # API는 401, 페이지는 로그인으로 리다이렉트
    if path.startswith("/api/"):
        return JSONResponse({"detail": "로그인이 필요합니다"}, status_code=401)
    return RedirectResponse("/signin", status_code=302)


# SessionMiddleware를 require_login보다 나중에 추가 → 더 바깥에서 먼저 실행되어
# require_login이 request.session에 접근할 수 있게 한다.
app.add_middleware(SessionMiddleware, secret_key=_session_secret(),
                   session_cookie="im_session", max_age=60 * 60 * 12)


class Runner:
    """동시에 하나의 작업만 돌도록 관리"""

    def __init__(self):
        self.lock = threading.Lock()
        self.engine: BulkLogin | None = None
        self.thread: threading.Thread | None = None
        self.job_id: int | None = None

    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start_login(self, accounts: list[dict]) -> int:
        with self.lock:
            if self.busy:
                raise HTTPException(409, "이미 실행 중인 작업이 있다")

            job_id = db.create_job("bulk_login", total=len(accounts))
            engine = BulkLogin(CONFIG_PATH)
            self.engine = engine
            self.job_id = job_id

            def work():
                try:
                    result = engine.run(accounts, job_id=job_id)
                    status = "stopped" if result["stopped"] else "done"
                    db.finish_job(
                        job_id, status,
                        f"성공 {result['ready']} / 챌린지 {result['challenge']} / "
                        f"2FA {result['2fa']} / 비번오류 {result['bad_pw']} / "
                        f"밴 {result['banned']} / 실패 {result['failed']}",
                    )
                except Exception as e:
                    logger.exception("대량 로그인 실패")
                    db.log_event(job_id, "error", f"작업 중단: {e}")
                    db.finish_job(job_id, "failed", str(e))

            self.thread = threading.Thread(target=work, daemon=True, name="bulk-login")
            self.thread.start()
            return job_id

    def stop(self) -> bool:
        if self.engine and self.busy:
            self.engine.stop()
            return True
        return False


runner = Runner()


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def slot_count() -> int:
    return len(load_config()["xproxy"]["slots"])


def open_account_client(username: str):
    """
    특정 계정의 세션+프록시로 instagrapi 클라이언트를 연다.
    프로필 변경/업로드/분석에 쓴다. (로그인 호출 없음, 세션 재사용)
    """
    from bulk_login import make_client
    from xproxy_manager import make_provider

    acc = db.get_account(username)
    if not acc:
        raise HTTPException(404, f"계정 없음: {username}")
    if not acc.get("session_file"):
        raise HTTPException(400, f"{username}은 아직 로그인되지 않았다 (세션 없음)")

    cfg = load_config()
    provider = make_provider(cfg)
    slot = acc.get("proxy_slot") or 0

    cl = make_client(request_timeout=30)
    try:
        cl.load_settings(acc["session_file"])
    except Exception as e:
        raise HTTPException(400, f"세션 로드 실패: {e}")

    if not getattr(provider, "is_direct", False):
        cl.set_proxy(provider.get_proxy_url(slot))
    cl.delay_range = [2, 5]

    if not cl.user_id:
        raise HTTPException(400, f"{username} 세션이 만료됐다. 재로그인 필요.")
    return cl

def _check_admin(username: str, password: str) -> bool:
    """관리자 아이디/비번 검증 (비번은 sha256 해시로 저장, 타이밍 세이프 비교)"""
    admin = load_config().get("admin", {})
    want_user = str(admin.get("username", ""))
    want_hash = str(admin.get("password_sha256", ""))
    got_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return (
        bool(want_user) and bool(want_hash)
        and hmac.compare_digest(username.strip(), want_user)
        and hmac.compare_digest(got_hash, want_hash)
    )


@asynccontextmanager
async def _lifespan(_app):
    db.init()
    # 서버가 죽은 채로 남은 작업은 중단 처리
    stale = db.running_job()
    if stale:
        db.finish_job(stale["id"], "failed", "서버 재시작으로 중단됨")
    logger.info("서버 준비 완료 → http://localhost:8000")
    # 상시 서버 모드일 때만 워밍업/모니터 스케줄러 백그라운드 기동
    if _SERVE_MODE:
        _start_background_schedulers()
    yield


# ─────────────────────── 페이지 ───────────────────────

@app.get("/signin", response_class=HTMLResponse)
def page_signin(request: Request, error: str = ""):
    # 이미 로그인했으면 대시보드로
    if request.session.get("auth"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("signin.html", {"request": request, "error": error})


@app.post("/signin")
def do_signin(request: Request, username: str = Form(...), password: str = Form(...)):
    if _check_admin(username, password):
        request.session["auth"] = True
        request.session["user"] = username.strip()
        return RedirectResponse("/", status_code=302)
    return RedirectResponse("/signin?error=1", status_code=302)


@app.get("/signout")
def do_signout(request: Request):
    request.session.clear()
    return RedirectResponse("/signin", status_code=302)


@app.get("/", response_class=HTMLResponse)
def page_dashboard(request: Request):
    cfg = load_config()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "stats": db.stats(),
        "slots": cfg["xproxy"]["slots"],
        "provider": cfg.get("xproxy", {}).get("provider", "xproxy"),
        "settings": cfg.get("settings", {}),
        "jobs": db.recent_jobs(10),
        "warming_count": db.stats().get("by_status", {}).get("warming", 0),
        "warming_due": len(db.warming_accounts(due_only=True)),
        "health": db.health_summary(),
    })


@app.get("/accounts", response_class=HTMLResponse)
def page_accounts(request: Request, status: str = "", q: str = "", page: int = 1):
    limit = 100
    rows, total = db.list_accounts(status or None, q or None, limit, (page - 1) * limit)
    return templates.TemplateResponse("accounts.html", {
        "request": request,
        "accounts": rows,
        "total": total,
        "page": page,
        "pages": max(1, (total + limit - 1) // limit),
        "status": status,
        "q": q,
        "stats": db.stats(),
    })


@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request):
    return templates.TemplateResponse("login.html", {
        "request": request,
        "stats": db.stats(),
        "slot_count": slot_count(),
        "running": db.running_job("bulk_login"),
    })


@app.get("/job/{job_id}", response_class=HTMLResponse)
def page_job(request: Request, job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "작업 없음")
    return templates.TemplateResponse("job.html", {"request": request, "job": job})


# ─────────────────────── API: 계정 ───────────────────────

@app.post("/api/accounts/import")
async def api_import(text: str = Form(""), file: UploadFile | None = File(None)):
    payload = text or ""
    if file is not None and file.filename:
        payload += "\n" + (await file.read()).decode("utf-8-sig", errors="replace")

    records = parse_account_text(payload)
    if not records:
        raise HTTPException(
            400,
            "인식된 계정이 없다. '아이디:비번' 또는 '아이디:비번:2FA시드' 형식으로 한 줄에 하나씩.",
        )

    result = db.add_accounts(records, slot_count())
    return {"parsed": len(records), **result, "stats": db.stats()}


@app.post("/api/accounts/totp")
def api_set_totp(username: str = Form(...), seed: str = Form(...)):
    """개별 계정 2FA 시드 등록/수정"""
    if not db.set_totp_seed(username, seed):
        raise HTTPException(400, "유효한 base32 TOTP 시드가 아니다 (16자 이상, A-Z / 2-7)")
    return {"ok": True, **db.preview_totp(username)}


@app.get("/api/accounts/totp/preview")
def api_preview_totp(username: str):
    """
    현재 TOTP 코드 미리보기.
    인증앱에 뜨는 숫자와 같은지 대조하면 시드가 맞는지 즉시 확인된다.
    """
    preview = db.preview_totp(username)
    if preview is None:
        raise HTTPException(404, "등록된 2FA 시드가 없다")
    return preview


@app.post("/api/accounts/delete")
def api_delete(usernames: str = Form(...)):
    names = [u.strip() for u in usernames.split(",") if u.strip()]
    return {"deleted": db.delete_accounts(names), "stats": db.stats()}


@app.post("/api/accounts/reset")
def api_reset(usernames: str = Form(...)):
    """실패/챌린지 계정을 재시도 대상으로 되돌린다"""
    names = [u.strip() for u in usernames.split(",") if u.strip()]
    for n in names:
        db.set_status(n, db.NEW, None)
    return {"reset": len(names), "stats": db.stats()}


@app.get("/api/accounts/export")
def api_export(status: str = ""):
    rows, _ = db.list_accounts(status or None, None, limit=100000)
    lines = ["username,password,status,proxy_slot,last_login,last_error"]
    for r in rows:
        lines.append(
            f"{r['username']},{r['password']},{r['status']},{r['proxy_slot']},"
            f"{r['last_login'] or ''},\"{(r['last_error'] or '').replace(chr(34), '')}\""
        )
    return PlainTextResponse(
        "\n".join(lines),
        headers={"Content-Disposition": f'attachment; filename="accounts_{status or "all"}.csv"'},
    )


# ─────────────────────── API: 계정 진단 ───────────────────────

@app.get("/api/accounts/diagnosis")
def api_diagnosis():
    """전체 계정 건강 진단 — 죽은 계정, 챌린지, 2FA 미해결 한눈에"""
    return db.account_diagnosis()


@app.post("/api/accounts/replace")
def api_replace(
    old: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    totp_seed: str = Form(""),
    backup_codes: str = Form(""),
):
    """죽은 계정을 새 계정으로 교체 (유심 슬롯 유지)"""
    ok = db.replace_account(
        old.strip(), username.strip(), password.strip(),
        totp_seed.strip() or None, backup_codes.strip() or None,
    )
    if not ok:
        raise HTTPException(404, f"교체 대상 계정 '{old}' 없음")
    return {"ok": True, "replaced": old, "new": username, "stats": db.stats()}


@app.get("/diagnosis", response_class=HTMLResponse)
def page_diagnosis(request: Request):
    diag = db.account_diagnosis()
    return templates.TemplateResponse("diagnosis.html", {
        "request": request,
        "diag": diag,
        "stats": db.stats(),
    })

# ─────────────────────── API: 대량 로그인 ───────────────────────

@app.post("/api/login/start")
def api_login_start(scope: str = Form("retryable"), limit: int = Form(0)):
    statuses = {
        "retryable": db.RETRYABLE_STATUSES,
        "new": (db.NEW,),
        "failed": (db.FAILED,),
        "challenge": (db.CHALLENGE,),
        "all": (db.NEW, db.FAILED, db.CHALLENGE, db.TWO_FACTOR, db.READY),
    }.get(scope, db.RETRYABLE_STATUSES)

    accounts = db.accounts_for_login(statuses, limit or None)
    if not accounts:
        raise HTTPException(400, "로그인 대상 계정이 없다")

    job_id = runner.start_login(accounts)
    return {"job_id": job_id, "total": len(accounts)}


@app.post("/api/login/stop")
def api_login_stop():
    return {"stopped": runner.stop()}


@app.get("/api/job/{job_id}")
def api_job(job_id: int, after: int = 0):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "작업 없음")
    return {"job": job, "events": db.job_events(job_id, after), "stats": db.stats()}


@app.get("/api/stats")
def api_stats():
    return db.stats()


# ─────────────────────── API: xProxy ───────────────────────

@app.get("/api/xproxy/health")
def api_xproxy_health():
    """
    슬롯 상태 + IP 누출 점검.

    슬롯의 외부 IP가 실제 IP와 같으면 프록시가 안 걸린 것이다.
    그 상태로 로그인하면 집 IP 하나에 계정 수천 개가 묶인다.
    """
    from xproxy_manager import make_provider
    cfg = load_config()
    xp = cfg.get("xproxy", {})
    try:
        mgr = make_provider(cfg)
        return mgr.preflight()
    except ValueError as e:
        return {
            "safe": False, "error": str(e), "slots": {},
            "online": 0, "total": len(xp.get("slots", [])), "unique_ips": 0,
            "leaking": [], "offline": [], "duplicate": False, "home_ip": "unknown",
        }


@app.get("/api/provider")
def api_provider_get():
    """현재 프로바이더 모드 + ADB 기기 목록"""
    from adb_provider import list_adb_devices
    cfg = load_config()
    return {
        "provider": cfg.get("xproxy", {}).get("provider", "xproxy"),
        "adb_devices": list_adb_devices(),
        "adb": cfg.get("adb", {}),
    }


@app.post("/api/provider")
def api_provider_set(provider: str = Form(...)):
    """프로바이더 모드 전환 (xproxy ↔ adb) — config.json에 저장"""
    if provider not in ("xproxy", "adb"):
        raise HTTPException(400, "provider는 xproxy 또는 adb만 가능")
    cfg = load_config()
    cfg.setdefault("xproxy", {})["provider"] = provider
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return {"ok": True, "provider": provider}


# ─────────────────────── API: 계정 액션 (분석/프로필/업로드/워밍업) ───────────────────────

@app.post("/api/account/{username}/analyze")
def api_account_analyze(username: str):
    """계정 나이/활동 분석 → DB 저장"""
    from account_actions import analyze_account
    cl = open_account_client(username)
    try:
        metrics = analyze_account(cl)
    except Exception as e:
        raise HTTPException(400, f"분석 실패: {e}")
    db.save_metrics(username, metrics)
    return metrics


@app.post("/api/account/{username}/profile")
def api_account_profile(
    username: str,
    full_name: str = Form(None),
    biography: str = Form(None),
    external_url: str = Form(None),
    new_username: str = Form(None),
):
    """프로필 변경 (이름/한줄소개/링크/아이디)"""
    from account_actions import ProfileEditor
    cl = open_account_client(username)
    editor = ProfileEditor(cl)
    result = editor.apply(
        full_name=full_name or None,
        biography=biography or None,
        external_url=external_url or None,
        username=new_username or None,
    )
    # 아이디를 바꿨으면 DB의 username도 갱신
    if new_username and result.get("username") == "ok":
        db.replace_account(username, new_username.strip().lstrip("@"),
                           db.get_account(username)["password"])
    return {"result": result}


@app.post("/api/account/{username}/profile-pic")
async def api_account_profile_pic(username: str, file: UploadFile = File(...)):
    """프로필 사진 변경"""
    from account_actions import ProfileEditor
    tmp = await _save_upload(file)
    try:
        cl = open_account_client(username)
        ProfileEditor(cl).change_profile_pic(str(tmp))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, f"프사 변경 실패: {e}")
    finally:
        tmp.unlink(missing_ok=True)


@app.post("/api/account/{username}/upload")
async def api_account_upload(
    username: str,
    kind: str = Form(...),
    caption: str = Form(""),
    files: list[UploadFile] = File(...),
):
    """
    게시물 업로드.
      kind: photo(사진1장) / album(사진여러장) / reel(릴스) / mixed(사진+릴스)
    """
    from account_actions import PostUploader
    if kind not in ("photo", "album", "reel", "mixed"):
        raise HTTPException(400, "kind는 photo/album/reel/mixed")

    saved = [await _save_upload(f) for f in files]
    try:
        cl = open_account_client(username)
        acc = db.get_account(username)
        uploader = PostUploader(cl, device_model=acc.get("device_model"))
        media = uploader.upload(kind, [str(p) for p in saved], caption)
        return {"ok": True, "code": getattr(media, "code", ""),
                "pk": str(getattr(media, "pk", ""))}
    except Exception as e:
        raise HTTPException(400, f"업로드 실패: {e}")
    finally:
        for p in saved:
            p.unlink(missing_ok=True)


async def _save_upload(file: UploadFile):
    """업로드 파일을 임시 저장하고 경로 반환"""
    import tempfile
    suffix = Path(file.filename or "up").suffix or ".bin"
    fd, path = tempfile.mkstemp(suffix=suffix)
    with open(fd, "wb") as f:
        f.write(await file.read())
    return Path(path)


@app.get("/account/{username}", response_class=HTMLResponse)
def page_account(request: Request, username: str):
    acc = db.get_account(username)
    if not acc:
        raise HTTPException(404, "계정 없음")
    return templates.TemplateResponse("account.html", {
        "request": request, "acc": acc, "stats": db.stats(),
    })


# ─────────────────────── API: 워밍업 ───────────────────────

class WarmupRunner:
    """워밍업 배치를 백그라운드로 돌린다 (동시 1개)"""

    def __init__(self):
        self.lock = threading.Lock()
        self.engine = None
        self.thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> int:
        with self.lock:
            if self.busy:
                raise HTTPException(409, "워밍업이 이미 실행 중이다")
            due = db.warming_accounts(due_only=True)
            if not due:
                raise HTTPException(400, "오늘 워밍업할 계정이 없다")

            job_id = db.create_job("warmup", total=len(due))
            from warmup_engine import WarmupEngine
            engine = WarmupEngine(CONFIG_PATH)
            self.engine = engine

            def work():
                try:
                    c = engine.run(job_id=job_id)
                    db.finish_job(job_id, "done",
                                  f"진행 {c['warmed']} / 포스팅 {c['posted']} / "
                                  f"졸업 {c['graduated']} / 실패 {c['failed']}")
                except Exception as e:
                    logger.exception("워밍업 실패")
                    db.log_event(job_id, "error", f"작업 중단: {e}")
                    db.finish_job(job_id, "failed", str(e))

            self.thread = threading.Thread(target=work, daemon=True, name="warmup")
            self.thread.start()
            return job_id

    def stop(self) -> bool:
        if self.engine and self.busy:
            self.engine.stop()
            return True
        return False


warmup_runner = WarmupRunner()


@app.post("/api/warmup/start")
def api_warmup_start():
    """오늘치 워밍업 배치 시작 (워밍업 중인 계정 전체)"""
    return {"job_id": warmup_runner.start()}


@app.post("/api/warmup/stop")
def api_warmup_stop():
    return {"stopped": warmup_runner.stop()}


@app.get("/api/warmup/status")
def api_warmup_status():
    """워밍업 현황"""
    warming = db.warming_accounts(due_only=False)
    due = db.warming_accounts(due_only=True)
    return {
        "warming_total": len(warming),
        "due_today": len(due),
        "running": warmup_runner.busy,
        "accounts": [
            {"username": a["username"], "day": a["warmup_day"],
             "age_class": a.get("age_class"), "total_posts": a.get("total_posts") or 0}
            for a in warming
        ],
    }


# ─────────────────────── API: 헬스 모니터 ───────────────────────

class MonitorRunner:
    """헬스체크를 백그라운드로 (동시 1개)"""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> int:
        with self.lock:
            if self.busy:
                raise HTTPException(409, "헬스체크가 이미 실행 중이다")
            targets = db.monitorable_accounts()
            if not targets:
                raise HTTPException(400, "점검할 계정이 없다 (세션 있는 계정 없음)")
            job_id = db.create_job("health_check", total=len(targets))

            def work():
                try:
                    from account_monitor import run_health_check
                    r = run_health_check(job_id=job_id)
                    db.finish_job(job_id, "done",
                                  f"정상 {r['alive']} / 대응필요 {len(r.get('dead_list', []))}")
                except Exception as e:
                    logger.exception("헬스체크 실패")
                    db.finish_job(job_id, "failed", str(e))

            self.thread = threading.Thread(target=work, daemon=True, name="monitor")
            self.thread.start()
            return job_id


monitor_runner = MonitorRunner()


@app.post("/api/monitor/run")
def api_monitor_run():
    """지금 즉시 전체 계정 헬스체크"""
    return {"job_id": monitor_runner.start()}


@app.get("/api/monitor/status")
def api_monitor_status():
    """헬스 현황 요약 (대시보드 실시간)"""
    summary = db.health_summary()
    summary["running"] = monitor_runner.busy
    return summary


# ─────────────────────── API: 실시간 활동 ───────────────────────

@app.get("/api/activity/recent")
def api_activity_recent(since: int = 0):
    """since 이후의 활동 이벤트 (실시간 폴링용)"""
    import activity
    return {"events": activity.recent(since), "last_id": activity.last_id()}


@app.get("/api/activity/current")
def api_activity_current():
    """계정별 현재 행동 (지금 뭐 하는지)"""
    import activity
    return {"current": activity.current()}


# ─────────────────────── 백그라운드 자동화 (서버가 곧 상시 서비스) ───────────────────────

def _start_background_schedulers():
    """
    서버 시작과 함께 워밍업/모니터 스케줄러를 백그라운드 스레드로 띄운다.
    → START_SERVER.bat 하나만 켜두면 워밍업+모니터+대시보드가 다 돈다.
    config의 auto.enabled=false면 끈다.
    """
    cfg = load_config()
    auto = cfg.get("auto", {})
    if not auto.get("enabled", True):
        logger.info("백그라운드 자동화 비활성 (config auto.enabled=false)")
        return

    warmup_hours = auto.get("warmup_window", [9, 22])
    monitor_interval = float(auto.get("monitor_interval_hours", 3))

    def warmup_loop():
        import warmup_scheduler as ws
        import sys as _sys
        _sys.argv = ["warmup_scheduler.py", str(warmup_hours[0]), str(warmup_hours[1])]
        try:
            ws.main()
        except Exception:
            logger.exception("워밍업 스케줄러 스레드 종료")

    def monitor_loop():
        import monitor_scheduler as ms
        import sys as _sys
        _sys.argv = ["monitor_scheduler.py", str(monitor_interval)]
        try:
            ms.main()
        except Exception:
            logger.exception("모니터 스케줄러 스레드 종료")

    threading.Thread(target=warmup_loop, daemon=True, name="warmup-sched").start()
    threading.Thread(target=monitor_loop, daemon=True, name="monitor-sched").start()
    logger.info(
        f"백그라운드 자동화 시작 — 워밍업 매일 {warmup_hours[0]}~{warmup_hours[1]}시 랜덤 / "
        f"모니터 {monitor_interval}시간마다")


if __name__ == "__main__":
    import uvicorn
    _SERVE_MODE = True
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")