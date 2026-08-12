"""
계정 관리 액션 — 나이 분석 / 프로필 변경 / 게시물 업로드 / 워밍업 스텝

instagrapi Client 하나를 받아서 그 계정에 대한 모든 조작을 수행한다.
세션 로드 + 프록시 세팅은 호출자가 미리 해둔 상태여야 한다.
"""

import time
import random
import logging
from pathlib import Path
from datetime import datetime

from instagrapi import Client

logger = logging.getLogger(__name__)

IMAGE_EXT = {".jpg", ".jpeg", ".png"}
VIDEO_EXT = {".mp4", ".mov"}


# ─────────────────────────── 나이/활동 분석 ───────────────────────────

def classify_age(follower: int, following: int, media: int) -> tuple[str, bool]:
    """
    팔로워/팔로잉/게시물 수로 계정 성숙도를 분류한다.

    반환: (등급, 워밍업_필요)
      fresh  - 완전 신규 (게시물 0, 활동 거의 없음) → 워밍업 필수
      young  - 초기 (게시물 몇 개, 팔로워 적음) → 짧은 워밍업 권장
      aged   - 숙성 (게시물+팔로워 있음) → 워밍업 불필요, 낮은 볼륨 바로 가능
    """
    if media == 0 and follower < 5 and following < 5:
        return "fresh", True
    if media < 3 or follower < 20:
        return "young", True
    return "aged", False


def analyze_account(cl: Client) -> dict:
    """
    로그인된 계정의 나이/활동 지표를 수집하고 등급을 매긴다.
    """
    info = cl.account_info()

    follower = int(getattr(info, "follower_count", 0) or 0)
    following = int(getattr(info, "following_count", 0) or 0)
    media = int(getattr(info, "media_count", 0) or 0)

    age_class, needs_warmup = classify_age(follower, following, media)

    return {
        "user_id": str(cl.user_id),
        "full_name": getattr(info, "full_name", "") or "",
        "biography": getattr(info, "biography", "") or "",
        "is_private": bool(getattr(info, "is_private", False)),
        "follower_count": follower,
        "following_count": following,
        "media_count": media,
        "age_class": age_class,
        "needs_warmup": needs_warmup,
        "profile_pic_url": str(getattr(info, "profile_pic_url", "") or ""),
    }


# ─────────────────────────── 프로필 변경 ───────────────────────────

class ProfileEditor:
    """계정 프로필 조작 (이름/한줄소개/아이디/프사)"""

    def __init__(self, client: Client):
        self.cl = client

    def _pause(self, lo: float = 1.5, hi: float = 4.0):
        time.sleep(random.uniform(lo, hi))

    def change_full_name(self, full_name: str) -> bool:
        """이름 변경 (표시 이름)"""
        self.cl.account_edit(full_name=full_name)
        logger.info(f"이름 변경: {full_name}")
        return True

    def change_biography(self, biography: str) -> bool:
        """한줄소개 변경"""
        self.cl.account_edit(biography=biography)
        logger.info(f"한줄소개 변경: {biography[:30]}...")
        return True

    def change_external_url(self, url: str) -> bool:
        """프로필 링크 변경"""
        self.cl.account_edit(external_url=url)
        return True

    def change_username(self, username: str) -> bool:
        """
        인스타그램 아이디(@username) 변경.

        ⚠️ 위험한 작업이다:
          - 14일에 2번까지만 가능 (인스타 제한)
          - 신규/워밍업 안 된 계정에서 하면 밴 위험 ↑
          - 원래 아이디는 다른 사람이 즉시 가져갈 수 있음
        """
        username = username.strip().lstrip("@")
        self.cl.account_edit(username=username)
        logger.info(f"아이디 변경: → @{username}")
        return True

    def change_profile_pic(self, path: str) -> bool:
        """프로필 사진 변경"""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"프사 파일 없음: {path}")
        self.cl.account_change_picture(p)
        logger.info(f"프로필 사진 변경: {p.name}")
        return True

    def apply(self, full_name: str = None, biography: str = None,
              external_url: str = None, username: str = None,
              profile_pic: str = None) -> dict:
        """
        여러 프로필 항목을 순차 변경. 각 변경 사이에 사람처럼 딜레이.
        변경 성공/실패를 항목별로 반환한다.
        """
        result = {}
        if full_name is not None:
            try:
                self.change_full_name(full_name); result["full_name"] = "ok"
            except Exception as e:
                result["full_name"] = f"실패: {e}"
            self._pause()
        if biography is not None:
            try:
                self.change_biography(biography); result["biography"] = "ok"
            except Exception as e:
                result["biography"] = f"실패: {e}"
            self._pause()
        if external_url is not None:
            try:
                self.change_external_url(external_url); result["external_url"] = "ok"
            except Exception as e:
                result["external_url"] = f"실패: {e}"
            self._pause()
        if profile_pic is not None:
            try:
                self.change_profile_pic(profile_pic); result["profile_pic"] = "ok"
            except Exception as e:
                result["profile_pic"] = f"실패: {e}"
            self._pause()
        if username is not None:
            try:
                self.change_username(username); result["username"] = "ok"
            except Exception as e:
                result["username"] = f"실패: {e}"
        return result


