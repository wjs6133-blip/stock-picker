"""
2026년 1월~현재월 백테스트 일괄 실행
- 원본 필터로만 실행
- 캐시 자동 갱신 (2025-01-01 ~ 2026-12-31)
- 월별 web_data.json 생성까지 자동 수행
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# 실행할 월 범위 (올해 1월 ~ 지난달)
YEAR   = 2026
now    = datetime.now()
months = list(range(1, now.month))   # 완전히 끝난 달까지 (현재월 제외)
# 현재월이 5일 이상 지났으면 포함
if now.day >= 5:
    months.append(now.month)

print(f"{'='*60}")
print(f"2026년 백테스트 일괄 실행: {len(months)}개월")
print(f"대상: {', '.join(f'{YEAR}-{m:02d}' for m in months)}")
print(f"{'='*60}\n")

results = {}

for m in months:
    ym  = f"{YEAR}-{m:02d}"
    tag = f"{YEAR}_{m:02d}_orig"
    out_dir = Path(f"data/backtest/{tag}")

    # 이미 완료된 달 건너뜀
    if (out_dir / "summary.json").exists():
        print(f"[{ym}] 이미 완료 — 건너뜀")
    else:
        print(f"\n[{ym}] 백테스트 시작...")
        ret = subprocess.run(
            [sys.executable, "scripts/backtest_september.py", ym],
            cwd=Path(__file__).parent.parent,
        )
        if ret.returncode not in (0, 1):   # 1 = em-dash 인코딩 오류지만 데이터는 저장됨
            print(f"[{ym}] 실패 (code={ret.returncode})")
            continue
        print(f"[{ym}] 완료")

    # web_data.json 생성
    web_path = out_dir / "web_data.json"
    if not web_path.exists() or (out_dir / "summary.json").exists():
        import glob
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
                     "fmax":  p.get("fwd10d_max_pct")}
                    for p in picks
                ]
            with open(web_path, "w", encoding="utf-8") as f:
                json.dump(web, f, ensure_ascii=False, separators=(",", ":"))
            print(f"  [{ym}] web_data.json 생성 ({len(web)}일)")

    # 통계 수집
    try:
        import glob as _glob
        all_picks = []
        for fp in sorted(_glob.glob(str(out_dir / f"{YEAR}-{m:02d}-*.json"))):
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

# 전체 요약 출력 및 저장
print(f"\n{'='*60}")
print(f"2026년 백테스트 종합 결과")
print(f"{'='*60}")
print(f"{'월':>8} | {'거래일':>4} | {'종목':>4} | {'평균10일':>8} | {'승률':>5} | {'+5%':>5} | {'+10%':>5} | {'장중+5%':>6}")
print("-" * 65)
for ym, s in sorted(results.items()):
    print(f"{ym:>8} | {s['days']:>4} | {s['total']:>4} | {s['avg10']:>+8.2f}% | {s['win']:>4.1f}% | {s['hit5']:>4.1f}% | {s['hit10']:>4.1f}% | {s['maxhit5']:>5.1f}%")

if results:
    all_f10  = [r["avg10"]   for r in results.values()]
    all_hit5 = [r["hit5"]    for r in results.values()]
    all_mh5  = [r["maxhit5"] for r in results.values()]
    print("-" * 65)
    print(f"{'전체평균':>8} | {'':>4} | {'':>4} | {sum(all_f10)/len(all_f10):>+8.2f}% | {'':>5} | {sum(all_hit5)/len(all_hit5):>4.1f}% | {'':>5} | {sum(all_mh5)/len(all_mh5):>5.1f}%")

# 결과 저장
Path("data/backtest").mkdir(exist_ok=True)
with open("data/backtest/2026_summary.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("\n결과 저장: data/backtest/2026_summary.json")
