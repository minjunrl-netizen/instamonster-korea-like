# 인스타몬스터 — 한국인 좋아요 자동화 시스템

인스타그램 계정을 대량으로 관리하고, 주문받은 게시물에 "리얼 한국인 좋아요"를 병렬로 발사하는 시스템.
계정 4,000개 + xProxy 유심 10개 규모 운영을 목표로 설계됨.

## 전체 구조

```
[웹 관리 서버]  server.py  ──  http://localhost:8000
      │
      ├─ 계정 등록/관리      db.py (SQLite)
      ├─ 대량 로그인         bulk_login.py  (유심 슬롯별 병렬)
      ├─ 계정 진단           /diagnosis     (죽은 계정 감지 + 교체)
      └─ 2FA 자동 해제       TOTP 시드 + 백업코드

[좋아요 발사 엔진]  order_processor.py  ── 병렬 처리 (유심 = 워커)
      │
      ├─ 주문 CSV 파싱       패널 주문 로그 그대로 입력
      ├─ 게시물 단위 합산     같은 게시물 주문 병합
      ├─ IP 로테이션         xProxy API
      └─ 인간 행동 시뮬       human_behavior.py

[인프라]
      xproxy_manager.py     유심 슬롯 IP 로테이션 + 누출 방지
      devices.py            한국 디바이스 18종 가상화
      account_warmer.py     신규 계정 21일 워밍업
```

## 파일별 역할

| 파일 | 역할 |
|------|------|
| `server.py` | FastAPI 웹 서버 (대시보드/계정/진단/로그인) |
| `db.py` | SQLite — 계정 상태/슬롯고정/2FA시드/디바이스/사용량 |
| `bulk_login.py` | 유심 슬롯별 병렬 로그인 엔진 + 2FA 자동 해제 |
| `order_processor.py` | 좋아요 병렬 발사 엔진 (검증 완료) |
| `xproxy_manager.py` | xProxy 유심 IP 로테이션 + IP 누출 3중 방어 |
| `devices.py` | 갤럭시 S24/A54 등 한국 기종 18종 가중치 배정 |
| `human_behavior.py` | 안티탐지 (앱오픈/피드/스토리 시뮬레이션) |
| `account_warmer.py` | 신규 계정 워밍업 스케줄러 |
| `insta_liker.py` | 타겟 계정 게시물 자동 수집 → 엔진 투입 |
| `simulator.py` | 처리량/시간 시뮬레이션 (실측 모델) |
| `analyze_orders.py` | 주문 CSV 전수 분석 |
| `login_test.py` / `run_login_test.py` | ADB 비행기모드 토글 로그인 테스터 |
| `test_*.py` | 회귀 테스트 (19개 전부 통과) |

## 설치 & 실행

```bash
pip install -r requirements.txt

# 웹 서버
python server.py            # http://localhost:8000

# 실기기 로그인 테스트 (ADB 비행기모드 자동 토글)
python run_login_test.py    # login_accounts.txt 필요

# 테스트
python test_server.py       # 웹 + 로그인 10/10
python test_parallel.py     # 좋아요 엔진 9/9
```

## 설정 (config.json)

```jsonc
{
  "xproxy": {
    "host": "192.168.0.100",   // 실제 xProxy 장비 IP로 교체
    "api_port": 8080,
    "api_pattern": null,        // 대시보드에서 확인한 실제 API 경로
    "slots": [ /* 유심 10개 포트 */ ]
  },
  "settings": {
    "likes_per_account": 10,    // 계정당 하루 좋아요
    "max_likes_per_post": 3000, // 게시물당 상한
    "human_simulation": true
  },
  "login": {
    "request_timeout": 15,
    "cooldown_on_429_seconds": 90
  }
}
```

---

## 지금까지의 작업 기록 (핵심 의사결정 + 발견)

### 1. 병렬 좋아요 엔진 (순차 → 병렬)
- 유심 = 워커 스레드 1개씩 전담 → 10개 IP 동시 발사
- 실측: 유심 10개로 3,000 좋아요 ≈ **2.5시간** (인간행동 시뮬 포함)
- 계정 4,000개면 하루 10건(3만 좋아요)까지 소화

### 2. 잡은 버그들
- **워커 무한 루프** — 계정 소진 후 남은 주문 있으면 영원히 헛돎 → `_note_idle` 고갈 판정
- **`media_id()` 좋아요마다 호출** — 요청량 2배 → 게시물당 1회 해석 캐시
- **같은 계정 중복 배정** — 원자적 claim/release로 교체
- **모든 실패 = 계정 영구 폐기** — 밴만 폐기, 레이트리밋은 쿨다운으로 분리
- **`media_pk_from_url` 함정** — 프로필 URL/텍스트를 조용히 가짜 pk로 변환 → 정규식 검증
- **account_warmer.py 문법 오류** (고아 `]`) — 파일 실행 불가였음

### 3. 실제 주문 로그 분석 (3,242건 / 30일)
- 처리 대상 3,180건, 총 254,557 좋아요, 일평균 8,212 / 피크 15,454
- **3,000 상한 실제로 안 걸림** (최대 주문 1,000)
- 링크 46건이 프로필/쓰레기 → 정규식으로 차단
- 완료/취소 3,169건은 상태 컬럼으로 자동 제외
- 필요 계정: 약 2,000개 (밴 여유 포함 4,000개 적정)

