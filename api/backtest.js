import fs from 'fs';
import path from 'path';

const DIR_MAP = {
  'orig': {
    '2025-01': 'data/backtest/2025_01_orig',
    '2025-02': 'data/backtest/2025_02_orig',
    '2025-03': 'data/backtest/2025_03_orig',
    '2025-04': 'data/backtest/2025_04_orig',
    '2025-05': 'data/backtest/2025_05_orig',
    '2025-06': 'data/backtest/2025_06_orig',
    '2025-07': 'data/backtest/2025_07_orig',
    '2025-08': 'data/backtest/2025_08_orig',
    '2025-09': 'data/backtest/2025_09_orig',
    '2025-10': 'data/backtest/2025_10_orig',
    '2025-11': 'data/backtest/2025_11_orig',
    '2025-12': 'data/backtest/2025_12_orig',
    '2026-01': 'data/backtest/2026_01_orig',
    '2026-02': 'data/backtest/2026_02_orig',
    '2026-03': 'data/backtest/2026_03_orig',
    '2026-04': 'data/backtest/2026_04_orig',
    '2026-05': 'data/backtest/2026_05_orig',
    '2026-06': 'data/backtest/2026_06_orig',
    '2026-07': 'data/backtest/2026_07_orig',
    '2026-08': 'data/backtest/2026_08_orig',
    '2026-09': 'data/backtest/2026_09_orig',
    '2026-10': 'data/backtest/2026_10_orig',
  },
  'phase': {
    '2025-01': 'data/backtest/2025_01_phase',
    '2025-02': 'data/backtest/2025_02_phase',
    '2025-03': 'data/backtest/2025_03_phase',
    '2025-04': 'data/backtest/2025_04_phase',
    '2025-05': 'data/backtest/2025_05_phase',
    '2025-06': 'data/backtest/2025_06_phase',
    '2025-07': 'data/backtest/2025_07_phase',
    '2025-08': 'data/backtest/2025_08_phase',
    '2025-09': 'data/backtest/2025_09_phase',
    '2025-10': 'data/backtest/2025_10_phase',
    '2025-11': 'data/backtest/2025_11_phase',
    '2025-12': 'data/backtest/2025_12_phase',
    '2026-01': 'data/backtest/2026_01_phase',
    '2026-02': 'data/backtest/2026_02_phase',
    '2026-03': 'data/backtest/2026_03_phase',
    '2026-04': 'data/backtest/2026_04_phase',
    '2026-05': 'data/backtest/2026_05_phase',
    '2026-06': 'data/backtest/2026_06_phase',
    '2026-07': 'data/backtest/2026_07_phase',
    '2026-08': 'data/backtest/2026_08_phase',
    '2026-09': 'data/backtest/2026_09_phase',
    '2026-10': 'data/backtest/2026_10_phase',
  },
};

export default function handler(req, res) {
  const month = req.query.month || '2026-10';
  const mode  = req.query.mode  || 'phase';
  const map   = DIR_MAP[mode] || DIR_MAP['phase'];
  const dir   = map[month];
  if (!dir) return res.status(400).json({ error: 'unknown month' });
  try {
    const fp   = path.join(process.cwd(), dir, 'web_data.json');
    const data = JSON.parse(fs.readFileSync(fp, 'utf-8'));
    res.setHeader('Cache-Control', 's-maxage=86400');
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.json(data);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
}
