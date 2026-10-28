"""
GitHub Actions 전용: FinanceDataReader로 눌림목 후보 수집
→ data/candidates.json 저장 (AI 분석 없음, 버튼 클릭 시 Gemini가 실시간 처리)
"""
import json
import os
import time
from datetime import datetime, timedelta

import FinanceDataReader as fdr
import pandas as pd
import requests
from bs4 import BeautifulSoup

VOLUME_CONTRACTION = 0.65
MA20_BAND          = 0.04
DRAWDOWN_MIN       = -0.20
DRAWDOWN_MAX       = -0.03
MAX_CANDIDATES     = 15


def get_all_tickers_with_names() -> dict:
    """KRX 전종목 코드 → 이름 딕셔너리"""
    df = fdr.StockListing('KRX')
    # 컬럼명 유연하게 처리
    code_col = next((c for c in ['Code', 'Symbol', 'ISU_SRT_CD'] if c in df.columns), df.columns[0])
    name_col = next((c for c in ['Name', 'ISU_NM', 'CompanyName'] if c in df.columns), df.columns[1])
    return dict(zip(df[code_col].astype(str), df[name_col].astype(str)))


def analyze_chart(ticker: str) -> dict | None:
    """눌림목 차트 필터"""
    try:
        end   = datetime.now()
        start = end - timedelta(days=130)

        df = fdr.DataReader(ticker, start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
        if df is None or len(df) < 62:
            return None

        close  = df['Close'].astype(float)
        volume = df['Volume'].astype(float)

        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean()

        cur      = float(close.iloc[-1])
        cur_ma20 = float(ma20.iloc[-1])
        cur_ma60 = float(ma60.iloc[-1])

        if pd.isna(cur_ma20) or pd.isna(cur_ma60) or cur == 0:
            return None

        # 1) 정배열 (20일선 > 60일선)
        if cur_ma20 < cur_ma60:
            return None

        # 2) 현재가 20일선 ±4% 이내
        gap = (cur - cur_ma20) / cur_ma20
        if not (DRAWDOWN_MAX <= gap <= MA20_BAND):
            return None

        # 3) 거래량 수축
        recent_vol = float(volume.iloc[-5:].mean())
        base_vol   = float(volume.iloc[-25:-5].mean())
        vol_ratio  = recent_vol / base_vol if base_vol > 0 else 1.0
        if vol_ratio > VOLUME_CONTRACTION:
            return None

        # 4) 고점 대비 조정폭
        high30 = float(close.iloc[-30:].max())
        dd     = (cur - high30) / high30
        if not (DRAWDOWN_MIN <= dd <= DRAWDOWN_MAX):
            return None

        return {
            "ticker"       : ticker,
            "current_price": int(cur),
            "ma20"         : int(cur_ma20),
            "ma60"         : int(cur_ma60),
            "ma20_gap_pct" : round(gap * 100, 2),
            "vol_ratio"    : round(vol_ratio, 2),
            "drawdown_pct" : round(dd * 100, 2),
        }
    except Exception:
        return None


def get_supply_naver(ticker: str) -> dict:
    """네이버 금융 외인·기관 수급 스크래핑 (실패 시 빈 dict)"""
    try:
        url  = f"https://finance.naver.com/item/frgn.naver?code={ticker}"
        res  = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        soup = BeautifulSoup(res.text, "html.parser")

        rows = soup.select("table.type2 tr")
        values = []
        for row in rows[1:4]:  # 최근 3일
            cols = row.select("td")
            if len(cols) >= 5:
                try:
                    val = cols[4].get_text(strip=True).replace(",", "").replace("+", "")
                    values.append(int(val) if val and val != "-" else 0)
                except Exception:
                    values.append(0)

        foreign_3d = sum(values) if values else None
        return {"foreign_net_3d_approx": foreign_3d, "supply_source": "naver_foreign"}
    except Exception:
        return {"foreign_net_3d_approx": None, "supply_source": "unavailable"}


def scrape_news(ticker: str) -> list[str]:
    try:
        url  = f"https://finance.naver.com/item/news_news.naver?code={ticker}&page=1"
        res  = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        soup = BeautifulSoup(res.text, "html.parser")
        return [a.get_text(strip=True) for a in soup.select("td.title a")[:5]]
    except Exception:
        return []


def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 후보 수집 시작")

    name_map = get_all_tickers_with_names()
    tickers  = list(name_map.keys())
    print(f"전종목: {len(tickers)}개")

    candidates = []
    for i, ticker in enumerate(tickers):
        if i % 200 == 0:
            print(f"  {i}/{len(tickers)} ...")
            time.sleep(0.3)

        chart = analyze_chart(ticker)
        if not chart:
            continue

        supply = get_supply_naver(ticker)
        news   = scrape_news(ticker)
        name   = name_map.get(ticker, ticker)

        candidates.append({**chart, "name": name, **supply, "recent_news": news})

        if len(candidates) >= MAX_CANDIDATES:
            break

    print(f"후보 {len(candidates)}개 저장")

    os.makedirs("data", exist_ok=True)
    with open("data/candidates.json", "w", encoding="utf-8") as f:
        json.dump({
            "date"      : datetime.now().strftime("%Y%m%d"),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "candidates": candidates,
        }, f, ensure_ascii=False, indent=2)

    print("완료")


if __name__ == "__main__":
    main()
