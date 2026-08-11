"""
인스타그램 계정 로그인 테스트 — 테더링 + 수동 비행기모드 토글

xProxy 장비 없이 휴대폰 테더링으로 IP를 바꿔가며 계정별 로그인을 테스트한다.
계정마다 IP를 바꾸고 → 로그인 → 세션 검증 → 결과 기록.

사용법:
    python login_test.py
"""

import json
import time
import logging
import sys
from pathlib import Path
from datetime import datetime

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
    ClientError,
)

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)

# ─── 테스트 대상 계정 ───
#
# 실제 계정은 login_accounts.txt 에서 읽는다 (gitignore로 커밋 제외).
# 형식: 한 줄에 "아이디:비번" 또는 "아이디 비번"

def load_test_accounts(path: str = "login_accounts.txt") -> list[tuple[str, str]]:
    from pathlib import Path as _P
    f = _P(path)
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in (":", "\t", ",", "|", " "):
            if sep in line:
                parts = [p.strip() for p in line.split(sep) if p.strip()]
                break
        else:
            parts = [line]
        if len(parts) >= 2:
            out.append((parts[0].lstrip("@"), parts[1]))
    return out


ACCOUNTS = load_test_accounts()


def get_external_ip() -> str:
    """현재 외부 IP 확인"""
    for url in [
        "https://api.ipify.org?format=json",
        "https://httpbin.org/ip",
        "https://api.ip.sb/ip",
    ]:
        try:
            r = requests.get(url, timeout=10)
            if "json" in url:
                return r.json().get("ip", r.json().get("origin", "unknown"))
            return r.text.strip()
        except Exception:
            continue
    return "확인 실패"


def wait_for_ip_change(prev_ip: str) -> str:
    """
    IP가 바뀔 때까지 기다린다.

    사용자에게 비행기모드를 토글하라고 안내하고,
    IP가 실제로 바뀌었는지 확인한 뒤 새 IP를 반환한다.
    """
    print()
    print("  ──────────────────────────────────────────────")
    print(f"  현재 IP: {prev_ip}")
    print("  📱 비행기모드를 껐다 켜주세요 (IP 변경)")
    print("  ──────────────────────────────────────────────")

    while True:
        input("  [Enter] 비행기모드 토글 완료 후 눌러주세요 → ")
        new_ip = get_external_ip()

        if new_ip == "확인 실패":
            print("  ⚠️  인터넷 연결 안 됨 — 테더링 확인 후 다시 Enter")
            continue

        if new_ip == prev_ip:
            print(f"  ⚠️  IP가 그대로다 ({new_ip}) — 다시 토글해주세요")
            continue

        print(f"  ✅ IP 변경 확인: {prev_ip} → {new_ip}")
        return new_ip


