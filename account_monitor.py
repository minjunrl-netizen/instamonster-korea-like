"""
계정 헬스 모니터

세션이 살아있는 계정들을 주기적으로 점검해서 죽은 계정을 빠르게 잡아낸다.
가벼운 인증 요청(타임라인 피드) 1번으로 세션 생사를 판정한다.

판정 결과:
  alive          - 세션 정상 (계속 사용 가능)
  session_expired - 세션 만료 (재로그인 필요) → status를 'new'로 되돌림
  challenge      - 챌린지 발생 → status 'challenge'
  banned         - 밴/스팸 감지 → status 'banned'
  rate_limit     - 레이트리밋 (일시적, 상태 유지)
  error          - 기타 오류 (네트워크 등, 상태 유지)

3시간마다 자동 실행하면 계정이 죽는 즉시 파악되어 빠른 대응이 가능하다.
"""

import json
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from instagrapi.exceptions import (
    LoginRequired, ClientLoginRequired, ChallengeRequired,
    FeedbackRequired, PleaseWaitFewMinutes, ClientThrottledError,
    UserNotFound,
)

import db
from bulk_login import make_client
from xproxy_manager import make_provider

logger = logging.getLogger(__name__)

# 헬스 판정 → DB 상태 전환 매핑 (None이면 상태 유지)
HEALTH_TO_STATUS = {
    "alive": None,               # 정상 → 상태 유지
    "session_expired": db.NEW,   # 세션 만료 → 재로그인 대상으로
    "challenge": db.CHALLENGE,
    "banned": db.BANNED,
    "not_exist": db.NOT_EXIST,
    "rate_limit": None,          # 일시적 → 유지
    "error": None,               # 네트워크 등 → 유지
}


class AccountMonitor:
    """계정 세션 생사 점검"""

    def __init__(self, config_path: str = "config.json"):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)
        self.provider = make_provider(self.config)

    def check_one(self, acc: dict) -> tuple[str, str]:
        """
        계정 1개의 세션 생사 판정. (health, detail) 반환.
        가벼운 인증 요청 1번으로 판단한다.
        """
        username = acc["username"]
        slot = acc.get("proxy_slot") or 0
        session_file = acc.get("session_file")

        if not session_file or not Path(session_file).exists():
            return "session_expired", "세션 파일 없음"

        cl = make_client(request_timeout=25)
        try:
            cl.load_settings(session_file)
        except Exception as e:
            return "session_expired", f"세션 로드 실패: {e}"

        if not getattr(self.provider, "is_direct", False):
            try:
                cl.set_proxy(self.provider.get_proxy_url(slot))
            except Exception as e:
                return "error", f"프록시 설정 실패: {e}"

        # 가벼운 인증 요청 — 세션이 살아있으면 통과
        try:
            cl.get_timeline_feed()
            return "alive", ""
        except (LoginRequired, ClientLoginRequired):
            return "session_expired", "세션 만료 (login_required)"
        except ChallengeRequired:
            return "challenge", "챌린지 발생"
        except FeedbackRequired:
            return "banned", "스팸/밴 감지 (feedback_required)"
        except UserNotFound:
            return "not_exist", "계정 삭제/비활성화"
        except (PleaseWaitFewMinutes, ClientThrottledError):
            return "rate_limit", "레이트리밋 (일시적)"
        except Exception as e:
            msg = str(e).lower()
            if "challenge" in msg:
                return "challenge", f"챌린지: {str(e)[:60]}"
            if "login_required" in msg or "login required" in msg:
                return "session_expired", "세션 만료"
            return "error", f"{type(e).__name__}: {str(e)[:60]}"

    def check_all(self, job_id: int | None = None) -> dict:
        """
        모니터 대상 전체 점검. 슬롯별로 순차(같은 슬롯 IP 공유)지만
        서로 다른 슬롯은 병렬로 돌려 빠르게 끝낸다.
        """
        accounts = db.monitorable_accounts()
        summary = {"alive": 0, "session_expired": 0, "challenge": 0,
                   "banned": 0, "not_exist": 0, "rate_limit": 0, "error": 0}

        if not accounts:
            logger.info("모니터할 계정 없음 (세션 있는 ready/warming 계정 없음)")
            return summary

        logger.info(f"헬스체크 시작 — 대상 {len(accounts)}개 계정")

        # 슬롯별로 그룹 (같은 슬롯은 IP 공유라 순차)
        by_slot: dict[int, list] = {}
        for a in accounts:
            by_slot.setdefault(a.get("proxy_slot") or 0, []).append(a)

        dead = []  # 죽은 계정 (빠른 대응용 알림)

        def check_slot(slot_accounts):
            for acc in slot_accounts:
                import activity
                activity.emit(acc["username"], "생사 점검 중", "", "monitor")
                health, detail = self.check_one(acc)
                new_status = HEALTH_TO_STATUS.get(health)
                db.record_health(acc["username"], health, new_status)
                summary[health] = summary.get(health, 0) + 1
                hlabel = {"alive": "정상 ✅"}.get(health, f"{health} ⚠️")
                activity.emit(acc["username"], "점검 완료", hlabel,
                              "done" if health == "alive" else "error")
                if health == "alive":
                    logger.debug(f"  ✅ {acc['username']}: 정상")
                else:
                    mark = {"session_expired": "🔄", "challenge": "🔐",
                            "banned": "⛔", "not_exist": "💀",
                            "rate_limit": "⏳", "error": "⚠️"}.get(health, "❓")
                    logger.info(f"  {mark} {acc['username']}: {health} - {detail}")
                    if health in ("banned", "not_exist", "challenge", "session_expired"):
                        dead.append((acc["username"], health, detail))
                    if job_id:
                        db.log_event(job_id, "warn",
                                     f"{acc['username']}: {health} - {detail}")

        with ThreadPoolExecutor(max_workers=max(1, len(by_slot))) as ex:
            list(ex.map(check_slot, by_slot.values()))

        # 요약
        alive = summary["alive"]
        total = len(accounts)
        logger.info(
            f"헬스체크 완료 — 정상 {alive}/{total} | "
            f"세션만료 {summary['session_expired']} / 챌린지 {summary['challenge']} / "
            f"밴 {summary['banned']} / 삭제 {summary['not_exist']} / "
            f"레이트리밋 {summary['rate_limit']} / 오류 {summary['error']}")

        if dead:
            logger.warning(f"🚨 대응 필요 계정 {len(dead)}개:")
            for u, h, d in dead:
                logger.warning(f"   - {u}: {h} ({d})")

        summary["dead_list"] = dead
        return summary


def run_health_check(config_path: str = "config.json", job_id: int | None = None) -> dict:
    """헬스체크 1회 실행 (스케줄러/웹에서 호출)"""
    db.init()
    monitor = AccountMonitor(config_path)
    return monitor.check_all(job_id=job_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%m-%d %H:%M:%S")
    result = run_health_check()
    print(f"\n결과: 정상 {result['alive']} / 대응필요 {len(result.get('dead_list', []))}")
