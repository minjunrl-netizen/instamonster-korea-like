"""
신규 계정 양성(워밍업) 모듈

신규 계정은 바로 대량 좋아요를 돌리면 즉시 밴당한다.
10~20일간 천천히 키워야 인스타가 "사람"으로 인식한다.

양성 스케줄:
  Day 1~3:   읽기만 (피드, 스토리, 탐색탭) + 프로필 세팅
  Day 4~7:   하루 좋아요 2~3개 + 피드 탐색
  Day 8~14:  하루 좋아요 3~5개 + 게시물 1개 업로드
  Day 15~20: 하루 좋아요 5~10개 + 게시물 1개 + 댓글 1~2개
  Day 21+:   실전 투입 가능 (단, 급격한 볼륨 증가 금지)
"""

import json
import time
import random
import logging
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired, FeedbackRequired

from xproxy_manager import XProxyManager
from human_behavior import HumanBehavior, setup_account_like_real_device
from content_manager import AccountContent, ContentPoster

logger = logging.getLogger(__name__)



class WarmupState:
    """계정별 워밍업 진행 상태 추적"""

    def __init__(self, username: str, state_file: str):
        self.username = username
        self.state_file = state_file
        self.data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.state_file):
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "username": self.username,
            "start_date": datetime.now().isoformat(),
            "current_day": 1,
            "last_run_date": None,
            "total_likes": 0,
            "total_posts": 0,
            "total_comments": 0,
            "total_story_views": 0,
            "total_feed_browses": 0,
            "daily_log": [],
        }

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    @property
    def current_day(self) -> int:
        start = datetime.fromisoformat(self.data["start_date"])
        return (datetime.now() - start).days + 1

    @property
    def already_ran_today(self) -> bool:
        last = self.data.get("last_run_date")
        if not last:
            return False
        return datetime.fromisoformat(last).date() == datetime.now().date()

    def log_daily(self, likes: int, posts: int, comments: int, story_views: int) -> None:
        self.data["last_run_date"] = datetime.now().isoformat()
        self.data["current_day"] = self.current_day
        self.data["total_likes"] += likes
        self.data["total_posts"] += posts
        self.data["total_comments"] += comments
        self.data["total_story_views"] += story_views
        self.data["daily_log"].append({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "day": self.current_day,
            "likes": likes,
            "posts": posts,
            "comments": comments,
            "story_views": story_views,
        })
        self.save()


class WarmupSchedule:
    """워밍업 일자별 행동 스케줄"""

    @staticmethod
    def get_plan(day: int) -> dict:
        """
        일자별 허용 행동량 반환.
        점진적으로 증가시키는 게 핵심.
        """
        if day <= 3:
            # 1~3일: 읽기만, 계정 존재감 만들기
            return {
                "phase": "초기 (읽기 전용)",
                "likes": 0,
                "posts": 0,
                "comments": 0,
                "story_views": random.randint(3, 8),
                "feed_browses": random.randint(3, 6),
                "explore_browses": random.randint(1, 3),
            }
        elif day <= 7:
            # 4~7일: 최소한의 상호작용 시작
            return {
                "phase": "입문 (최소 좋아요)",
                "likes": random.randint(2, 3),
                "posts": 0,
                "comments": 0,
                "story_views": random.randint(5, 12),
                "feed_browses": random.randint(4, 8),
                "explore_browses": random.randint(2, 4),
            }
        elif day <= 14:
            # 8~14일: 포스팅 시작 + 좋아요 약간 증가
            return {
                "phase": "성장 (포스팅 시작)",
                "likes": random.randint(3, 5),
                "posts": 1,
                "comments": random.randint(0, 1),
                "story_views": random.randint(8, 15),
                "feed_browses": random.randint(5, 10),
                "explore_browses": random.randint(2, 5),
            }
        elif day <= 20:
            # 15~20일: 활발한 활동
            return {
                "phase": "활성화 (활발한 활동)",
                "likes": random.randint(5, 10),
                "posts": 1,
                "comments": random.randint(1, 2),
                "story_views": random.randint(10, 20),
                "feed_browses": random.randint(6, 12),
                "explore_browses": random.randint(3, 6),
            }
        else:
            # 21일+: 실전 투입 가능
            return {
                "phase": "✅ 실전 준비 완료",
                "likes": random.randint(10, 20),
                "posts": random.randint(0, 1),
                "comments": random.randint(1, 3),
                "story_views": random.randint(10, 25),
                "feed_browses": random.randint(5, 10),
                "explore_browses": random.randint(2, 5),
            }


