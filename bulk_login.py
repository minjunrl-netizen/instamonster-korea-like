"""
대량 로그인 엔진

계정 수천 개를 한 IP에서 연속 로그인하면 즉시 차단된다.
유심 슬롯마다 워커를 띄우고, 계정에 고정된 슬롯으로만 로그인시킨다.

계정-슬롯 고정이 핵심:
  인스타는 계정-디바이스-IP 대역의 일관성을 본다.
  오늘 SKT, 내일 LGU+로 접속하는 계정은 그 자체로 이상 신호다.
  등록 시점에 슬롯을 배정하고, 로그인도 실전 발사도 항상 같은 슬롯을 쓴다.

로그인 결과 분류:
  ready      - 성공, 세션 저장됨
  challenge  - 인증 요구 (수동 해결 필요)
  2fa        - 2단계 인증 코드 필요
  bad_pw     - 비밀번호 틀림 (재시도 무의미)
  banned     - 밴/스팸 감지
  failed     - 일시적 실패 (재시도 가능)
"""

import json
import time
import random
import logging
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from instagrapi import Client
from instagrapi.exceptions import (
    BadPassword,
    ChallengeRequired,
    TwoFactorRequired,
    PleaseWaitFewMinutes,
    ClientThrottledError,
    FeedbackRequired,
    ProxyAddressIsBlocked,
    ReloginAttemptExceeded,
    SelectContactPointRecoveryForm,
    RecaptchaChallengeForm,
    UserNotFound,
)
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

import db
from xproxy_manager import XProxyManager, make_provider
from human_behavior import setup_account_like_real_device
from devices import pick_device


