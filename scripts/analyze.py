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
RECENT_REC_DAYS    = 5    # 최근 N거래일 내 추천된 종목 제외
RECENT_RET_LIMIT   = -0.03  # 최근 5거래일 수익률 하한 (-3%)
MIN_DAILY_VOL      = 0.015  # 일변동성 하한 (1.5% 미만이면 +5% 달성 불가능한 방어주)


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

        # ── 개선 필터 ① MA20 기울기: 5일 전보다 낮으면 하락 추세 ──
        if len(ma20) < 6 or float(ma20.iloc[-1]) <= float(ma20.iloc[-6]):
            return None

        # ── 개선 필터 ② 최근 5거래일 수익률: -3% 이하면 낙폭 과다 ──
        if len(close) >= 6:
            ret5 = (cur - float(close.iloc[-6])) / float(close.iloc[-6])
            if ret5 < RECENT_RET_LIMIT:
                return None

        # ── 개선 필터 ③ 일변동성: 1.5% 미만이면 +5% 달성 불가 방어주 ──
        daily_vol = float(close.pct_change().dropna().iloc[-20:].std())
        if daily_vol < MIN_DAILY_VOL:
            return None

        return {
            "ticker"       : ticker,
            "daily_vol_pct": round(daily_vol * 100, 2),
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




def get_recently_recommended(n: int = RECENT_REC_DAYS) -> set:
    """최근 N거래일 추천 종목 집합 — 반복 추천 방지"""
    recent = set()
    try:
        with open("data/history/index.json", "r", encoding="utf-8") as f:
            idx = json.load(f)
        for date_str in idx[:n]:
            with open(f"data/history/{date_str}.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            for r in data.get("recommendations", []):
                if r.get("ticker"):
                    recent.add(r["ticker"])
    except Exception:
        pass
    return recent


def get_market_trend() -> str:
    """KOSPI 최근 5거래일 추세 확인 (bearish / neutral / bullish)"""
    try:
        end   = datetime.now()
        start = end - timedelta(days=20)
        df    = fdr.DataReader('KS11', start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
        if df is None or len(df) < 6:
            return "neutral"
        close = df['Close'].astype(float)
        ret5  = (float(close.iloc[-1]) - float(close.iloc[-6])) / float(close.iloc[-6])
        if ret5 <= -0.02:
            return "bearish"
        if ret5 >= 0.02:
            return "bullish"
        return "neutral"
    except Exception:
        return "neutral"


def get_news(ticker: str) -> list:
    try:
        url  = f"https://finance.naver.com/item/news_news.naver?code={ticker}&page=1"
        res  = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        soup = BeautifulSoup(res.text, "html.parser")
        return [a.get_text(strip=True) for a in soup.select("td.title a")[:6]]
    except Exception:
        return []


# ── Gemini 심사 ───────────────────────────────────────────────

def ask_gemini(candidates: list, market_trend: str = "neutral") -> list:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    slim     = [{k: v for k, v in c.items() if k not in ("chart_data",)} for c in candidates]
    data_str = json.dumps(slim, ensure_ascii=False, indent=2)

    market_note = {
        "bearish": "⚠️ 현재 KOSPI 5일 하락 추세(약세장). 기준을 대폭 강화: 재료 없는 순수 차트 종목은 전부 REJECT. 확실한 연속 모멘텀 없으면 추천 종목 수를 줄이거나 0개도 가능.",
        "bullish": "✅ 현재 KOSPI 5일 상승 추세(강세장). 상승 모멘텀 종목에 우호적 환경.",
        "neutral": "현재 KOSPI 중립 추세.",
    }.get(market_trend, "")

    prompt = f"""당신은 대한민국 최상위 헤지펀드의 '리스크 관리 총괄 심사역'입니다.
목표: 지금 매수해서 2주(10영업일) 안에 장중 +5%를 한 번이라도 달성할 종목 엄선.
[차트지표] [뉴스] [일변동성(daily_vol_pct)]를 교차 검증하세요.

시장 상황: {market_note}

━━━ 즉시 REJECT 조건 (하나라도 해당 시 탈락) ━━━
✗ daily_vol_pct < 1.8% → 10거래일 내 +5% 물리적으로 불가능한 방어주
✗ 식품·음료·보험·시멘트·철강원료·물류·유통 섹터 → 단기 모멘텀 없는 방어 업종
✗ 테마 대장주 아닌 후발·잡주 → 수급 이탈 시 손절 직행
✗ CB·유상증자·대주주 매도 공시 → 악재 확정
✗ 단발성 뉴스(FDA 승인, 계약 공시 1건) → 이미 소화됨, 후속 모멘텀 없음

[1단계: 재료·모멘텀 검증 60점]
- 향후 2주간 연속 뉴스가 나올 구조적 모멘텀인가? (실적 시즌, 정책 수혜, 수주 사이클)
- 해당 섹터에서 최근 강한 자금 유입이 있는 테마 대장주인가?
- 수급: 외인·기관이 최근 순매수 중이거나 중립인가?

[2단계: 손익비 검증 40점]
- 20일선 기울기 우상향, 지지 확인
- 목표가(+5%)까지 전고점 저항이 없는가?
- 손절선(-2.5%) 아래 지지선이 명확한가?

catalyst_stars는 재료 강도 (1~5): 5=강력한 연속 모멘텀, 4=유효한 재료, 3=보통, 2=약한 재료, 1=단발·소멸 우려.
점수 높은 순으로 최대 3개 반환. 85점 미만이면 포함 금지. 85점 이상 없으면 빈 배열 [] 반환.

분석 데이터:
{data_str}

순수 JSON 배열로만 응답 (코드블록 없이):
[
  {{
    "ticker": "종목코드",
    "name": "종목명",
    "score": 93,
    "catalyst_summary": "핵심 매수 근거를 2~3문장으로 요약. 왜 지금 이 종목인지 핵심만.",
    "catalyst_stars": 4,
    "catalyst_detail": {{
      "theme": "속한 테마/섹터와 현재 시장 흐름과의 연관성 (2문장)",
      "momentum": "향후 2주 내 후속 촉매 예상 근거. 구체적 이벤트·일정 포함 (2~3문장)",
      "risk": "가장 주의해야 할 리스크 1~2가지 (1~2문장)"
    }},
    "chart_reason": "차트 기술적 근거 — 20일선/60일선 위치, 거래량 수축, 눌림목 구간 설명 (2문장)",
    "entry_plan": {{
      "buy_price": 매수가,
      "target_price": 목표가,
      "stop_loss": 손절가,
      "strategy": "매수 타이밍과 분할 전략 한 문장"
    }}
  }}
]"""

    for attempt in range(5):
        try:
            raw = client.models.generate_content(model="gemini-3.8-flash", contents=prompt).text.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            return json.loads(raw)
        except Exception as e:
            if attempt < 4:
                wait = 30 * (attempt + 1)
                print(f"Gemini 오류 (시도 {attempt+1}/5), {wait}초 후 재시도: {e}")
                time.sleep(wait)
            else:
                print(f"Gemini 최종 실패: {e}")
                return []


# ── 메인 ─────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 시작")

    # ── 개선 필터 ③ 사전 준비 ────────────────────────────────
    recently_recommended = get_recently_recommended()
    market_trend         = get_market_trend()
    print(f"시장 추세: {market_trend} | 최근 {RECENT_REC_DAYS}일 추천 제외 종목: {len(recently_recommended)}개")

    name_map = get_name_map()
    tickers  = list(name_map.keys())
    print(f"전종목 {len(tickers)}개 스캔 중...")

    # 1) 눌림목 필터 (개선 필터 ③ 반복 추천 차단 포함)
    candidates = []
    for i, ticker in enumerate(tickers):
        if i % 200 == 0:
            print(f"  {i}/{len(tickers)}")
            time.sleep(0.3)
        if ticker in recently_recommended:
            continue
        chart = analyze_chart(ticker)
        if not chart:
            continue
        candidates.append({**chart, "name": name_map.get(ticker, ticker)})
        if len(candidates) >= MAX_CANDIDATES:
            break

    print(f"후보 {len(candidates)}개 → 뉴스·차트 수집 중")

    # 2) 세부 데이터 추가
    for c in candidates:
        news       = get_news(c["ticker"])
        chart_data = get_chart_series(c["ticker"])
        c.update({"recent_news": news, "chart_data": chart_data})
        time.sleep(0.2)

    # 3) Gemini 심사 (시장 추세 전달)
    if not candidates:
        recs    = []
        message = "오늘은 조건을 충족하는 종목이 없습니다."
    else:
        print("Gemini 심사 중...")
        ai_recs = ask_gemini(candidates, market_trend)
        cmap    = {c["ticker"]: c for c in candidates}
        recs    = []
        for r in ai_recs:
            c = cmap.get(r.get("ticker"), {})
            r["chart_data"] = c.get("chart_data", [])
            r["news"]       = c.get("recent_news", [])
            recs.append(r)
        message = f"후보 {len(candidates)}개 중 {len(recs)}개 통과"

    date_str = datetime.now().strftime("%Y%m%d")

    os.makedirs("data", exist_ok=True)
    with open("data/recommendations.json", "w", encoding="utf-8") as f:
        json.dump({
            "date"            : date_str,
            "updated_at"      : datetime.now().strftime("%Y-%m-%d %H:%M"),
            "candidates_count": len(candidates),
            "message"         : message,
            "recommendations" : recs,
        }, f, ensure_ascii=False, indent=2)

    # ── 히스토리 저장 (차트 데이터 제외 경량 버전) ──────────────
    os.makedirs("data/history", exist_ok=True)
    history_recs = [{
        "ticker"          : r.get("ticker"),
        "name"            : r.get("name"),
        "score"           : r.get("score"),
        "catalyst_summary": r.get("catalyst_summary"),
        "catalyst_stars"  : r.get("catalyst_stars"),
        "entry_plan"      : r.get("entry_plan"),
    } for r in recs]

    with open(f"data/history/{date_str}.json", "w", encoding="utf-8") as f:
        json.dump({
            "date"           : date_str,
            "updated_at"     : datetime.now().strftime("%Y-%m-%d %H:%M"),
            "recommendations": history_recs,
        }, f, ensure_ascii=False, indent=2)

    index_path = "data/history/index.json"
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            idx = json.load(f)
    except Exception:
        idx = []
    if date_str not in idx:
        idx.append(date_str)
    idx.sort(reverse=True)
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False)

    print(f"완료 — 추천 {len(recs)}개")


if __name__ == "__main__":
    main()
