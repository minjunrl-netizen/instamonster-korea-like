"""
헬스 모니터 스케줄러

일정 간격(기본 3시간)마다 전체 계정의 세션 생사를 자동 점검한다.
계정이 죽으면(밴/챌린지/세션만료) 즉시 로그로 알려 빠른 대응을 돕는다.

실행:
  python monitor_scheduler.py        # 기본 3시간마다
  python monitor_scheduler.py 2      # 2시간마다
  python monitor_scheduler.py 0.5    # 30분마다

중단: Ctrl+C
"""

import sys
import time
import logging
from datetime import datetime, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%m-%d %H:%M:%S",
)
logger = logging.getLogger("monitor_scheduler")

DEFAULT_INTERVAL_HOURS = 3.0


def run_once() -> dict:
    import db
    from account_monitor import run_health_check
    db.init()
    job_id = db.create_job("health_check", total=len(db.monitorable_accounts()))
    try:
        result = run_health_check(job_id=job_id)
        dead = len(result.get("dead_list", []))
        db.finish_job(job_id, "done",
                      f"정상 {result['alive']} / 대응필요 {dead}")
        return result
    except Exception as e:
        logger.exception("헬스체크 실행 실패")
        db.finish_job(job_id, "failed", str(e))
        return {"alive": 0, "dead_list": []}


def main():
    interval_h = float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INTERVAL_HOURS
    interval_s = interval_h * 3600

    logger.info("=" * 55)
    logger.info(f"  계정 헬스 모니터 시작")
    logger.info(f"  {interval_h}시간마다 전체 계정 생사 점검")
    logger.info(f"  중단: Ctrl+C")
    logger.info("=" * 55)

    while True:
        try:
            result = run_once()
            dead = result.get("dead_list", [])
            if dead:
                logger.warning(f"⚠️  대응 필요 {len(dead)}개 — 대시보드/진단 확인")
            next_run = datetime.now() + timedelta(seconds=interval_s)
            logger.info(f"다음 점검: {next_run.strftime('%m-%d %H:%M')}")

            # interval을 1시간 단위로 끊어서 대기 (중단 반응성)
            remaining = interval_s
            while remaining > 0:
                time.sleep(min(remaining, 3600))
                remaining -= 3600

        except KeyboardInterrupt:
            logger.info("모니터 중단됨 (Ctrl+C)")
            break
        except Exception as e:
            logger.exception(f"스케줄러 오류 (계속 진행): {e}")
            time.sleep(300)


if __name__ == "__main__":
    main()
