"""
지수역행 강한 종목 스캐너 (Index Beater Scanner)

시그널 조건 (동시 충족):
  1. KOSPI 지수가 하락한 날
  2. 해당 종목 등락률 +5% 이상
  3. 해당 종목 거래대금 2,000억원 이상

매수 대기 조건:
  - 시그널 발생 후 현재 가격이 시그널일 고가 대비 -3% ~ -35% (눌림목 구간)

출력: data/index_beater.json
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

CACHE_DIR   = ROOT / "data/backtest/cache"
OUTPUT      = ROOT / "data/index_beater.json"

SCAN_DAYS   = 90      # 최근 90 거래일에서 시그널 탐색
MIN_CHANGE  = 0.05    # +5% 이상 양봉
MIN_AMOUNT  = 2e11    # 거래대금 2,000억
PULL_MIN    = -0.35   # 눌림목 최대 -35%
PULL_MAX    = -0.02   # 눌림목 최소 -2%
MAX_RESULTS = 60      # 최대 출력 수


def main():
    import FinanceDataReader as fdr

    end   = datetime.now()
    start = end - timedelta(days=SCAN_DAYS * 2)

    # 1. KOSPI 하락일 목록
    print("KOSPI 데이터 로딩...")
    kospi = fdr.DataReader('KS11', start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
    kospi.index = pd.to_datetime(kospi.index).tz_localize(None).normalize()
    kospi_down = set(kospi.index[kospi['Change'] < 0].strftime('%Y-%m-%d'))
    kospi_change_map = {
        d.strftime('%Y-%m-%d'): round(float(r['Change']) * 100, 2)
        for d, r in kospi.iterrows()
    }
    print(f"  → 하락일 {len(kospi_down)}개 확인")

    # 2. 종목명 맵
    print("종목 목록 로딩...")
    listing  = fdr.StockListing('KRX')
    code_col = next((c for c in ['Code', 'Symbol', 'ISU_SRT_CD'] if c in listing.columns), listing.columns[0])
    name_col = next((c for c in ['Name', 'CompanyName', 'ISU_NM']  if c in listing.columns), listing.columns[1])
    name_map = dict(zip(listing[code_col].astype(str), listing[name_col].astype(str)))

    # 3. 캐시 종목 순회
    cache_files = sorted(CACHE_DIR.glob("*.parquet"))
    print(f"캐시 종목 {len(cache_files)}개 스캔 중...")

    cutoff   = pd.Timestamp(end - timedelta(days=SCAN_DAYS * 2))
    all_sigs = []

    for i, pq in enumerate(cache_files):
        if i % 500 == 0:
            print(f"  {i}/{len(cache_files)}...")
        ticker = pq.stem
        try:
            df = pd.read_parquet(pq)
            df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
            df = df[df.index >= cutoff].copy()
            if len(df) < 5:
                continue

            cur_price = float(df['Close'].iloc[-1])
            if cur_price <= 0:
                continue

            # 최근 SCAN_DAYS 거래일 슬라이스
            scan_df = df.iloc[:-1]  # 마지막 날 제외(오늘 현재가 비교용)
            if len(scan_df) == 0:
                continue

            for idx, row in scan_df.iterrows():
                date_str = idx.strftime('%Y-%m-%d')

                # 조건 1: 지수 하락일
                if date_str not in kospi_down:
                    continue

                # 조건 2: 등락률 +5%↑
                change = float(row.get('Change', 0))
                if change < MIN_CHANGE:
                    continue

                # 조건 3: 거래대금 2,000억↑
                amount = float(row['Close']) * float(row['Volume'])
                if amount < MIN_AMOUNT:
                    continue

                # 눌림목 확인: 현재가 vs 시그널일 고가
                signal_high  = float(row['High'])
                signal_close = float(row['Close'])
                if signal_high <= 0:
                    continue
                pullback = (cur_price - signal_high) / signal_high
                if not (PULL_MIN <= pullback <= PULL_MAX):
                    continue

                days_ago = (df.index[-1] - idx).days

                all_sigs.append({
                    'ticker':            ticker,
                    'name':              name_map.get(ticker, ticker),
                    'signal_date':       date_str,
                    'signal_change_pct': round(change * 100, 1),
                    'signal_amount_B':   int(amount / 1e8),   # 억 단위
                    'signal_high':       int(signal_high),
                    'signal_close':      int(signal_close),
                    'current_price':     int(cur_price),
                    'pullback_pct':      round(pullback * 100, 1),
                    'days_since':        days_ago,
                    'kospi_pct':         kospi_change_map.get(date_str, 0),
                })

        except Exception:
            continue

    # 4. 종목별 가장 최근 시그널만 남기기
    seen = {}
    for s in sorted(all_sigs, key=lambda x: x['signal_date'], reverse=True):
        if s['ticker'] not in seen:
            seen[s['ticker']] = s

    # 5. 정렬: 눌림목이 -5% ~ -20% 사이가 가장 매력적 → abs(pullback+12.5) 오름차순
    result = sorted(seen.values(), key=lambda x: abs(x['pullback_pct'] + 12.5))
    result = result[:MAX_RESULTS]

    output = {
        'updated':     end.strftime('%Y-%m-%d %H:%M'),
        'scan_days':   SCAN_DAYS,
        'total':       len(result),
        'signals':     result,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n완료: {len(result)}개 종목 → {OUTPUT}")
    for s in result[:10]:
        print(f"  {s['signal_date']} {s['name']:10s} "
              f"시그널+{s['signal_change_pct']}% {s['signal_amount_B']:,}억 "
              f"눌림{s['pullback_pct']}% ({s['days_since']}일전)")


if __name__ == '__main__':
    main()
