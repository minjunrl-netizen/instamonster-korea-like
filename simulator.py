"""
좋아요 처리 시뮬레이션

실제 API 호출 없이 시간/계정/병목을 시뮬레이션한다.
order_processor의 병렬 엔진과 동일한 모델을 쓴다:

  - 유심 슬롯 N개가 각각 독립 워커로 동시에 돈다
  - 워커 1사이클 = 계정 1개 확보 → IP 로테이션 → 세션 로드 → 게시물 최대 K개 좋아요
  - 같은 계정은 같은 게시물에 두 번 좋아요를 누를 수 없다
    → 게시물 1개에 3,000 좋아요 = 서로 다른 계정 3,000개 필요

이 제약이 전체 처리량을 결정하므로, 계정 재사용을 허용하는 단순 계산은 항상 낙관적으로 틀린다.
"""

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

# ─── 실측 기반 시간 상수 (초) ───

IP_ROTATION = 13.0          # xProxy 로테이션 API + 새 IP 할당 대기
SESSION_LOAD = 0.5          # 세션 파일 로드 (로컬 디스크)
APP_OPEN_SIM = 9.0          # 앱 오픈 시뮬레이션 (타임라인 + 스토리 트레이)
LIKE_PACKET = 3.0           # 좋아요 패킷 + instagrapi 요청 간 딜레이
VIEW_MEDIA = 5.0            # 게시물 상세 조회 (확률 적용)
RANDOM_ACTION = 3.0         # 피드/스토리 랜덤 행동 (확률 적용)
LIKE_GAP_MIN = 3.0          # 좋아요 사이 딜레이
LIKE_GAP_MAX = 10.0
ACCOUNT_GAP = 3.0           # 계정 교체 사이 딜레이
BREAK_TIME = 75.0           # 휴식 (평균)
BREAK_EVERY = 8             # N개마다 휴식 판정
BREAK_CHANCE = 0.35

BAN_RATE = 0.003            # 좋아요 1회당 밴/챌린지 확률
OPERATING_HOURS = 18        # 하루 가동 시간


@dataclass
class SimConfig:
    account_count: int
    sim_count: int
    likes_per_account: int = 10
    human_simulation: bool = True
    view_media_prob: float = 0.25
    random_action_prob: float = 0.2

    @classmethod
    def from_config(cls, account_count: int, sim_count: int | None = None,
                    path: str = "config.json") -> "SimConfig":
        """config.json의 실제 설정값으로 시뮬레이션 파라미터 구성"""
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
        s = cfg.get("settings", {})
        return cls(
            account_count=account_count,
            sim_count=sim_count if sim_count is not None else len(cfg["xproxy"]["slots"]),
            likes_per_account=int(s.get("likes_per_account", 10)),
            human_simulation=bool(s.get("human_simulation", True)),
            view_media_prob=float(s.get("view_media_probability", 0.25)),
            random_action_prob=float(s.get("random_action_probability", 0.2)),
        )


@dataclass
class SimResult:
    total_likes: int
    delivered: int
    undelivered: int
    elapsed_sec: float
    accounts_used: int
    accounts_banned: int
    sim_count: int
    account_count: int
    orders: list = field(default_factory=list)

    @property
    def hours(self) -> float:
        return self.elapsed_sec / 3600

    @property
    def rate_per_hour(self) -> float:
        return self.delivered / max(self.hours, 1e-9)


def _cycle_time(cfg: SimConfig, like_count: int) -> float:
    """계정 1개가 게시물 like_count개를 처리하는 데 걸리는 시간"""
    t = IP_ROTATION + SESSION_LOAD + ACCOUNT_GAP

    if cfg.human_simulation:
        t += APP_OPEN_SIM

    for i in range(like_count):
        t += LIKE_PACKET
        if cfg.human_simulation:
            t += VIEW_MEDIA * cfg.view_media_prob
        if i < like_count - 1:
            t += random.uniform(LIKE_GAP_MIN, LIKE_GAP_MAX)
            if cfg.human_simulation:
                t += RANDOM_ACTION * cfg.random_action_prob
                if (i + 1) % BREAK_EVERY == 0:
                    t += BREAK_TIME * BREAK_CHANCE
    return t


