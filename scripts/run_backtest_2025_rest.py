"""
2025년 6~12월 orig + phase-auto 백테스트 일괄 실행
"""
import subprocess, sys, os
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

YEAR = 2025
MONTHS = list(range(6, 13))

print(f"{'='*60}")
print(f"2025년 6~12월 백테스트 ({len(MONTHS)}개월)")
print(f"{'='*60}\n")

for m in MONTHS:
    ym = f"{YEAR}-{m:02d}"
    out_orig  = ROOT / f"data/backtest/{YEAR}_{m:02d}_orig"
    out_phase = ROOT / f"data/backtest/{YEAR}_{m:02d}_phase"

    orig_done  = (out_orig  / "summary.json").exists()
    phase_done = (out_phase / "summary.json").exists()

    if orig_done and phase_done:
        print(f"[{ym}] 이미 완료 — 건너뜀")
        continue

    print(f"[{ym}] 시작 (orig={'완료' if orig_done else '실행'}, phase={'완료' if phase_done else '실행'})...")
    t0 = datetime.now()

    procs = []
    if not orig_done:
        p = subprocess.Popen(
            [sys.executable, "scripts/backtest_september.py", ym],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(ROOT),
        )
        procs.append(("orig", p))
    if not phase_done:
        p = subprocess.Popen(
            [sys.executable, "scripts/backtest_september.py", ym, "--phase-auto"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(ROOT),
        )
        procs.append(("phase", p))

    for tag, p in procs:
        out_bytes, _ = p.communicate()
        out = out_bytes.decode("utf-8", errors="replace")
        lines = [l for l in out.strip().splitlines() if l.strip()]
        for line in lines[-5:]:
            print(f"  [{tag}] {line}")

    elapsed = (datetime.now() - t0).seconds
    print(f"[{ym}] 완료 ({elapsed}초)\n")

print(f"\n{'='*60}")
print("web_data.json 생성 중...")
subprocess.run([sys.executable, "scripts/patch_fwd20.py"], check=False, cwd=str(ROOT))
print(f"{'='*60}")
print("2025년 6~12월 백테스트 완료!")
