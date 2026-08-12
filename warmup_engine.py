"""
워밍업 엔진 — 신규 계정을 실제 한국인처럼 성장시킨다

매일 1회 실행:
  워밍업 중인 계정마다
    1. 세션+프록시로 접속
    2. 오늘 일차에 맞는 워밍업 (피드 읽기, 스토리 보기, 낮은 볼륨 좋아요)
    3. 포스팅 날이면 게시물 자동 업로드 (3~5일에 1개, 종류 랜덤)
    4. 일차 진행 / Day 21 되면 졸업(ready)

포스팅 전략 (실제 한국인 활발 사용자):
  - Day 1~3: 포스팅 없음 (읽기만)
  - Day 4~:  3~5일에 1개, 종류 랜덤(사진40/앨범25/릴스25/혼합10)
  - 업로드 전 이미지 EXIF 제거 + 미세 변형 (계정 기종 맞춤)
"""

import random
import logging
import threading
from pathlib import Path
from datetime import datetime

import db
from bulk_login import make_client
from xproxy_manager import make_provider
from human_behavior import HumanBehavior
from account_actions import WarmupStep, PostUploader

logger = logging.getLogger(__name__)

IMAGE_EXT = {".jpg", ".jpeg", ".png"}
VIDEO_EXT = {".mp4", ".mov"}

# 포스팅 종류 가중치 (실제 사용자 분포)
POST_KINDS = ["photo", "album", "reel", "mixed"]
POST_WEIGHTS = [40, 25, 25, 10]

# 포스팅 간격 (일)
POST_GAP_MIN = 3
POST_GAP_MAX = 5


class ContentPicker:
    """계정별 콘텐츠 폴더에서 업로드할 파일을 고른다"""

    def __init__(self, username: str, base_dir: str = "content"):
        self.dir = Path(base_dir) / username
        self.images = self._scan(IMAGE_EXT)
        self.videos = self._scan(VIDEO_EXT)
        self.captions = self._load_captions()

    def _scan(self, exts: set) -> list[Path]:
        found = []
        for sub in (self.dir, self.dir / "images", self.dir / "videos"):
            if sub.exists():
                found += [f for f in sub.iterdir()
                          if f.is_file() and f.suffix.lower() in exts]
        return sorted(set(found))

    def _load_captions(self) -> list[str]:
        for name in ("captions.txt", "caption.txt"):
            f = self.dir / name
            if f.exists():
                lines = [l.strip() for l in f.read_text(encoding="utf-8").splitlines()
                         if l.strip() and not l.startswith("#")]
                if lines:
                    return lines
        return [""]

    def has_content(self) -> bool:
        return bool(self.images or self.videos)

    def pick(self) -> tuple[str, list[str], str] | None:
        """
        (종류, 파일경로들, 캡션) 선택. 콘텐츠 없으면 None.
        가진 콘텐츠에 맞춰 가능한 종류만 뽑는다.
        """
        available = []
        if self.images:
            available.append("photo")
        if len(self.images) >= 2:
            available.append("album")
        if self.videos:
            available.append("reel")
        if self.images and self.videos:
            available.append("mixed")
        if not available:
            return None

        weights = [POST_WEIGHTS[POST_KINDS.index(k)] for k in available]
        kind = random.choices(available, weights=weights, k=1)[0]
        caption = random.choice(self.captions)

        if kind == "photo":
            return kind, [str(random.choice(self.images))], caption
        if kind == "reel":
            return kind, [str(random.choice(self.videos))], caption
        if kind == "album":
            n = min(random.randint(2, 4), len(self.images))
            return kind, [str(p) for p in random.sample(self.images, n)], caption
        if kind == "mixed":
            imgs = random.sample(self.images, min(2, len(self.images)))
            vid = random.choice(self.videos)
            files = [str(p) for p in imgs] + [str(vid)]
            random.shuffle(files)
            return kind, files, caption
        return None


def should_post_today(acc: dict) -> bool:
    """
    오늘 포스팅해야 하는지 판단.
      - 워밍업 Day 4 이상
      - 마지막 포스팅 후 POST_GAP_MIN~MAX일 지났거나 아직 한 번도 안 함
    """
    day = int(acc.get("warmup_day", 1) or 1)
    if day < 4:
        return False

    last_post = acc.get("last_post")
    if not last_post:
        return True  # 아직 한 번도 안 올림 → 첫 포스팅

    try:
        last = datetime.fromisoformat(last_post)
    except Exception:
        return True
    gap = (datetime.now() - last).days
    threshold = random.randint(POST_GAP_MIN, POST_GAP_MAX)
    return gap >= threshold


