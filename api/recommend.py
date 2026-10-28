"""
Vercel Serverless Function
버튼 클릭 시 candidates.json 읽어서 Gemini AI 심사 → JSON 반환
"""
import json
import os
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import google.generativeai as genai


def run_gemini(candidates: list) -> list:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-1.5-flash")

    data_str = json.dumps(candidates, ensure_ascii=False, indent=2)

    prompt = f"""당신은 대한민국 최상위 헤지펀드의 '리스크 관리 총괄 심사역'입니다.
목표: 지금 매수해서 2주(10영업일) 안에 +5%를 찍고 나올 종목 1~2개만 엄선.
제공된 종목의 [차트지표] [수급] [뉴스]를 3중 교차 검증하세요.

[1단계: 수급 검증 40점]
- 조정 기간 외인·기관 순매도가 잠잠한가?
- 개인만 순매수 + 외인·기관 동시 이탈이면 점수 무관 즉시 REJECT.

[2단계: 재료·모멘텀 검증 40점]
- 2주간 후속 뉴스가 나올 연속 모멘텀인가? 아니면 이미 소멸된 단발성 뉴스인가?
- 해당 테마 대장주인가? 후발·잡주면 즉시 REJECT.
- CB·유상증자·대주주 매도 등 악재 공시가 없는가?

[3단계: 손익비 검증 20점]
- 20일선 지지 구간이 확인되는가?
- 목표가(+5%)까지 저항 매물대가 비어 있는가?
- 손절선(-2.5%)이 명확한가?

총점 90점 이상만 추천. 없으면 반드시 [] 반환. 억지 추천 금지.

분석 데이터:
{data_str}

순수 JSON 배열로만 응답 (코드블록 없이):
[
  {{
    "ticker": "종목코드",
    "name": "종목명",
    "score": 93,
    "supply_check": "수급 판단 근거",
    "catalyst_check": "재료 지속성 근거",
    "entry_plan": {{
      "buy_price": 매수가,
      "target_price": 목표가,
      "stop_loss": 손절가
    }}
  }}
]"""

    response = model.generate_content(prompt)
    raw = response.text.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except Exception:
        return []


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            # candidates.json 로드 (Vercel 루트 기준)
            base = Path(__file__).parent.parent
            path = base / "data" / "candidates.json"

            if not path.exists():
                result = {"error": "아직 데이터가 없습니다. GitHub Actions가 아직 실행되지 않았습니다."}
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
                return

            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            candidates = data.get("candidates", [])
            if not candidates:
                result = {
                    "date": data.get("date", ""),
                    "recommendations": [],
                    "candidates_count": 0,
                    "message": "오늘은 눌림목 조건을 충족하는 종목이 없습니다.",
                }
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode())
                return

            recs = run_gemini(candidates)
            result = {
                "date": data.get("date", ""),
                "updated_at": data.get("updated_at", ""),
                "candidates_count": len(candidates),
                "recommendations": recs,
                "message": f"후보 {len(candidates)}개 중 {len(recs)}개 통과",
            }
        except Exception as e:
            result = {"error": str(e)}

        self.wfile.write(json.dumps(result, ensure_ascii=False).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()
