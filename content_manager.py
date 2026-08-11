"""
계정별 콘텐츠 관리 모듈

각 계정마다 독립적으로:
  - 프로필 사진, 이름, 바이오 세팅
  - 전용 이미지 폴더에서 사진 가져와서 포스팅
  - 전용 캡션 파일에서 원고 가져오기
  - 이미 올린 사진은 다시 안 올리도록 추적

디렉토리 구조:
  content/
  ├── acc1/
  │   ├── profile.jpg          ← 프로필 사진
  │   ├── captions.txt         ← 캡션 원고 (한 줄에 하나씩)
  │   ├── images/              ← 포스팅할 사진들
  │   │   ├── photo_001.jpg
  │   │   ├── photo_002.jpg
  │   │   └── ...
  │   └── posted.txt           ← 이미 올린 파일명 기록 (자동 생성)
  ├── acc2/
  │   ├── ...
"""

import os
import random
import logging
from pathlib import Path
from typing import Optional

from instagrapi import Client

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}


class AccountContent:
    """단일 계정의 콘텐츠 관리"""

    def __init__(self, username: str, content_config: dict):
        self.username = username
        self.image_dir = Path(content_config.get("image_dir", f"content/{username}/images"))
        self.captions_file = Path(content_config.get("captions_file", f"content/{username}/captions.txt"))
        self.profile_pic = content_config.get("profile_pic")
        self.bio = content_config.get("bio")
        self.full_name = content_config.get("full_name")

        # 이미 올린 파일 추적
        self.posted_file = self.image_dir.parent / "posted.txt"
        self._posted_set = self._load_posted()

    def _load_posted(self) -> set:
        if self.posted_file.exists():
            return set(self.posted_file.read_text(encoding="utf-8").strip().splitlines())
        return set()

    def _mark_posted(self, filename: str) -> None:
        self._posted_set.add(filename)
        self.posted_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.posted_file, "a", encoding="utf-8") as f:
            f.write(filename + "\n")

    def get_next_image(self) -> Optional[Path]:
        """아직 안 올린 이미지 하나 가져오기"""
        if not self.image_dir.exists():
            logger.warning(f"[{self.username}] 이미지 디렉토리 없음: {self.image_dir}")
            return None

        all_images = [
            f for f in self.image_dir.iterdir()
            if f.suffix.lower() in IMAGE_EXTENSIONS
        ]

        unposted = [f for f in all_images if f.name not in self._posted_set]

        if not unposted:
            if all_images:
                logger.info(f"[{self.username}] 모든 이미지를 올림, posted.txt 리셋")
                self._posted_set.clear()
                self.posted_file.write_text("", encoding="utf-8")
                unposted = all_images
            else:
                logger.warning(f"[{self.username}] 이미지가 없음: {self.image_dir}")
                return None

        # 순서대로 올리려면 sort, 랜덤이면 shuffle
        unposted.sort()
        return unposted[0]

    def get_next_video(self) -> Optional[Path]:
        """영상 파일 가져오기 (릴스/비디오용)"""
        if not self.image_dir.exists():
            return None

        videos = [
            f for f in self.image_dir.iterdir()
            if f.suffix.lower() in VIDEO_EXTENSIONS
        ]

        unposted = [f for f in videos if f.name not in self._posted_set]
        if not unposted:
            return None

        unposted.sort()
        return unposted[0]

    def get_caption(self) -> str:
        """캡션 원고 가져오기"""
        if self.captions_file.exists():
            lines = [
                line.strip()
                for line in self.captions_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            ]
            if lines:
                return random.choice(lines)

        # 기본 캡션
        return random.choice([
            "오늘도 좋은 하루 🌤️",
            "일상 기록 📸",
            "소소한 행복 ☕",
        ])

    def setup_dirs(self) -> None:
        """디렉토리 구조 자동 생성"""
        self.image_dir.mkdir(parents=True, exist_ok=True)

        if not self.captions_file.exists():
            self.captions_file.parent.mkdir(parents=True, exist_ok=True)
            self.captions_file.write_text(
                "# 한 줄에 캡션 하나씩 작성\n"
                "# #으로 시작하는 줄은 무시됨\n"
                "오늘도 좋은 하루 🌤️\n"
                "일상 기록 📸\n"
                "소소한 행복 ☕\n",
                encoding="utf-8",
            )
            logger.info(f"[{self.username}] 캡션 파일 생성: {self.captions_file}")

    def has_content(self) -> bool:
        """포스팅할 콘텐츠가 있는지"""
        return self.get_next_image() is not None


