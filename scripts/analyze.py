"""
GitHub Actions 전용 — 매일 새벽 실행
1. FinanceDataReader로 눌림목 후보 수집
2. Gemini AI 심사
3. 차트·수급·뉴스 데이터 포함 → data/recommendations.json 저장
"""
import json
import os
import time
from datetime import datetime, timedelta

import FinanceDataReader as fdr
from google import genai
import pandas as pd
import requests
from bs4 import BeautifulSoup

VOLUME_CONTRACTION = 0.65
MA20_BAND          = 0.04
DRAWDOWN_MIN       = -0.20
DRAWDOWN_MAX       = -0.03
MAX_CANDIDATES     = 15


# ── 데이터 수집 ────────────────────────────────────────────────

def get_name_map() -> dict:
    df       = fdr.StockListing('KRX')
    code_col = next((c for c in ['Code', 'Symbol'] if c in df.columns), df.columns[0])
    name_col = next((c for c in ['Name', 'ISU_NM']  if c in df.columns), df.columns[1])
    return dict(zip(df[code_col].astype(str), df[name_col].astype(str)))


def fetch_ohlcv(ticker: str, days: int = 130) -> pd.DataFrame | None:
    try:
        end   = datetime.now()
        start = end - timedelta(days=days)
        df    = fdr.DataReader(ticker, start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
        return df if df is not None and len(df) >= 62 else None
    except Exception:
        return None


def analyze_chart(ticker: str) -> dict | None:
    df = fetch_ohlcv(ticker)
    if df is None:
        return None
    try:
        close  = df['Close'].astype(float)
        volume = df['Volume'].astype(float)
        ma20   = close.rolling(20).mean()
        ma60   = close.rolling(60).mean()

        cur      = float(close.iloc[-1])
        cur_ma20 = float(ma20.iloc[-1])
        cur_ma60 = float(ma60.iloc[-1])

        if any(pd.isna(v) for v in [cur, cur_ma20, cur_ma60]) or cur == 0:
            return None
        if cur_ma20 < cur_ma60:
            return None

        gap = (cur - cur_ma20) / cur_ma20
        if not (DRAWDOWN_MAX <= gap <= MA20_BAND):
            return None

        rv = float(volume.iloc[-5:].mean())
        bv = float(volume.iloc[-25:-5].mean())
        vol_ratio = rv / bv if bv > 0 else 1.0
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


def get_chart_series(ticker: str) -> list:
    """60일 캔들 + MA20/60 (프론트 차트용)"""
    df = fetch_ohlcv(ticker)
    if df is None:
        return []
    try:
        close = df['Close'].astype(float)
        ma20  = close.rolling(20).mean()
        ma60  = close.rolling(60).mean()
        rows  = []
        for idx, row in df.tail(60).iterrows():
            m20 = float(ma20[idx])
            m60 = float(ma60[idx])
            rows.append({
                "time"  : idx.strftime("%Y-%m-%d"),
                "open"  : int(row['Open'])   if row['Open']   > 0 else int(row['Close']),
                "high"  : int(row['High']),
                "low"   : int(row['Low']),
                "close" : int(row['Close']),
                "volume": int(row['Volume']),
                "ma20"  : int(m20) if not pd.isna(m20) else None,
                "ma60"  : int(m60) if not pd.isna(m60) else None,
            })
        return rows
    except Exception:
        return []


def get_supply(ticker: str) -> dict:
    """네이버 외국인 3일 순매수"""
    try:
        url  = f"https://finance.naver.com/item/frgn.naver?code={ticker}"
        res  = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        soup = BeautifulSoup(res.text, "html.parser")
        daily = []
        for row in soup.select("table.type2 tr")[1:4]:
            cols = row.select("td")
            if len(cols) >= 5:
                txt = cols[4].get_text(strip=True).replace(",", "").replace("+", "")
                try:
                    daily.append(int(txt))
                except Exception:
                    daily.append(0)
        return {"foreign_net_3d": sum(daily), "foreign_daily": daily}
    except Exception:
        return {"foreign_net_3d": 0, "foreign_daily": []}


def get_news(ticker: str) -> list:
    try:
        url  = f"https://finance.naver.com/item/news_news.naver?code={ticker}&page=1"
        res  = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        soup = BeautifulSoup(res.text, "html.parser")
        return [a.get_text(strip=True) for a in soup.select("td.title a")[:6]]
    except Exception:
        return []


# ── Gemini 심사 ───────────────────────────────────────────────

def ask_gemini(candidates: list) -> list:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    slim     = [{k: v for k, v in c.items() if k not in ("chart_data",)} for c in candidates]
    data_str = json.dumps(slim, ensure_ascii=False, indent=2)

    prompt = f"""당신은 대한민국 최상위 헤지펀드의 '리스크 관리 총괄 심사역'입니다.
목표: 지금 매수해서 2주(10영업일) 안에 +5%를 찍고 나올 종목 1~2개만 엄선.
[차트지표] [수급] [뉴스]를 3중 교차 검증하세요.

[1단계: 수급 검증 40점]
- 외국인 3일 순매수(foreign_net_3d)가 양수인가?
- 개인만 순매수·외인 이탈이면 점수 무관 즉시 REJECT.

[2단계: 재료·모멘텀 검증 40점]
- 향후 2주간 후속 뉴스가 나올 연속 모멘텀인가? 단발성 소멸 뉴스인가?
- 해당 테마 대장주인가? 후발·잡주 즉시 REJECT.
- CB·유상증자·대주주 매도 등 악재 공시가 없는가?

[3단계: 손익비 검증 20점]
- 20일선 지지가 확인되는가?
- 목표가(+5%)까지 저항이 비어 있는가?
- 손절선(-2.5%)이 명확한가?

총점 90점 이상만 추천. 없으면 반드시 [] 반환.

분석 데이터:
{data_str}

순수 JSON 배열로만 응답 (코드블록 없이):
[
  {{
    "ticker": "종목코드",
    "name": "종목명",
    "score": 93,
    "supply_check": "수급 판단 근거",
    "catalyst_check": "재료 지속성 근거",
    "entry_plan": {{
      "buy_price": 매수가,
      "target_price": 목표가,
      "stop_loss": 손절가
    }}
  }}
]"""

    raw = client.models.generate_content(model="gemini-1.5-flash", contents=prompt).text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except Exception:
        return []


# ── 메인 ─────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 시작")

    name_map = get_name_map()
    tickers  = list(name_map.keys())
    print(f"전종목 {len(tickers)}개 스캔 중...")

    # 1) 눌림목 필터
    candidates = []
    for i, ticker in enumerate(tickers):
        if i % 200 == 0:
            print(f"  {i}/{len(tickers)}")
            time.sleep(0.3)
        chart = analyze_chart(ticker)
        if not chart:
            continue
        candidates.append({**chart, "name": name_map.get(ticker, ticker)})
        if len(candidates) >= MAX_CANDIDATES:
            break

    print(f"후보 {len(candidates)}개 → 수급·뉴스·차트 수집 중")

    # 2) 세부 데이터 추가
    for c in candidates:
        supply     = get_supply(c["ticker"])
        news       = get_news(c["ticker"])
        chart_data = get_chart_series(c["ticker"])
        c.update({**supply, "recent_news": news, "chart_data": chart_data})
        time.sleep(0.2)

    # 3) Gemini 심사
    if not candidates:
        recs    = []
        message = "오늘은 조건을 충족하는 종목이 없습니다."
    else:
        print("Gemini 심사 중...")
        ai_recs = ask_gemini(candidates)
        cmap    = {c["ticker"]: c for c in candidates}
        recs    = []
        for r in ai_recs:
            c = cmap.get(r.get("ticker"), {})
            r["chart_data"]  = c.get("chart_data", [])
            r["supply_data"] = {
                "foreign_net_3d" : c.get("foreign_net_3d", 0),
                "foreign_daily"  : c.get("foreign_daily", []),
            }
            r["news"] = c.get("recent_news", [])
            recs.append(r)
        message = f"후보 {len(candidates)}개 중 {len(recs)}개 통과"

    os.makedirs("data", exist_ok=True)
    with open("data/recommendations.json", "w", encoding="utf-8") as f:
        json.dump({
            "date"            : datetime.now().strftime("%Y%m%d"),
            "updated_at"      : datetime.now().strftime("%Y-%m-%d %H:%M"),
            "candidates_count": len(candidates),
            "message"         : message,
            "recommendations" : recs,
        }, f, ensure_ascii=False, indent=2)

    print(f"완료 — 추천 {len(recs)}개")


if __name__ == "__main__":
    main()