# ─────────────────────────── 게시물 업로드 ───────────────────────────

class PostUploader:
    """게시물 업로드 (사진1장 / 사진여러장 / 릴스 / 사진+릴스)

    업로드 전 이미지를 자동 안티탐지 처리한다:
      - EXIF 제거 (기기/GPS/편집 흔적)
      - 미세 변형 (같은 원본도 매번 다른 파일 → 중복 감지 회피)
      - 계정 기종에 맞춘 가짜 EXIF 삽입
    """

    def __init__(self, client: Client, device_model: str = None, anti_detect: bool = True):
        self.cl = client
        self.device_model = device_model
        self.anti_detect = anti_detect

    @staticmethod
    def _is_image(path: Path) -> bool:
        return path.suffix.lower() in IMAGE_EXT

    @staticmethod
    def _is_video(path: Path) -> bool:
        return path.suffix.lower() in VIDEO_EXT

    def _prep(self, paths: list[str]) -> tuple[list[str], list[str]]:
        """업로드 전 이미지 안티탐지 처리. (처리된 경로, 정리할 임시파일)"""
        if not self.anti_detect:
            return paths, []
        try:
            from image_processor import process_for_upload
            return process_for_upload(paths, device_model=self.device_model)
        except Exception as e:
            logger.warning(f"이미지 처리 실패, 원본 업로드: {e}")
            return paths, []

    @staticmethod
    def _cleanup(temps: list[str]):
        for t in temps:
            try:
                Path(t).unlink(missing_ok=True)
            except Exception:
                pass

    def upload_photo(self, path: str, caption: str = ""):
        """사진 1장 업로드"""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"파일 없음: {path}")
        if not self._is_image(p):
            raise ValueError(f"이미지 파일 아님: {p.name}")
        prepped, temps = self._prep([path])
        try:
            media = self.cl.photo_upload(Path(prepped[0]), caption)
            logger.info(f"사진 업로드 완료: {getattr(media, 'code', '')}")
            return media
        finally:
            self._cleanup(temps)

    def upload_album(self, paths: list[str], caption: str = ""):
        """
        사진 여러 장 (또는 사진+릴스 혼합) 업로드 = 캐러셀.

        instagrapi의 album_upload는 이미지/비디오 혼합을 지원한다.
        → "사진 여러장"과 "사진 + 릴스" 둘 다 이걸로 처리.
        """
        ps = [Path(x) for x in paths]
        missing = [str(p) for p in ps if not p.exists()]
        if missing:
            raise FileNotFoundError(f"파일 없음: {missing}")
        if len(ps) < 2:
            raise ValueError("앨범은 2장 이상이어야 함")
        if len(ps) > 10:
            raise ValueError("앨범은 최대 10장")

        prepped, temps = self._prep(paths)
        try:
            media = self.cl.album_upload([Path(p) for p in prepped], caption)
            kinds = ", ".join("영상" if self._is_video(Path(p)) else "사진" for p in prepped)
            logger.info(f"앨범 업로드 완료 ({len(prepped)}개: {kinds})")
            return media
        finally:
            self._cleanup(temps)

    def upload_reel(self, path: str, caption: str = "", thumbnail: str = None):
        """릴스(짧은 영상) 업로드"""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"파일 없음: {path}")
        if not self._is_video(p):
            raise ValueError(f"영상 파일 아님: {p.name}")
        thumb = Path(thumbnail) if thumbnail else None
        media = self.cl.clip_upload(p, caption, thumbnail=thumb)
        logger.info(f"릴스 업로드 완료: {getattr(media, 'code', '')}")
        return media

    def upload(self, kind: str, paths: list[str], caption: str = "", thumbnail: str = None):
        """
        종류별 업로드 디스패처.
          photo  - 사진 1장 (paths[0])
          album  - 사진 여러 장 (paths)
          reel   - 릴스 (paths[0])
          mixed  - 사진 + 릴스 혼합 앨범 (paths)
        """
        if kind == "photo":
            return self.upload_photo(paths[0], caption)
        if kind == "reel":
            return self.upload_reel(paths[0], caption, thumbnail)
        if kind in ("album", "mixed"):
            return self.upload_album(paths, caption)
        raise ValueError(f"알 수 없는 업로드 종류: {kind}")