def login_account(username: str, password: str, index: int, total: int) -> dict:
    """
    계정 1개 로그인 + 세션 검증.

    결과 구조:
      status      — ready/challenge/2fa/bad_pw/banned/failed
      message     — 상세 사유
      session     — 세션 파일 경로 (성공 시)
      user_id     — 인스타 user_id (성공 시)
      ip          — 로그인에 사용한 IP
      timeline_ok — 세션 활성 검증 통과 여부
      error_type  — 예외 클래스명
    """
    result = {
        "username": username,
        "index": f"{index}/{total}",
        "status": "failed",
        "message": "",
        "session": None,
        "user_id": None,
        "ip": get_external_ip(),
        "timeline_ok": False,
        "error_type": None,
        "took_sec": 0,
    }

    logger.info(f"[{index}/{total}] {username} 로그인 시도 (IP: {result['ip']})")
    session_path = SESSIONS_DIR / f"{username}.json"
    started = time.time()

    from bulk_login import make_client
    from devices import pick_device

    cl = make_client(request_timeout=20)

    # 기존 세션 없으면 배정된 랜덤 기종으로 디바이스 정체성 생성
    if not session_path.exists():
        cl.set_device(pick_device(username))

    # 한국 디바이스 설정
    cl.set_country("KR")
    cl.set_country_code(82)
    cl.set_locale("ko_KR")
    cl.set_timezone_offset(9 * 3600)
    cl.delay_range = [2, 5]

    # 기존 세션이 있으면 복원 시도
    if session_path.exists():
        try:
            cl.load_settings(str(session_path))
            logger.info(f"  기존 세션 복원 시도...")
        except Exception as e:
            logger.warning(f"  세션 파일 손상, 새로 로그인: {e}")
            session_path.unlink(missing_ok=True)

    try:
        ok = cl.login(username, password)
        if not ok:
            result["status"] = "failed"
            result["message"] = "login()이 False 반환"
            result["took_sec"] = time.time() - started
            return result

        result["user_id"] = cl.user_id
        logger.info(f"  로그인 성공 (user_id: {cl.user_id})")

        # 세션이 실제로 살아있는지 검증
        logger.info(f"  세션 검증 중 (타임라인 피드 호출)...")
        try:
            feed = cl.get_timeline_feed("cold_start_fetch")
            items = feed.get("feed_items") or feed.get("items") or []
            result["timeline_ok"] = True
            logger.info(f"  ✅ 타임라인 응답 정상 (아이템 {len(items)}개)")
        except Exception as e:
            logger.warning(f"  ⚠️ 타임라인 실패: {e}")
            result["timeline_ok"] = False

        # 세션 저장
        session_path.parent.mkdir(parents=True, exist_ok=True)
        cl.dump_settings(str(session_path))
        result["session"] = str(session_path)

        # ── 핵심: 저장한 세션이 실제로 재사용되는지 검증 ──
        # 새 Client를 만들고, 로그인 없이 세션만 로드해서 API가 먹히는지 본다.
        # 이게 되어야 나중에 좋아요 발사 때 매번 로그인 안 해도 된다.
        logger.info("  세션 재사용 검증 중 (새 클라이언트 → 세션 로드 → API 호출)...")
        try:
            cl2 = Client()
            cl2.load_settings(str(session_path))
            cl2.delay_range = [1, 3]
            info = cl2.account_info()
            result["session_reload_ok"] = True
            result["full_name"] = info.full_name if info else None
            result["is_private"] = info.is_private if info else None
            logger.info(f"  ✅ 세션 재사용 성공 — {info.full_name} (비공개:{info.is_private})")
        except Exception as e:
            result["session_reload_ok"] = False
            logger.warning(f"  ❌ 세션 재사용 실패: {e}")

        if result["timeline_ok"] and result.get("session_reload_ok"):
            result["status"] = "ready"
            result["message"] = "로그인 + 세션 저장/복원 검증 완료"
        elif result["timeline_ok"]:
            result["status"] = "ready"
            result["message"] = "로그인 성공, 세션 복원 미확인"
        else:
            result["status"] = "ready"
            result["message"] = "로그인은 됐으나 세션 검증 불완전"

    except TwoFactorRequired as e:
        result["status"] = "2fa"
        result["message"] = "2단계 인증 필요"
        result["error_type"] = "TwoFactorRequired"
        logger.warning(f"  🔑 2FA 필요")

    except (ChallengeRequired, SelectContactPointRecoveryForm, RecaptchaChallengeForm) as e:
        # 자동 해결 가능한 챌린지(본인확인 "네 접니다" 등)는 시도해본다
        resolved = False
        try:
            if isinstance(e, ChallengeRequired):
                cl.challenge_resolve(cl.last_json)
                # 해결됐으면 세션 검증
                cl.get_timeline_feed("cold_start_fetch")
                cl.dump_settings(str(session_path))
                result["status"] = "ready"
                result["message"] = "챌린지 자동 해결 + 로그인 성공"
                result["session"] = str(session_path)
                result["user_id"] = cl.user_id
                result["timeline_ok"] = True
                resolved = True
                logger.info("  ✅ 챌린지 자동 해결 성공")
        except Exception:
            pass

        if not resolved:
            result["status"] = "challenge"
            result["message"] = f"인증 챌린지: {type(e).__name__}"
            result["error_type"] = type(e).__name__
            logger.warning(f"  🔐 챌린지 (수동 해결 필요): {type(e).__name__}")

            try:
                last = cl.last_json if hasattr(cl, "last_json") else {}
                if isinstance(last, dict):
                    challenge = last.get("challenge", {})
                    step = last.get("step_name", "")
                    if challenge or step:
                        result["message"] += f" (step: {step})"
            except Exception:
                pass

    except BadPassword:
        result["status"] = "bad_pw"
        result["message"] = "비밀번호 불일치"
        result["error_type"] = "BadPassword"
        logger.error(f"  ❌ 비번 틀림")

    except UserNotFound:
        result["status"] = "bad_pw"
        result["message"] = "존재하지 않는 계정"
        result["error_type"] = "UserNotFound"
        logger.error(f"  ❌ 계정 없음")

    except FeedbackRequired as e:
        result["status"] = "banned"
        result["message"] = f"스팸 감지/밴: {e}"
        result["error_type"] = "FeedbackRequired"
        logger.error(f"  ⛔ 밴/스팸: {e}")

    except ProxyAddressIsBlocked:
        result["status"] = "failed"
        result["message"] = "IP 차단됨"
        result["error_type"] = "ProxyAddressIsBlocked"
        logger.error(f"  🚫 IP 차단")

    except (PleaseWaitFewMinutes, ClientThrottledError) as e:
        result["status"] = "failed"
        result["message"] = f"레이트리밋: {e}"
        result["error_type"] = type(e).__name__
        logger.warning(f"  ⏳ 레이트리밋: {e}")

    except ReloginAttemptExceeded:
        result["status"] = "failed"
        result["message"] = "재로그인 시도 초과"
        result["error_type"] = "ReloginAttemptExceeded"
        logger.warning(f"  ⚠️ 재로그인 초과")

    except ClientError as e:
        result["status"] = "failed"
        result["message"] = f"클라이언트 에러: {e}"
        result["error_type"] = type(e).__name__
        logger.error(f"  ❌ ClientError: {e}")

    except Exception as e:
        result["status"] = "failed"
        result["message"] = f"{type(e).__name__}: {e}"
        result["error_type"] = type(e).__name__
        logger.error(f"  ❌ {type(e).__name__}: {e}")

    result["took_sec"] = time.time() - started
    return result


