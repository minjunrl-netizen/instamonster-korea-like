"""
좋아요 주문 처리 시스템 - 병렬 처리 엔진

주문 형식 (CSV):
  User, Charge, Cost, Link, Start count, Quantity

병렬 구조:
  유심 N개 = 워커 스레드 N개 = 서로 다른 IP N개로 동시 발사.
  워커 1개는 자기 슬롯(유심)만 쓰고, 계정 풀에서 계정을 하나씩 받아 처리한다.

  워커 1사이클:
    계정 확보 -> 슬롯 IP 로테이션 -> 세션 로드(+프록시) -> 게시물 N개 좋아요 -> 계정 반납

  같은 계정이 같은 게시물에 두 번 좋아요를 누르는 건 불가능하므로,
  게시물별로 이미 사용한 계정을 추적해서 중복 배정을 막는다.
"""

import csv
import json
import time
import random
import logging
import re
import html
import threading
from pathlib import Path
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor

from instagrapi import Client
from instagrapi.utils.ids import InstagramIdCodec
from instagrapi.exceptions import (
    LoginRequired,
    ChallengeRequired,
    FeedbackRequired,
    PleaseWaitFewMinutes,
    MediaNotFound,
)

from xproxy_manager import XProxyManager
from human_behavior import HumanBehavior

logger = logging.getLogger(__name__)


# ─── 계정을 영구 폐기해야 하는 예외 ───
BURN_EXCEPTIONS = (LoginRequired, ChallengeRequired, FeedbackRequired)

# ─── 계정을 잠시 쉬게 하면 되는 예외 ───
COOLDOWN_EXCEPTIONS = (PleaseWaitFewMinutes,)

# ─── 이미 끝났거나 취소된 주문 상태 ───
SKIP_STATUSES = {"completed", "cancel", "canceled", "cancelled", "refunded", "done"}

# ─── 게시물 URL만 통과시킨다 ───
#
# instagrapi의 media_pk_from_url은 프로필 URL("/xxuqlsxx/")이나 순수 텍스트도
# 예외 없이 가짜 pk로 디코딩해버린다. 그대로 두면 엉뚱한 게시물에 좋아요가 나간다.
# 실제 패널 로그 3,242건 중 46건이 이런 쓰레기 링크였다.
POST_URL_RE = re.compile(
    r"instagram\.com/(?:[^/?#]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]{5,})"
)


@dataclass
class Order:
    """단일 주문"""
    order_id: int
    user: str            # 주문자
    charge: float        # 판매가
    cost: float          # 원가
    link: str            # 인스타 게시물 URL
    start_count: int     # 주문 시점 좋아요 수
    quantity: int        # 납품할 좋아요 수
    delivered: int = 0   # 처리 완료 수
    status: str = "pending"  # pending / processing / done / failed
    media_pk: str = ""


@dataclass
class MediaTask:
    """게시물 단위 작업 (같은 게시물 주문 합산)"""
    link: str
    total_likes_needed: int = 0
    media_pk: str = ""
    media_id: str = ""            # {pk}_{user_id} 완전 형식
    likes_delivered: int = 0      # 실제 성공 수
    claimed: int = 0              # 예약된 수 (진행 중 포함) — 초과 발사 방지
    liked_by: set = field(default_factory=set)   # 이미 배정된 계정 (중복 방지)
    dead: bool = False            # 게시물 삭제/비공개 등으로 처리 불가
    orders: list = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(0, self.total_likes_needed - self.claimed)


