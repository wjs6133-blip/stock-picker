"""
GitHub Actions 전용: FinanceDataReader로 눌림목 후보 수집
→ data/candidates.json 저장 (AI 분석 없음, 버튼 클릭 시 Gemini가 실시간 처리)

[국면별 필터]
 BULL  : KOSPI > MA20 + MA20 우상향  → 깊은 눌림(-20%)·고변동(3%) 허용, 최대 15종목
 SIDE  : KOSPI ≈ MA20 (±3%)         → 중간 눌림(-15%), 고변동(3%), MA20 ±3%, 최대 15종목
 BEAR  : KOSPI < MA20 or MA20 하향  → MA20 위 종목만, 얕은 눌림(-10%), 최대 5종목
"""
import json
import os
import time
from datetime import datetime, timedelta

import FinanceDataReader as fdr
import pandas as pd
import requests
from bs4 import BeautifulSoup


# ── 국면별 필터 파라미터 ───────────────────────────────────────────
PHASE_PARAMS = {
    'BULL': {
        'VOL_MAX':       0.70,
        'VOL_MIN':       0.40,
        'MA20_UPPER':    0.04,   # MA20 위로 최대 4%
        'MA20_LOWER':   -0.03,   # MA20 아래로 최대 3%
        'DD_MIN':       -0.20,   # 30일 고점 대비 최대 20% 하락
        'DD_MAX':       -0.03,   # 최소 3% 이상 눌림
        'MIN_DAILY_VOL': 0.030,  # 일간 변동성 3% 이상
        'MAX_DAILY_VOL': 1.0,
        'MAX_CANDS':     15,
        'LABEL':         '상승장',
        'EMOJI':         '🟢',
        'GUIDE':         '10일 보유, 깊은 눌림(-10~-20%)·고변동 종목 선호',
    },
    'SIDE': {
        'VOL_MAX':       0.70,
        'VOL_MIN':       0.45,
        'MA20_UPPER':    0.03,   # MA20 위로 최대 3%
        'MA20_LOWER':   -0.03,
        'DD_MIN':       -0.15,
        'DD_MAX':       -0.03,
        'MIN_DAILY_VOL': 0.030,
        'MAX_DAILY_VOL': 1.0,
        'MAX_CANDS':     15,
        'LABEL':         '횡보장',
        'EMOJI':         '🟡',
        'GUIDE':         '5일 단기 보유, MA20 근접·고변동 종목 집중',
    },
    'BEAR': {
        'VOL_MAX':       0.70,
        'VOL_MIN':       0.50,
        'MA20_UPPER':    0.03,   # MA20 위 종목만
        'MA20_LOWER':    0.00,
        'DD_MIN':       -0.10,
        'DD_MAX':        0.00,   # 고점 근처 (상대강도 강한 종목)
        'MIN_DAILY_VOL': 0.015,
        'MAX_DAILY_VOL': 0.07,   # 과도한 변동성 제외
        'MAX_CANDS':      5,
        'LABEL':         '하락장',
        'EMOJI':         '🔴',
        'GUIDE':         '단기(2~3일) 장중 익절 목표, 10일 보유 금지',
    },
}