class WarmupEngine:
    """워밍업 배치 실행기"""

    def __init__(self, config_path: str = "config.json"):
        import json
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)
        self.provider = make_provider(self.config)
        self.sessions_dir = Path(self.config.get("settings", {}).get("sessions_dir", "sessions"))

        s = self.config.get("settings", {})
        self.rotate_wait = int(self.config.get("login", {}).get("ip_rotate_wait_seconds", 12))

        self._stop = threading.Event()
        self.job_id: int | None = None
        self.counts = {"warmed": 0, "posted": 0, "graduated": 0, "failed": 0}

    def stop(self):
        self._stop.set()

    def _open(self, acc: dict, slot: int):
        """계정 세션+프록시로 클라이언트 연다"""
        cl = make_client(request_timeout=30)
        cl.load_settings(acc["session_file"])
        if not getattr(self.provider, "is_direct", False):
            cl.set_proxy(self.provider.get_proxy_url(slot))
        else:
            cl.set_country("KR"); cl.set_locale("ko_KR"); cl.set_timezone_offset(9 * 3600)
        cl.delay_range = [2, 5]
        if not cl.user_id:
            raise RuntimeError("세션 만료")
        return cl

    def _log(self, level: str, msg: str):
        logger.info(msg)
        if self.job_id:
            db.log_event(self.job_id, level, msg)

    def run(self, job_id: int | None = None) -> dict:
        """워밍업 대상 계정 전체를 하루치 진행"""
        self.job_id = job_id
        self._stop.clear()
        self.counts = {"warmed": 0, "posted": 0, "graduated": 0, "failed": 0}

        accounts = db.warming_accounts(due_only=True)
        if not accounts:
            self._log("info", "오늘 워밍업할 계정이 없다 (전부 완료했거나 워밍업 중인 계정 없음)")
            return self.counts

        self._log("info", f"워밍업 시작 — 대상 {len(accounts)}개 계정")

        for acc in accounts:
            if self._stop.is_set():
                break
            username = acc["username"]
            slot = acc.get("proxy_slot") or 0
            day = int(acc.get("warmup_day", 1) or 1)

            try:
                # IP 로테이션 후 접속
                self.provider.rotate_ip(slot, wait_seconds=self.rotate_wait)
                cl = self._open(acc, slot)
                human = HumanBehavior(cl, username)

                # 하루치 워밍업
                step = WarmupStep(cl, human)
                result = step.run_day(day)
                self.counts["warmed"] += 1
                self._log("info",
                          f"🌱 [{username}] Day{day} {result['phase']} — "
                          f"피드{result['feed_browses']} 스토리{result['story_views']} 좋아요{result['likes']}")

                # 포스팅 날이면 자동 업로드
                if should_post_today(acc):
                    self._auto_post(cl, acc, human)

                # 일차 진행 / 졸업
                graduated = result.get("graduated", False) or day >= WarmupStep.GRADUATION_DAY
                db.advance_warmup(username, graduated=graduated)
                if graduated:
                    self.counts["graduated"] += 1
                    self._log("info", f"🎓 [{username}] 워밍업 졸업 → 실전 투입 가능(ready)")

            except Exception as e:
                self.counts["failed"] += 1
                self._log("warn", f"⚠️ [{username}] 워밍업 실패: {e}")

            if self.job_id:
                db.update_job(self.job_id, processed=sum(
                    v for k, v in self.counts.items() if k in ("warmed", "failed")))

        self._log("info",
                  f"워밍업 완료 — 진행 {self.counts['warmed']} / 포스팅 {self.counts['posted']} / "
                  f"졸업 {self.counts['graduated']} / 실패 {self.counts['failed']}")
        return self.counts

    def _auto_post(self, cl, acc: dict, human):
        """콘텐츠 폴더에서 뽑아 자동 포스팅"""
        username = acc["username"]
        picker = ContentPicker(username)
        if not picker.has_content():
            self._log("warn", f"  📭 [{username}] 콘텐츠 폴더 비어있음 (content/{username}/) - 포스팅 스킵")
            return

        picked = picker.pick()
        if not picked:
            return
        kind, files, caption = picked

        # 포스팅 전 잠깐 앱 쓰는 척
        human._human_pause(5.0, 15.0)

        try:
            uploader = PostUploader(cl, device_model=acc.get("device_model"))
            media = uploader.upload(kind, files, caption)
            db.record_post(username)
            self.counts["posted"] += 1
            self._log("info",
                      f"  📸 [{username}] {kind} 포스팅 완료: {getattr(media, 'code', '')}")
        except Exception as e:
            self._log("warn", f"  ❌ [{username}] 포스팅 실패: {e}")
