"""
타겟 계정 좋아요 - 게시물 자동 수집 프론트엔드

주문 CSV 대신 "특정 계정의 최근 게시물"을 대상으로 삼는 진입점.
수집한 게시물을 주문 형태로 만들어서 order_processor의 병렬 엔진에 그대로 넘긴다.

좋아요 발사 로직(병렬 처리, IP 로테이션, 계정 풀, 인간 행동 시뮬레이션)은
order_processor.OrderProcessor 하나만 쓴다. 여기서 중복 구현하지 않는다.

패킷 구조:
  POST https://i.instagram.com/api/v1/media/{media_id}/like/
  Headers:
    User-Agent: Instagram ... Android (...)
    X-IG-App-ID: 567067343352427
    X-CSRF-Token: ...
    Cookie: sessionid=...; ds_user_id=...; ...
  Body (signed):
    {"media_id": "...", "module_name": "feed_timeline", ...}
"""

import logging

from order_processor import OrderProcessor

logger = logging.getLogger(__name__)

POST_URL = "https://www.instagram.com/p/{code}/"


class TargetLiker:
    """타겟 계정의 최근 게시물에 좋아요를 몰아주는 컨트롤러"""

    def __init__(self, config_path: str = "config.json"):
        self.processor = OrderProcessor(config_path)
        self.target = self.processor.config.get("target", {})

    def collect_media_urls(self, username: str, post_count: int) -> list[str]:
        """타겟 유저의 최근 게시물 URL 수집"""
        client = self.processor.open_pooled_client()
        if client is None:
            logger.error("게시물 수집용 세션이 없음")
            return []

        try:
            user_id = client.user_id_from_username(username)
            medias = client.user_medias(user_id, amount=post_count)
        except Exception as e:
            logger.error(f"타겟 '{username}' 게시물 수집 실패: {e}")
            return []

        urls = [POST_URL.format(code=m.code) for m in medias if m.code]
        logger.info(f"타겟 '@{username}' 게시물 {len(urls)}개 수집")
        return urls

    def run(self) -> dict:
        username = self.target.get("username", "").lstrip("@")
        post_count = int(self.target.get("post_count", 10))
        likes_per_post = int(self.target.get("likes_per_post", 100))

        if not username or username == "target_username":
            logger.error("config.json의 target.username을 실제 계정으로 바꿔야 함")
            return {}

        logger.info("=" * 56)
        logger.info("  타겟 좋아요 실행")
        logger.info(f"  타겟: @{username}")
        logger.info(f"  게시물: 최근 {post_count}개 / 게시물당 {likes_per_post:,}개")
        logger.info("=" * 56)

        logger.info("\n[0] xProxy 슬롯 상태 체크...")
        for name, info in self.processor.xproxy.health_check().items():
            mark = "✅" if info["status"] == "online" else "❌"
            logger.info(f"  {mark} {name}: {info['ip']}")

        urls = self.collect_media_urls(username, post_count)
        if not urls:
            logger.error("수집된 게시물 없음. 종료.")
            return {}

        self.processor.load_orders_list([
            {"user": username, "link": url, "quantity": likes_per_post}
            for url in urls
        ])

        stats = self.processor.process_all()
        self.processor.save_results("target_results.csv")
        self.processor.print_summary()
        return stats


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    TargetLiker("config.json").run()
