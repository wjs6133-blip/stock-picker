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


def get_news_with_dates(ticker: str, days: int = 14) -> list:
    """최근 N일 내 뉴스 최대 10건 (날짜 포함) — 오래된 뉴스 제외"""
    try:
        url  = f"https://finance.naver.com/item/news_news.naver?code={ticker}&page=1"
        res  = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        soup = BeautifulSoup(res.text, "html.parser")
        cutoff = datetime.now() - timedelta(days=days)
        result = []
        for row in soup.select("tr"):
            title_el = row.select_one("td.title a")
            date_el  = row.select_one("td.date")
            if not title_el or not date_el:
                continue
            date_txt = date_el.get_text(strip=True)
            try:
                # "2026.10.28" 또는 "10.28" 형식
                parts = date_txt.split(" ")[0].split(".")
                if len(parts) == 3:
                    news_dt = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
                elif len(parts) == 2:
                    news_dt = datetime(datetime.now().year, int(parts[0]), int(parts[1]))
                else:
                    news_dt = datetime.now()
                if news_dt < cutoff:
                    continue
                result.append(f"[{date_txt}] {title_el.get_text(strip=True)}")
            except Exception:
                result.append(title_el.get_text(strip=True))
            if len(result) >= 10:
                break
        return result
    except Exception:
        return []


# ── Gemini 1단계: 모멘텀 전용 스코어링 ─────────────────────────

def score_momentum(candidates: list, market_trend: str) -> list:
    """
    뉴스만 보고 각 종목의 상승 모멘텀 강도를 0~100으로 스코어링.
    momentum_score >= 60인 종목만 2단계로 진행.
    """
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    slim = [{
        "ticker"      : c["ticker"],
        "name"        : c["name"],
        "recent_news" : c.get("recent_news", []),
        "daily_vol_pct": c.get("daily_vol_pct"),
    } for c in candidates]
    data_str = json.dumps(slim, ensure_ascii=False, indent=2)

    market_note = {
        "bearish": "⚠️ 현재 KOSPI 약세장 — 재료 없이 순수 차트만인 종목은 momentum_score 30 이하로 낮게 평가.",
        "bullish": "✅ 현재 KOSPI 강세장 — 테마 모멘텀 종목에 우호적.",
        "neutral": "현재 KOSPI 중립.",
    }.get(market_trend, "")

    prompt = f"""당신은 뉴스 기반 단기 모멘텀 전문 애널리스트입니다.
시장 상황: {market_note}

아래 종목들의 최근 뉴스(recent_news)를 분석해 향후 2주(10거래일) 안에
주가가 +5% 이상 오를 수 있는 '상승 모멘텀 강도'를 평가하세요.

━━━ 평가 기준 ━━━
90~100: 강력한 연속 모멘텀 (수주 공시 연속, 실적 서프라이즈 시즌, 정책 수혜 직접 수혜주, AI·반도체·방산 테마 대장주)
70~89: 유효한 모멘텀 (재료는 있으나 후속 지속 여부 불확실, 섹터 2위 이하 대장주)
50~69: 약한 모멘텀 (뉴스는 있으나 이미 주가에 반영됐거나 단발성)
0~49: 모멘텀 없음 (방어주·내수주·뉴스 없음, 순차트·방어 업종)

━━━ 즉시 0점 조건 ━━━
- recent_news가 비어있거나 14일 이상 된 뉴스만 있을 때
- 식품·음료·보험·시멘트·물류·유통 업종
- CB·유상증자·대주주 매도 뉴스 있을 때

분석 데이터:
{data_str}

순수 JSON 배열로만 응답 (코드블록 없이):
[
  {{
    "ticker": "종목코드",
    "momentum_score": 85,
    "momentum_reason": "모멘텀 판단 근거 1~2문장",
    "continue_probability": "HIGH/MEDIUM/LOW",
    "theme": "해당 테마명 (없으면 없음)"
  }}
]"""

    for attempt in range(3):
        try:
            raw = client.models.generate_content(model="gemini-3.8-flash", contents=prompt).text.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            return json.loads(raw)
        except Exception as e:
            if attempt < 2:
                time.sleep(20 * (attempt + 1))
            else:
                print(f"모멘텀 스코어링 실패: {e}")
                return []


# ── Gemini 2단계: 모멘텀 확인 종목 최종 선별 ─────────────────