class AccountPool:
    """세션 파일 기반 계정 풀 (스레드 세이프)"""

    def __init__(self, sessions_dir: str = "sessions", max_daily_use: int = 10):
        self.sessions_dir = Path(sessions_dir)
        self.max_daily_use = max_daily_use
        self.accounts: list[dict] = []
        self._lock = threading.Lock()
        self._cursor = 0
        self._load_sessions()

    def _load_sessions(self) -> None:
        """세션 디렉토리에서 사용 가능한 계정 로드"""
        if not self.sessions_dir.exists():
            logger.error(f"세션 디렉토리 없음: {self.sessions_dir}")
            return

        for f in sorted(self.sessions_dir.glob("*.json")):
            self.accounts.append({
                "session_file": str(f),
                "username": f.stem,     # acc1.json → acc1
                "used_count": 0,
                "burned": False,        # 밴/챌린지 → 영구 제외
                "cooldown_until": 0.0,  # 레이트리밋 → 일시 제외
                "in_use": False,
            })

        logger.info(f"계정 풀: {len(self.accounts)}개 로드 (계정당 하루 {self.max_daily_use}개 좋아요)")

    def claim(self) -> dict | None:
        """
        사용 가능한 계정 1개를 원자적으로 점유한다.
        풀 전체를 한 바퀴 돌아도 없으면 None.
        """
        now = time.time()
        total = len(self.accounts)
        if total == 0:
            return None

        with self._lock:
            for _ in range(total):
                acc = self.accounts[self._cursor % total]
                self._cursor += 1

                if acc["burned"] or acc["in_use"]:
                    continue
                if acc["used_count"] >= self.max_daily_use:
                    continue
                if acc["cooldown_until"] > now:
                    continue

                acc["in_use"] = True
                return acc
        return None

    def release(self, acc: dict, likes_done: int = 0) -> None:
        with self._lock:
            acc["used_count"] += likes_done
            acc["in_use"] = False

    def burn(self, acc: dict) -> None:
        """밴/챌린지 계정 영구 제외"""
        with self._lock:
            acc["burned"] = True
            acc["in_use"] = False

    def cooldown(self, acc: dict, seconds: float = 900.0) -> None:
        """레이트리밋 계정 일시 제외"""
        with self._lock:
            acc["cooldown_until"] = time.time() + seconds
            acc["in_use"] = False

    @property
    def available_count(self) -> int:
        now = time.time()
        with self._lock:
            return sum(
                1 for a in self.accounts
                if not a["burned"]
                and a["used_count"] < self.max_daily_use
                and a["cooldown_until"] <= now
            )

    @property
    def burned_count(self) -> int:
        with self._lock:
            return sum(1 for a in self.accounts if a["burned"])


