export default async function handler(req, res) {
  const { ticker } = req.query;
  if (!ticker || !/^\d{6}$/.test(ticker)) {
    return res.status(400).json({ error: 'ticker required' });
  }

  try {
    const url = `https://fchart.stock.naver.com/sise.nhn?symbol=${ticker}&timeframe=day&count=120&requestType=0`;
    const r = await fetch(url, {
      headers: { 'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.naver.com/' }
    });
    if (!r.ok) throw new Error(`naver ${r.status}`);
    const xml = await r.text();

    const items = [...xml.matchAll(/<item data="([^"]+)"/g)];
    const data = items.map(m => {
      const [date, open, high, low, close, volume] = m[1].split('|');
      return {
        time: `${date.slice(0,4)}-${date.slice(4,6)}-${date.slice(6,8)}`,
        open: +open, high: +high, low: +low, close: +close, volume: +volume
      };
    }).filter(d => d.close > 0);

    res.setHeader('Cache-Control', 's-maxage=300, stale-while-revalidate=60');
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.json({ ticker, data });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
}
