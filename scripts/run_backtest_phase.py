"""
2026년 1~10월 phase-auto 백테스트 일괄 실행
- 각 거래일마다 KOSPI MA20 기반 국면 감지 후 필터 자동 적용
- 출력 디렉토리: data/backtest/2026_{월}_phase/
"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

YEAR   = 2026
MONTHS = list(range(1, 11))   # 1~10월

print(f"{'='*60}")
print(f"2026년 phase-auto 백테스트 일괄 실행: {len(MONTHS)}개월")
print(f"대상: {', '.join(f'{YEAR}-{m:02d}' for m in MONTHS)}")
print(f"{'='*60}\n")

results = {}

for m in MONTHS:
    ym      = f"{YEAR}-{m:02d}"
    out_dir = Path(f"data/backtest/{YEAR}_{m:02d}_phase")

    # 이미 완료된 달 건너뜀
    if (out_dir / "summary.json").exists():
        print(f"[{ym}] 이미 완료 - 건너뜀")
    else:
        print(f"\n[{ym}] phase-auto 백테스트 시작...")
        ret = subprocess.run(
            [sys.executable, "scripts/backtest_september.py", ym, "--phase-auto"],
            cwd=Path(__file__).parent.parent,
        )
        if ret.returncode not in (0, 1):
            print(f"[{ym}] 실패 (code={ret.returncode})")
            continue
        print(f"[{ym}] 완료")

    # web_data.json 생성
    import glob
    web_path  = out_dir / "web_data.json"
    day_files = sorted(glob.glob(str(out_dir / f"{YEAR}-{m:02d}-*.json")))
    if day_files:
        web = {}
        for fp in day_files:
            date = Path(fp).stem
            with open(fp, encoding="utf-8") as f:
                picks = json.load(f)
            web[date] = [
                {"ticker": p["ticker"], "name": p["name"],
                 "price": p["current_price"],
                 "f5":    p.get("fwd5d_pct"),
                 "f10":   p.get("fwd10d_pct"),
                 "fmax":  p.get("fwd20d_max_pct")}
                for p in picks
            ]
        with open(web_path, "w", encoding="utf-8") as f:
            json.dump(web, f, ensure_ascii=False, separators=(",", ":"))
        print(f"  [{ym}] web_data.json 생성 ({len(web)}일)")

    # 통계 수집
    try:
        all_picks = []
        for fp in sorted(glob.glob(str(out_dir / f"{YEAR}-{m:02d}-*.json"))):
            with open(fp, encoding="utf-8") as f:
                all_picks.extend(json.load(f))
        f10  = [p["fwd10d_pct"]     for p in all_picks if p.get("fwd10d_pct")     is not None]
        fmax = [p["fwd10d_max_pct"] for p in all_picks if p.get("fwd10d_max_pct") is not None]
        if f10:
            results[ym] = {
                "days"    : len(set(p["date"] for p in all_picks)),
                "total"   : len(f10),
                "avg10"   : round(sum(f10)/len(f10), 2),
                "win"     : round(sum(1 for v in f10 if v > 0)/len(f10)*100, 1),
                "hit5"    : round(sum(1 for v in f10 if v >= 5)/len(f10)*100, 1),
                "hit10"   : round(sum(1 for v in f10 if v >= 10)/len(f10)*100, 1),
                "maxhit5" : round(sum(1 for v in fmax if v >= 5)/len(fmax)*100, 1) if fmax else 0,
            }
    except Exception as e:
        print(f"  [{ym}] 통계 오류: {e}")

# 종합 결과 출력
print(f"\n{'='*60}")
print(f"2026년 phase-auto 백테스트 종합 결과")
print(f"{'='*60}")

orig_results = {}
try:
    with open("data/backtest/2026_summary.json", encoding="utf-8") as f:
        orig_results = json.load(f)
except Exception:
    pass

print(f"{'월':>8} | {'거래일':>4} | {'종목':>4} | {'phase avg10':>11} | {'orig avg10':>10} | {'phase +5%':>9} | {'orig +5%':>8} | {'장중+5%':>7}")
print("-" * 90)
for ym in sorted(results.keys()):
    s  = results[ym]
    o  = orig_results.get(ym, {})
    orig_avg  = f"{o.get('avg10',0):+.2f}%" if o else "    -"
    orig_hit5 = f"{o.get('hit5',0):.1f}%"   if o else "   -"
    print(f"{ym:>8} | {s['days']:>4} | {s['total']:>4} | {s['avg10']:>+11.2f}% | {orig_avg:>10} | {s['hit5']:>8.1f}% | {orig_hit5:>8} | {s['maxhit5']:>6.1f}%")

if results:
    pa = [r["avg10"] for r in results.values()]
    ph = [r["hit5"]  for r in results.values()]
    oa = [v.get("avg10", 0) for v in orig_results.values() if v]
    oh = [v.get("hit5",  0) for v in orig_results.values() if v]
    print("-" * 90)
    print(f"{'phase 평균':>8} | {'':>4} | {'':>4} | {sum(pa)/len(pa):>+11.2f}% | {sum(oa)/len(oa) if oa else 0:>+10.2f}% | {sum(ph)/len(ph):>8.1f}% | {sum(oh)/len(oh) if oh else 0:>8.1f}%")

Path("data/backtest").mkdir(exist_ok=True)
with open("data/backtest/2026_phase_summary.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("\n결과 저장: data/backtest/2026_phase_summary.json")