def ask_gemini(candidates: list, market_trend: str = "neutral") -> list:
    """
    2단계: 모멘텀 스코어링 통과 종목에 대해 차트 확인 후 최종 3개 선별.
    candidates는 이미 momentum_score >= 60 필터 통과 종목만.
    """
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    slim     = [{k: v for k, v in c.items() if k not in ("chart_data",)} for c in candidates]
    data_str = json.dumps(slim, ensure_ascii=False, indent=2)

    market_note = {
        "bearish": "⚠️ KOSPI 약세장. 확신 없으면 추천 0개도 가능.",
        "bullish": "✅ KOSPI 강세장. 모멘텀 종목 우호적.",
        "neutral": "KOSPI 중립.",
    }.get(market_trend, "")

    prompt = f"""당신은 대한민국 최상위 헤지펀드의 최종 투자 심사역입니다.
시장 상황: {market_note}

아래 종목들은 이미 '모멘텀 1차 검증'을 통과한 후보입니다.
각 종목의 momentum_score(뉴스 기반 모멘텀 강도)와 차트 지표를 교차 검증해
2주(10거래일) 안에 장중 +5% 달성 가능성이 가장 높은 종목을 선별하세요.

━━━ 최종 선별 기준 ━━━
1. momentum_score가 높고 continue_probability가 HIGH/MEDIUM인가?
2. 차트: MA20 기울기 우상향, 거래량 수축 후 반등 에너지 축적 확인
3. 목표가(+5%)까지 전고점 저항 없는가?
4. daily_vol_pct ≥ 1.8%인가? (변동성 충분한가)

최대 3개 반환. momentum_score 60 미만이거나 차트 조건 미달이면 그 수만큼 제외.
확신 없으면 빈 배열 [] 반환 (억지로 채우지 말 것).

분석 데이터:
{data_str}

순수 JSON 배열로만 응답 (코드블록 없이):
[
  {{
    "ticker": "종목코드",
    "name": "종목명",
    "score": 93,
    "catalyst_summary": "모멘텀 핵심 근거 2~3문장 — 왜 향후 2주 뉴스가 지속되는지 구체적으로.",
    "catalyst_stars": 4,
    "catalyst_detail": {{
      "theme": "테마명과 현재 시장 수급 흐름 (2문장)",
      "momentum": "향후 2주 후속 촉매 구체적 이벤트·일정 (2~3문장)",
      "risk": "주요 리스크 1~2가지 (1~2문장)"
    }},
    "chart_reason": "MA20/MA60 위치, 거래량 수축, 눌림목 매수 근거 (2문장)",
    "entry_plan": {{
      "buy_price": 매수가,
      "target_price": 목표가,
      "stop_loss": 손절가,
      "strategy": "분할매수 타이밍 한 문장"
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

    # 2) 세부 데이터 추가 (뉴스 날짜 포함)
    for c in candidates:
        c["recent_news"] = get_news_with_dates(c["ticker"])
        c["chart_data"]  = get_chart_series(c["ticker"])
        time.sleep(0.2)

    # 3) 1단계: 모멘텀 스코어링 (뉴스 전용 Gemini)
    if not candidates:
        recs    = []
        message = "오늘은 조건을 충족하는 종목이 없습니다."
    else:
        print("1단계: 모멘텀 스코어링 중...")
        momentum_scores = score_momentum(candidates, market_trend)

        # 모멘텀 스코어 candidates에 병합
        mscore_map = {m["ticker"]: m for m in momentum_scores}
        for c in candidates:
            ms = mscore_map.get(c["ticker"], {})
            c["momentum_score"]       = ms.get("momentum_score", 0)
            c["momentum_reason"]      = ms.get("momentum_reason", "")
            c["continue_probability"] = ms.get("continue_probability", "LOW")
            c["theme"]                = ms.get("theme", "")

        # 모멘텀 60 이상만 2단계로 진행 (최대 6개)
        momentum_passed = sorted(
            [c for c in candidates if c.get("momentum_score", 0) >= 60],
            key=lambda x: x.get("momentum_score", 0),
            reverse=True
        )[:6]

        print(f"  모멘텀 통과: {len(momentum_passed)}/{len(candidates)}개")

        if not momentum_passed:
            recs    = []
            message = "모멘텀 기준 통과 종목 없음 (뉴스·재료 부족)"
        else:
            # 4) 2단계: 차트 교차검증 최종 선별
            print("2단계: 차트 교차검증 최종 선별 중...")
            ai_recs = ask_gemini(momentum_passed, market_trend)
            cmap    = {c["ticker"]: c for c in momentum_passed}
            recs    = []
            for r in ai_recs:
                c = cmap.get(r.get("ticker"), {})
                r["chart_data"]     = c.get("chart_data", [])
                r["news"]           = c.get("recent_news", [])
                r["momentum_score"] = c.get("momentum_score")
                recs.append(r)
            message = f"후보 {len(candidates)}개 → 모멘텀 {len(momentum_passed)}개 → 최종 {len(recs)}개"

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
