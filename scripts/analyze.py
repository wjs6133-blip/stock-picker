"""
눌림목 매매 자동 분석 스크립트
매일 새벽 GitHub Actions에서 실행 → data/recommendations.json 저장
"""
import json
import os
import time
from datetime import datetime, timedelta

import google.generativeai as genai
import requests
from bs4 import BeautifulSoup
from pykrx import stock

# ─── 설정 ───────────────────────────────────────────────────────────────────
MAX_CANDIDATES_FOR_AI = 10   # Gemini에게 넘길 최대 후보 수
VOLUME_CONTRACTION    = 0.65 # 거래량 수축 기준 (20일 평균 대비)
MA20_BAND             = 0.04 # 20일 이평선 ±4% 이내
DRAWDOWN_MIN          = -0.20
DRAWDOWN_MAX          = -0.03
# ────────────────────────────────────────────────────────────────────────────


def get_trading_date() -> str:
    """전날 영업일(YYYYMMDD) 반환"""
    t = datetime.now()
    # 월요일이면 금요일로
    if t.weekday() == 0:
        t -= timedelta(days=3)
    elif t.weekday() == 6:
        t -= timedelta(days=2)
    else:
        t -= timedelta(days=1)
    return t.strftime("%Y%m%d")


def get_all_tickers() -> list[str]:
    kospi  = stock.get_market_ticker_list(market="KOSPI")
    kosdaq = stock.get_market_ticker_list(market="KOSDAQ")
    return list(kospi) + list(kosdaq)


def analyze_chart(ticker: str, date: str) -> dict | None:
    """눌림목 차트 필터. 통과 시 지표 dict 반환, 탈락 시 None."""
    try:
        end   = date
        start = (datetime.strptime(date, "%Y%m%d") - timedelta(days=100)).strftime("%Y%m%d")
        df = stock.get_market_ohlcv_by_date(start, end, ticker)
        if len(df) < 62:
            return None

        close  = df["종가"]
        volume = df["거래량"]
        ma20   = close.rolling(20).mean()
        ma60   = close.rolling(60).mean()

        cur       = float(close.iloc[-1])
        cur_ma20  = float(ma20.iloc[-1])
        cur_ma60  = float(ma60.iloc[-1])

        # 1) 정배열
        if cur_ma20 < cur_ma60:
            return None

        # 2) 20일선 근접
        gap = (cur - cur_ma20) / cur_ma20
        if not (DRAWDOWN_MAX <= gap <= MA20_BAND):
            return None

        # 3) 거래량 수축
        recent_vol = float(volume.iloc[-5:].mean())
        base_vol   = float(volume.iloc[-25:-5].mean())
        vol_ratio  = recent_vol / base_vol if base_vol else 1.0
        if vol_ratio > VOLUME_CONTRACTION:
            return None

        # 4) 고점 대비 조정
        high30 = float(close.iloc[-30:].max())
        dd     = (cur - high30) / high30
        if not (DRAWDOWN_MIN <= dd <= DRAWDOWN_MAX):
            return None

        return {
            "ticker"           : ticker,
            "current_price"    : int(cur),
            "ma20"             : int(cur_ma20),
            "ma60"             : int(cur_ma60),
            "ma20_gap_pct"     : round(gap * 100, 2),
            "vol_ratio"        : round(vol_ratio, 2),
            "drawdown_pct"     : round(dd * 100, 2),
        }
    except Exception:
        return None


def get_supply(ticker: str, date: str) -> dict | None:
    """외인·기관 3일 수급. 개인만 순매수+외인기관 이탈이면 즉시 None."""
    try:
        end   = date
        start = (datetime.strptime(date, "%Y%m%d") - timedelta(days=14)).strftime("%Y%m%d")
        df = stock.get_market_trading_value_by_date(start, end, ticker)
        if len(df) < 3:
            return None

        r    = df.iloc[-3:]
        f_net = int(r["외국인합계"].sum())
        i_net = int(r["기관합계"].sum())
        p_net = int(r["개인"].sum())

        # 하드 REJECT: 개인만 사고 외인+기관 둘 다 이탈
        if p_net > 0 and f_net < 0 and i_net < 0:
            return None

        return {
            "foreign_net_3d"    : f_net,
            "institution_net_3d": i_net,
            "individual_net_3d" : p_net,
        }
    except Exception:
        return None


