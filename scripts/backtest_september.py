"""
눌림목 월별 백테스트
usage: python backtest_september.py [YYYY-MM]
  예) python backtest_september.py 2025-10
- 각 거래일 기준 그날까지의 데이터만 사용해 눌림목 후보 선별
- Gemini/뉴스 없음 (과거 날짜 뉴스 조회 불가 -> 차트 필터만)
- 전종목 OHLCV를 한 번 bulk 다운로드 후 날짜별 슬라이스 (속도 최적화)
- 추천 후 10거래일 실제 수익률 자동 계산
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
import calendar

import FinanceDataReader as fdr
import pandas as pd

# ── 파라미터 ───────────────────────────────────────────────────
_ym = sys.argv[1] if len(sys.argv) > 1 else "2025-09"
_y, _m = int(_ym.split("-")[0]), int(_ym.split("-")[1])
_last_day = calendar.monthrange(_y, _m)[1]

BACKTEST_START  = f"{_y}-{_m:02d}-01"
BACKTEST_END    = f"{_y}-{_m:02d}-{_last_day:02d}"
CACHE_START     = "2024-01-01"                              # 고정: 충분한 히스토리
CACHE_END       = datetime.now().strftime("%Y-%m-%d")       # 오늘까지 (동적)
FORWARD_DAYS    = 10
MAX_PER_DAY     = 15             # 일별 최대 후보 수
CACHE_DIR       = Path("data/backtest/cache")

# 플래그 처리
_use_new    = "--new"        in sys.argv
_phase_auto = "--phase-auto" in sys.argv
_tag        = "new" if _use_new else "phase" if _phase_auto else "orig"
OUTPUT_DIR  = Path(f"data/backtest/{_y}_{_m:02d}_{_tag}")

# 국면별 필터 프리셋
_PHASE_PRESETS = {
    'BULL': dict(VOL_MAX=0.70, VOL_MIN=0.40, MA20_UPPER=0.04,  MA20_LOWER=-0.03, DD_MIN=-0.20, DD_MAX=-0.03, MIN_VOL=0.030, MAX_VOL=1.0),
    'SIDE': dict(VOL_MAX=0.70, VOL_MIN=0.45, MA20_UPPER=0.03,  MA20_LOWER=-0.03, DD_MIN=-0.15, DD_MAX=-0.03, MIN_VOL=0.030, MAX_VOL=1.0),
    'BEAR': dict(VOL_MAX=0.70, VOL_MIN=0.50, MA20_UPPER=0.03,  MA20_LOWER= 0.00, DD_MIN=-0.10, DD_MAX= 0.00, MIN_VOL=0.015, MAX_VOL=0.07),
}

def _detect_phase_at(kospi_df: pd.DataFrame, as_of: str) -> str:
    """특정 기준일의 KOSPI 국면 반환"""
    try:
        df   = kospi_df[kospi_df.index <= as_of].copy()
        if len(df) < 25: return 'SIDE'
        close     = df['Close'].astype(float)
        ma20_ser  = close.rolling(20).mean().dropna()
        cur       = float(close.iloc[-1])
        ma20_now  = float(ma20_ser.iloc[-1])
        ma20_5ago = float(ma20_ser.iloc[-6]) if len(ma20_ser) >= 6 else ma20_now
        gap_pct   = (cur - ma20_now) / ma20_now * 100
        slope     = ma20_now - ma20_5ago
        if gap_pct > 3 and slope > 0:    return 'BULL'
        elif gap_pct < -3 or slope < -(ma20_now * 0.012): return 'BEAR'
        else:                             return 'SIDE'
    except Exception:
        return 'SIDE'

# 필터 파라미터 (phase-auto 아닌 경우 고정값)
if _use_new:
    VOLUME_CONTRACTION  = 0.65
    VOL_RATIO_MIN       = 0.25
    MA20_BAND           = 0.010
    DRAWDOWN_MIN        = -0.20
    DRAWDOWN_MAX        = -0.03
    MIN_DAILY_VOL       = 0.030
    MAX_DAILY_VOL       = 1.0
elif not _phase_auto:
    # 원본 필터
    VOLUME_CONTRACTION  = 0.65
    VOL_RATIO_MIN       = 0.0
    MA20_BAND           = 0.04
    DRAWDOWN_MIN        = -0.20
    DRAWDOWN_MAX        = -0.03
    MIN_DAILY_VOL       = 0.015
    MAX_DAILY_VOL       = 1.0


# ── 종목 목록 ────────────────────────────────────────────────────
def get_name_map() -> dict:
    df       = fdr.StockListing('KRX')
    code_col = next((c for c in ['Code', 'Symbol'] if c in df.columns), df.columns[0])
    name_col = next((c for c in ['Name', 'ISU_NM']  if c in df.columns), df.columns[1])
    return dict(zip(df[code_col].astype(str), df[name_col].astype(str)))


# ── 전종목 데이터 캐시 ────────────────────────────────────────────
def load_or_fetch_ticker(ticker: str, name_map: dict) -> pd.DataFrame | None:
    """캐시 있으면 로드(stale이면 재fetch), 없으면 FDR로 다운로드 후 저장"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{ticker}.parquet"
    if cache_path.exists():
        try:
            df = pd.read_parquet(cache_path)
            last = str(df.index[-1].date())
            # 캐시가 CACHE_END - 10일 이상 커버하면 재사용
            needed = (datetime.strptime(CACHE_END, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
            if last >= needed:
                return df
        except Exception:
            pass
    try:
        df = fdr.DataReader(ticker, CACHE_START, CACHE_END)
        if df is None or len(df) < 20:
            return None
        df.to_parquet(cache_path)
        return df
    except Exception:
        return None


# ── 거래일 목록 ──────────────────────────────────────────────────
def get_trading_days(start: str, end: str) -> list[str]:
    df = fdr.DataReader('KS11', start, end)
    return [d.strftime('%Y-%m-%d') for d in df.index]


# ── 차트 필터 (특정 기준일까지 데이터 슬라이스) ─────────────────
def analyze_chart_at(df_full: pd.DataFrame, as_of: str, phase_params: dict | None = None) -> dict | None:
    """df_full에서 as_of 날짜까지만 사용해 눌림목 필터 적용
    phase_params 없으면 모듈 상단 고정값 사용 (orig/new 모드)"""
    try:
        df = df_full[df_full.index <= as_of].copy()
        if len(df) < 62:
            return None

        close  = df['Close'].astype(float)
        volume = df['Volume'].astype(float)
        ma20   = close.rolling(20).mean()
        ma60   = close.rolling(60).mean()

        cur      = float(close.iloc[-1])
        cur_ma20 = float(ma20.iloc[-1])
        cur_ma60 = float(ma60.iloc[-1])

        if any(pd.isna(v) for v in [cur, cur_ma20, cur_ma60]) or cur == 0:
            return None

        # 정배열 (MA20 > MA60)
        if cur_ma20 < cur_ma60:
            return None

        # 국면 파라미터 선택
        if phase_params:
            p_ma_lo   = phase_params['MA20_LOWER']
            p_ma_up   = phase_params['MA20_UPPER']
            p_vol_min = phase_params['VOL_MIN']
            p_vol_max = phase_params['VOL_MAX']
            p_dd_min  = phase_params['DD_MIN']
            p_dd_max  = phase_params['DD_MAX']
            p_dv_min  = phase_params['MIN_VOL']
            p_dv_max  = phase_params['MAX_VOL']
        else:
            p_ma_lo   = DRAWDOWN_MAX   # 기존 변수명 재사용 (MA20 하한)
            p_ma_up   = MA20_BAND
            p_vol_min = VOL_RATIO_MIN
            p_vol_max = VOLUME_CONTRACTION
            p_dd_min  = DRAWDOWN_MIN
            p_dd_max  = DRAWDOWN_MAX
            p_dv_min  = MIN_DAILY_VOL
            p_dv_max  = MAX_DAILY_VOL

        # MA20 위치
        gap = (cur - cur_ma20) / cur_ma20
        if not (p_ma_lo <= gap <= p_ma_up):
            return None

        # 거래량 수축
        rv        = float(volume.iloc[-5:].mean())
        bv        = float(volume.iloc[-25:-5].mean())
        vol_ratio = rv / bv if bv > 0 else 1.0
        if not (p_vol_min <= vol_ratio <= p_vol_max):
            return None

        # 고점 대비 조정폭
        high30 = float(close.iloc[-30:].max())
        dd     = (cur - high30) / high30
        if not (p_dd_min <= dd <= p_dd_max):
            return None

        # MA20 기울기 우상향
        if len(ma20) < 6 or float(ma20.iloc[-1]) <= float(ma20.iloc[-6]):
            return None

        # 최근 5일 수익률 -3% 초과 하락 제외
        if len(close) >= 6:
            ret5 = (cur - float(close.iloc[-6])) / float(close.iloc[-6])
            if ret5 < -0.03:
                return None

        # 일변동성 범위
        daily_vol = float(close.pct_change().dropna().iloc[-20:].std())
        if not (p_dv_min <= daily_vol <= p_dv_max):
            return None

        return {
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


# ── Forward Return 계산 ─────────────────────────────────────────
def calc_forward_return(df_full: pd.DataFrame, from_date: str, n: int = 10) -> dict:
    """from_date 이후 거래일 기준 수익률"""
    try:
        df_fwd = df_full[df_full.index >= from_date]
        if len(df_fwd) < 2:
            return {}
        entry = float(df_fwd['Close'].iloc[0])
        result = {}
        for days in [5, 10]:
            if len(df_fwd) > days:
                exit_p = float(df_fwd['Close'].iloc[days])
                result[f"fwd{days}d_pct"] = round((exit_p - entry) / entry * 100, 2)
                result[f"fwd{days}d_price"] = int(exit_p)
            else:
                last = float(df_fwd['Close'].iloc[-1])
                result[f"fwd{days}d_pct"] = round((last - entry) / entry * 100, 2)
                result[f"fwd{days}d_price"] = int(last)
        # 최고가 (10거래일 / 20거래일 내 고점 기준 최대 수익률)
        if len(df_fwd) >= 2:
            high10 = float(df_fwd['High'].iloc[1:min(11, len(df_fwd))].max())
            result["fwd10d_max_pct"]   = round((high10 - entry) / entry * 100, 2)
            result["fwd10d_max_price"] = int(high10)
            high20 = float(df_fwd['High'].iloc[1:min(21, len(df_fwd))].max())
            result["fwd20d_max_pct"]   = round((high20 - entry) / entry * 100, 2)
            result["fwd20d_max_price"] = int(high20)
        return result
    except Exception:
        return {}


# ── 메인 ─────────────────────────────────────────────────────────
def main():
    print(f"{'='*60}")
    print(f"2025년 9월 백테스트 시작")
    print(f"{'='*60}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 거래일 목록
    trading_days = get_trading_days(BACKTEST_START, BACKTEST_END)
    print(f"거래일: {len(trading_days)}일 ({trading_days[0]} ~ {trading_days[-1]})")

    # 종목 목록
    print("KRX 전종목 목록 로드 중...")
    name_map = get_name_map()
    tickers  = list(name_map.keys())
    print(f"종목 수: {len(tickers)}개")

    # phase-auto 모드: KOSPI 데이터 사전 로드
    kospi_df = None
    if _phase_auto:
        print("KOSPI 데이터 로드 중 (phase-auto 모드)...")
        kospi_df = fdr.DataReader('KS11', CACHE_START, CACHE_END)
        print(f"KOSPI 로드 완료: {len(kospi_df)}일")

    # 전종목 데이터 bulk 캐시
    print(f"\n전종목 데이터 다운로드/캐시 로드 중 (최초 실행 시 수십 분 소요)...")
    all_data: dict[str, pd.DataFrame] = {}
    for i, ticker in enumerate(tickers):
        if i % 100 == 0:
            print(f"  {i}/{len(tickers)} 캐시 처리 중...")
        df = load_or_fetch_ticker(ticker, name_map)
        if df is not None:
            all_data[ticker] = df
        if i < len(tickers) - 1 and i % 50 == 49:
            time.sleep(0.5)   # FDR 과부하 방지

    print(f"데이터 로드 완료: {len(all_data)}개 종목")

    # ── 일별 백테스트 ────────────────────────────────────────────
    all_daily: list[dict] = []
    phase_log: dict[str, str] = {}  # {date: phase}

    for day_idx, trade_date in enumerate(trading_days):
        # phase-auto: 해당일 국면 감지
        if _phase_auto and kospi_df is not None:
            day_phase  = _detect_phase_at(kospi_df, trade_date)
            day_params = _PHASE_PRESETS[day_phase]
            day_max    = 5 if day_phase == 'BEAR' else MAX_PER_DAY
        else:
            day_phase  = None
            day_params = None
            day_max    = MAX_PER_DAY

        phase_log[trade_date] = day_phase or _tag
        phase_str = f" [{day_phase}]" if day_phase else ""
        print(f"\n[{day_idx+1}/{len(trading_days)}] {trade_date}{phase_str} 분석 중...")

        day_candidates = []
        for ticker, df_full in all_data.items():
            result = analyze_chart_at(df_full, trade_date, day_params)
            if result is None:
                continue

            fwd = calc_forward_return(df_full, trade_date)

            day_candidates.append({
                "ticker"       : ticker,
                "name"         : name_map.get(ticker, ticker),
                "date"         : trade_date,
                **result,
                **fwd,
            })

            if len(day_candidates) >= day_max:
                break

        # 변동성 높은 순으로 정렬 (더 많이 움직일 종목 우선)
        day_candidates.sort(key=lambda x: x.get("daily_vol_pct", 0), reverse=True)

        print(f"  후보: {len(day_candidates)}개")
        for c in day_candidates:
            fwd10 = c.get('fwd10d_pct')
            fwd10_max = c.get('fwd10d_max_pct')
            fwd_str = f"10일후={fwd10:+.1f}%, 최고={fwd10_max:+.1f}%" if fwd10 is not None else "forward N/A"
            print(f"    {c['ticker']} {c['name']:10s}  {c['current_price']:>8,}원  {fwd_str}")

        all_daily.append({
            "date"      : trade_date,
            "candidates": day_candidates,
        })

        # 일별 저장
        with open(OUTPUT_DIR / f"{trade_date}.json", "w", encoding="utf-8") as f:
            json.dump(day_candidates, f, ensure_ascii=False, indent=2)

    # ── 전체 요약 ─────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("전체 결과 집계 중...")

    all_entries: list[dict] = []
    for day in all_daily:
        all_entries.extend(day["candidates"])

    # 중복 종목: 첫 등장만 유지
    seen: dict[str, dict] = {}
    for c in all_entries:
        if c["ticker"] not in seen:
            seen[c["ticker"]] = c
    unique = list(seen.values())

    # 수익률 통계
    fwd10_list  = [c["fwd10d_pct"]     for c in unique if c.get("fwd10d_pct")     is not None]
    fwd5_list   = [c["fwd5d_pct"]      for c in unique if c.get("fwd5d_pct")      is not None]
    max10_list  = [c["fwd10d_max_pct"] for c in unique if c.get("fwd10d_max_pct") is not None]

    stats = {}
    for label, lst in [("10거래일", fwd10_list), ("5거래일", fwd5_list), ("10일내최고", max10_list)]:
        if lst:
            stats[label] = {
                "count"     : len(lst),
                "avg_pct"   : round(sum(lst) / len(lst), 2),
                "win_rate"  : round(sum(1 for r in lst if r > 0) / len(lst) * 100, 1),
                "hit5pct"   : round(sum(1 for r in lst if r >= 5) / len(lst) * 100, 1),
                "hit10pct"  : round(sum(1 for r in lst if r >= 10) / len(lst) * 100, 1),
                "best"      : round(max(lst), 2),
                "worst"     : round(min(lst), 2),
            }

    # 종목별 상세 (10일 수익률 내림차순)
    unique.sort(key=lambda x: (x.get("fwd10d_pct") or -999), reverse=True)

    summary = {
        "period"           : f"{BACKTEST_START} ~ {BACKTEST_END}",
        "trading_days"     : len(trading_days),
        "total_appearances": len(all_entries),
        "unique_tickers"   : len(unique),
        "mode"             : _tag,
        "stats"            : stats,
        "top_performers"   : unique[:10],
        "all_results"      : unique,
        "daily_counts"     : {d["date"]: len(d["candidates"]) for d in all_daily},
        "phase_log"        : phase_log,
    }

    with open(OUTPUT_DIR / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # ── 콘솔 결과 출력 ───────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"백테스트 결과 [{_y}년 {_m}월 / {_tag}]")
    print(f"{'='*60}")
    print(f"거래일         : {len(trading_days)}일")
    print(f"총 등장 건수   : {len(all_entries)}건 (중복 포함)")
    print(f"고유 종목 수   : {len(unique)}개")
    print()

    for label, s in stats.items():
        print(f"[{label}]")
        print(f"  평균 수익률  : {s['avg_pct']:+.2f}%")
        print(f"  승률 (>0%)   : {s['win_rate']:.1f}%")
        print(f"  +5% 달성률   : {s['hit5pct']:.1f}%")
        print(f"  +10% 달성률  : {s['hit10pct']:.1f}%")
        print(f"  최고 / 최저  : {s['best']:+.2f}% / {s['worst']:+.2f}%")
        print()

    print("상위 10개 종목 (10거래일 수익률 기준):")
    print(f"{'날짜':>12}  {'코드':>7}  {'종목명':12}  {'진입가':>9}  {'10일후':>8}  {'최고':>8}")
    print("-" * 65)
    for c in unique[:10]:
        fwd10   = c.get('fwd10d_pct')
        fwd_max = c.get('fwd10d_max_pct')
        fwd10_s   = f"{fwd10:>+7.1f}%"   if fwd10   is not None else "    N/A"
        fwd_max_s = f"{fwd_max:>+7.1f}%" if fwd_max is not None else "    N/A"
        print(f"{c['date']:>12}  {c['ticker']:>7}  {c['name'][:12]:12}  {c['current_price']:>9,}  {fwd10_s}  {fwd_max_s}")

    print(f"\n결과 저장 완료: {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
