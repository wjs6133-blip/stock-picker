"""
2025년 1~12월 orig + phase-auto 백테스트 일괄 실행
- 각 월: orig(기본 필터)와 phase-auto(국면별 필터) 동시 실행
- 완료된 월(summary.json 있으면) 건너뜀
- 출력 디렉토리: data/backtest/2025_{월}_orig / 2025_{월}_phase
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# 항상 프로젝트 루트에서 실행
ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

YEAR   = 2025
MONTHS = list(range(1, 13))   # 1~12월

print(f"{'='*60}")
print(f"2025년 orig+phase 백테스트 일괄 실행: {len(MONTHS)}개월")
print(f"대상: {', '.join(f'{YEAR}-{m:02d}' for m in MONTHS)}")
print(f"{'='*60}\n")

for m in MONTHS:
    ym       = f"{YEAR}-{m:02d}"
    out_orig  = Path(f"data/backtest/{YEAR}_{m:02d}_orig")
    out_phase = Path(f"data/backtest/{YEAR}_{m:02d}_phase")

    orig_done  = (out_orig  / "summary.json").exists()
    phase_done = (out_phase / "summary.json").exists()

    if orig_done and phase_done:
        print(f"[{ym}] 이미 완료 — 건너뜀")
        continue

    print(f"\n[{ym}] 시작 (orig={'완료' if orig_done else '실행'}, phase={'완료' if phase_done else '실행'})...")
    t0 = datetime.now()

    procs = []
    if not orig_done:
        p = subprocess.Popen(
            [sys.executable, "scripts/backtest_september.py", ym],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT,
        )
        procs.append(("orig", p))

    if not phase_done:
        p = subprocess.Popen(
            [sys.executable, "scripts/backtest_september.py", ym, "--phase-auto"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT,
        )
        procs.append(("phase", p))

    for tag, p in procs:
        out, _ = p.communicate()
        # 마지막 5줄만 출력
        lines = [l for l in out.strip().splitlines() if l.strip()]
        for line in lines[-5:]:
            print(f"  [{tag}] {line}")

    elapsed = (datetime.now() - t0).seconds
    print(f"[{ym}] 완료 ({elapsed}초)")

# web_data.json 일괄 생성
print(f"\n{'='*60}")
print("web_data.json 생성 중...")
subprocess.run([sys.executable, "scripts/patch_fwd20.py"], check=False)

print(f"\n{'='*60}")
print("2025년 전체 백테스트 완료")
print(f"{'='*60}")
