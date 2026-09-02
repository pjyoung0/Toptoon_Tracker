#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
탑툰챗 트래커 — 매일 07:00 수집기
==================================
하는 일
  1) 지역별 랭킹에서 캐릭터 순위와 활동지수를 읽는다
  2) history.csv 에 오늘 스냅샷을 덧붙인다
  3) 어제 값과 빼서 "오늘 새로 열린 방"을 계산한다
  4) data.js 를 다시 쓴다 → dashboard.html 을 새로고침하면 반영된다

실행
    python collect.py            # 수집 + data.js 갱신
    python collect.py --probe    # 엔드포인트 찾기 도우미 (처음 1회)
    python collect.py --rebuild  # 수집 없이 history.csv 로 data.js 만 다시 생성

처음 한 번은 --probe 를 돌려서 SCORE_API 를 채워야 합니다. 아래 설명 참고.
"""

import argparse
import csv
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("requests 가 필요합니다:  pip install requests")

HERE = Path(__file__).resolve().parent
HISTORY = HERE / "history.csv"
DATA_JS = HERE / "data.js"
LOG = HERE / "collect.log"

# ── 지역 ──────────────────────────────────────────────────────────
REGIONS = {
    "한국":        "https://chat.toptoon.com",
    "일본":        "https://chat.toptoon.jp",
    "중화권":      "https://chat.toptoon.net",
    "북미·글로벌": "https://chat.global.toptoon.com",
}
# 간체 중국어는 별도 도메인입니다. 중화권에 합치려면 주석을 푸세요.
# REGIONS["중화권_간체"] = "https://chat.cn.toptoon.net"

# ── 여기를 채우세요 ───────────────────────────────────────────────
# --probe 로 찾은 JSON 엔드포인트를 넣습니다. {base} 는 위 도메인으로 치환됩니다.
# 예: "{base}/api/v1/ranking/character?period=weekly&limit=50"
SCORE_API = ""

# 응답 JSON 안에서 목록이 있는 경로. 예: ["data", "list"] → resp["data"]["list"]
LIST_PATH = ["data", "list"]
F_NAME, F_SCORE, F_ID = "name", "score", "id"
# ──────────────────────────────────────────────────────────────────

TOP_N = 50
SLEEP = 2.5          # 요청 사이 대기(초). 낮추지 마세요.
TIMEOUT = 20
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
UNIT_PRICE = 2050    # data.js 기본값 계산에만 씀 (대시보드에서 다시 조절 가능)


def log(msg):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ko,en;q=0.8"})
    return s


# ══════════════════════════════════════════════════════════════════
#  1. 엔드포인트 찾기
# ══════════════════════════════════════════════════════════════════
def probe():
    """랭킹 페이지를 받아서 점수처럼 생긴 값이 어디 들어있는지 보여줍니다."""
    s = session()
    base = REGIONS["한국"]
    url = f"{base}/ranking"
    log(f"probe: {url}")
    html = s.get(url, timeout=TIMEOUT).text
    (HERE / "probe_ranking.html").write_text(html, encoding="utf-8")
    log(f"원본 저장 → probe_ranking.html ({len(html):,} bytes)")

    # Next.js 는 self.__next_f.push([...]) 안에 데이터를 밀어넣습니다
    chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*"(.*?)"\]\)', html, re.S)
    log(f"__next_f 청크 {len(chunks)}개 발견")

    blob = "".join(chunks)
    try:
        blob = blob.encode().decode("unicode_escape")
    except Exception:
        pass

    keys = sorted(set(re.findall(r'"([a-zA-Z_][a-zA-Z0-9_]{2,24})"\s*:\s*-?\d+', blob)))
    log("숫자를 값으로 갖는 키 후보:")
    for k in keys[:60]:
        print("   ", k)

    for cand in ("score", "activityScore", "point", "cnt", "count",
                 "chatCount", "roomCount", "totalScore", "rankScore"):
        hits = re.findall(rf'"{cand}"\s*:\s*(\d+)', blob)
        if hits:
            log(f"  → '{cand}' 값 예시: {hits[:8]}")

    print("""