# ─────────────────────────── 워밍업 스텝 ───────────────────────────

class WarmupStep:
    """
    하루치 워밍업 행동을 수행한다.

    스케줄 (14일 졸업, 계정 나이/일차에 따라 볼륨 증가):
      Day 1~3:   읽기만 (피드 스크롤, 스토리 보기)
      Day 4~6:   좋아요 2~3개 + 읽기
      Day 7~10:  좋아요 3~5개 + 게시물 1개(선택)
      Day 11~13: 좋아요 5~7개 + 게시물
      Day 14+:   졸업 (실전 투입 가능, 단 저볼륨부터)
    """

    GRADUATION_DAY = 14

    def __init__(self, client, human):
        self.cl = client
        self.human = human  # HumanBehavior 인스턴스

    @staticmethod
    def plan_for_day(day: int) -> dict:
        if day <= 3:
            return {"phase": "읽기만", "likes": 0, "feed": 3, "stories": 2, "can_post": False}
        if day <= 6:
            return {"phase": "초기 좋아요", "likes": random.randint(2, 3), "feed": 3, "stories": 2, "can_post": False}
        if day <= 10:
            return {"phase": "볼륨 증가", "likes": random.randint(3, 5), "feed": 4, "stories": 3, "can_post": True}
        if day < WarmupStep.GRADUATION_DAY:
            return {"phase": "실전 준비", "likes": random.randint(5, 7), "feed": 4, "stories": 3, "can_post": True}
        return {"phase": "졸업", "likes": 0, "feed": 2, "stories": 1, "can_post": False, "graduated": True}

    def run_day(self, day: int) -> dict:
        """
        하루치 워밍업 실행. 피드/스토리만 반복하지 않고
        검색·해시태그·탐색릴스·프로필훑기·팔로잉을 랜덤하게 섞어
        실제 사람이 앱 쓰는 것처럼 행동한다.
        """
        plan = self.plan_for_day(day)
        done = {"activities": 0, "likes": 0, "action_log": {}}

        # 앱 켠 것처럼 피드/스토리부터 (실제 앱 시작 동작)
        self.human.simulate_app_open()

        # 그날 행동 횟수 = 피드+스토리 계획치를 합쳐 다양한 행동으로 소비
        activity_count = plan["feed"] + plan["stories"] + random.randint(1, 3)
        for i in range(activity_count):
            action = self.human.warmup_activity()  # 검색/탐색/릴스/프로필/피드/스토리 랜덤
            done["activities"] += 1
            done["action_log"][action] = done["action_log"].get(action, 0) + 1
            self.human._human_pause(3.0, 8.0)
            # 가끔 사람처럼 잠깐 쉼
            if self.human.should_take_break(i + 1, every=5, chance=0.3):
                self.human.take_break(20, 60)

        # 좋아요는 다양한 탐색 후 자연스럽게 (타겟 아님 — 워밍업은 랜덤 피드)
        if plan["likes"] > 0:
            done["likes"] = self._like_timeline(plan["likes"])

        done["phase"] = plan["phase"]
        done["graduated"] = plan.get("graduated", False)
        return done

    def _like_timeline(self, count: int) -> int:
        """타임라인 피드에서 랜덤하게 좋아요 (워밍업용 자연 행동)"""
        liked = 0
        try:
            feed = self.cl.get_timeline_feed("pull_to_refresh")
            items = feed.get("feed_items", []) if isinstance(feed, dict) else []
            media_ids = []
            for it in items:
                media = it.get("media_or_ad") if isinstance(it, dict) else None
                if media and media.get("id"):
                    media_ids.append(media["id"])
            random.shuffle(media_ids)
            for mid in media_ids[:count]:
                try:
                    self.cl.media_like(mid)
                    liked += 1
                    self.human._human_pause(*self.human_delay())
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"워밍업 좋아요 실패(무시): {e}")
        return liked

    @staticmethod
    def human_delay() -> tuple[float, float]:
        return (8.0, 20.0)  # 워밍업은 더 느긋하게
