#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
탑툰챗 트래커 — 매일 07:00 수집기 (v2)
=========================================
v1과 다른 점: 별도 API 주소를 찾을 필요가 없습니다.
랭킹 페이지(/ranking) 자체에 순위·이름·점수가 통째로 들어있다는 게
확인돼서, 그 페이지 하나만 받으면 끝입니다.

실행
    python collect.py            # 수집 + data.js 갱신
    python collect.py --rebuild  # 수집 없이 history.csv 로 data.js 만 다시 생성
    python collect.py --test FILE  # 저장해둔 HTML로 파싱만 검증 (네트워크 없이)

--------------------------------------------------------------------------
이 "점수"가 정확히 무엇인지 (중요, 꼭 읽어주세요)
--------------------------------------------------------------------------
랭킹 페이지에는 "실시간" 탭 데이터만 서버에서 미리 채워져 옵니다.
원본 대시보드가 보여준 "주간 활동지수"(예: 박채원 7,146)와는 다른 값입니다 —
실시간 탭 1위가 249점이었던 것으로 볼 때, 이 점수는 특정 주기(아마 자정)로
리셋되며 그 이후 누적되는 값으로 보입니다.

그래서 이 스크립트는 매일 스냅샷을 떠서:
  - 어제보다 값이 커졌으면 → 그 차이를 "오늘 새로 열린 방"으로 봅니다
  - 어제보다 값이 작아졌으면 → 리셋이 있었다고 보고, 오늘 값 자체를
    "오늘 새로 열린 방"으로 봅니다 (0부터 다시 쌓인 것으로 취급)

이 방식은 근사치입니다. 몇 주 데이터가 쌓이면 리셋 패턴이 보일 테니
그때 더 정교하게 다듬을 수 있습니다. 정확한 "주간" 원본 수치를 그대로
받고 싶다면 아래 "더 정확하게 만들고 싶다면" 항목을 참고하세요.

--------------------------------------------------------------------------
더 정확하게 만들고 싶다면 (선택)
--------------------------------------------------------------------------
랭킹 페이지의 "주간" 탭을 클릭하면 그 순간 별도 요청이 발생할 가능성이
높습니다 (실시간 데이터만 페이지 로드 시 미리 채워지고, 주간·월간은
버튼을 눌러야 불러오는 구조로 보입니다). 그 요청을 찾으면 원본 대시보드와
완전히 같은 단위의 "주간 활동지수"를 그대로 받을 수 있습니다.

찾는 법: F12 → Network → Fetch/XHR 필터 → 페이지를 새로고침해서
"실시간" 탭이 뜬 상태를 확인 → 이제 "주간" 탭을 클릭 → 그 순간 목록에
새로 뜨는 요청이 있는지 확인. 있으면 그 요청의 Request URL을 캡처해서
저에게 알려주세요. 없으면(즉 주간도 서버에서 이미 다 렌더링돼 온다면)
지금 이 방식이 최선입니다.

--------------------------------------------------------------------------
주의
--------------------------------------------------------------------------
- 실행 전에 해당 사이트의 robots.txt와 이용약관을 확인하세요.
- 요청 간격(SLEEP)을 넉넉히 두고, 하루 1~2회 이상으로 올리지 마세요.
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

REGIONS = {
    "한국":        "https://chat.toptoon.com",
    "일본":        "https://chat.toptoon.jp",
    "중화권":      "https://chat.toptoon.net",
    "북미·글로벌": "https://chat.global.toptoon.com",
}
# 간체 중국어는 별도 도메인입니다. 중화권과 따로 보고 싶으면 이 줄의 주석을 푸세요.
# REGIONS["중화권_간체"] = "https://chat.cn.toptoon.net"

