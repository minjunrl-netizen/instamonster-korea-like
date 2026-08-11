"""
인스타몬스터 관리 서버 (로컬 웹)

실행:  python server.py
접속:  http://localhost:8000

계정 4,000개를 CLI로 관리하는 건 불가능하다.
등록 → 대량 로그인 → 상태 확인 → 실전 발사를 브라우저에서 처리한다.
"""

import json
import logging
import threading
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

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

app = FastAPI(title="인스타몬스터 관리", lifespan=lambda _app: _lifespan(_app))
templates = Jinja2Templates(directory=str(BASE / "templates"))

(BASE / "static").mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


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


@asynccontextmanager
async def _lifespan(_app):
    db.init()
    # 서버가 죽은 채로 남은 작업은 중단 처리
    stale = db.running_job()
    if stale:
        db.finish_job(stale["id"], "failed", "서버 재시작으로 중단됨")
    logger.info("서버 준비 완료 → http://localhost:8000")
    yield


# ─────────────────────── 페이지 ───────────────────────

@app.get("/", response_class=HTMLResponse)
def page_dashboard(request: Request):
    cfg = load_config()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "stats": db.stats(),
        "slots": cfg["xproxy"]["slots"],
        "settings": cfg.get("settings", {}),
        "jobs": db.recent_jobs(10),
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
    from xproxy_manager import XProxyManager
    xp = load_config()["xproxy"]
    try:
        mgr = XProxyManager(
            host=xp["host"], api_port=xp["api_port"],
            proxy_type=xp.get("proxy_type", "socks5"), slots=xp["slots"],
            api_pattern=xp.get("api_pattern"),
            username=xp.get("username"), password=xp.get("password"),
        )
        return mgr.preflight()
    except ValueError as e:
        return {
            "safe": False, "error": str(e), "slots": {},
            "online": 0, "total": len(xp.get("slots", [])), "unique_ips": 0,
            "leaking": [], "offline": [], "duplicate": False, "home_ip": "unknown",
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