class AccountWarmer:
    """단일 계정 워밍업 실행기"""

    def __init__(self, username: str, password: str, session_file: str,
                 proxy_url: str, warmup_state_file: str, content_config: dict = None):
        self.username = username
        self.password = password
        self.session_file = session_file
        self.proxy_url = proxy_url

        self.cl: Optional[Client] = None
        self.human: Optional[HumanBehavior] = None
        self.content = AccountContent(username, content_config or {})
        self.poster: Optional[ContentPoster] = None
        self.state = WarmupState(username, warmup_state_file)

    def login(self) -> bool:
        """로그인"""
        self.cl = Client()
        session_path = Path(self.session_file)

        # load_settings가 locale/country/device를 덮어쓰므로 반드시 먼저 복원한 뒤
        # 디바이스 설정을 적용해야 한다.
        if session_path.exists():
            try:
                self.cl.load_settings(str(session_path))
            except Exception as e:
                logger.warning(f"[{self.username}] 세션 복원 실패, 새로 만듦: {e}")

        setup_account_like_real_device(self.cl, self.proxy_url)

        try:
            self.cl.login(self.username, self.password)
            session_path.parent.mkdir(parents=True, exist_ok=True)
            self.cl.dump_settings(str(session_path))
            self.human = HumanBehavior(self.cl, self.username)
            self.poster = ContentPoster(self.cl, self.username, self.content)
            logger.info(f"[{self.username}] 로그인 성공")
            return True
        except ChallengeRequired:
            logger.error(f"[{self.username}] 챌린지 발생 - 수동 해결 필요")
            return False
        except Exception as e:
            logger.error(f"[{self.username}] 로그인 실패: {e}")
            return False

    def _browse_feed(self, count: int) -> None:
        """피드 둘러보기"""
        for i in range(count):
            try:
                self.cl.get_timeline_feed()
                time.sleep(random.uniform(5, 15))
            except Exception:
                pass

    def _browse_explore(self, count: int) -> None:
        """탐색탭 둘러보기"""
        for i in range(count):
            try:
                self.cl.explore_page()
                time.sleep(random.uniform(5, 15))
            except Exception:
                pass

    def _view_stories(self, count: int) -> int:
        """스토리 보기"""
        viewed = 0
        try:
            reels = self.cl.get_reels_tray_feed("cold_start")
            tray = reels.get("tray", [])
            if not tray:
                return 0

            sample_count = min(count, len(tray))
            for reel_data in random.sample(tray, sample_count):
                user_id = reel_data.get("user", {}).get("pk")
                if user_id:
                    try:
                        self.cl.user_stories(user_id)
                        viewed += 1
                        time.sleep(random.uniform(3, 8))
                    except Exception:
                        pass
        except Exception:
            pass
        return viewed

    def _do_likes(self, count: int) -> int:
        """
        탐색탭/피드에서 자연스럽게 좋아요.
        특정 타겟이 아닌, 피드에 뜨는 게시물에 랜덤으로 좋아요.
        """
        liked = 0
        try:
            # 타임라인 피드에서 미디어 가져오기
            feed = self.cl.get_timeline_feed()
            items = feed.get("feed_items", [])

            candidates = []
            for item in items:
                media = item.get("media_or_ad") or item.get("media")
                if media and isinstance(media, dict):
                    media_id = media.get("id")
                    has_liked = media.get("has_liked", False)
                    if media_id and not has_liked:
                        candidates.append(media_id)

            if not candidates:
                return 0

            to_like = random.sample(candidates, min(count, len(candidates)))

            for media_id in to_like:
                try:
                    # 먼저 게시물을 본다 (사람처럼)
                    time.sleep(random.uniform(3, 10))

                    self.cl.media_like(media_id)
                    liked += 1
                    logger.info(f"[{self.username}] 좋아요: {media_id}")

                    # 좋아요 후 딜레이
                    time.sleep(random.uniform(10, 30))
                except FeedbackRequired:
                    logger.warning(f"[{self.username}] 스팸 감지 - 좋아요 중단")
                    break
                except Exception as e:
                    logger.debug(f"[{self.username}] 좋아요 실패: {e}")
        except Exception as e:
            logger.debug(f"[{self.username}] 피드 로딩 실패: {e}")
        return liked

    def _do_post(self) -> bool:
        """계정 전용 콘텐츠로 포스팅"""
        if not self.poster:
            return False
        return self.poster.post_photo() is not None

    def _do_comments(self, count: int) -> int:
        """피드 게시물에 자연스러운 댓글"""
        commented = 0
        try:
            feed = self.cl.get_timeline_feed()
            items = feed.get("feed_items", [])

            candidates = []
            for item in items:
                media = item.get("media_or_ad") or item.get("media")
                if media and isinstance(media, dict):
                    media_id = media.get("id")
                    if media_id:
                        candidates.append(media_id)

            if not candidates:
                return 0

            to_comment = random.sample(candidates, min(count, len(candidates)))

            for media_id in to_comment:
                try:
                    comment_text = random.choice(COMMENTS_POOL)
                    time.sleep(random.uniform(5, 15))
                    self.cl.media_comment(media_id, comment_text)
                    commented += 1
                    logger.info(f"[{self.username}] 💬 댓글: '{comment_text}'")
                    time.sleep(random.uniform(15, 40))
                except Exception as e:
                    logger.debug(f"[{self.username}] 댓글 실패: {e}")
        except Exception:
            pass
        return commented

    def run_daily(self) -> dict:
        """하루치 워밍업 실행"""
        if self.state.already_ran_today:
            logger.info(f"[{self.username}] 오늘 이미 실행함, 스킵")
            return {"skipped": True}

        day = self.state.current_day
        plan = WarmupSchedule.get_plan(day)

        logger.info(f"\n{'='*50}")
        logger.info(f"[{self.username}] Day {day} - {plan['phase']}")
        logger.info(f"  계획: 좋아요 {plan['likes']}개, 포스팅 {plan['posts']}개, "
                     f"댓글 {plan['comments']}개, 스토리 {plan['story_views']}개")
        logger.info(f"{'='*50}")

        if not self.login():
            return {"error": "login_failed"}

        # Day 1: 프로필 세팅 (최초 1회)
        if day == 1 and self.poster:
            logger.info(f"[{self.username}] 프로필 세팅...")
            self.poster.setup_profile()
            time.sleep(random.uniform(5, 15))

        # 1. 앱 오픈 시뮬레이션
        logger.info(f"[{self.username}] 앱 오픈...")
        self.human.simulate_app_open()
        time.sleep(random.uniform(3, 8))

        # 2. 피드 둘러보기
        logger.info(f"[{self.username}] 피드 둘러보기...")
        self._browse_feed(plan["feed_browses"])

        # 3. 스토리 보기
        logger.info(f"[{self.username}] 스토리 보기...")
        story_views = self._view_stories(plan["story_views"])

        # 4. 탐색탭
        logger.info(f"[{self.username}] 탐색탭 둘러보기...")
        self._browse_explore(plan["explore_browses"])

        # 5. 좋아요 (Day 4부터)
        likes = 0
        if plan["likes"] > 0:
            logger.info(f"[{self.username}] 좋아요 {plan['likes']}개...")
            likes = self._do_likes(plan["likes"])

        # 6. 포스팅 (Day 8부터)
        posts = 0
        if plan["posts"] > 0:
            logger.info(f"[{self.username}] 포스팅...")
            time.sleep(random.uniform(30, 90))  # 포스팅 전 대기
            if self._do_post():
                posts = 1

        # 7. 댓글 (Day 15부터)
        comments = 0
        if plan["comments"] > 0:
            logger.info(f"[{self.username}] 댓글...")
            time.sleep(random.uniform(15, 45))
            comments = self._do_comments(plan["comments"])

        # 상태 저장
        self.state.log_daily(likes, posts, comments, story_views)

        result = {
            "day": day,
            "phase": plan["phase"],
            "likes": likes,
            "posts": posts,
            "comments": comments,
            "story_views": story_views,
        }

        logger.info(f"\n[{self.username}] Day {day} 완료:")
        logger.info(f"  좋아요: {likes}, 포스팅: {posts}, 댓글: {comments}, 스토리: {story_views}")
        logger.info(f"  누적 - 좋아요: {self.state.data['total_likes']}, "
                     f"포스팅: {self.state.data['total_posts']}")

        return result


