"""
실제 주문 로그 전수 분석

패널에서 내려받은 주문 CSV를 현재 엔진 기준으로 검사한다.
  - 파싱 실패/처리 불가 주문 색출
  - 일자별 물량 → 필요한 유심/계정 규모 산출
  - 게시물당 최대 좋아요 → 계정 풀 하한 산출
"""

import csv
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

POST_RE = re.compile(r"/(p|reel|reels|tv)/([A-Za-z0-9_-]{5,})")


def clean_url(url: str) -> str:
    """HTML 엔티티 복원 + 쿼리 제거"""
    url = url.replace("&amp;", "&").strip()
    return url.split("?")[0].split("#")[0].rstrip("/")


def extract_code(url: str) -> str | None:
    """게시물 숏코드 추출. 게시물 URL이 아니면 None."""
    m = POST_RE.search(clean_url(url))
    return m.group(2) if m else None


def load(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main(path: str) -> None:
    rows = load(path)
    print("=" * 74)
    print(f"  주문 로그 분석: {Path(path).name}")
    print(f"  총 {len(rows):,}건")
    print("=" * 74)

    # ─── 1. 상태 분포 ───
    status = defaultdict(int)
    for r in rows:
        status[r["Status"]] += 1
    print("\n[1] 주문 상태 분포")
    for k, v in sorted(status.items(), key=lambda x: -x[1]):
        print(f"  {k:<14} {v:>6,}건 ({v/len(rows)*100:>5.1f}%)")

    # ─── 2. 링크 유효성 ───
    print("\n[2] 링크 유효성 검사")
    bad_link, profile_link, entity_link, no_www, valid = [], [], [], [], []
    for r in rows:
        link = (r["Link"] or "").strip()
        code = extract_code(link)
        if code:
            valid.append(r)
            if "&amp;" in link:
                entity_link.append(r)
            if "instagram.com" in link and "www.instagram.com" not in link:
                no_www.append(r)
        elif "instagram.com" in link:
            profile_link.append(r)
        else:
            bad_link.append(r)

    print(f"  ✅ 정상 게시물 링크    {len(valid):>6,}건")
    print(f"  ⚠️  프로필/비게시물     {len(profile_link):>6,}건")
    print(f"  ❌ 링크 아님(쓰레기)   {len(bad_link):>6,}건")
    print(f"  ℹ️  &amp; 엔티티 포함   {len(entity_link):>6,}건")
    print(f"  ℹ️  www 없는 도메인     {len(no_www):>6,}건")

    if bad_link:
        print("\n  링크 아님 샘플:")
        for r in bad_link[:5]:
            print(f"    ID {r['ID']} [{r['Status']}] {r['Link'][:60]!r}")
    if profile_link:
        print("\n  프로필 링크 샘플:")
        for r in profile_link[:5]:
            print(f"    ID {r['ID']} [{r['Status']}] {r['Link'][:60]}")

    # 처리 대상: 취소가 아니고 링크가 유효한 것
    live = [r for r in rows if r["Status"] != "Cancel" and extract_code(r["Link"])]
    dropped = [r for r in rows if r not in live]
    print(f"\n  → 실제 처리 대상 {len(live):,}건 / 제외 {len(rows)-len(live):,}건")

    # ─── 3. 수량 분포 ───
    qty = [int(r["Quantity"]) for r in live if r["Quantity"]]
    qty_sorted = sorted(qty)
    print("\n[3] 주문 수량 분포")
    print(f"  최소 {min(qty):,} / 중앙값 {qty_sorted[len(qty)//2]:,} / 평균 {sum(qty)/len(qty):,.0f} / 최대 {max(qty):,}")
    print(f"  총 좋아요 {sum(qty):,}개")
    over = [q for q in qty if q > 3000]
    print(f"  3,000 초과 주문: {len(over)}건" + (f" (최대 {max(over):,})" if over else ""))

    buckets = [(0, 50), (50, 100), (100, 300), (300, 1000), (1000, 3000), (3000, 10**9)]
    for lo, hi in buckets:
        n = sum(1 for q in qty if lo < q <= hi)
        label = f"{lo+1:,}~{hi:,}" if hi < 10**9 else f"{lo+1:,}+"
        print(f"    {label:<14} {n:>6,}건")

    # ─── 4. 게시물 단위 합산 (엔진의 _build_media_tasks 재현) ───
    per_post = defaultdict(int)
    per_post_orders = defaultdict(int)
    for r in live:
        code = extract_code(r["Link"])
        per_post[code] += int(r["Quantity"])
        per_post_orders[code] += 1

    print("\n[4] 게시물 단위 합산 (같은 게시물 주문 병합)")
    print(f"  주문 {len(live):,}건 → 게시물 {len(per_post):,}개")
    print(f"  게시물당 최대 좋아요: {max(per_post.values()):,}개")
    print(f"  → 계정 풀 하한: {max(per_post.values()):,}개 (한 게시물에 계정 1개는 1번만)")

    top = sorted(per_post.items(), key=lambda x: -x[1])[:10]
    print("\n  최다 좋아요 게시물 TOP 10:")
    for code, total in top:
        capped = min(total, 3000)
        mark = " ← 3,000 상한 적용" if total > 3000 else ""
        print(f"    {code:<14} {per_post_orders[code]:>3}건 합산 {total:>6,}개 → 처리 {capped:>5,}개{mark}")

    # ─── 5. 일자별 물량 ───
    daily = defaultdict(lambda: {"orders": 0, "likes": 0, "posts": set()})
    for r in live:
        day = r["Created"][:10]
        code = extract_code(r["Link"])
        daily[day]["orders"] += 1
        daily[day]["likes"] += int(r["Quantity"])
        daily[day]["posts"].add(code)

    days = sorted(daily)
    print(f"\n[5] 일자별 물량 ({days[0]} ~ {days[-1]}, {len(days)}일)")
    likes_per_day = [daily[d]["likes"] for d in days]
    print(f"  일 평균 {sum(likes_per_day)/len(days):,.0f}개 / 최대 {max(likes_per_day):,}개 / 최소 {min(likes_per_day):,}개")

    peak_day = max(days, key=lambda d: daily[d]["likes"])
    p = daily[peak_day]
    print(f"  피크: {peak_day} — 주문 {p['orders']}건 / 게시물 {len(p['posts'])}개 / 좋아요 {p['likes']:,}개")

    print("\n  상위 5일:")
    for d in sorted(days, key=lambda x: -daily[x]["likes"])[:5]:
        v = daily[d]
        print(f"    {d}  주문 {v['orders']:>3}건  게시물 {len(v['posts']):>3}개  좋아요 {v['likes']:>6,}개")

    # ─── 6. 처리 능력 판정 ───
    print("\n[6] 처리 능력 판정")
    # 검증된 실측: 유심 10개 = 1,206 likes/hour (human_simulation ON)
    RATE_PER_SIM_HOUR = 120.6
    OPERATING_HOURS = 18

    avg = sum(likes_per_day) / len(days)
    peak = max(likes_per_day)

    print(f"  {'유심':<6}{'시간당':>9}{'18h 처리량':>12}{'평균일 소화':>14}{'피크일 소화':>14}")
    print("  " + "-" * 55)
    for sims in [5, 10, 20, 30, 50]:
        cap = RATE_PER_SIM_HOUR * sims * OPERATING_HOURS
        a = "✅" if cap >= avg else "❌"
        pk = "✅" if cap >= peak else "❌"
        print(f"  {sims:<6}{RATE_PER_SIM_HOUR*sims:>9,.0f}{cap:>12,.0f}{a:>10}{a and '':<0}{pk:>14}")

    print(f"\n  일 평균 물량 {avg:,.0f}개 / 피크 {peak:,}개")
    need_avg = avg / (RATE_PER_SIM_HOUR * OPERATING_HOURS)
    need_peak = peak / (RATE_PER_SIM_HOUR * OPERATING_HOURS)
    print(f"  → 평균 소화 최소 유심 {need_avg:.1f}개, 피크 소화 최소 유심 {need_peak:.1f}개")

    # 계정 요구량: 하루 좋아요 / 계정당 10개, 단 단일 게시물 최대치가 하한
    max_post = max(per_post.values())
    daily_post_max = max(
        max(cnt for cnt in
            [sum(int(r["Quantity"]) for r in live
                 if r["Created"][:10] == d and extract_code(r["Link"]) == c)
             for c in daily[d]["posts"]])
        for d in [peak_day]
    )
    print(f"\n  계정 요구량:")
    print(f"    피크일 총 좋아요 {peak:,}개 ÷ 계정당 10개 = {peak/10:,.0f}개 (재사용 기준)")
    print(f"    피크일 단일 게시물 최대 {daily_post_max:,}개 → 계정 {daily_post_max:,}개 필요 (중복 불가)")
    print(f"    → 필요 계정: {max(peak/10, daily_post_max):,.0f}개 + 밴 여유분 30%"
          f" = 약 {max(peak/10, daily_post_max)*1.3:,.0f}개")

    # ─── 7. 현재 파서가 놓치는 것 ───
    print("\n[7] 현재 엔진이 처리 못 하는 주문")
    problems = []
    for r in rows:
        link = (r["Link"] or "").strip()
        if r["Status"] == "Cancel":
            continue
        if not extract_code(link):
            problems.append((r["ID"], r["Status"], link[:50]))
    if problems:
        print(f"  ❌ 취소가 아닌데 링크가 깨진 주문 {len(problems)}건:")
        for pid, st, ln in problems[:10]:
            print(f"    ID {pid} [{st}] {ln!r}")
    else:
        print("  ✅ 취소 건을 빼면 전부 정상 파싱됨")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "orders.csv")
