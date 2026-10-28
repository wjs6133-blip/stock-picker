"""
히스토리 성과 체크 스크립트
추천일로부터 14일(+2 여유)이 지난 기록에 대해
이후 10 거래일 종가 기준으로 목표가(+5%) 달성 여부를 기록합니다.
"""
import json
import os
from datetime import datetime, timedelta

import FinanceDataReader as fdr
import pandas as pd


def check_performance():
    index_path = "data/history/index.json"
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            idx = json.load(f)
    except Exception:
        print("index.json 없음")
        return

    today = datetime.now()

    for date_str in idx:
        hist_path = f"data/history/{date_str}.json"
        try:
            with open(hist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        if data.get("performance_checked"):
            continue

        rec_date = datetime.strptime(date_str, "%Y%m%d")
        # 16 영업일(약 23 달력일) 후 데이터가 쌓인 뒤 체크
        if today - rec_date < timedelta(days=16):
            print(f"{date_str}: 아직 2주 미경과, 스킵")
            continue

        print(f"{date_str}: 성과 체크 중...")
        recs = data.get("recommendations", [])

        # 추천일 다음날 ~ +20 달력일 범위로 조회
        start = (rec_date + timedelta(days=1)).strftime("%Y-%m-%d")
        end   = (rec_date + timedelta(days=22)).strftime("%Y-%m-%d")

        for r in recs:
            ticker      = r.get("ticker")
            entry_plan  = r.get("entry_plan") or {}
            buy_price   = entry_plan.get("buy_price")
            target_price = entry_plan.get("target_price")

            if not ticker or not buy_price or not target_price:
                r["performance"] = {"checked": False, "reason": "데이터 부족"}
                continue

            try:
                df = fdr.DataReader(ticker, start, end)
                if df is None or len(df) == 0:
                    r["performance"] = {"checked": False, "reason": "시세 없음"}
                    continue

                # 10 거래일 기준
                df10    = df.head(10)
                closes  = df10["Close"].astype(float).tolist()

                hit_target  = any(c >= target_price for c in closes)
                max_close   = max(closes) if closes else buy_price
                max_ret_pct = round((max_close - buy_price) / buy_price * 100, 2)
                final_close = int(closes[-1]) if closes else None

                hit_date = None
                if hit_target:
                    for ts, row in df10.iterrows():
                        if float(row["Close"]) >= target_price:
                            hit_date = ts.strftime("%Y-%m-%d")
                            break

                # D+N 계산 (추천일 기준 영업일 수)
                days_to_hit = None
                if hit_date:
                    all_dates = [ts.strftime("%Y-%m-%d") for ts in df10.index]
                    if hit_date in all_dates:
                        days_to_hit = all_dates.index(hit_date) + 1

                r["performance"] = {
                    "checked"       : True,
                    "hit_target"    : hit_target,
                    "max_return_pct": max_ret_pct,
                    "hit_date"      : hit_date,
                    "days_to_hit"   : days_to_hit,
                    "final_close"   : final_close,
                }
            except Exception as e:
                r["performance"] = {"checked": False, "reason": str(e)}

        data["performance_checked"] = True
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"  → {date_str} 완료 ({len(recs)}개)")

    print("성과 체크 스크립트 종료")


if __name__ == "__main__":
    check_performance()