class WarmupManager:
    """전체 계정 워밍업 관리자"""

    def __init__(self, config_path: str = "config.json"):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        xp_cfg = self.config["xproxy"]
        self.xproxy = XProxyManager(
            host=xp_cfg["host"],
            api_port=xp_cfg["api_port"],
            proxy_type=xp_cfg.get("proxy_type", "socks5"),
            slots=xp_cfg["slots"],
        )

    def run_all(self) -> None:
        """전체 계정 하루치 워밍업 실행"""
        logger.info("=" * 60)
        logger.info("  🌱 계정 양성 모드 시작")
        logger.info("=" * 60)

        for acc_cfg in self.config["accounts"]:
            proxy_url = self.xproxy.get_proxy_url(acc_cfg["proxy_slot"])
            state_file = f"warmup_state/{acc_cfg['username']}.json"

            warmer = AccountWarmer(
                username=acc_cfg["username"],
                password=acc_cfg["password"],
                session_file=acc_cfg["session_file"],
                proxy_url=proxy_url,
                warmup_state_file=state_file,
                content_config=acc_cfg.get("content", {}),
            )

            result = warmer.run_daily()

            if result.get("skipped"):
                continue

            # 계정 간 긴 딜레이 (사람처럼 다른 폰 꺼내서 하는 시간)
            delay = random.uniform(60, 180)
            logger.info(f"다음 계정까지 {delay:.0f}초 대기...")
            time.sleep(delay)

        logger.info("\n🌱 전체 계정 양성 완료!")

    def show_status(self) -> None:
        """전체 계정 양성 상태 출력"""
        logger.info("\n" + "=" * 60)
        logger.info("  📊 계정 양성 현황")
        logger.info("=" * 60)

        for acc_cfg in self.config["accounts"]:
            state_file = f"warmup_state/{acc_cfg['username']}.json"
            state = WarmupState(acc_cfg["username"], state_file)

            day = state.current_day
            plan = WarmupSchedule.get_plan(day)
            ready = "✅ 실전 가능" if day >= 21 else f"🌱 {plan['phase']}"

            logger.info(f"\n  @{acc_cfg['username']}")
            logger.info(f"    Day {day}/21  {ready}")
            logger.info(f"    누적: 좋아요 {state.data['total_likes']}개, "
                         f"포스팅 {state.data['total_posts']}개, "
                         f"댓글 {state.data['total_comments']}개")


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        mgr = WarmupManager("config.json")
        mgr.show_status()
    else:
        mgr = WarmupManager("config.json")
        mgr.run_all()
