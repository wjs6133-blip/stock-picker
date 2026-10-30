"""
과거 날짜 범위 기준 눌림목 분석 백필
- 각 날짜 기준으로 해당 날짜까지의 데이터만 사용 (미래 데이터 미참조)
- 전 종목 데이터를 1회만 로드 후 날짜별 슬라이싱 (효율화)

사용: python scripts/backfill.py [start_date] [end_date]
예:   python scripts/backfill.py 20261001 20261028
"""
import sys
import json
import os
import time
from datetime import datetime, timedelta

import FinanceDataReader as fdr
from google import genai
import pandas as pd

VOLUME_CONTRACTION = 0.65
MA20_BAND          = 0.04
DRAWDOWN_MIN       = -0.20
DRAWDOWN_MAX       = -0.03
MAX_CANDIDATES     = 15


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


# ── 차트 분석 (슬라이싱된 df 직접 입력) ───────────────────────

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


# ── Gemini 심사 ───────────────────────────────────────────────

def ask_gemini(candidates: list, date_str: str) -> list:
    client   = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    slim     = [{k: v for k, v in c.items()} for c in candidates]
    data_str = json.dumps(slim, ensure_ascii=False, indent=2)

    prompt = f"""당신은 대한민국 최상위 헤지펀드의 '리스크 관리 총괄 심사역'입니다.
분석 기준일: {date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}
목표: 기준일 기준으로 매수해서 2주(10영업일) 안에 +5%를 찍고 나올 종목을 엄선.
기준일 기준 뉴스는 없으므로 차트 기술적 분석에 집중하세요.

[1단계: 차트 기술적 분석 60점]
- 정배열(MA20 > MA60) 확인
- 20일선 지지가 확인되는가?
- 거래량 수축 눌림목인가?

[2단계: 손익비 검증 40점]
- 목표가(+5%)까지 저항이 비어 있는가?
- 손절선(-2.5%)이 명확한가?

catalyst_stars는 차트 강도 (1~5): 5=완벽한 눌림목, 4=양호, 3=보통, 2=약한, 1=불량.
점수 높은 순으로 반드시 3개 반환. 3개 미만 후보면 있는 만큼만.

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
            hit_target = any(c >= target_price for c in closes)
            max_close  = max(closes) if closes else buy_price
            max_ret    = round((max_close - buy_price) / buy_price * 100, 2)
            final      = int(closes[-1]) if closes else None

            hit_date    = None
            days_to_hit = None
            if hit_target:
                for i, (ts, row) in enumerate(df10.iterrows()):
                    if float(row["Close"]) >= target_price:
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

    # ── 전 종목 OHLCV 한 번만 로드 (핵심 최적화) ──────────────
    # 백필 시작일 130거래일 전부터 end_date까지 가져옴
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

    print(f"유효 종목 {len(all_data)}개 로드 완료\n")

    today = datetime.now()

    # ── 날짜별 분석 ───────────────────────────────────────────
    for date_str in to_run:
        as_of = datetime.strptime(date_str, "%Y%m%d")
        as_of_end = pd.Timestamp(as_of)

        print(f"{'='*50}")
        print(f"{date_str} 분석 중...")

        candidates = []
        for ticker, full_df in all_data.items():
            df = full_df[full_df.index <= as_of_end]
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
            ai_recs = ask_gemini(candidates, date_str)
            recs    = ai_recs
            message = f"후보 {len(candidates)}개 중 {len(recs)}개 통과"

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