TIMEOUT = 20
SLEEP = 2.5          # 지역 사이 대기(초). 낮추지 마세요.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 페이지 안에서 데이터를 담고 있는 키. 사이트가 구조를 바꾸면 이 이름도
# 바뀔 수 있습니다 — 그럴 땐 --test 로 새로 받은 HTML을 넣어 다시 찾으세요.
DATA_KEY = '"initialRealtimeData":{"items":'


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
#  1. 페이지에서 데이터 꺼내기
# ══════════════════════════════════════════════════════════════════
def extract_items(html):
    """랭킹 페이지 HTML에서 실시간 랭킹 배열을 꺼냅니다."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*"(.*?)"\]\)', html, re.S)
    if not chunks:
        raise RuntimeError("페이지 안에서 데이터 조각(__next_f)을 찾지 못했습니다 — 사이트 구조가 바뀌었을 수 있습니다")

    blob = "".join(chunks)
    # 이스케이프된 문자열(JS 리터럴)을 원래 텍스트로 되돌립니다
    blob = blob.encode().decode("unicode_escape").encode("latin1").decode("utf-8", errors="replace")

    start = blob.find(DATA_KEY)
    if start == -1:
        raise RuntimeError(f"'{DATA_KEY}' 를 찾지 못했습니다 — 사이트 구조가 바뀌었을 수 있습니다")

    arr_start = blob.find("[", start)
    depth, i = 0, arr_start
    while i < len(blob):
        if blob[i] == "[":
            depth += 1
        elif blob[i] == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        raise RuntimeError("배열 괄호 짝을 맞추지 못했습니다 — HTML이 잘렸을 수 있습니다")

    return json.loads(blob[arr_start:i + 1])


def items_to_rows(items, region, date):
    rows = []
    for it in items:
        kind = it.get("kind")
        cid = it.get("characterId") if kind == "character" else it.get("contentId")
        rows.append({
            "date": date,
            "region": region,
            "rank": it.get("rank"),
            "char_id": f"{kind}:{cid}",
            "name": it.get("name"),
            "score": it.get("score"),
        })
    return rows


# ══════════════════════════════════════════════════════════════════
#  2. 수집
# ══════════════════════════════════════════════════════════════════
def fetch_region(s, region, base):
    r = s.get(f"{base}/ranking", timeout=TIMEOUT)
    r.raise_for_status()
    items = extract_items(r.text)
    return items_to_rows(items, region, dt.date.today().isoformat())


def collect():
    s = session()
    rows = []
    for region, base in REGIONS.items():
        try:
            r = fetch_region(s, region, base)
            rows += r
            log(f"  {region}: {len(r)}개")
        except Exception as e:
            # 한 지역이 실패해도 나머지는 계속합니다. 부분 데이터가 없는 것보다 낫습니다.
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
#  3. 누적값 → 일일 증분  (리셋 감지 포함)
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
            try:
                r["rank"] = int(r["rank"])
            except (TypeError, ValueError):
                r["rank"] = None
            out.append(r)
        return out


def daily_deltas(hist):
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
            if d < 0:
                # 리셋으로 보고, 오늘 값 자체를 오늘 몫으로 인정합니다
                d = series[cur]
            per_day.setdefault((cur, region), 0.0)
            per_day[(cur, region)] += d

    return [{"d": d, "region": reg, "rooms": round(v)}
            for (d, reg), v in sorted(per_day.items())]


# ══════════════════════════════════════════════════════════════════
#  4. data.js 다시 쓰기
# ══════════════════════════════════════════════════════════════════
def read_existing_js():
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

    # 실시간 순위 스냅샷 + 카탈로그 크기(최대 캐릭터 ID)
    if hist:
        latest = max(r["date"] for r in hist)
        kr = sorted([r for r in hist if r["date"] == latest and r["region"] == "한국"],
                    key=lambda r: r["rank"] or 9999)
        if kr:
            ids = [int(m.group(1)) for r in kr
                   if r["char_id"].startswith("character:") and (m := re.search(r"(\d+)$", r["char_id"]))]
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
                            "note": "랭킹 페이지 최대 캐릭터 ID 기준 (하한값)"})
                data["catalog"] = sorted(cat, key=lambda c: c["d"])

    data.setdefault("meta", {})
    data["meta"]["collectedAt"] = f"{dt.datetime.now():%Y-%m-%d %H:%M}"
    data["meta"]["generatedBy"] = "collect.py v2"
    data["meta"].setdefault("nextRun", "매일 07:00")

    header = ("/* 탑툰챗 트래커 데이터 — collect.py 자동 생성.\n"
              f" * 마지막 갱신 {data['meta']['collectedAt']}\n"
              " * 과거 주간·월별 값은 손으로 넣은 그대로 보존됩니다.\n"
              " */\n")
    DATA_JS.write_text(header + "window.TOPTOON_DATA = " +
                       json.dumps(data, ensure_ascii=False, indent=2) + ";\n",
                       encoding="utf-8")
    log("data.js 갱신 완료 → 대시보드를 새로고침하세요")


# ══════════════════════════════════════════════════════════════════
#  테스트 (네트워크 없이, 저장해둔 HTML로 파싱만 확인)
# ══════════════════════════════════════════════════════════════════
def test_file(path):
    html = Path(path).read_text(encoding="utf-8", errors="replace")
    items = extract_items(html)
    print(f"항목 {len(items)}개 발견\n")
    for it in items[:10]:
        cid = it.get("characterId") if it.get("kind") == "character" else it.get("contentId")
        print(f"  {it['rank']:>3}위  {it['score']:>5}점  [{it['kind']:>9}:{cid}]  {it.get('name')}")


# ══════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="수집 없이 data.js 만 재생성")
    ap.add_argument("--test", metavar="FILE", help="저장해둔 HTML로 파싱만 검증 (네트워크 없음)")
    a = ap.parse_args()

    if a.test:
        test_file(a.test)
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
