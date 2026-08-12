"""
자동 워밍업 스케줄러

매일 1회, 랜덤한 시간에 워밍업을 자동 실행한다.
  - 사람처럼 매일 다른 시간에 활동 (탐지 회피)
  - 오늘 이미 워밍업한 계정은 건너뜀 (중복 방지)
  - PC 켜두면 알아서 돌아감 → 손 안 대도 14일 워밍업 완주

실행:
  python warmup_scheduler.py           # 기본 (오전 9시~오후 10시 사이 랜덤)
  python warmup_scheduler.py 10 20     # 오전 10시~오후 8시 사이 랜덤

중단: Ctrl+C
"""

import sys
import time
import random
import logging
from datetime import datetime, date, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%m-%d %H:%M:%S",
)
logger = logging.getLogger("warmup_scheduler")

# 하루 중 워밍업을 돌릴 시간대 (기본 오전 9시 ~ 오후 10시)
DEFAULT_START_HOUR = 9
DEFAULT_END_HOUR = 22


def pick_run_time(day: date, start_hour: int, end_hour: int) -> datetime:
    """그날의 랜덤 실행 시각을 정한다"""
    hour = random.randint(start_hour, end_hour - 1)
    minute = random.randint(0, 59)
    return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)


def run_warmup_once() -> dict:
    """워밍업 배치 1회 실행 (DB에 job 기록 + 로그)"""
    import db
    from warmup_engine import WarmupEngine

    db.init()
    due = db.warming_accounts(due_only=True)
    if not due:
        logger.info("오늘 워밍업할 계정 없음 (전부 완료했거나 워밍업 계정 없음)")
        return {"warmed": 0, "posted": 0, "graduated": 0, "failed": 0}

    logger.info(f"워밍업 시작 — 대상 {len(due)}개 계정")
    job_id = db.create_job("warmup", total=len(due))
    engine = WarmupEngine()
    try:
        counts = engine.run(job_id=job_id)
        db.finish_job(job_id, "done",
                      f"진행 {counts['warmed']} / 포스팅 {counts['posted']} / "
                      f"졸업 {counts['graduated']} / 실패 {counts['failed']}")
        logger.info(
            f"워밍업 완료 — 진행 {counts['warmed']} / 포스팅 {counts['posted']} / "
            f"졸업 {counts['graduated']} / 실패 {counts['failed']}")
        return counts
    except Exception as e:
        logger.exception("워밍업 실행 실패")
        db.finish_job(job_id, "failed", str(e))
        return {"warmed": 0, "posted": 0, "graduated": 0, "failed": 0, "error": str(e)}


def main():
    start_hour = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_START_HOUR
    end_hour = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_END_HOUR

    logger.info("=" * 55)
    logger.info(f"  자동 워밍업 스케줄러 시작")
    logger.info(f"  매일 {start_hour}시~{end_hour}시 사이 랜덤 시간에 실행")
    logger.info(f"  중단: Ctrl+C")
    logger.info("=" * 55)

    last_run_date = None

    while True:
        try:
            now = datetime.now()
            today = now.date()

            # 오늘 아직 안 돌렸으면 → 오늘 실행 시각 정하기
            if last_run_date != today:
                run_at = pick_run_time(today, start_hour, end_hour)

                # 이미 그 시각이 지났으면 (스케줄러를 늦게 켠 경우)
                if now >= run_at:
                    # 시간대 안이면 지금 바로, 시간대 지났으면 내일로
                    if start_hour <= now.hour < end_hour:
                        logger.info(f"오늘 실행 시간대 안 — 지금 바로 실행")
                        run_warmup_once()
                        last_run_date = today
                    else:
                        # 오늘 시간대 지남 → 내일 대기
                        last_run_date = today
                        logger.info("오늘 시간대 지남 — 내일 대기")
                else:
                    # 실행 시각까지 대기
                    wait = (run_at - now).total_seconds()
                    logger.info(f"오늘 워밍업 예정: {run_at.strftime('%H:%M')} "
                                f"({wait/3600:.1f}시간 후)")
                    time.sleep(min(wait, 3600))  # 최대 1시간씩 끊어서 대기
                    # 대기 후 루프 재확인 (시각 도달했으면 실행)
                    if datetime.now() >= run_at:
                        run_warmup_once()
                        last_run_date = today
            else:
                # 오늘 이미 실행함 → 자정 지나 내일까지 대기
                tomorrow = datetime.combine(today + timedelta(days=1), datetime.min.time())
                wait = (tomorrow - datetime.now()).total_seconds()
                logger.info(f"오늘 완료. 다음 날까지 대기 ({wait/3600:.1f}시간)")
                time.sleep(min(wait + 60, 3600))

        except KeyboardInterrupt:
            logger.info("스케줄러 중단됨 (Ctrl+C)")
            break
        except Exception as e:
            logger.exception(f"스케줄러 오류 (계속 진행): {e}")
            time.sleep(300)


if __name__ == "__main__":
    main()
