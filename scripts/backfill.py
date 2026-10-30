"""
과거 날짜 범위 기준 눌림목 분석 백필
- 각 날짜 기준으로 해당 날짜까지의 데이터만 사용 (미래 데이터 미참조)
- 전 종목 데이터를 1회만 로드 후 날짜별 슬라이싱 (효율화)
- analyze.py와 동일한 개선 필터 적용
  ① MA20 기울기 (5일 전보다 낮으면 하락 추세 제외)
  ② 최근 5거래일 수익률 -3% 이하 제외
  ③ 직전 5거래일 추천 종목 반복 차단
  ④ 시장 추세(KOSPI) → Gemini 프롬프트에 반영

사용: python scripts/backfill.py [start_date] [end_date]
예:   python scripts/backfill.py 20261001 20261028
"""
import sys
import json
import os
import time
from collections import deque
from datetime import datetime, timedelta

import FinanceDataReader as fdr
from google import genai
import pandas as pd

VOLUME_CONTRACTION = 0.65
MA20_BAND          = 0.04
DRAWDOWN_MIN       = -0.20
DRAWDOWN_MAX       = -0.03
MAX_CANDIDATES     = 15
RECENT_REC_DAYS    = 5
RECENT_RET_LIMIT   = -0.03
MIN_DAILY_VOL      = 0.015  # 일변동성 1.5% 미만 방어주 제외


# ── 유틸 ──────────────────────────────────────────────────────

def get_trading_days(start_dt: datetime, end_dt: datetime) -> list[str]:
    df = fdr.DataReader('KS11', start_dt.strftime('%Y-%m-%d'), end_dt.strftime('%Y-%m-%d'))
    if df is None or len(df) == 0:
        return []
    return [ts.strftime('%Y%m%d') for ts in df.index]


def get_name_map() -> dict:
    df       = fdr.StockListing('KRX')
    code_col = next((c for c in ['Code', 'Symbol'] if c in df.columns), df.columns[0])
    name_col = next((c for c in ['Name', 'ISU_NM']  if c in df.columns), df.columns[1])
    return dict(zip(df[code_col].astype(str), df[name_col].astype(str)))


# ── 시장 추세 (기준일 기준 KOSPI 슬라이싱) ────────────────────

def get_market_trend_asof(kospi_df: pd.DataFrame, as_of: pd.Timestamp) -> str:
    try:
        df = kospi_df[kospi_df.index <= as_of]
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


# ── 차트 분석 (개선 필터 포함) ────────────────────────────────

def analyze_chart_from_df(ticker: str, df: pd.DataFrame) -> dict | None:
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

        rv        = float(volume.iloc[-5:].mean())
        bv        = float(volume.iloc[-25:-5].mean())
        vol_ratio = rv / bv if bv > 0 else 1.0
        if vol_ratio > VOLUME_CONTRACTION:
            return None

        high30 = float(close.iloc[-30:].max())
        dd     = (cur - high30) / high30
        if not (DRAWDOWN_MIN <= dd <= DRAWDOWN_MAX):
            return None

        # ── 개선 필터 ① MA20 기울기 ──────────────────────────
        if len(ma20) < 6 or float(ma20.iloc[-1]) <= float(ma20.iloc[-6]):
            return None

        # ── 개선 필터 ② 최근 5거래일 수익률 ─────────────────
        if len(close) >= 6:
            ret5 = (cur - float(close.iloc[-6])) / float(close.iloc[-6])
            if ret5 < RECENT_RET_LIMIT:
                return None

        # ── 개선 필터 ③ 일변동성: 1.5% 미만 방어주 제외 ─────
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


# ── Gemini 심사 ───────────────────────────────────────────────

