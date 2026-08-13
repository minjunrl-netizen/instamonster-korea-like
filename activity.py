"""
실시간 활동 이벤트 버스

워밍업/모니터/좋아요 엔진이 하는 모든 행동을 여기에 기록한다.
대시보드가 이걸 폴링해서 "지금 각 계정이 뭐 하는지"를 실시간으로 보여준다.

메모리 링버퍼(최근 500건) + 계정별 현재 상태.
스레드 세이프 — 여러 워커가 동시에 emit해도 안전.
"""

import threading
from collections import deque
from datetime import datetime

_lock = threading.Lock()
_events: deque = deque(maxlen=500)
_counter = [0]
_current: dict[str, dict] = {}  # 계정별 현재(마지막) 행동


def emit(account: str, action: str, detail: str = "", kind: str = "info") -> None:
    """
    행동 1건 기록.
      account - 계정 아이디 (또는 'system')
      action  - 행동 코드/이름 (예: '해시태그 검색', '릴스 시청', '좋아요')
      detail  - 상세 (예: '#맛집', '3개')
      kind    - info / like / search / warmup / monitor / error / done
    """
    with _lock:
        _counter[0] += 1
        ev = {
            "id": _counter[0],
            "time": datetime.now().isoformat(),
            "account": account,
            "action": action,
            "detail": detail,
            "kind": kind,
        }
        _events.append(ev)
        if account and account != "system":
            _current[account] = ev


def recent(since: int = 0, limit: int = 80) -> list[dict]:
    """since 이후의 이벤트 (폴링용). since=0이면 최근 limit개."""
    with _lock:
        if since <= 0:
            return list(_events)[-limit:]
        return [e for e in _events if e["id"] > since][-limit:]


def current() -> dict[str, dict]:
    """계정별 현재(마지막) 행동 — '지금 뭐 하는지' 뷰용."""
    with _lock:
        return dict(_current)


def last_id() -> int:
    with _lock:
        return _counter[0]


def clear_account(account: str) -> None:
    """계정이 끝났을 때 현재 상태에서 제거"""
    with _lock:
        _current.pop(account, None)