def main():
    print("=" * 60)
    print("  인스타그램 계정 로그인 테스트")
    print(f"  대상: {len(ACCOUNTS)}개 계정")
    print(f"  방식: 테더링 + 수동 비행기모드 IP 변경")
    print("=" * 60)

    # 초기 IP 확인
    current_ip = get_external_ip()
    print(f"\n  현재 IP: {current_ip}")

    if current_ip == "확인 실패":
        print("  ❌ 인터넷 연결이 없다. 테더링을 확인해라.")
        return

    results = []

    for i, (username, password) in enumerate(ACCOUNTS, 1):
        # 첫 계정 이후부터 IP 변경
        if i > 1:
            current_ip = wait_for_ip_change(current_ip)

        # 로그인 시도
        result = login_account(username, password, i, len(ACCOUNTS))
        results.append(result)

        # 결과 즉시 출력
        icon = {
            "ready": "✅", "challenge": "🔐", "2fa": "🔑",
            "bad_pw": "❌", "banned": "⛔", "failed": "⚠️",
        }.get(result["status"], "•")
        logger.info(f"  {icon} 결과: {result['status']} — {result['message']} ({result['took_sec']:.1f}초)")

        # 결과 중간 저장 (중단돼도 남도록)
        save_results(results)

        # 로그인 사이 짧은 대기
        if i < len(ACCOUNTS):
            time.sleep(3)

    # 최종 요약
    print_summary(results)
    save_results(results)


def print_summary(results: list[dict]):
    print()
    print("=" * 68)
    print("  최종 결과")
    print("=" * 68)

    status_icons = {
        "ready": "✅", "challenge": "🔐", "2fa": "🔑",
        "bad_pw": "❌", "banned": "⛔", "failed": "⚠️",
    }

    for r in results:
        icon = status_icons.get(r["status"], "•")
        tl = "📡" if r.get("timeline_ok") else "  "
        print(
            f"  {icon} {r['username']:<18} {r['status']:<11} "
            f"{tl} IP:{r['ip']:<16} "
            f"{r['message'][:40]}"
        )

    print()
    by_status = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1

    print("  상태별 집계:")
    labels = {
        "ready": "로그인 성공",
        "challenge": "인증 챌린지",
        "2fa": "2단계 인증",
        "bad_pw": "비번 오류/없는 계정",
        "banned": "밴/스팸",
        "failed": "기타 실패",
    }
    for status, label in labels.items():
        count = by_status.get(status, 0)
        if count:
            print(f"    {status_icons[status]} {label}: {count}건")

    # 실패 유형 상세
    error_types = {}
    for r in results:
        if r["status"] != "ready" and r.get("error_type"):
            error_types[r["error_type"]] = error_types.get(r["error_type"], 0) + 1
    if error_types:
        print("\n  예외 유형별:")
        for et, count in sorted(error_types.items(), key=lambda x: -x[1]):
            print(f"    {et}: {count}건")

    # 세션 파일 생성 현황
    sessions = [r for r in results if r.get("session")]
    print(f"\n  세션 파일 생성: {len(sessions)}개 / {len(results)}개")

    print("=" * 68)


def save_results(results: list[dict]):
    """결과를 JSON으로 저장 — 중단돼도 유지된다"""
    out = Path("login_test_results.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(
            {
                "timestamp": datetime.now().isoformat(),
                "total": len(results),
                "results": results,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


if __name__ == "__main__":
    main()