class OrderProcessor:
    """주문 큐 병렬 처리 엔진"""

    def __init__(self, config_path: str = "config.json"):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        settings = self.config.get("settings", {})
        self.likes_per_account = int(settings.get("likes_per_account", 10))
        self.delay_min = float(settings.get("delay_between_likes_min", 3))
        self.delay_max = float(settings.get("delay_between_likes_max", 10))
        self.account_gap_min = float(settings.get("delay_between_accounts_min", 1))
        self.account_gap_max = float(settings.get("delay_between_accounts_max", 5))
        self.rotate_wait = int(settings.get("ip_rotate_wait_seconds", 12))
        self.max_likes_per_post = int(settings.get("max_likes_per_post", 3000))
        self.human_sim = bool(settings.get("human_simulation", True))
        self.view_media_prob = float(settings.get("view_media_probability", 0.25))
        self.random_action_prob = float(settings.get("random_action_probability", 0.2))

        xp_cfg = self.config["xproxy"]
        self.xproxy = XProxyManager(
            host=xp_cfg["host"],
            api_port=xp_cfg["api_port"],
            proxy_type=xp_cfg.get("proxy_type", "socks5"),
            slots=xp_cfg["slots"],
            api_pattern=xp_cfg.get("api_pattern"),
            username=xp_cfg.get("username"),
            password=xp_cfg.get("password"),
        )

        self.pool = AccountPool(
            settings.get("sessions_dir", "sessions"),
            max_daily_use=self.likes_per_account,
        )

        self.orders: list[Order] = []
        self.media_tasks: dict[str, MediaTask] = {}

        self._task_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stop = threading.Event()
        self._exhausted = threading.Event()
        self._idle_streak = 0
        self.stats = {"success": 0, "fail": 0, "total_needed": 0, "accounts_used": 0}

    # ─── 주문 읽기 ───

    def load_orders_csv(self, csv_path: str) -> int:
        """CSV 파일에서 주문 읽기"""
        self.orders = []
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            for i, row in enumerate(csv.DictReader(f)):
                order = self._row_to_order(i + 1, row)
                if order:
                    self.orders.append(order)

        total = sum(o.quantity for o in self.orders)
        logger.info(f"주문 {len(self.orders)}건 로드 (총 {total:,}개 좋아요)")
        return len(self.orders)

    def load_orders_list(self, orders_data: list[dict]) -> int:
        """딕셔너리 리스트에서 주문 읽기 (API 연동용)"""
        self.orders = []
        for i, row in enumerate(orders_data):
            order = self._row_to_order(i + 1, row)
            if order:
                self.orders.append(order)

        logger.info(f"주문 {len(self.orders)}건 로드")
        return len(self.orders)

    def _row_to_order(self, order_id: int, row: dict) -> Order | None:
        """
        CSV/dict 한 줄 → Order.

        실제 패널 CSV는 취소/완료 주문과 게시물이 아닌 링크(프로필 URL, 계정명,
        해시태그 텍스트, 계좌번호)가 섞여서 내려온다. 여기서 전부 걸러낸다.
        """
        def pick(*keys, default=""):
            for k in keys:
                if k in row and row[k] not in (None, ""):
                    return row[k]
            return default

        # 1. 이미 끝났거나 취소된 주문은 건드리지 않는다
        status = str(pick("status", "Status")).strip().lower()
        if status in SKIP_STATUSES:
            return None

        # 2. 실제 패널 주문번호가 있으면 그걸 쓴다
        try:
            real_id = int(pick("id", "ID", default=order_id))
        except (TypeError, ValueError):
            real_id = order_id

        # 3. 부분 처리된 주문은 남은 수량(Remains)만 채운다
        qty = self._to_int(pick("remains", "Remains", default=None))
        if qty is None or qty <= 0:
            qty = self._to_int(pick("quantity", "Quantity", default=None))
        if qty is None:
            logger.warning(f"주문 #{real_id} 수량 파싱 실패, 스킵")
            return None
        if qty <= 0:
            return None

        # 4. 게시물 링크인지 검증 — 프로필/텍스트는 여기서 잘라낸다
        link = self._clean_url(str(pick("link", "Link")))
        if not POST_URL_RE.search(link):
            logger.warning(
                f"주문 #{real_id} 게시물 링크가 아님, 스킵: {str(pick('link', 'Link'))[:60]!r}"
            )
            return None

        if qty > self.max_likes_per_post:
            logger.warning(
                f"주문 #{real_id} 수량 {qty:,} → 상한 {self.max_likes_per_post:,}로 조정"
            )
            qty = self.max_likes_per_post

        def num(*keys) -> float:
            try:
                return float(pick(*keys, default=0) or 0)
            except (TypeError, ValueError):
                return 0.0

        return Order(
            order_id=real_id,
            user=str(pick("user", "User")),
            charge=num("charge", "Charge"),
            cost=num("cost", "Cost"),
            link=link,
            start_count=int(num("start_count", "Start count")),
            quantity=qty,
        )

    @staticmethod
    def _to_int(value) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None

    # ─── 게시물 단위로 합치기 ───

    def _build_media_tasks(self) -> None:
        """같은 게시물 주문을 합산 (게시물당 상한 적용)"""
        self.media_tasks = {}

        for order in self.orders:
            if order.status in ("done", "failed"):
                continue

            clean_url = self._clean_url(order.link)
            task = self.media_tasks.get(clean_url)
            if task is None:
                task = MediaTask(link=clean_url)
                self.media_tasks[clean_url] = task

            task.total_likes_needed += (order.quantity - order.delivered)
            task.orders.append(order)

        # 게시물당 상한 적용
        for task in self.media_tasks.values():
            if task.total_likes_needed > self.max_likes_per_post:
                logger.warning(
                    f"게시물 합산 {task.total_likes_needed:,} → 상한 {self.max_likes_per_post:,}로 조정: {task.link}"
                )
                task.total_likes_needed = self.max_likes_per_post

        total = sum(t.total_likes_needed for t in self.media_tasks.values())
        logger.info(f"게시물 {len(self.media_tasks)}개로 합산 (총 {total:,}개 좋아요)")
        for task in self.media_tasks.values():
            logger.info(f"  {task.total_likes_needed:>6,}개 ← {task.link}")

    @staticmethod
    def _clean_url(url: str) -> str:
        """HTML 엔티티 복원 + 쿼리 파라미터(igsh, img_index 등) 제거"""
        url = html.unescape(str(url)).strip()
        return url.split("?")[0].split("#")[0].rstrip("/")

    # ─── media_id 해석 (작업당 1회, 전 계정 재사용) ───

    def _resolve_media_ids(self) -> None:
        """
        게시물 URL → media_id 변환.

        media_pk는 URL 숏코드에서 로컬 디코딩되므로 네트워크가 필요 없다.
        완전 형식({pk}_{user_id})은 API 1회가 필요한데, 이걸 좋아요마다 호출하면
        요청량이 2배가 되므로 작업당 1회만 해석해서 캐시한다.
        """
        resolver = self.open_pooled_client()

        for task in self.media_tasks.values():
            # 숏코드를 직접 뽑아서 디코딩한다.
            # media_pk_from_url에 통째로 넘기면 게시물이 아닌 경로도 조용히
            # 가짜 pk로 변환되므로 절대 그대로 쓰지 않는다.
            match = POST_URL_RE.search(task.link)
            if not match:
                logger.error(f"  ❌ 게시물 URL 아님: {task.link}")
                task.dead = True
                continue

            try:
                task.media_pk = str(InstagramIdCodec.decode(match.group(1)[:11]))
            except Exception as e:
                logger.error(f"  ❌ pk 디코딩 실패: {task.link} ({e})")
                task.dead = True
                continue

            if resolver is None:
                task.media_id = task.media_pk
                logger.info(f"  ⚠️ {task.link} → pk:{task.media_pk} (완전 ID 미해석)")
                continue

            try:
                task.media_id = resolver.media_id(task.media_pk)
                logger.info(f"  ✅ {task.link} → {task.media_id}")
            except MediaNotFound:
                logger.error(f"  ❌ 게시물 없음/비공개: {task.link}")
                task.dead = True
            except Exception as e:
                task.media_id = task.media_pk
                logger.warning(f"  ⚠️ {task.link} → pk:{task.media_pk} (완전 ID 해석 실패: {e})")

        for task in self.media_tasks.values():
            for order in task.orders:
                order.media_pk = task.media_pk
                if task.dead:
                    order.status = "failed"

    def open_pooled_client(self, slot_index: int = 0) -> Client | None:
        """
        조회 전용 클라이언트 (좋아요 소모 없이 세션만 빌려 쓴다).
        media_id 해석, 타겟 게시물 수집 등에 사용한다.
        """
        acc = self.pool.claim()
        if acc is None:
            logger.warning("조회용 계정 없음")
            return None
        try:
            cl = Client()
            cl.load_settings(acc["session_file"])
            cl.set_proxy(self.xproxy.get_proxy_url(slot_index))
            cl.delay_range = [1, 3]
            return cl
        except Exception as e:
            logger.warning(f"조회용 클라이언트 준비 실패: {e}")
            return None
        finally:
            self.pool.release(acc, likes_done=0)

    # ─── 작업 배정 (스레드 세이프) ───

    def _claim_tasks(self, username: str, limit: int) -> list[MediaTask]:
        """
        계정 하나가 처리할 게시물을 원자적으로 예약한다.
        같은 게시물에는 같은 계정을 두 번 배정하지 않는다.
        """
        picked: list[MediaTask] = []
        with self._task_lock:
            for task in self.media_tasks.values():
                if len(picked) >= limit:
                    break
                if task.dead or not task.media_id:
                    continue
                if task.remaining <= 0:
                    continue
                if username in task.liked_by:
                    continue

                task.claimed += 1
                task.liked_by.add(username)
                picked.append(task)
        return picked

    def _commit(self, task: MediaTask) -> None:
        with self._task_lock:
            task.likes_delivered += 1

    def _rollback(self, task: MediaTask, username: str) -> None:
        """좋아요 실패 → 예약 취소해서 다른 계정이 가져갈 수 있게"""
        with self._task_lock:
            task.claimed = max(0, task.claimed - 1)
            task.liked_by.discard(username)

    def _kill_task(self, task: MediaTask) -> None:
        with self._task_lock:
            task.dead = True

    def _work_remaining(self) -> bool:
        with self._task_lock:
            return any(
                not t.dead and t.media_id and t.remaining > 0
                for t in self.media_tasks.values()
            )

    def _bump(self, key: str, n: int = 1) -> None:
        with self._stats_lock:
            self.stats[key] += n

    def _note_idle(self) -> bool:
        """
        계정을 하나 받았는데 배정할 게시물이 없었다.
        모든 워커를 통틀어 풀 전체를 한 바퀴 돌 만큼 헛돌면 고갈로 판정한다.
        """
        with self._stats_lock:
            self._idle_streak += 1
            return self._idle_streak >= max(len(self.pool.accounts), 1)

    def _reset_idle(self) -> None:
        with self._stats_lock:
            self._idle_streak = 0

    # ─── 워커 ───

    def _slot_worker(self, slot_index: int) -> None:
        """
        유심 슬롯 1개를 전담하는 워커.
        계정을 하나씩 받아 IP 갈고 좋아요 N개 쏘고 반납하는 걸 반복한다.
        """
        slot_name = self.xproxy.slots[slot_index].get("name", f"slot-{slot_index}")

        while not self._stop.is_set() and not self._exhausted.is_set():
            if not self._work_remaining():
                break

            acc = self.pool.claim()
            if acc is None:
                logger.warning(f"[{slot_name}] 사용 가능한 계정 없음 - 워커 종료")
                self._exhausted.set()
                break

            username = acc["username"]
            budget = self.likes_per_account - acc["used_count"]
            tasks = self._claim_tasks(username, budget) if budget > 0 else []

            if not tasks:
                self.pool.release(acc, likes_done=0)
                if not self._work_remaining():
                    break
                # 이 계정은 남은 게시물을 전부 이미 눌렀음 → 다음 계정으로.
                # 풀을 한 바퀴 다 돌아도 배정될 게 없으면 계정이 고갈된 것이다.
                if self._note_idle():
                    logger.warning(
                        f"[{slot_name}] 남은 게시물을 처리할 계정이 없음 - 워커 종료"
                    )
                    self._exhausted.set()
                    break
                continue

            self._reset_idle()

            # IP 로테이션 (계정 교체 시점에 1회)
            self.xproxy.rotate_ip(slot_index, wait_seconds=self.rotate_wait)
            proxy_url = self.xproxy.get_proxy_url(slot_index)

            client, human = self._open_client(acc, proxy_url)
            if client is None:
                for task in tasks:
                    self._rollback(task, username)
                self.pool.burn(acc)
                self._bump("fail", len(tasks))
                continue

            # 세션이 붙자마자 좋아요만 쏘는 패턴을 피한다
            if human:
                human.simulate_app_open()

            done = self._run_account(client, human, acc, tasks, slot_name)
            self.pool.release(acc, likes_done=done)
            if done:
                self._bump("accounts_used")

            self._sleep(self.account_gap_min, self.account_gap_max)

    def _open_client(self, acc: dict, proxy_url: str) -> tuple[Client | None, HumanBehavior | None]:
        """세션 파일 로드 + 프록시 적용 (로그인 호출 없음)"""
        try:
            cl = Client()
            cl.load_settings(acc["session_file"])
            cl.set_proxy(proxy_url)
            cl.delay_range = [1, 3]
            if not cl.user_id:
                logger.warning(f"  🔒 [{acc['username']}] 세션에 user_id 없음")
                return None, None
        except Exception as e:
            logger.warning(f"  🔒 [{acc['username']}] 세션 로드 실패: {e}")
            return None, None

        human = HumanBehavior(cl, acc["username"]) if self.human_sim else None
        return cl, human

    def _run_account(
        self,
        client: Client,
        human: HumanBehavior | None,
        acc: dict,
        tasks: list[MediaTask],
        slot_name: str,
    ) -> int:
        """계정 1개로 배정된 게시물들에 좋아요 발사. 성공 수 반환."""
        username = acc["username"]
        done = 0

        for i, task in enumerate(tasks):
            if self._stop.is_set():
                self._rollback(task, username)
                continue

            # 사람은 게시물을 열어보고 나서 누르기도 하고, 피드에서 바로 누르기도 한다
            if human and random.random() < self.view_media_prob:
                human.view_media_before_like(task.media_pk)

            try:
                ok = client.media_like(task.media_id)
            except BURN_EXCEPTIONS as e:
                logger.warning(f"  ⛔ [{slot_name}/{username}] 계정 폐기 ({type(e).__name__})")
                self._rollback(task, username)
                for rest in tasks[i + 1:]:
                    self._rollback(rest, username)
                self.pool.burn(acc)
                self._bump("fail")
                return done
            except COOLDOWN_EXCEPTIONS:
                logger.info(f"  ⏳ [{slot_name}/{username}] 레이트리밋 - 15분 쿨다운")
                self._rollback(task, username)
                for rest in tasks[i + 1:]:
                    self._rollback(rest, username)
                self.pool.cooldown(acc)
                self._bump("fail")
                return done
            except MediaNotFound:
                logger.error(f"  ❌ 게시물 접근 불가 → 작업 중단: {task.link}")
                self._rollback(task, username)
                self._kill_task(task)
                self._bump("fail")
                continue
            except Exception as e:
                logger.error(f"  ❌ [{slot_name}/{username}] 좋아요 실패: {e}")
                self._rollback(task, username)
                self._bump("fail")
                continue

            if ok:
                self._commit(task)
                self._bump("success")
                done += 1
                logger.info(
                    f"  ✅ [{slot_name}/{username}] "
                    f"{task.likes_delivered:,}/{task.total_likes_needed:,} ← {task.link[-22:]}"
                )
            else:
                self._rollback(task, username)
                self._bump("fail")

            if i >= len(tasks) - 1:
                continue

            # 좋아요만 연속으로 누르는 패턴을 깨기 위해 다른 행동을 섞는다
            if human and random.random() < self.random_action_prob:
                human.do_random_action()

            if human and human.should_take_break(done):
                human.take_break()

            self._sleep(self.delay_min, self.delay_max)

        return done

    def _sleep(self, lo: float, hi: float) -> None:
        """중단 신호를 존중하는 대기"""
        self._stop.wait(random.uniform(lo, hi))

    # ─── 메인 처리 루프 ───

    def process_all(self) -> dict:
        """전체 주문 병렬 처리"""
        start_time = time.time()
        self.stats = {"success": 0, "fail": 0, "total_needed": 0, "accounts_used": 0}
        self._exhausted.clear()
        self._idle_streak = 0

        self._build_media_tasks()
        if not self.media_tasks:
            logger.info("처리할 주문 없음")
            return self.stats

        if not self.pool.accounts:
            logger.error("계정 풀이 비어있음. 종료.")
            return self.stats

        logger.info("\n[1] 게시물 URL → media_id 해석...")
        self._resolve_media_ids()

        alive = [t for t in self.media_tasks.values() if not t.dead and t.media_id]
        if not alive:
            logger.error("처리 가능한 게시물 없음. 종료.")
            return self.stats

        self.stats["total_needed"] = sum(t.total_likes_needed for t in alive)

        # 용량 점검 — 게시물 1개에는 계정 1개가 1번만 좋아요 가능
        biggest = max(t.total_likes_needed for t in alive)
        usable = self.pool.available_count
        if usable < biggest:
            logger.warning(
                f"⚠️ 계정 부족: 최대 게시물 {biggest:,}개 필요 / 사용 가능 계정 {usable:,}개 "
                f"→ {biggest - usable:,}개 미납 예상"
            )

        workers = len(self.xproxy.slots)
        logger.info(
            f"\n[2] 병렬 좋아요 시작 — 유심 {workers}개 동시 발사 / "
            f"계정 {usable:,}개 / 목표 {self.stats['total_needed']:,}개"
        )

        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sim") as ex:
                futures = [ex.submit(self._slot_worker, i) for i in range(workers)]
                for fut in futures:
                    try:
                        fut.result()
                    except Exception as e:
                        logger.error(f"워커 예외: {e}", exc_info=True)
        except KeyboardInterrupt:
            logger.warning("중단 요청 - 워커 정리 중...")
            self._stop.set()
            raise
        finally:
            for task in self.media_tasks.values():
                self._update_order_status(task)

        elapsed = time.time() - start_time
        delivered = self.stats["success"]
        rate = delivered / max(elapsed, 1) * 3600

        logger.info(f"\n{'='*56}")
        logger.info("  처리 완료")
        logger.info(f"  목표:      {self.stats['total_needed']:,}개")
        logger.info(f"  성공:      {delivered:,}개")
        logger.info(f"  실패:      {self.stats['fail']:,}개")
        logger.info(f"  소요:      {elapsed/60:.1f}분 ({elapsed/3600:.2f}시간)")
        logger.info(f"  처리율:    {rate:,.0f}개/시간")
        logger.info(f"  사용 계정: {self.stats['accounts_used']:,}개")
        logger.info(f"  폐기 계정: {self.pool.burned_count:,}개")
        logger.info(f"  남은 계정: {self.pool.available_count:,}개")
        logger.info(f"{'='*56}")

        return self.stats

    def _update_order_status(self, task: MediaTask) -> None:
        """게시물 단위 납품 결과를 주문별로 분배"""
        pool = task.likes_delivered

        for order in task.orders:
            if order.status == "failed":
                continue

            needed = order.quantity - order.delivered
            if needed <= 0:
                order.status = "done"
                continue

            give = min(pool, needed)
            order.delivered += give
            pool -= give

            if order.delivered >= order.quantity:
                order.status = "done"
            elif order.delivered > 0:
                order.status = "processing"
            else:
                order.status = "pending"

    # ─── 결과 저장 ───

    def save_results(self, output_path: str = "order_results.csv") -> None:
        """처리 결과 CSV 저장"""
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["Order ID", "User", "Link", "Quantity", "Delivered", "Status", "Charge", "Cost"]
            )
            for o in self.orders:
                writer.writerow(
                    [o.order_id, o.user, o.link, o.quantity, o.delivered, o.status, o.charge, o.cost]
                )
        logger.info(f"결과 저장: {output_path}")

    def print_summary(self) -> None:
        """주문 처리 요약"""
        done = [o for o in self.orders if o.status == "done"]
        processing = [o for o in self.orders if o.status == "processing"]
        failed = [o for o in self.orders if o.status == "failed"]

        # 매출은 실제 납품 비율로 계산
        revenue = sum(o.charge * o.delivered / o.quantity for o in self.orders if o.quantity)
        cost = sum(o.cost * o.delivered / o.quantity for o in self.orders if o.quantity)

        logger.info(f"\n{'='*56}")
        logger.info("  📊 주문 처리 요약")
        logger.info(f"{'='*56}")
        logger.info(f"  전체:   {len(self.orders)}건")
        logger.info(f"  완료:   {len(done)}건 ✅")
        logger.info(f"  진행중: {len(processing)}건 🔄")
        logger.info(f"  실패:   {len(failed)}건 ❌")
        logger.info(f"  매출:   {revenue:,.0f}원")
        logger.info(f"  원가:   {cost:,.0f}원")
        logger.info(f"  이익:   {revenue - cost:,.0f}원")
        logger.info(f"{'='*56}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    processor = OrderProcessor("config.json")
    processor.load_orders_csv("orders.csv")
    processor.process_all()
    processor.save_results()
    processor.print_summary()