def detect_market_phase() -> tuple[str, dict]:
    """KOSPI MA20 기반 시장 국면 판별"""
    try:
        end   = datetime.now()
        start = end - timedelta(days=90)
        kospi = fdr.DataReader('KS11', start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
        if kospi is None or len(kospi) < 25:
            return 'SIDE', {}

        close    = kospi['Close'].astype(float)
        ma20     = close.rolling(20).mean()
        cur      = float(close.iloc[-1])
        cur_ma20 = float(ma20.dropna().iloc[-1])
        slope_5d = float(ma20.dropna().iloc[-1]) - float(ma20.dropna().iloc[-6])
        gap_pct  = (cur - cur_ma20) / cur_ma20 * 100

        if gap_pct > 3 and slope_5d > 0:
            phase = 'BULL'
        elif gap_pct < -3 or slope_5d < -(cur_ma20 * 0.012):
            phase = 'BEAR'
        else:
            phase = 'SIDE'

        return phase, {
            'kospi':      round(cur, 2),
            'kospi_ma20': round(cur_ma20, 2),
            'gap_pct':    round(gap_pct, 2),
            'slope_5d':   round(slope_5d, 2),
        }
    except Exception as e:
        return 'SIDE', {'error': str(e)}


def get_all_tickers_with_names() -> dict:
    df       = fdr.StockListing('KRX')
    code_col = next((c for c in ['Code', 'Symbol', 'ISU_SRT_CD'] if c in df.columns), df.columns[0])
    name_col = next((c for c in ['Name', 'ISU_NM', 'CompanyName']  if c in df.columns), df.columns[1])
    return dict(zip(df[code_col].astype(str), df[name_col].astype(str)))


def analyze_chart(ticker: str, params: dict) -> dict | None:
    """눌림목 차트 필터 (국면별 params 적용)"""
    try:
        end   = datetime.now()
        start = end - timedelta(days=130)
        df = fdr.DataReader(ticker, start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
        if df is None or len(df) < 62:
            return None

        close  = df['Close'].astype(float)
        volume = df['Volume'].astype(float)
        ma20   = close.rolling(20).mean()
        ma60   = close.rolling(60).mean()

        cur      = float(close.iloc[-1])
        cur_ma20 = float(ma20.iloc[-1])
        cur_ma60 = float(ma60.iloc[-1])

        if pd.isna(cur_ma20) or pd.isna(cur_ma60) or cur == 0:
            return None

        # 1) 정배열 (MA20 > MA60)
        if cur_ma20 < cur_ma60:
            return None

        # 2) MA20 위치 (국면별 범위)
        gap = (cur - cur_ma20) / cur_ma20
        if not (params['MA20_LOWER'] <= gap <= params['MA20_UPPER']):
            return None

        # 3) 거래량 수축 (국면별 범위)
        recent_vol = float(volume.iloc[-5:].mean())
        base_vol   = float(volume.iloc[-25:-5].mean())
        vol_ratio  = recent_vol / base_vol if base_vol > 0 else 1.0
        if not (params['VOL_MIN'] <= vol_ratio <= params['VOL_MAX']):
            return None

        # 4) 고점 대비 조정폭 (국면별 범위)
        high30 = float(close.iloc[-30:].max())
        dd     = (cur - high30) / high30
        if not (params['DD_MIN'] <= dd <= params['DD_MAX']):
            return None

        # 5) MA20 기울기 우상향
        if len(ma20.dropna()) < 6 or float(ma20.dropna().iloc[-1]) <= float(ma20.dropna().iloc[-6]):
            return None

        # 6) 최근 5일 급락 제외 (-3% 이상 하락)
        if len(close) >= 6:
            ret5 = (cur - float(close.iloc[-6])) / float(close.iloc[-6])
            if ret5 < -0.03:
                return None

        # 7) 일간 변동성 범위 (국면별)
        daily_vol = float(close.pct_change().dropna().iloc[-20:].std())
        if not (params['MIN_DAILY_VOL'] <= daily_vol <= params['MAX_DAILY_VOL']):
            return None

        return {
            "ticker"       : ticker,
            "current_price": int(cur),
            "ma20"         : int(cur_ma20),
            "ma60"         : int(cur_ma60),
            "ma20_gap_pct" : round(gap * 100, 2),
            "vol_ratio"    : round(vol_ratio, 2),
            "drawdown_pct" : round(dd * 100, 2),
            "daily_vol_pct": round(daily_vol * 100, 2),
        }
    except Exception:
        return None


def get_supply_naver(ticker: str) -> dict:
    try:
        url  = f"https://finance.naver.com/item/frgn.naver?code={ticker}"
        res  = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        soup = BeautifulSoup(res.text, "html.parser")
        rows = soup.select("table.type2 tr")
        values = []
        for row in rows[1:4]:
            cols = row.select("td")
            if len(cols) >= 5:
                try:
                    val = cols[4].get_text(strip=True).replace(",", "").replace("+", "")
                    values.append(int(val) if val and val != "-" else 0)
                except Exception:
                    values.append(0)
        return {"foreign_net_3d_approx": sum(values) if values else None, "supply_source": "naver_foreign"}
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

    # 시장 국면 감지
    phase, phase_info = detect_market_phase()
    params = PHASE_PARAMS[phase]
    print(f"시장 국면: {params['EMOJI']} {params['LABEL']} ({phase})")
    print(f"  KOSPI={phase_info.get('kospi','?')} / MA20={phase_info.get('kospi_ma20','?')} / "
          f"gap={phase_info.get('gap_pct','?')}% / 5일기울기={phase_info.get('slope_5d','?')}")
    print(f"  적용 가이드: {params['GUIDE']}")
    print(f"  최대 추천 종목: {params['MAX_CANDS']}개")

    name_map = get_all_tickers_with_names()
    tickers  = list(name_map.keys())
    print(f"전종목: {len(tickers)}개")

    candidates = []
    for i, ticker in enumerate(tickers):
        if i % 200 == 0:
            print(f"  {i}/{len(tickers)} ...")
            time.sleep(0.3)

        chart = analyze_chart(ticker, params)
        if not chart:
            continue

        supply = get_supply_naver(ticker)
        news   = scrape_news(ticker)
        name   = name_map.get(ticker, ticker)

        candidates.append({**chart, "name": name, **supply, "recent_news": news})

        if len(candidates) >= params['MAX_CANDS']:
            break

    print(f"후보 {len(candidates)}개 저장 ({params['LABEL']})")

    os.makedirs("data", exist_ok=True)
    with open("data/candidates.json", "w", encoding="utf-8") as f:
        json.dump({
            "date"        : datetime.now().strftime("%Y%m%d"),
            "updated_at"  : datetime.now().strftime("%Y-%m-%d %H:%M"),
            "market_phase": phase,
            "phase_label" : params['LABEL'],
            "phase_emoji" : params['EMOJI'],
            "phase_guide" : params['GUIDE'],
            "phase_info"  : phase_info,
            "candidates"  : candidates,
        }, f, ensure_ascii=False, indent=2)

    print("완료")


if __name__ == "__main__":
    main()