class ContentPoster:
    """계정별 콘텐츠 포스팅 실행"""

    def __init__(self, client: Client, username: str, content: AccountContent):
        self.cl = client
        self.username = username
        self.content = content

    def setup_profile(self) -> bool:
        """프로필 사진 + 이름 + 바이오 세팅"""
        changed = False

        # 프로필 사진
        if self.content.profile_pic:
            pic_path = Path(self.content.profile_pic)
            if pic_path.exists():
                try:
                    self.cl.account_change_picture(str(pic_path))
                    logger.info(f"[{self.username}] 프로필 사진 변경 완료")
                    changed = True
                except Exception as e:
                    logger.warning(f"[{self.username}] 프로필 사진 변경 실패: {e}")

        # 이름 + 바이오
        if self.content.full_name or self.content.bio:
            try:
                current = self.cl.account_info()
                new_name = self.content.full_name or current.full_name
                new_bio = self.content.bio or current.biography

                self.cl.account_edit(
                    full_name=new_name,
                    biography=new_bio,
                )
                logger.info(f"[{self.username}] 프로필 수정 완료: {new_name} / {new_bio}")
                changed = True
            except Exception as e:
                logger.warning(f"[{self.username}] 프로필 수정 실패: {e}")

        return changed

    def post_photo(self, caption: str = None) -> Optional[str]:
        """
        사진 게시물 업로드.

        내부 패킷:
          1. POST /rupload_igphoto/{upload_id} → 사진 바이트 업로드
          2. POST /api/v1/media/configure/ → 게시물 설정 (캡션, 위치 등)

        반환: 성공 시 media_id, 실패 시 None
        """
        image_path = self.content.get_next_image()
        if not image_path:
            logger.warning(f"[{self.username}] 포스팅할 이미지 없음")
            return None

        if caption is None:
            caption = self.content.get_caption()

        try:
            media = self.cl.photo_upload(str(image_path), caption)
            self.content._mark_posted(image_path.name)
            logger.info(f"[{self.username}] 📸 포스팅 완료: '{caption[:30]}...' ({image_path.name})")
            return str(media.pk)
        except Exception as e:
            logger.error(f"[{self.username}] 포스팅 실패: {e}")
            return None

    def post_reel(self, caption: str = None) -> Optional[str]:
        """
        릴스(Reels) 업로드.

        내부 패킷:
          1. POST /rupload_igvideo/{upload_id} → 영상 바이트 업로드
          2. POST /api/v1/media/configure_to_clips/ → 릴스 설정

        반환: 성공 시 media_id, 실패 시 None
        """
        video_path = self.content.get_next_video()
        if not video_path:
            logger.info(f"[{self.username}] 릴스용 영상 없음, 스킵")
            return None

        if caption is None:
            caption = self.content.get_caption()

        try:
            media = self.cl.clip_upload(str(video_path), caption)
            self.content._mark_posted(video_path.name)
            logger.info(f"[{self.username}] 🎬 릴스 업로드 완료: '{caption[:30]}...'")
            return str(media.pk)
        except Exception as e:
            logger.error(f"[{self.username}] 릴스 업로드 실패: {e}")
            return None

    def post_album(self, image_count: int = 2, caption: str = None) -> Optional[str]:
        """
        앨범(캐러셀) 업로드 - 사진 여러 장을 한 게시물로.

        반환: 성공 시 media_id, 실패 시 None
        """
        images = []
        for _ in range(image_count):
            img = self.content.get_next_image()
            if img:
                images.append(img)

        if len(images) < 2:
            logger.info(f"[{self.username}] 앨범용 이미지 부족, 단일 포스팅으로 전환")
            return self.post_photo(caption)

        if caption is None:
            caption = self.content.get_caption()

        try:
            media = self.cl.album_upload([str(p) for p in images], caption)
            for img in images:
                self.content._mark_posted(img.name)
            logger.info(f"[{self.username}] 📸📸 앨범 업로드 완료: {len(images)}장")
            return str(media.pk)
        except Exception as e:
            logger.error(f"[{self.username}] 앨범 업로드 실패: {e}")
            return None


def init_all_content_dirs(config_path: str = "config.json") -> None:
    """전체 계정의 콘텐츠 디렉토리 구조를 한 번에 생성"""
    import json
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    for acc in config["accounts"]:
        content_cfg = acc.get("content", {})
        ac = AccountContent(acc["username"], content_cfg)
        ac.setup_dirs()

    logger.info("전체 계정 콘텐츠 디렉토리 생성 완료!")
    logger.info("각 계정 폴더에 이미지와 캡션을 넣어주세요:")
    for acc in config["accounts"]:
        content_cfg = acc.get("content", {})
        img_dir = content_cfg.get("image_dir", f"content/{acc['username']}/images")
        cap_file = content_cfg.get("captions_file", f"content/{acc['username']}/captions.txt")
        logger.info(f"  @{acc['username']}:")
        logger.info(f"    이미지: {img_dir}/")
        logger.info(f"    캡션:   {cap_file}")
        if content_cfg.get("profile_pic"):
            logger.info(f"    프사:   {content_cfg['profile_pic']}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    init_all_content_dirs()
