"""
GitHub Actions 전용: pykrx로 눌림목 후보 수집 → data/candidates.json 저장
AI 분석 없음 (버튼 클릭 시 실시간으로 Gemini가 처리)
"""
import json
import os
import time
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from pykrx import stock

VOLUME_CONTRACTION = 0.65
MA20_BAND          = 0.04
DRAWDOWN_MIN       = -0.20
DRAWDOWN_MAX       = -0.03
MAX_CANDIDATES     = 15


def get_trading_date() -> str:
    t = datetime.now()
    if t.weekday() == 0:
        t -= timedelta(days=3)
    elif t.weekday() == 6:
        t -= timedelta(days=2)
    else:
        t -= timedelta(days=1)
    return t.strftime("%Y%m%d")


def get_all_tickers():
    return list(stock.get_market_ticker_list(market="KOSPI")) + \
           list(stock.get_market_ticker_list(market="KOSDAQ"))


def analyze_chart(ticker, date):
    try:
        end   = date
        start = (datetime.strptime(date, "%Y%m%d") - timedelta(days=100)).strftime("%Y%m%d")
        df    = stock.get_market_ohlcv_by_date(start, end, ticker)
        if len(df) < 62:
            return None

        close  = df["종가"]
        volume = df["거래량"]
        ma20   = close.rolling(20).mean()
        ma60   = close.rolling(60).mean()

        cur      = float(close.iloc[-1])
        cur_ma20 = float(ma20.iloc[-1])
        cur_ma60 = float(ma60.iloc[-1])

        if cur_ma20 < cur_ma60:
            return None

        gap = (cur - cur_ma20) / cur_ma20
        if not (DRAWDOWN_MAX <= gap <= MA20_BAND):
            return None

        recent_vol = float(volume.iloc[-5:].mean())
        base_vol   = float(volume.iloc[-25:-5].mean())
        vol_ratio  = recent_vol / base_vol if base_vol else 1.0
        if vol_ratio > VOLUME_CONTRACTION:
            return None

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


def get_supply(ticker, date):
    try:
        end   = date
        start = (datetime.strptime(date, "%Y%m%d") - timedelta(days=14)).strftime("%Y%m%d")
        df    = stock.get_market_trading_value_by_date(start, end, ticker)
        if len(df) < 3:
            return None

        r     = df.iloc[-3:]
        f_net = int(r["외국인합계"].sum())
        i_net = int(r["기관합계"].sum())
        p_net = int(r["개인"].sum())

        if p_net > 0 and f_net < 0 and i_net < 0:
            return None

        return {
            "foreign_net_3d"    : f_net,
            "institution_net_3d": i_net,
            "individual_net_3d" : p_net,
        }
    except Exception:
        return None


def scrape_news(ticker):
    try:
        url  = f"https://finance.naver.com/item/news_news.naver?code={ticker}&page=1"
        res  = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        soup = BeautifulSoup(res.text, "html.parser")
        return [a.get_text(strip=True) for a in soup.select("td.title a")[:5]]
    except Exception:
        return []


def main():
    date    = get_trading_date()
    tickers = get_all_tickers()
    print(f"기준일: {date} | 전종목: {len(tickers)}개")

    candidates = []
    for i, ticker in enumerate(tickers):
        if i % 200 == 0:
            print(f"  {i}/{len(tickers)} ...")
            time.sleep(0.5)

        chart = analyze_chart(ticker, date)
        if not chart:
            continue

        supply = get_supply(ticker, date)
        if not supply:
            continue

        name = stock.get_market_ticker_name(ticker)
        news = scrape_news(ticker)

        candidates.append({**chart, "name": name, **supply, "recent_news": news})
        if len(candidates) >= MAX_CANDIDATES:
            break

    print(f"후보 {len(candidates)}개 저장")

    os.makedirs("data", exist_ok=True)
    with open("data/candidates.json", "w", encoding="utf-8") as f:
        json.dump({
            "date"      : date,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "candidates": candidates,
        }, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
