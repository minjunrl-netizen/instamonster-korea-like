"""
인간 행동 시뮬레이션 모듈

인스타그램이 봇을 감지하는 건 패킷 형태가 아니라 행동 패턴이다.
실제 사람처럼 행동하려면:
  1. 앱을 열었을 때 하는 일들을 먼저 한다 (피드 로딩, 스토리 확인)
  2. 랜덤 딜레이로 사람처럼 느리게
  3. 단조로운 반복이 아닌 다양한 행동 믹스
  4. 세션과 디바이스를 안정적으로 유지

모든 메서드는 인스턴스에 묶인 Client 하나만 건드리므로,
계정마다 인스턴스를 따로 만들면 스레드에서 병렬로 써도 안전하다.
"""

import time
import random
import logging

from instagrapi import Client

logger = logging.getLogger(__name__)


class HumanBehavior:
    """실제 사람의 인스타그램 사용 패턴을 시뮬레이션"""

    def __init__(self, client: Client, username: str):
        self.cl = client
        self.username = username

    def simulate_app_open(self) -> None:
        """
        앱을 열었을 때 실제 인스타 앱이 하는 동작을 재현.
        세션이 붙자마자 좋아요만 쏘는 패턴이 가장 잘 걸리므로,
        작업 시작 전에 앱을 켠 흔적을 먼저 남긴다.
        """
        try:
            # 1. 타임라인 피드 로딩 (앱 실행 시 자동으로 하는 것)
            self.cl.get_timeline_feed("cold_start_fetch")
            self._human_pause(1.5, 4.0)

            # 2. 스토리 트레이 로딩 (상단 스토리 원형 아이콘들)
            self.cl.get_reels_tray_feed("cold_start")
            self._human_pause(2.0, 5.0)
        except Exception as e:
            logger.debug(f"[{self.username}] 앱 오픈 시뮬레이션 일부 실패 (무시): {e}")

    def browse_feed_before_like(self) -> None:
        """
        좋아요를 누르기 전에 피드를 둘러보는 행동.
        실제 사람은 바로 좋아요를 누르지 않고 스크롤을 한다.
        """
        try:
            self.cl.get_timeline_feed("pull_to_refresh")
            self._human_pause(1.0, 3.0)
        except Exception as e:
            logger.debug(f"[{self.username}] 피드 조회 실패 (무시): {e}")

    def browse_user_profile(self, user_id: str) -> None:
        """
        좋아요 전에 유저 프로필 방문.
        실제 사람은 프로필 → 게시물 → 좋아요 순서로 행동한다.
        """
        try:
            self.cl.user_info(str(user_id))
            self._human_pause(1.5, 4.0)
        except Exception as e:
            logger.debug(f"[{self.username}] 프로필 조회 실패 (무시): {e}")

    def view_media_before_like(self, media_pk: str) -> None:
        """
        좋아요 전에 게시물 상세 조회.
        실제 사람은 게시물을 먼저 보고 나서 좋아요를 누른다.

        media_pk는 짧은 형식이어야 한다. {pk}_{user_id} 형식을 넘기면 조회가 실패한다.
        """
        pk = str(media_pk).split("_", 1)[0]
        if not pk.isdigit():
            logger.debug(f"[{self.username}] 잘못된 media_pk: {media_pk}")
            return
        try:
            self.cl.media_info(pk)
            self._human_pause(2.0, 6.0)  # 게시물 읽는 시간
        except Exception as e:
            logger.debug(f"[{self.username}] 게시물 조회 실패 (무시): {e}")

    def random_story_view(self) -> None:
        """
        랜덤으로 스토리를 본다. 좋아요만 하는 단조로운 패턴을 깨준다.
        """
        try:
            reels = self.cl.get_reels_tray_feed("pull_to_refresh")
            tray = reels.get("tray", []) if isinstance(reels, dict) else []
            if not tray:
                return

            sample_count = min(random.randint(1, 2), len(tray))
            for reel_data in random.sample(tray, sample_count):
                reel_user_id = (reel_data.get("user") or {}).get("pk")
                if not reel_user_id:
                    continue
                try:
                    self.cl.user_stories(str(reel_user_id))
                    self._human_pause(2.0, 5.0)
                except Exception as e:
                    logger.debug(f"[{self.username}] 스토리 조회 실패 (무시): {e}")
        except Exception as e:
            logger.debug(f"[{self.username}] 스토리 트레이 실패 (무시): {e}")

    @staticmethod
    def should_take_break(actions_done: int, every: int = 8, chance: float = 0.35) -> bool:
        """
        일정 횟수 이상 행동했을 때 확률적으로 쉬어야 하는지 판단.
        """
        if actions_done <= 0 or actions_done % every != 0:
            return False
        return random.random() < chance

    def take_break(self, min_sec: float = 30.0, max_sec: float = 120.0) -> None:
        """사람처럼 앱을 잠시 안 쓰는 시간"""
        break_time = random.uniform(min_sec, max_sec)
        logger.info(f"[{self.username}] 💤 휴식 {break_time:.0f}초...")
        time.sleep(break_time)

    def do_random_action(self) -> None:
        """
        좋아요 사이사이에 랜덤 행동을 섞는다.
        행동이 "좋아요만 계속" 이면 봇으로 감지됨.
        """
        action = random.choices(
            ["feed", "story", "idle"],
            weights=[45, 20, 35],
            k=1,
        )[0]

        if action == "feed":
            self.browse_feed_before_like()
        elif action == "story":
            self.random_story_view()
        else:
            self._human_pause(1.0, 3.0)

    @staticmethod
    def get_like_delay() -> float:
        """
        좋아요 간 딜레이.
        사람이 게시물 보고 → 좋아요 누르는 시간을 시뮬레이션.

        실제 사람 패턴:
        - 빠른 좋아요: 3~7초 (바로 누름)
        - 보통: 7~18초 (게시물 읽고 누름)
        - 느린: 18~35초 (댓글도 읽고 누름)
        """
        pattern = random.choices(
            ["fast", "normal", "slow"],
            weights=[30, 50, 20],
            k=1,
        )[0]

        if pattern == "fast":
            return random.uniform(3, 7)
        if pattern == "normal":
            return random.uniform(7, 18)
        return random.uniform(18, 35)

    @staticmethod
    def _human_pause(min_sec: float, max_sec: float) -> None:
        """사람처럼 불규칙한 대기"""
        time.sleep(random.uniform(min_sec, max_sec))


def setup_account_like_real_device(cl: Client, proxy_url: str) -> None:
    """
    계정을 실제 디바이스처럼 설정.

    핵심: 인스타그램은 계정-디바이스-IP 조합의 일관성을 본다.
    - 같은 계정은 항상 같은 디바이스 정보
    - 같은 계정은 항상 같은 IP 대역 (xProxy 유심)
    - locale/timezone은 IP 국가와 일치

    이 값들은 dump_settings에 저장되므로, 이후 load_settings만 하면 그대로 복원된다.
    """
    cl.set_proxy(proxy_url)

    cl.set_country("KR")
    cl.set_country_code(82)
    cl.set_locale("ko_KR")
    cl.set_timezone_offset(9 * 3600)  # KST = UTC+9

    cl.delay_range = [1, 3]  # 모든 API 요청 사이에 1~3초 랜덤 딜레이