### 4. 로컬 웹 관리 서버 (FastAPI + SQLite)
- 계정 4,000개는 CLI 관리 불가 → 웹 UI
- 계정 등록 시 유심 슬롯 자동 균등 배정 + **슬롯 영구 고정** (계정-IP 대역 일관성)
- 대량 로그인 실시간 진행률 + 중단 + 로그

### 5. 프록시 IP 누출 3중 방어
- `instagrapi.set_proxy`는 빈 값 받으면 프록시를 '해제' → 집 IP 유출 위험
- (1) URL 생성 단계 빈값 거부 (2) 작업 전 프리플라이트 (실제 IP 대조) (3) 계정별 세션 검증
- **fail-closed** — 확인 불가면 차단

### 6. 2FA 자동 해제
- TOTP 시드 저장 → 로그인 시 코드 자체 생성 → 자동 통과 (무한 재사용)
- 백업코드 폴백 (1회용, 성공 시만 소모)
- **함정: `totp_generate_code('')`가 빈 시드로도 코드 생성** → base32 검증기로 차단
- 입력: `아이디:비번:2FA시드:백업코드`

### 7. 디바이스 가상화 (18종)
- 4,000계정이 전부 Pixel 8 Pro면 비현실적 → 한국 기종 18종 가중치 배정
- 삼성 86% / 구글 3% (한국 시장 점유율 반영)
- 계정 등록 시 배정, **세션에 저장 → 재로그인 시 복원** (한번 정해진 폰 불변)

### 8. 계정 진단 + 교체
- 상태 세분화: `not_exist`(삭제), `rate_limit`(일시), `bad_pw`, `banned`, `challenge`, `2fa`
- "계정을 찾을 수 없습니다" → `not_exist`로 분류 (재시도 무의미)
- 죽은 계정 → 새 계정 교체 (유심 슬롯 유지) — 개별/일괄

### 9. 실기기 로그인 테스트 (ADB 비행기모드)
- USB 테더링 + ADB로 비행기모드 자동 토글 → 계정마다 IP 변경 (3초)
- 가정망 IP 유출 방지 (테더링 우선 라우팅 확인)

### 10. 🔴 429 재시도 폭탄 (중대 발견)
- instagrapi 기본값 `session_retry_statuses=[429,...]` → 로그인마다 429를 3번 재시도
- 이 재시도 폭탄이 **통신사 대역 전체를 차단**시키는 주범
- **해결: `make_client()`** — 429를 재시도에서 제거, instagrapi 내부 "Ignore 429"에 위임
- 효과: `launcher/sync [429] → RetryError` (로그인 불가) → `[200]` (정상)

### 11. 네이티브 챌린지 (우회 불가 결론)
- 최신 챌린지는 `{"action":"close"}` 반환 → API/웹HTTP로 코드 경로 없음 (실측)
- 프라이빗 API choice=email/sms 전부 "close" / 웹 엔드포인트 404 (JS 필요)
- **결론: 순수 코드 우회 불가.** 브라우저 자동화(무거움+이메일필요) 또는 앱 수동 해결
- **정답: 2FA 켜진 계정을 쓰면 챌린지 대신 2FA 경로로 감** → 시드로 자동 통과

### 계정 구매 기준 (핵심 교훈)
```
최상급:  세션(.json) 포함        → 로그인 스킵, 챌린지 없음
상급:    2FA 시드 + 이메일 포함  → 챌린지 회피 + 코드 자동 해결
중급:    2FA 시드만              → 네이티브 챌린지 회피
하급:    아이디/비번만           → 챌린지 뜨면 못 살림
```
- 한국인 좋아요 사업 → **한국 계정(KR) 필수** (지역 IP-계정 매칭)
- SMM 패널(JAP/Peakerr)은 좋아요 서비스지 계정 마켓 아님
- 계정 마켓: lolz.live(러시아), accsmarket, accfarm

---

## 남은 작업 (TODO)

- [ ] 이메일 IMAP 코드 챌린지 자동 해결 핸들러 (코드 방식 챌린지용)
- [ ] xProxy 실장비 연결 후 검증 (준비 도구 완성 2026-08-12)
      1. `python xproxy_setup.py scan`     → 랜에서 장비 IP 찾기
      2. config.json의 host/api_port 수정
      3. `python xproxy_setup.py probe`    → 슬롯 포트 도달 확인
      4. `python xproxy_setup.py apicheck` → IP 로테이션 API 경로 자동 감지 → api_pattern 채우기
      5. `python test_xproxy.py`           → 슬롯 온라인/고유IP/누출없음/로테이션 최종 검증
      6. 대시보드 → "xProxy 장비" 토글 → "연결 상태 확인"
- [ ] 실 계정 구매 → `/diagnosis`로 생존율 검증 → 대량 투입
- [x] **좋아요 엔진 ↔ DB 계정풀 연동** (완료 2026-08-12)
      - order_processor가 DB의 ready 계정을 읽음 (세션 글롭은 폴백)
      - 계정이 고정된 proxy_slot으로만 발사 → 계정-IP 대역 일관성
      - likes_today를 DB에 영구 기록 → 재시작해도 일일 한도 유지
      - 밴/스팸 감지 시 DB 상태를 banned로 자동 갱신
      - 검증: test_parallel [10][11] (슬롯고정/카운터영구/밴갱신)

## 보안 주의

- `login_accounts.txt`, `sessions/`, `*.db`, `login_test_results.json`은 **절대 커밋 금지** (.gitignore 처리됨)
- `config.json`은 플레이스홀더만 있음 (실제 xProxy IP/계정은 로컬에서 교체)