────────────────────────────────────────────────────────────
위에 그럴듯한 키가 안 보이면 브라우저로 찾는 게 확실합니다.

 1. 크롬에서 https://chat.toptoon.com/ranking 접속
 2. F12 → Network → Fetch/XHR 만 필터 → 새로고침
 3. JSON 을 돌려주는 요청을 찾습니다 (이름에 rank / ranking 등)
 4. 우클릭 → Copy → Copy as cURL
 5. https://curlconverter.com 에 붙여넣어 파이썬 코드로 변환
 6. 나온 URL 을 이 파일 위쪽 SCORE_API 에 넣습니다.
    도메인 부분은 {base} 로 바꿔 쓰세요.
    응답 구조를 보고 LIST_PATH / F_NAME / F_SCORE / F_ID 도 맞추세요.

주의: 실행 전에 robots.txt 와 이용약관을 확인하세요. 공개 랭킹을
하루 한 번 읽는 것과 로그인 뒤 대량으로 긁는 것은 전혀 다른 문제입니다.
────────────────────────────────────────────────────────────""")


# ══════════════════════════════════════════════════════════════════
#  2. 수집
# ══════════════════════════════════════════════════════════════════
def fetch_scores(s, region, base):
    """활동지수 포함 목록. SCORE_API 가 비어 있으면 순위만 돌려줍니다."""
    if SCORE_API:
        url = SCORE_API.format(base=base)
        r = s.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        obj = r.json()
        for k in LIST_PATH:
            obj = obj[k]
        return [
            {"rank": i + 1,
             "char_id": str(it.get(F_ID, it.get(F_NAME))),
             "name": it.get(F_NAME),
             "score": it.get(F_SCORE)}
            for i, it in enumerate(obj[:TOP_N])
        ]

    # 대안: 랭킹 페이지 HTML 에서 순위와 캐릭터 ID 만 긁습니다.
    # 점수는 못 가져오지만 순위 회전율은 이것만으로도 추적됩니다.
    html = s.get(f"{base}/ranking", timeout=TIMEOUT).text
    hits = re.findall(r'/detail/(character|content)/(\d+)[^>]*>([^<]{0,40})', html)
    out, seen = [], set()
    for kind, cid, label in hits:
        key = f"{kind}:{cid}"
        if key in seen:
            continue
        seen.add(key)
        name = re.sub(r'(EP\+|NEW|MULTI|^\d+)', '', label).strip()
        out.append({"rank": len(out) + 1, "char_id": key,
                    "name": name or key, "score": None})
        if len(out) >= TOP_N:
            break
    return out


def collect():
    s = session()
    today = dt.date.today().isoformat()
    rows = []
    for region, base in REGIONS.items():
        try:
            items = fetch_scores(s, region, base)
            for it in items:
                rows.append({"date": today, "region": region, **it})
            got = sum(1 for i in items if i["score"] is not None)
            log(f"  {region}: {len(items)}개 (점수 {got}개)")
        except Exception as e:
            # 한 지역이 죽어도 나머지는 살립니다. 부분 데이터가 없는 것보다 낫습니다.
            log(f"  {region}: 실패 — {e}")
        time.sleep(SLEEP)

    if not rows:
        log("수집 실패. data.js 는 건드리지 않습니다.")
        return False

    new = not HISTORY.exists()
    with HISTORY.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["date", "region", "rank", "char_id", "name", "score"])
        if new:
            w.writeheader()
        w.writerows(rows)
    log(f"{len(rows)}행 추가 → {HISTORY.name}")
    return True


# ══════════════════════════════════════════════════════════════════
#  3. data.js 다시 쓰기
# ══════════════════════════════════════════════════════════════════
def load_history():
    if not HISTORY.exists():
        return []
    with HISTORY.open(encoding="utf-8-sig") as f:
        out = []
        for r in csv.DictReader(f):
            try:
                r["score"] = float(r["score"]) if r["score"] not in ("", "None") else None
            except ValueError:
                r["score"] = None
            r["rank"] = int(r["rank"])
            out.append(r)
        return out


def daily_deltas(hist):
    """누적 활동지수 → 그날 새로 열린 방. 캐릭터별로 어제와 뺍니다."""
    by = {}
    for r in hist:
        if r["score"] is None:
            continue
        by.setdefault((r["region"], r["char_id"]), {})[r["date"]] = r["score"]

    per_day = {}
    for (region, _cid), series in by.items():
        days = sorted(series)
        for prev, cur in zip(days, days[1:]):
            d = series[cur] - series[prev]
            if d < 0:      # 랭킹 이탈·리셋 → 버립니다
                continue
            per_day.setdefault((cur, region), 0.0)
            per_day[(cur, region)] += d

    return [{"d": d, "region": reg, "rooms": round(v)}
            for (d, reg), v in sorted(per_day.items())]


def read_existing_js():
    """기존 data.js 에서 손으로 넣은 값(과거 주간·월별·시총 등)을 살립니다."""
    if not DATA_JS.exists():
        return {}
    txt = DATA_JS.read_text(encoding="utf-8")
    m = re.search(r"window\.TOPTOON_DATA\s*=\s*(\{.*\});?\s*$", txt, re.S)
    if not m:
        return {}
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    body = re.sub(r"(['\"])?([A-Za-z_][A-Za-z0-9_]*)\1?\s*:", r'"\2":', body)
    body = re.sub(r",\s*([}\]])", r"\1", body)
    try:
        return json.loads(body)
    except Exception as e:
        log(f"기존 data.js 파싱 실패 ({e}) — 과거 데이터는 그대로 두고 넘어갑니다")
        return {}


def rebuild():
    data = read_existing_js()
    if not data:
        log("data.js 가 없거나 읽지 못했습니다. 원본을 옆에 두고 다시 실행하세요.")
        return

    hist = load_history()
    deltas = daily_deltas(hist)

    if deltas:
        latest = max(r["d"] for r in deltas)
        rows = []
        for r in deltas:
            if r["d"] != latest:
                continue
            old = next((x for x in data.get("daily", []) if x["region"] == r["region"]), {})
            rows.append({"d": r["d"], "region": r["region"], "rooms": r["rooms"],
                         "views": old.get("views"), "spread": old.get("spread"),
                         "top5": old.get("top5"), "legacy": old.get("legacy")})
        if rows:
            data["daily"] = rows
            log(f"일일 지표 갱신: {latest} · {len(rows)}개 지역")
    else:
        log("증분을 계산할 만큼 이력이 쌓이지 않았습니다 (최소 2일 필요).")

    # 실시간 순위 스냅샷
    if hist:
        latest = max(r["date"] for r in hist)
        kr = sorted([r for r in hist if r["date"] == latest and r["region"] == "한국"],
                    key=lambda r: r["rank"])[:24]
        if kr:
            ids = [int(m.group(1)) for r in kr
                   if (m := re.search(r"(\d+)$", r["char_id"]))]
            data["rankSnapshot"] = {
                "d": latest, "region": "한국",
                "maxCharId": max(ids) if ids else None,
                "items": [{"rank": r["rank"], "name": r["name"],
                           "id": int(m.group(1)) if (m := re.search(r"(\d+)$", r["char_id"])) else None,
                           "type": "multi" if r["char_id"].startswith("content") else "character"}
                          for r in kr],
            }
            if ids:
                cat = [c for c in data.get("catalog", []) if c["d"] != latest]
                cat.append({"d": latest, "chars": max(ids),
                            "note": "랭킹 페이지 최대 ID 기준 (하한값)"})
                data["catalog"] = sorted(cat, key=lambda c: c["d"])

    data.setdefault("meta", {})
    data["meta"]["collectedAt"] = f"{dt.datetime.now():%Y-%m-%d %H:%M}"
    data["meta"]["generatedBy"] = "collect.py"
    data["meta"].setdefault("nextRun", "매일 07:00")

    header = ("/* 탑툰챗 트래커 데이터 — collect.py 자동 생성.\n"
              f" * 마지막 갱신 {data['meta']['collectedAt']}\n"
              " * 과거 주간·월별 값은 손으로 넣은 그대로 보존됩니다.\n"
              " */\n")
    DATA_JS.write_text(header + "window.TOPTOON_DATA = " +
                       json.dumps(data, ensure_ascii=False, indent=2) + ";\n",
                       encoding="utf-8")
    log(f"data.js 갱신 완료 → 대시보드를 새로고침하세요")


# ══════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="엔드포인트 찾기 도우미")
    ap.add_argument("--rebuild", action="store_true", help="수집 없이 data.js 만 재생성")
    a = ap.parse_args()

    if a.probe:
        probe()
        return
    if a.rebuild:
        rebuild()
        return

    log("수집 시작")
    if collect():
        rebuild()
    log("끝\n")


if __name__ == "__main__":
    main()