def simulate(cfg: SimConfig, orders: list[dict], verbose: bool = True) -> SimResult:
    """
    주문 처리 시뮬레이션.

    orders: [{"post": "게시물ID", "quantity": 좋아요수}, ...]
    """
    # 게시물 단위로 합산 (실제 엔진의 _build_media_tasks와 동일)
    tasks: dict[str, int] = {}
    for o in orders:
        tasks[o["post"]] = tasks.get(o["post"], 0) + o["quantity"]

    needed = dict(tasks)
    delivered_per_post = {p: 0 for p in tasks}
    liked_by = {p: set() for p in tasks}   # 게시물별 사용된 계정

    accounts = [
        {"id": f"acc_{i+1:05d}", "used": 0, "banned": False}
        for i in range(cfg.account_count)
    ]

    total_needed = sum(tasks.values())
    banned_count = 0
    cursor = 0
    idle_streak = 0

    # 슬롯마다 독립 시계 — 가장 빨리 끝난 슬롯이 다음 계정을 가져간다
    clocks = [0.0] * cfg.sim_count

    if verbose:
        print(f"\n{'='*72}")
        print("  시뮬레이션")
        print(f"  계정 {cfg.account_count:,}개 | 유심 {cfg.sim_count}개 | "
              f"계정당 {cfg.likes_per_account}개 | 인간행동 {'ON' if cfg.human_simulation else 'OFF'}")
        print(f"  주문 {len(orders)}건 → 게시물 {len(tasks)}개 | 총 {total_needed:,}개 좋아요")
        print(f"{'='*72}")

    while True:
        pending = [p for p in tasks if delivered_per_post[p] < needed[p]]
        if not pending:
            break

        # 사용 가능한 계정 찾기
        acc = None
        for _ in range(cfg.account_count):
            cand = accounts[cursor % cfg.account_count]
            cursor += 1
            if cand["banned"] or cand["used"] >= cfg.likes_per_account:
                continue
            acc = cand
            break

        if acc is None:
            break  # 계정 완전 고갈

        # 이 계정이 아직 안 누른 게시물만 배정 (핵심 제약)
        budget = cfg.likes_per_account - acc["used"]
        assigned = [p for p in pending if acc["id"] not in liked_by[p]][:budget]

        if not assigned:
            idle_streak += 1
            if idle_streak >= cfg.account_count:
                break  # 남은 게시물을 처리할 계정이 없음
            continue
        idle_streak = 0

        # 가장 한가한 슬롯에 배정
        slot = min(range(cfg.sim_count), key=lambda i: clocks[i])
        clocks[slot] += _cycle_time(cfg, len(assigned))

        for post in assigned:
            if random.random() < BAN_RATE:
                acc["banned"] = True
                banned_count += 1
                break
            liked_by[post].add(acc["id"])
            delivered_per_post[post] += 1
            acc["used"] += 1

    elapsed = max(clocks) if clocks else 0.0
    delivered = sum(delivered_per_post.values())

    order_results = []
    for post, need in tasks.items():
        got = delivered_per_post[post]
        order_results.append({"post": post, "requested": need, "delivered": got})
        if verbose:
            mark = "✅" if got >= need else "⚠️"
            print(f"  {mark} {post}: {got:,}/{need:,}")

    result = SimResult(
        total_likes=total_needed,
        delivered=delivered,
        undelivered=total_needed - delivered,
        elapsed_sec=elapsed,
        accounts_used=sum(1 for a in accounts if a["used"] > 0),
        accounts_banned=banned_count,
        sim_count=cfg.sim_count,
        account_count=cfg.account_count,
        orders=order_results,
    )

    if verbose:
        print(f"{'─'*72}")
        print(f"  처리 성공:     {delivered:,}개 / {total_needed:,}개 "
              f"({delivered/max(total_needed,1)*100:.1f}%)")
        print(f"  미납:          {result.undelivered:,}개")
        print(f"  소요 시간:     {result.hours:.2f}시간")
        print(f"  시간당 처리량: {result.rate_per_hour:,.0f}개/시간")
        print(f"  계정 사용:     {result.accounts_used:,}개 / {cfg.account_count:,}개")
        print(f"  계정 밴:       {banned_count}개")
        if result.hours > OPERATING_HOURS:
            print(f"  ⚠️ 하루 {OPERATING_HOURS}시간 초과 — {result.hours/OPERATING_HOURS:.1f}일 소요")
        print(f"{'='*72}")

    return result


