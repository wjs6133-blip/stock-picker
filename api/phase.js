/**
 * 실시간 KOSPI + KOSDAQ MA20 기반 시장 국면 반환
 * phase: BULL | SIDE | BEAR
 */
const NAV_HDR = { 'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.naver.com/' };

async function fetchCloses(symbol, count = 35) {
  const url = `https://fchart.stock.naver.com/sise.nhn?symbol=${symbol}&timeframe=day&count=${count}&requestType=0`;
  const r   = await fetch(url, { headers: NAV_HDR });
  const xml = await r.text();
  return [...xml.matchAll(/<item data="([^"]+)"/g)]
    .map(m => parseFloat(m[1].split('|')[4]))
    .filter(v => v > 0 && !isNaN(v));
}

function calcStats(closes) {
  if (closes.length < 25) return null;
  const cur      = closes.at(-1);
  const prev5    = closes.at(-6)  ?? closes[0];
  const prev20   = closes.at(-21) ?? closes[0];
  const last20   = closes.slice(-20);
  const ma20     = last20.reduce((a, b) => a + b, 0) / 20;
  const ma20_5ago = closes.slice(-25, -5).reduce((a, b) => a + b, 0) / 20;
  return {
    cur,
    ma20,
    slope:  ma20 - ma20_5ago,
    gapPct: (cur - ma20) / ma20 * 100,
    ret5:   (cur - prev5)  / prev5  * 100,
    ret20:  (cur - prev20) / prev20 * 100,
  };
}

export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  try {
    const [ksCloses, kdCloses] = await Promise.all([
      fetchCloses('KOSPI',  35),
      fetchCloses('KOSDAQ', 35),
    ]);

    const ks = calcStats(ksCloses);
    if (!ks) return res.json({ phase: 'SIDE', label: '횡보장', emoji: '🟡', note: 'insufficient_data' });
    const kd = calcStats(kdCloses);

    // ── 국면 판단 ──
    let phase, label, emoji, guide;
    const reasons = [];

    let goal_pct;
    if (ks.gapPct > 3 && ks.slope > 0) {
      phase = 'BULL'; label = '상승장'; emoji = '🟢'; goal_pct = 8;
      guide = '10일 보유 · 깊은 눌림(-10~-20%) · 고변동 종목(4~7%) 선호 · 장중 +8% 목표';
      reasons.push(`KOSPI가 MA20 대비 +${ks.gapPct.toFixed(1)}% 위에서 거래 중`);
      if (ks.slope > 0) reasons.push(`MA20 기울기 우상향 (5일 전 대비 +${Math.round(ks.slope)}p)`);
      if (ks.ret5  > 0) reasons.push(`코스피 5일 수익률 ${ks.ret5.toFixed(1)}% 양호`);
    } else if (ks.gapPct < -3 || ks.slope < -(ks.ma20 * 0.012)) {
      phase = 'BEAR'; label = '하락장'; emoji = '🔴'; goal_pct = 3;
      guide = '단기 2~3일 장중 익절 목표 · 10일 보유 금지 · 최대 5종목 · 장중 +3% 목표';
      if (ks.gapPct < -3) reasons.push(`KOSPI가 MA20 대비 ${ks.gapPct.toFixed(1)}% 하회`);
      if (ks.slope < -(ks.ma20 * 0.012)) reasons.push(`MA20 기울기 급격히 하락 (5일 전 대비 ${Math.round(ks.slope)}p)`);
      if (ks.ret5  < 0) reasons.push(`코스피 5일 수익률 ${ks.ret5.toFixed(1)}% 약세`);
      if (ks.ret20 < 0) reasons.push(`코스피 20일 수익률 ${ks.ret20.toFixed(1)}% — 추세 하향`);
    } else {
      phase = 'SIDE'; label = '횡보장'; emoji = '🟡'; goal_pct = 5;
      guide = '5일 단기 보유 · MA20 근접 종목 · 고변동(4~7%) 집중 · 장중 +5% 목표';
      reasons.push(`KOSPI가 MA20 대비 ${ks.gapPct > 0 ? '+' : ''}${ks.gapPct.toFixed(1)}% — 뚜렷한 방향성 없음`);
      if (Math.abs(ks.slope) < ks.ma20 * 0.005) reasons.push('MA20 기울기 평탄 — 박스권 구간');
    }

    // 코스닥 모멘텀 보조 판단
    if (kd) {
      if (kd.gapPct < -3 && phase !== 'BEAR') reasons.push(`⚠️ 코스닥도 MA20 ${kd.gapPct.toFixed(1)}% 하회 — 중소형주 약세`);
      if (kd.gapPct >  3 && phase === 'BULL')  reasons.push(`코스닥도 MA20 +${kd.gapPct.toFixed(1)}% 상회 — 중소형주 동반 강세`);
    }

    res.setHeader('Cache-Control', 's-maxage=1800, stale-while-revalidate=300');
    res.json({
      phase, label, emoji, guide, goal_pct,
      reasons,
      kospi:      Math.round(ks.cur),
      kospi_ma20: Math.round(ks.ma20),
      kospi_ret5:  Math.round(ks.ret5  * 10) / 10,
      kospi_ret20: Math.round(ks.ret20 * 10) / 10,
      gap_pct:    Math.round(ks.gapPct * 10) / 10,
      slope:      Math.round(ks.slope),
      kosdaq:      kd ? Math.round(kd.cur)      : null,
      kosdaq_ma20: kd ? Math.round(kd.ma20)     : null,
      kosdaq_ret5: kd ? Math.round(kd.ret5 * 10) / 10 : null,
      kosdaq_ret20:kd ? Math.round(kd.ret20* 10) / 10 : null,
      kosdaq_gap:  kd ? Math.round(kd.gapPct* 10) / 10 : null,
    });
  } catch (e) {
    res.status(500).json({ phase: 'SIDE', label: '횡보장', emoji: '🟡', error: e.message });
  }
}