def get_name(ticker: str) -> str:
    try:
        return stock.get_market_ticker_name(ticker)
    except Exception:
        return ticker


def scrape_news(ticker: str) -> list[str]:
    try:
        url = (
            f"https://finance.naver.com/item/news_news.naver"
            f"?code={ticker}&page=1&sm=title_entity_id.basic&clusterId="
        )
        res  = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        soup = BeautifulSoup(res.text, "html.parser")
        return [a.get_text(strip=True) for a in soup.select("td.title a")[:5]]
    except Exception:
        return []


def ask_gemini(candidates: list[dict]) -> list[dict]:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-1.5-flash")

    data_str = json.dumps(candidates, ensure_ascii=False, indent=2)

    prompt = f"""당신은 대한민국 최상위 헤지펀드의 '리스크 관리 총괄 심사역'입니다.
목표는 단 하나입니다: "지금 매수해서 2주(10영업일) 안에 무조건 +5%를 찍고 나올 종목 1~2개만 엄선."
절대 대충 넘어가지 말고, 제공된 종목의 [차트] [수급 상세] [뉴스/재료]를 3중으로 교차 검증(Cross-Check)하세요.

[1단계: 수급 검증 (배점 40점)]
- 조정 3일간 외인·기관 누적 순매도가 잠잠한가?
- 주가는 떨어지는데 개인만 대량 순매수하고 외인/기관이 이탈 중이라면 점수와 관계없이 즉시 탈락(REJECT).

[2단계: 재료 및 모멘텀 검증 (배점 40점)]
- 이 재료가 이미 반영되어 끝난 단기 소멸성 뉴스인가, 향후 2주간 후속 기사가 계속 나올 수 있는 연속적 모멘텀인가?
- 해당 테마에서 확실한 대장주(선도주)인가? (후발주/잡주 즉시 탈락)
- 최근 전환사채(CB), 유상증자, 대주주 매도 등 악재성 공시가 섞여 있지 않은가?

[3단계: 지지선 및 5% 손익비 검증 (배점 20점)]
- 20일선 근접 구간에서 지지 캔들이 확인되는가?
- 현 가격 대비 목표가(+5.0%)까지 저항 매물대가 비어 있는가?
- 지지선 이탈 시 손절 기준(-2.5%)이 명확한가?

총점 90점 이상인 종목에 대해서만 아래 JSON으로 응답하세요.
90점 초과 종목이 없다면 반드시 빈 배열 []을 반환하세요. 억지 추천 절대 금지.

분석 대상 종목 데이터:
{data_str}

JSON 배열로만 응답하세요. 코드블록(```)이나 다른 텍스트 없이 순수 JSON만:
[
  {{
    "ticker": "종목코드",
    "name": "종목명",
    "score": 93,
    "supply_check": "수급 검증 통과 이유",
    "catalyst_check": "재료 지속성 검증 이유",
    "entry_plan": {{
      "buy_price": 매수가,
      "target_price": 목표가,
      "stop_loss": 손절가
    }}
  }}
]"""

    response = model.generate_content(prompt)
    raw = response.text.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raw = raw.replace("```json", "").replace("```", "").strip()
        try:
            return json.loads(raw)
        except Exception:
            return []


def main():
    date = get_trading_date()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 기준일: {date}")

    tickers = get_all_tickers()
    print(f"전종목 {len(tickers)}개 분석 시작...")

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

        name = get_name(ticker)
        news = scrape_news(ticker)

        candidates.append({**chart, "name": name, **supply, "recent_news": news})

        if len(candidates) >= MAX_CANDIDATES_FOR_AI:
            break

    print(f"눌림목 후보 {len(candidates)}개 발견")

    if not candidates:
        recommendations = []
        message = "오늘은 조건을 충족하는 종목이 없습니다."
    else:
        print("Gemini AI 심사 중...")
        recommendations = ask_gemini(candidates)
        message = f"후보 {len(candidates)}개 중 {len(recommendations)}개 통과"

    output = {
        "date"            : date,
        "updated_at"      : datetime.now().strftime("%Y-%m-%d %H:%M"),
        "candidates_count": len(candidates),
        "message"         : message,
        "recommendations" : recommendations,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/recommendations.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"완료 — 추천 {len(recommendations)}개")


if __name__ == "__main__":
    main()
