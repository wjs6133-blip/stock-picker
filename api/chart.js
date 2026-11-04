export default async function handler(req, res) {
  const { ticker, timeframe = 'day' } = req.query;
  if (!ticker || !/^\d{6}$/.test(ticker)) {
    return res.status(400).json({ error: 'ticker required' });
  }

  // 기간별 네이버 파라미터 — MA480 계산 가능하도록 충분히 확보
  const cfg = {
    day:   { tf: 'day',   count: 600  },   // 일봉  ~2.4년  (MA480 커버)
    week:  { tf: 'week',  count: 550  },   // 주봉  ~10년   (MA480 커버)
    month: { tf: 'month', count: 550  },   // 월봉  ~45년분 요청 (Naver 최대치)
    year:  { tf: 'month', count: 550  },   // 년봉용: 월봉 → 클라이언트 집계
  };
  const { tf, count } = cfg[timeframe] || cfg.day;

  try {
    const url = `https://fchart.stock.naver.com/sise.nhn?symbol=${ticker}&timeframe=${tf}&count=${count}&requestType=0`;
    const r = await fetch(url, {
      headers: { 'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.naver.com/' }
    });
    if (!r.ok) throw new Error(`naver ${r.status}`);
    const xml = await r.text();

    const items = [...xml.matchAll(/<item data="([^"]+)"/g)];
    const data = items.map(m => {
      const [date, open, high, low, close, volume] = m[1].split('|');
      return {
        time:   `${date.slice(0,4)}-${date.slice(4,6)}-${date.slice(6,8)}`,
        open:   +open, high: +high, low: +low, close: +close, volume: +volume
      };
    }).filter(d => d.close > 0);

    res.setHeader('Cache-Control', 's-maxage=3600, stale-while-revalidate=300');
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.json({ ticker, timeframe, data });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
}
