// 공통 유틸 — 페이지별 로직은 각 템플릿에 있다.

async function refreshNavStats() {
  const el = document.getElementById('navStats');
  if (!el) return;
  try {
    const d = await (await fetch('/api/stats')).json();
    el.innerHTML = `준비 <b>${d.ready.toLocaleString()}</b> / 전체 <b>${d.total.toLocaleString()}</b>`;
  } catch (e) { /* 무시 */ }
}

// 작업 페이지는 자체 폴링에서 갱신하므로 제외
if (!location.pathname.startsWith('/job/')) {
  setInterval(refreshNavStats, 15000);
}