def main():
    print("=" * 72)
    print("  인스타몬스터 좋아요 시뮬레이터")
    print("  게시물당 상한 3,000 / 계정 4,000개 기준")
    print("=" * 72)

    # ━━━ 1. 유심 개수별: 게시물 1개에 3,000 좋아요 ━━━
    print("\n\n📌 테스트 1: 게시물 1개 × 3,000 좋아요 (주문 1건)")
    single = [{"post": "게시물_A", "quantity": 3000}]
    table1 = []
    for sims in [5, 10, 20, 30]:
        cfg = SimConfig.from_config(account_count=4000, sim_count=sims)
        r = simulate(cfg, single, verbose=False)
        table1.append((sims, r))
        print(f"  유심 {sims:>2}개 → {r.delivered:,}개 / {r.hours:>5.2f}시간 "
              f"/ {r.rate_per_hour:>6,.0f}개per시간 / 계정 {r.accounts_used:,}개 사용")

    # ━━━ 2. 인간행동 시뮬레이션 ON/OFF 비교 ━━━
    print("\n\n📌 테스트 2: 인간행동 시뮬레이션 영향 (유심 10개, 3,000개)")
    for human in [True, False]:
        cfg = SimConfig.from_config(account_count=4000, sim_count=10)
        cfg.human_simulation = human
        r = simulate(cfg, single, verbose=False)
        label = "ON (안전)" if human else "OFF (빠름)"
        print(f"  시뮬레이션 {label:<10} → {r.hours:.2f}시간 / {r.rate_per_hour:,.0f}개per시간")

    # ━━━ 3. 동시 주문 건수별 ━━━
    print("\n\n📌 테스트 3: 동시 주문 건수별 (유심 10개, 계정 4,000개, 각 3,000개)")
    for n in [1, 3, 5, 10]:
        orders = [{"post": f"게시물_{i+1}", "quantity": 3000} for i in range(n)]
        cfg = SimConfig.from_config(account_count=4000, sim_count=10)
        r = simulate(cfg, orders, verbose=False)
        days = r.hours / OPERATING_HOURS
        print(f"  주문 {n:>2}건 ({r.total_likes:>6,}개) → 성공 {r.delivered:>6,}개 "
              f"/ 미납 {r.undelivered:>5,}개 / {r.hours:>5.2f}시간 ({days:.1f}일)")

    # ━━━ 4. 실제 주문 CSV ━━━
    csv_path = Path("orders_sample.csv")
    if csv_path.exists():
        import csv as _csv
        rows = list(_csv.DictReader(csv_path.open(encoding="utf-8-sig")))
        orders = [
            {"post": r["Link"].split("?")[0].rstrip("/").split("/")[-1],
             "quantity": int(r["Quantity"])}
            for r in rows if int(r["Quantity"]) > 0
        ]
        print(f"\n\n📌 테스트 4: 실제 주문 CSV ({len(orders)}건)")
        for sims in [5, 10, 20]:
            cfg = SimConfig.from_config(account_count=4000, sim_count=sims)
            r = simulate(cfg, orders, verbose=False)
            print(f"  유심 {sims:>2}개 → {r.delivered:,}개 / {r.hours:.2f}시간 "
                  f"/ 계정 {r.accounts_used:,}개 사용")

    # ━━━ 5. 계정 수별 한계 ━━━
    print("\n\n📌 테스트 5: 계정 수가 병목이 되는 지점 (유심 10개, 게시물 1개 × 3,000)")
    for accs in [1000, 2000, 3000, 4000]:
        cfg = SimConfig.from_config(account_count=accs, sim_count=10)
        r = simulate(cfg, single, verbose=False)
        mark = "✅" if r.undelivered == 0 else "❌"
        print(f"  {mark} 계정 {accs:>5,}개 → 납품 {r.delivered:>5,}개 / 미납 {r.undelivered:>5,}개 "
              f"/ {r.hours:.2f}시간")

    print("\n" + "=" * 72)
    print("  핵심: 게시물 1개당 좋아요 N개 = 서로 다른 계정 N개 필요")
    print("        계정이 부족하면 유심을 늘려도 납품이 불가능하다")
    print("=" * 72)


if __name__ == "__main__":
    main()
