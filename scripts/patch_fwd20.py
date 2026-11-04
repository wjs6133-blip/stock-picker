"""
기존 day JSON 파일들에 fwd20d_max_pct 필드를 추가하는 패치 스크립트
캐시된 parquet 파일에서 20거래일 내 장중 최고가를 계산해 갱신
"""
import json
import glob
import sys
from pathlib import Path

import pandas as pd

CACHE_DIR = Path("data/backtest/cache")

DIRS = [
    "data/backtest/september_2025",
    "data/backtest/2025_10",
    "data/backtest/2026_01_orig",   "data/backtest/2026_01_phase",
    "data/backtest/2026_02_orig",   "data/backtest/2026_02_phase",
    "data/backtest/2026_03_orig",   "data/backtest/2026_03_phase",
    "data/backtest/2026_04_orig",   "data/backtest/2026_04_phase",
    "data/backtest/2026_05_orig",   "data/backtest/2026_05_phase",
    "data/backtest/2026_06_orig",   "data/backtest/2026_06_phase",
    "data/backtest/2026_07_orig",   "data/backtest/2026_07_phase",
    "data/backtest/2026_08_orig",   "data/backtest/2026_08_phase",
    "data/backtest/2026_09_orig",   "data/backtest/2026_09_phase",
    "data/backtest/2026_10_orig",   "data/backtest/2026_10_phase",
]

_cache = {}

def load_ticker(ticker: str) -> pd.DataFrame | None:
    if ticker in _cache:
        return _cache[ticker]
    pq = CACHE_DIR / f"{ticker}.parquet"
    if not pq.exists():
        _cache[ticker] = None
        return None
    df = pd.read_parquet(pq)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.index = df.index.normalize()
    _cache[ticker] = df
    return df


def calc_fwd20_max(ticker: str, trade_date: str) -> float | None:
    df = load_ticker(ticker)
    if df is None:
        return None
    try:
        td = pd.Timestamp(trade_date)
        df_fwd = df[df.index >= td]
        if len(df_fwd) < 2:
            return None
        entry = float(df_fwd['Close'].iloc[0])
        # 매수 다음 날부터 20거래일까지 장중 최고가
        slice_high = df_fwd['High'].iloc[1:min(21, len(df_fwd))]
        if slice_high.empty:
            return None
        max_high = float(slice_high.max())
        return round((max_high - entry) / entry * 100, 2)
    except Exception as e:
        return None


def web_data_from_dir(dirpath: Path) -> dict:
    """day JSON들에서 web_data.json 형식으로 재생성"""
    web = {}
    day_files = sorted(dirpath.glob("20??-??-??.json"))
    for fp in day_files:
        date = fp.stem
        with open(fp, encoding="utf-8") as f:
            picks = json.load(f)
        web[date] = [
            {
                "ticker": p["ticker"],
                "name":   p["name"],
                "price":  p["current_price"],
                "f5":     p.get("fwd5d_pct"),
                "f10":    p.get("fwd10d_pct"),
                "fmax":   p.get("fwd20d_max_pct"),   # 20일 기준
            }
            for p in picks
        ]
    return web


total_updated = 0

for d in DIRS:
    dirpath = Path(d)
    if not dirpath.exists():
        continue

    day_files = sorted(dirpath.glob("20??-??-??.json"))
    if not day_files:
        continue

    dir_updated = 0
    for fp in day_files:
        trade_date = fp.stem
        with open(fp, encoding="utf-8") as f:
            picks = json.load(f)

        changed = False
        for p in picks:
            if "fwd20d_max_pct" not in p:
                val = calc_fwd20_max(p["ticker"], trade_date)
                p["fwd20d_max_pct"] = val
                changed = True

        if changed:
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(picks, f, ensure_ascii=False, separators=(",", ":"))
            dir_updated += 1

    # web_data.json 재생성
    web = web_data_from_dir(dirpath)
    if web:
        with open(dirpath / "web_data.json", "w", encoding="utf-8") as f:
            json.dump(web, f, ensure_ascii=False, separators=(",", ":"))

    total_updated += dir_updated
    print(f"[{d}] {len(day_files)}일 처리, {dir_updated}개 파일 갱신")

print(f"\n완료: 총 {total_updated}개 파일에 fwd20d_max_pct 추가")