def make_client(request_timeout: int = 15) -> Client:
    """
    로그인용 클라이언트 생성 — 429 재시도 폭탄 방지.

    instagrapi 기본값은 session_retry_statuses에 429가 들어있어서,
    레이트리밋을 받으면 같은 엔드포인트를 3번 더 때린다. 이 재시도 폭탄이
    통신사 대역 전체를 차단시키는 주범이다.

    429는 재시도하지 않고 instagrapi 내부의 "Ignore 429: Continue login"에
    맡긴다. 5xx만 짧게 재시도한다.
    """
    cl = Client()
    cl.request_timeout = request_timeout
    cl.delay_range = [2, 5]

    retry = Retry(
        total=1,
        connect=1,
        read=1,
        status_forcelist=[500, 502, 503, 504],  # 429 제외
        backoff_factor=1,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    cl.private.mount("https://", adapter)
    cl.private.mount("http://", adapter)
    cl.public.mount("https://", adapter)
    cl.public.mount("http://", adapter)
    return cl

logger = logging.getLogger(__name__)


class BulkLogin:
    """유심 슬롯별 병렬 로그인"""

    def __init__(self, config_path: str = "config.json"):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self.xproxy = make_provider(self.config)

        s = self.config.get("settings", {})
        self.sessions_dir = Path(s.get("sessions_dir", "sessions"))
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        login_cfg = self.config.get("login", {})
        # 로그인은 좋아요보다 훨씬 민감하다. 간격을 넉넉히 잡는다.
        self.gap_min = float(login_cfg.get("delay_min", 20))
        self.gap_max = float(login_cfg.get("delay_max", 45))
        self.rotate_every = int(login_cfg.get("rotate_ip_every_n", 1))
        self.rotate_wait = int(login_cfg.get("ip_rotate_wait_seconds", 12))
        self.request_timeout = int(login_cfg.get("request_timeout", 15))
        # 429를 받으면 이 슬롯을 잠시 쉬게 한다 (대역 과열 방지)
        self.cooldown_on_429 = float(login_cfg.get("cooldown_on_429_seconds", 90))
        self.warmup_after_login = bool(login_cfg.get("warmup_after_login", True))

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.job_id: int | None = None
        self.counts = {"ready": 0, "challenge": 0, "2fa": 0,
                       "bad_pw": 0, "not_exist": 0, "banned": 0,
                       "rate_limit": 0, "failed": 0}

    # ─── 제어 ───

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # ─── 로그인 1건 ───

    @staticmethod
    def _proxy_leak_check(cl: Client, expected: str) -> str:
        """
        클라이언트의 모든 전송 세션이 지정한 프록시를 쓰는지 확인.
        문제가 있으면 사유 문자열, 정상이면 빈 문자열.

        반드시 fail-closed다. 확인할 수 없으면 통과가 아니라 차단이다.
        private/public 중 하나라도 검증에 실패하면 패킷을 보내지 않는다.
        """
        if not expected:
            return "프록시 URL이 비어있음"

        def verify(session, name: str) -> str:
            proxies = getattr(session, "proxies", None)
            if not proxies:
                return f"{name} 세션에 프록시 없음"
            for scheme in ("http", "https"):
                if expected not in str(proxies.get(scheme, "")):
                    return f"{name}.{scheme} 프록시 불일치({proxies.get(scheme)!r})"
            return ""

        # private/public은 반드시 존재해야 하는 전송 경로다
        for name in ("private", "public"):
            session = getattr(cl, name, None)
            if session is None:
                return f"{name} 세션이 없어 프록시 검증 불가"
            reason = verify(session, name)
            if reason:
                return reason

        # graphql은 버전에 따라 없을 수 있으므로 있을 때만 검사
        graphql = getattr(cl, "graphql", None)
        if graphql is not None:
            reason = verify(graphql, "graphql")
            if reason:
                return reason

        return ""

    def _login_one(self, acc: dict, slot: int) -> tuple[str, str]:
        """
        계정 1개 로그인. (status, message) 반환.

        세션 파일이 있으면 먼저 복원해서 재로그인 비용을 줄인다.

        디바이스 정체성:
          첫 로그인 → 계정에 배정된 랜덤 기종(갤럭시 S24, A54 등)을 set_device로 적용
          재로그인 → load_settings가 세션에 저장된 기종을 그대로 복원 (set_device 안 함)
          → 한번 정해진 "가상 폰"은 절대 바뀌지 않는다

        2FA가 걸리면 저장된 TOTP 시드로 코드를 직접 만들어 재시도한다.
        시드가 없으면 백업코드를 1개 소모한다. 둘 다 없을 때만 수동 대상으로 남긴다.
        """
        username = acc["username"]
        session_path = self.sessions_dir / f"{username}.json"
        proxy_url = self.xproxy.get_proxy_url(slot)

        cl = make_client(request_timeout=self.request_timeout)

        has_session = session_path.exists()
        if has_session:
            try:
                cl.load_settings(str(session_path))
            except Exception:
                session_path.unlink(missing_ok=True)
                has_session = False

        if not has_session:
            # 첫 로그인 — 배정된 기종으로 디바이스 정체성을 만든다
            device = pick_device(username)
            cl.set_device(device)

        if getattr(self.xproxy, "is_direct", False):
            # ADB 테더링 모드 — 프록시 없이 기본 라우트(테더링)로 나간다.
            # 프록시가 없으므로 누출 검사 대신 디바이스/로케일만 세팅한다.
            # 테더링이 집 IP가 아닌지는 run()의 preflight가 이미 확인했다.
            cl.set_country("KR")
            cl.set_country_code(82)
            cl.set_locale("ko_KR")
            cl.set_timezone_offset(9 * 3600)
            cl.delay_range = [2, 5]
        else:
            setup_account_like_real_device(cl, proxy_url)

            # 첫 패킷이 나가기 전 마지막 방어선.
            # instagrapi의 set_proxy는 falsy 값을 받으면 프록시를 '해제'하므로,
            # 세 전송 경로(private/public/graphql)가 전부 프록시를 물었는지 직접 확인한다.
            leak = self._proxy_leak_check(cl, proxy_url)
            if leak:
                return db.FAILED, f"프록시 미적용 - 접속 차단: {leak}"

        try:
            return self._attempt(cl, acc, session_path, "")
        except TwoFactorRequired:
            return self._solve_two_factor(cl, acc, session_path)
        except Exception as e:
            return self._classify(e)

    def _attempt(self, cl: Client, acc: dict, session_path: Path,
                 verification_code: str) -> tuple[str, str]:
        """로그인 시도 1회. 2FA 예외는 호출자가 처리하도록 그대로 올린다."""
        ok = cl.login(acc["username"], acc["password"],
                      verification_code=verification_code)
        if not ok:
            return db.FAILED, "login()이 False 반환"

        # 세션이 실제로 살아있는지 확인 — 로그인만 통과하고 죽은 세션이 흔하다
        cl.get_timeline_feed("cold_start_fetch")

        session_path.parent.mkdir(parents=True, exist_ok=True)
        cl.dump_settings(str(session_path))
        return db.READY, ""

    def _solve_two_factor(self, cl: Client, acc: dict,
                          session_path: Path) -> tuple[str, str]:
        """
        2FA 자동 해제.

        1순위 TOTP 시드 — 무한 재사용 가능
        2순위 백업코드 — 1회용이라 꺼내 쓰면 목록에서 사라진다

        TOTP는 30초마다 바뀐다. 창 경계에서 코드가 만료될 수 있으므로
        거부당하면 다음 창까지 기다렸다가 한 번 더 시도한다.
        """
        seed = acc.get("totp_seed")

        if seed:
            for attempt in range(2):
                try:
                    code = db.totp_code(seed)
                except Exception as e:
                    return db.TWO_FACTOR, f"TOTP 시드 오류: {e}"

                try:
                    status, msg = self._attempt(cl, acc, session_path, code)
                    return (db.READY, "TOTP 자동 인증") if status == db.READY else (status, msg)
                except TwoFactorRequired:
                    if attempt == 0:
                        self._wait_next_totp_window()
                        continue
                    return db.TWO_FACTOR, "TOTP 코드 거부됨 - 시드 확인 필요"
                except Exception as e:
                    return self._classify(e)

        code = db.consume_backup_code(acc["username"])
        if code:
            try:
                status, msg = self._attempt(cl, acc, session_path, code)
                return (db.READY, "백업코드 인증 (1개 소모)") if status == db.READY else (status, msg)
            except TwoFactorRequired:
                return db.TWO_FACTOR, "백업코드 거부됨"
            except Exception as e:
                return self._classify(e)

        return db.TWO_FACTOR, "2단계 인증 - TOTP 시드나 백업코드 필요"

    @staticmethod
    def _wait_next_totp_window() -> None:
        """다음 30초 TOTP 창이 열릴 때까지 대기"""
        time.sleep(30 - (time.time() % 30) + 1)

    @staticmethod
    def _classify(e: Exception) -> tuple[str, str]:
        """
        로그인 예외를 계정 상태로 분류.

        핵심: "계정을 찾을 수 없습니다"는 UnknownError로 올라오지만
        메시지에 "찾을 수 없" 또는 "find" 가 있으면 계정 삭제/비활성화다.
        이걸 failed로 넣으면 영원히 재시도하게 되므로 not_exist로 분리한다.
        """
        msg = str(e)

        if isinstance(e, TwoFactorRequired):
            return db.TWO_FACTOR, "2단계 인증 코드 필요"
        if isinstance(e, (ChallengeRequired, SelectContactPointRecoveryForm,
                          RecaptchaChallengeForm)):
            return db.CHALLENGE, f"인증 챌린지: {type(e).__name__}"
        if isinstance(e, BadPassword):
            return db.BAD_PASSWORD, "비밀번호 불일치"
        if isinstance(e, UserNotFound):
            return db.NOT_EXIST, "존재하지 않는 계정"
        if isinstance(e, FeedbackRequired):
            return db.BANNED, f"스팸 감지: {msg}"
        if isinstance(e, ProxyAddressIsBlocked):
            return db.FAILED, "프록시 IP 차단됨 - IP 로테이션 필요"
        if isinstance(e, (PleaseWaitFewMinutes, ClientThrottledError)):
            return db.RATE_LIMITED, f"레이트리밋: {msg}"
        if isinstance(e, ReloginAttemptExceeded):
            return db.RATE_LIMITED, "재로그인 시도 초과 - 나중에 재시도"

        # "계정을 찾을 수 없습니다" / "find your account" 패턴
        if "찾을 수 없" in msg or "find your account" in msg.lower() or "find" in msg.lower() and "account" in msg.lower():
            return db.NOT_EXIST, f"계정 삭제/비활성화: {msg[:80]}"

        return db.FAILED, f"{type(e).__name__}: {msg[:120]}"

    # ─── 슬롯 워커 ───

    def _slot_worker(self, slot: int, accounts: list[dict]) -> None:
        name = self.xproxy.slot_name(slot)
        done = 0

        for acc in accounts:
            if self._stop.is_set():
                self._log("warn", f"[{name}] 중단 요청 - 남은 {len(accounts)-done}건 스킵")
                return

            # 로그인마다 IP를 간다. 같은 IP에서 연속 로그인이 가장 위험하다.
            if done % self.rotate_every == 0:
                self.xproxy.rotate_ip(slot, wait_seconds=self.rotate_wait)

            status, message = self._login_one(acc, slot)
            session_file = (
                str(self.sessions_dir / f"{acc['username']}.json")
                if status == db.READY else None
            )
            db.mark_login_result(acc["username"], status, session_file, message or None)

            with self._lock:
                self.counts[status] = self.counts.get(status, 0) + 1
                total_done = sum(self.counts.values())

            icon = {"ready": "✅", "challenge": "🔐", "2fa": "🔑",
                    "bad_pw": "❌", "banned": "⛔", "failed": "⚠️"}.get(status, "•")
            self._log(
                "info" if status == db.READY else "warn",
                f"{icon} [{name}] {acc['username']} → {status}" + (f" ({message})" if message else ""),
            )

            if self.job_id:
                db.update_job(
                    self.job_id,
                    processed=total_done,
                    succeeded=self.counts["ready"],
                    failed=total_done - self.counts["ready"],
                )

            done += 1
            if done < len(accounts):
                self._sleep(self.gap_min, self.gap_max)

    def _sleep(self, lo: float, hi: float) -> None:
        self._stop.wait(random.uniform(lo, hi))

    def _log(self, level: str, message: str) -> None:
        logger.info(message)
        if self.job_id:
            db.log_event(self.job_id, level, message)

    # ─── 실행 ───

    def run(self, accounts: list[dict], job_id: int | None = None,
            skip_preflight: bool = False) -> dict:
        """
        계정 목록을 슬롯별로 나눠 병렬 로그인.
        각 계정은 등록 시 배정된 proxy_slot으로만 로그인한다.

        시작 전 반드시 프록시 누출 점검을 통과해야 한다.
        집 IP로 수천 개를 로그인하면 계정도 IP도 한 번에 날아간다.
        """
        self.job_id = job_id
        self._stop.clear()
        self.counts = {"ready": 0, "challenge": 0, "2fa": 0,
                       "bad_pw": 0, "not_exist": 0, "banned": 0,
                       "rate_limit": 0, "failed": 0}

        slot_count = len(self.xproxy.slots)
        if slot_count == 0:
            raise RuntimeError("config.json에 xproxy 슬롯이 없다")

        if not skip_preflight:
            self._log("info", "프록시 안전 점검 중...")
            pf = self.xproxy.preflight()
            if not pf["safe"]:
                reasons = []
                if pf["leaking"]:
                    reasons.append(f"실제 IP 누출: {', '.join(pf['leaking'])}")
                if pf["offline"]:
                    reasons.append(f"오프라인 슬롯: {', '.join(pf['offline'])}")
                if pf["duplicate"]:
                    reasons.append("슬롯 간 IP 중복")
                if not pf["online"]:
                    reasons.append("사용 가능한 슬롯 없음")
                msg = " / ".join(reasons)
                self._log("error", f"🚨 안전 점검 실패 - 로그인 중단: {msg}")
                raise RuntimeError(f"프록시 안전 점검 실패: {msg}")
            self._log(
                "info",
                f"✅ 안전 점검 통과 — 슬롯 {pf['online']}/{pf['total']}개 온라인, "
                f"고유 IP {pf['unique_ips']}개 (실제 IP {pf['home_ip']} 와 전부 다름)",
            )

        buckets: dict[int, list[dict]] = {i: [] for i in range(slot_count)}
        for acc in accounts:
            slot = acc.get("proxy_slot")
            if slot is None or not (0 <= slot < slot_count):
                slot = hash(acc["username"]) % slot_count
            buckets[slot].append(acc)

        started = time.time()
        self._log(
            "info",
            f"대량 로그인 시작 — 계정 {len(accounts):,}개 / 유심 {slot_count}개 동시 / "
            f"슬롯당 최대 {max(len(v) for v in buckets.values()):,}개",
        )

        with ThreadPoolExecutor(max_workers=slot_count, thread_name_prefix="login") as ex:
            futures = [
                ex.submit(self._slot_worker, slot, accs)
                for slot, accs in buckets.items() if accs
            ]
            for f in futures:
                try:
                    f.result()
                except Exception as e:
                    self._log("error", f"워커 예외: {e}")

        elapsed = time.time() - started
        result = {
            **self.counts,
            "total": len(accounts),
            "elapsed_sec": elapsed,
            "stopped": self._stop.is_set(),
        }

        self._log(
            "info",
            f"완료 — 성공 {self.counts['ready']:,} / 챌린지 {self.counts['challenge']:,} / "
            f"2FA {self.counts['2fa']:,} / 비번오류 {self.counts['bad_pw']:,} / "
            f"밴 {self.counts['banned']:,} / 실패 {self.counts['failed']:,} "
            f"({elapsed/60:.1f}분)",
        )
        return result


HEADER_WORDS = {"username", "user", "id", "아이디", "계정", "login"}


def parse_account_text(text: str) -> list[dict]:
    """
    붙여넣기 텍스트에서 계정 정보 추출.

    지원 형식 (한 줄에 하나):
        아이디:비번
        아이디:비번:2FA시드
        아이디:비번:2FA시드:백업코드1 백업코드2 ...

    지원 구분자: 콜론, 탭, 쉼표, 세미콜론, 파이프, 공백

    3번째 칸은 TOTP 시드(base32)로 본다. 형식이 안 맞으면 백업코드로 취급한다.
    """
    records: list[dict] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = None
        for sep in (":", "\t", ";", "|", ","):
            if sep in line:
                parts = [p.strip() for p in line.split(sep)]
                break
        if parts is None:
            parts = line.split()

        parts = [p for p in parts if p]
        if len(parts) < 2:
            continue

        username = parts[0].lstrip("@")
        if username.lower() in HEADER_WORDS:
            continue  # 헤더 행

        rec = {"username": username, "password": parts[1]}

        if len(parts) >= 3:
            third = parts[2]
            if db.normalize_totp_seed(third):
                rec["totp_seed"] = third
                if len(parts) >= 4:
                    rec["backup_codes"] = " ".join(parts[3:])
            else:
                # base32 시드가 아니면 백업코드 묶음으로 본다
                rec["backup_codes"] = " ".join(parts[2:])

        records.append(rec)

    return records
