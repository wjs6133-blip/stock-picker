import fs from 'fs';
import path from 'path';

export default function handler(req, res) {
  try {
    const fp   = path.join(process.cwd(), 'data/index_beater.json');
    const data = JSON.parse(fs.readFileSync(fp, 'utf-8'));
    res.setHeader('Cache-Control', 's-maxage=3600');
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.json(data);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
}