def ask_gemini(candidates: list, date_str: str, market_trend: str = "neutral") -> list:
    client   = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    slim     = [{k: v for k, v in c.items()} for c in candidates]
    data_str = json.dumps(slim, ensure_ascii=False, indent=2)

    market_note = {
        "bearish": "⚠️ 기준일 기준 KOSPI 5일 하락 추세(약세장). 기준을 대폭 강화: 재료 없는 순수 차트 종목은 전부 REJECT. 확실한 반등 근거 없으면 추천 수를 줄이거나 0개도 가능.",
        "bullish": "✅ 기준일 기준 KOSPI 5일 상승 추세(강세장). 상승 모멘텀 종목에 우호적 환경.",
        "neutral": "기준일 기준 KOSPI 중립 추세.",
    }.get(market_trend, "")

    prompt = f"""당신은 대한민국 최상위 헤지펀드의 '리스크 관리 총괄 심사역'입니다.
분석 기준일: {date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}
목표: 기준일 기준으로 매수해서 2주(10영업일) 안에 장중 +5%를 한 번이라도 달성할 종목 엄선.
기준일 기준 뉴스 없음 — 차트 기술적 분석 + daily_vol_pct(일변동성) 중심으로 판단.

시장 상황: {market_note}

━━━ 즉시 REJECT 조건 ━━━
✗ daily_vol_pct < 1.8% → 10거래일 내 +5% 달성 불가능한 방어주
✗ 식품·음료·보험·시멘트·철강원료·물류·유통 섹터 → 단기 모멘텀 없는 업종
✗ 차트상 전고점 이후 계속 우하향 흔적 → 구조적 하락 배제

[1단계: 차트 기술적 분석 60점]
- daily_vol_pct 높을수록 가점 (2% 이상 = 충분한 변동성)
- MA20 기울기 우상향, 20일선 지지 단단한가?
- 거래량 수축 → 반등 에너지 축적 확인

[2단계: 손익비 검증 40점]
- 목표가(+5%)까지 전고점 저항이 없는가?
- 손절선(-2.5%)이 명확한가?

catalyst_stars는 차트 강도 (1~5): 5=완벽한 눌림목+고변동성, 4=양호, 3=보통, 2=약한, 1=불량.
점수 높은 순으로 최대 3개 반환. 85점 미만이면 포함 금지. 85점 이상 없으면 빈 배열 [] 반환.

분석 데이터:
{data_str}

순수 JSON 배열로만 응답 (코드블록 없이):
[
  {{
    "ticker": "종목코드",
    "name": "종목명",
    "score": 93,
    "catalyst_summary": "기준일 기준 차트 근거 2~3문장",
    "catalyst_stars": 4,
    "catalyst_detail": {{
      "theme": "속한 섹터와 기술적 특징 (2문장)",
      "momentum": "눌림목 이후 반등 근거 (2문장)",
      "risk": "주요 리스크 1~2가지 (1~2문장)"
    }},
    "chart_reason": "20일선/60일선 위치, 거래량 수축, 눌림목 구간 설명 (2문장)",
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


# ── 성과 체크 ─────────────────────────────────────────────────

def run_performance_check(date_str: str, data: dict) -> dict:
    rec_date = datetime.strptime(date_str, "%Y%m%d")
    start    = (rec_date + timedelta(days=1)).strftime("%Y-%m-%d")
    end      = (rec_date + timedelta(days=22)).strftime("%Y-%m-%d")

    for r in data.get("recommendations", []):
        ticker       = r.get("ticker")
        entry_plan   = r.get("entry_plan") or {}
        buy_price    = entry_plan.get("buy_price")
        target_price = entry_plan.get("target_price")

        if not ticker or not buy_price or not target_price:
            r["performance"] = {"checked": False, "reason": "데이터 부족"}
            continue
        try:
            df = fdr.DataReader(ticker, start, end)
            if df is None or len(df) == 0:
                r["performance"] = {"checked": False, "reason": "시세 없음"}
                continue

            df10       = df.head(10)
            closes     = df10["Close"].astype(float).tolist()
            highs      = df10["High"].astype(float).tolist()

            # 고가 기준: 장중 한 번이라도 목표가 터치 시 달성
            hit_target = any(h >= target_price for h in highs)
            max_high   = max(highs) if highs else buy_price
            max_ret    = round((max_high - buy_price) / buy_price * 100, 2)
            final      = int(closes[-1]) if closes else None

            hit_date    = None
            days_to_hit = None
            if hit_target:
                for i, (ts, row) in enumerate(df10.iterrows()):
                    if float(row["High"]) >= target_price:
                        hit_date    = ts.strftime("%Y-%m-%d")
                        days_to_hit = i + 1
                        break

            r["performance"] = {
                "checked"       : True,
                "hit_target"    : hit_target,
                "max_return_pct": max_ret,
                "hit_date"      : hit_date,
                "days_to_hit"   : days_to_hit,
                "final_close"   : final,
            }
        except Exception as e:
            r["performance"] = {"checked": False, "reason": str(e)}

    data["performance_checked"] = True
    return data


# ── 메인 ─────────────────────────────────────────────────────

def main():
    start_str = sys.argv[1] if len(sys.argv) > 1 else "20261001"
    end_str   = sys.argv[2] if len(sys.argv) > 2 else "20261028"

    start_dt = datetime.strptime(start_str, "%Y%m%d")
    end_dt   = datetime.strptime(end_str,   "%Y%m%d")

    print(f"백필 범위: {start_str} ~ {end_str}")

    # 기존 히스토리 확인
    index_path = "data/history/index.json"
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            existing_idx = set(json.load(f))
    except Exception:
        existing_idx = set()

    # 거래일 목록
    print("KRX 거래일 조회 중...")
    trading_days = get_trading_days(start_dt, end_dt)
    to_run = [d for d in trading_days if d not in existing_idx]
    print(f"거래일 {len(trading_days)}개 중 미생성 {len(to_run)}개")
    if not to_run:
        print("모두 이미 존재합니다.")
        return

    # 종목명 맵
    print("종목 목록 로딩 중...")
    name_map = get_name_map()
    tickers  = list(name_map.keys())
    print(f"전체 {len(tickers)}개 종목")

    # ── 전 종목 OHLCV 한 번만 로드 ────────────────────────────
    fetch_start = (start_dt - timedelta(days=200)).strftime('%Y-%m-%d')
    fetch_end   = end_dt.strftime('%Y-%m-%d')

    print(f"\n전 종목 OHLCV 로딩: {fetch_start} ~ {fetch_end}")
    all_data = {}
    for i, ticker in enumerate(tickers):
        if i % 300 == 0:
            print(f"  {i}/{len(tickers)} 로딩 중...")
        try:
            df = fdr.DataReader(ticker, fetch_start, fetch_end)
            if df is not None and len(df) >= 62:
                all_data[ticker] = df
        except Exception:
            pass

    # KOSPI 데이터 로드 (시장 추세용)
    print("KOSPI 데이터 로딩 중...")
    kospi_df = fdr.DataReader('KS11', fetch_start, fetch_end)

    print(f"유효 종목 {len(all_data)}개 로드 완료\n")

    today = datetime.now()

    # ── 개선 필터 ③ 날짜별 추천 종목 롤링 윈도우 ─────────────
    # deque로 최근 RECENT_REC_DAYS 거래일의 추천 종목 관리
    recent_recs_window: deque = deque(maxlen=RECENT_REC_DAYS)

    # ── 날짜별 분석 ───────────────────────────────────────────
    for date_str in to_run:
        as_of     = datetime.strptime(date_str, "%Y%m%d")
        as_of_ts  = pd.Timestamp(as_of)

        # 현재 제외 종목 집합
        recently_recommended = {t for day_set in recent_recs_window for t in day_set}

        # 시장 추세
        market_trend = get_market_trend_asof(kospi_df, as_of_ts)

        print(f"{'='*50}")
        print(f"{date_str} 분석 | 시장: {market_trend} | 반복 제외: {len(recently_recommended)}개")

        candidates = []
        for ticker, full_df in all_data.items():
            # ── 개선 필터 ③ 반복 추천 차단 ──────────────────
            if ticker in recently_recommended:
                continue
            df = full_df[full_df.index <= as_of_ts]
            if len(df) < 62:
                continue
            chart = analyze_chart_from_df(ticker, df)
            if not chart:
                continue
            candidates.append({**chart, "name": name_map.get(ticker, ticker)})
            if len(candidates) >= MAX_CANDIDATES:
                break

        print(f"후보 {len(candidates)}개")

        if not candidates:
            recs    = []
            message = "조건을 충족하는 종목이 없습니다."
        else:
            print("Gemini 심사 중...")
            ai_recs = ask_gemini(candidates, date_str, market_trend)
            recs    = ai_recs
            message = f"후보 {len(candidates)}개 중 {len(recs)}개 통과"

        # 오늘 추천 종목을 롤링 윈도우에 추가
        today_tickers = {r.get("ticker") for r in recs if r.get("ticker")}
        recent_recs_window.append(today_tickers)

        # 히스토리 저장 (경량 — 차트 데이터 제외)
        history_recs = [{
            "ticker"          : r.get("ticker"),
            "name"            : r.get("name"),
            "score"           : r.get("score"),
            "catalyst_summary": r.get("catalyst_summary"),
            "catalyst_stars"  : r.get("catalyst_stars"),
            "entry_plan"      : r.get("entry_plan"),
        } for r in recs]

        hist_data = {
            "date"           : date_str,
            "updated_at"     : f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]} 00:00",
            "recommendations": history_recs,
        }

        # 16일 이상 지난 날짜는 성과 체크 즉시 실행
        if today - as_of >= timedelta(days=16):
            print("성과 체크 중...")
            hist_data = run_performance_check(date_str, hist_data)

        os.makedirs("data/history", exist_ok=True)
        with open(f"data/history/{date_str}.json", "w", encoding="utf-8") as f:
            json.dump(hist_data, f, ensure_ascii=False, indent=2)

        # 인덱스 갱신
        existing_idx.add(date_str)
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(sorted(list(existing_idx), reverse=True), f, ensure_ascii=False)

        print(f"  → {date_str} 저장 완료: {message}")
        time.sleep(1)

    print("\n백필 완료!")


if __name__ == "__main__":
    main()
