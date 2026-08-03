#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
뉴카멜리아(고려훼리) 좌석 감시  [v5]

10/2 의 모든 등급 탭을 훑어서, 2인 전용 객실에 자리가 나면 알린다.

v4 대비 바뀐 점
  * 표 전체가 아니라 '줄 단위'로 판정한다.
    - 2등실 탭처럼 한 탭에 일반실/침대실이 같이 있어도 각각 구분된다.
    - '입력하신 인원으로는 예약 하실 수 없습니다' 가 붙은 줄은 자리 없음으로 본다.
    - 표에 머리글만 있고 내용이 없는 탭을 '여유'로 잘못 읽지 않는다.
  * 감시 대상이 등급 하나가 아니라 전 등급이다. 정원 2인 방만 골라 알린다.
  * 알림에 등급명 / 정원 / 요금이 모두 들어간다.

종료 코드
    0  2인 전용 객실 자리 없음 (정상)
    1  자리 있음 → 텔레그램 발송 + 일부러 실패 처리해서 깃허브 알림도 울림
    2  조회 실패 → debug 아티팩트 확인
"""

import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))
URL = "https://www.koreaferry.co.kr/rs/idv/reservation"

TARGET_DATE = os.environ.get("TARGET_DATE", "2026-10-02")
PASSENGERS = os.environ.get("PASSENGERS", "2")
OW_VALUE = "2" if os.environ.get("DEPART_FROM_BUSAN", "1") == "1" else "1"

# TEST_MODE=1 이면 정원과 무관하게 예약 가능한 줄이 하나라도 있으면 알린다.
# 알림 배선이 살아있는지 시험할 때만 쓴다.
TEST_MODE = os.environ.get("TEST_MODE", "0") == "1"

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

ALL_TABS = ["2등실", "1등 양실 (4명)", "1등 양실 (2명)", "1등 화실", "기타"]
SOLD_OUT = ("매진", "SOLD OUT", "満席")
BLOCKED = ("입력하신 인원", "예약 하실 수 없", "예약하실 수 없")
# 정원 표기가 없어도 2인 전용으로 취급할 등급 이름
TWO_PERSON_KEYWORDS = ("특등", "특별", "스위트", "디럭스", "트윈")

DEBUG = Path("debug")
DEBUG.mkdir(exist_ok=True)


def log(m: str) -> None:
    print(f"[{datetime.now(KST):%H:%M:%S}] {m}", flush=True)


def summary(t: str) -> None:
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(t + "\n")


def telegram(text: str) -> None:
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        log("텔레그램 시크릿 없음 — 발송 생략")
        return
    try:
        import requests
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text},
            timeout=20,
        )
        log(f"텔레그램 발송: {r.status_code} {r.text[:120]}")
    except Exception as e:  # noqa: BLE001
        log(f"텔레그램 실패: {e}")


def save(page, tag: str) -> None:
    try:
        page.screenshot(path=str(DEBUG / f"{tag}.png"), full_page=True)
        (DEBUG / f"{tag}.html").write_text(page.content(), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────────────────────────────────────────
# STEP1 — 조건 입력 후 검색
# ─────────────────────────────────────────────────────────────
def submit_search(page) -> None:
    page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector("#entry-form", timeout=30_000)
    page.wait_for_timeout(1_500)

    page.locator('#entry-form input[name="oufuku"][value="1"]').click()      # 편도
    page.select_option('#entry-form select[name="ow"]', OW_VALUE)            # 출발편
    page.wait_for_timeout(500)

    if not page.input_value('#entry-form select[name="rt"]'):
        page.select_option('#entry-form select[name="rt"]', "1" if OW_VALUE == "2" else "2")

    page.evaluate(                                                          # 출발일 (readonly)
        """(v) => { const e = document.querySelector('#entry-form input[name="ow_date"]');
                    e.removeAttribute('readonly'); e.value = v;
                    e.dispatchEvent(new Event('change', {bubbles:true})); }""",
        TARGET_DATE,
    )
    page.evaluate(                                                          # 인원
        """(v) => { const e = document.querySelector('#entry-form input[name="number"]');
                    e.value = v; e.dispatchEvent(new Event('change', {bubbles:true})); }""",
        PASSENGERS,
    )

    page.locator('#entry-form button[type="submit"]').click()
    page.wait_for_load_state("domcontentloaded", timeout=45_000)
    page.wait_for_timeout(3_000)
    log(f"검색 완료 — {TARGET_DATE} / {PASSENGERS}인")


# ─────────────────────────────────────────────────────────────
# STEP2 — 등급 탭을 돌며 줄 단위로 읽기
# ─────────────────────────────────────────────────────────────
ROWS_JS = """() => {
    const out = [];
    document.querySelectorAll('table').forEach(t => {
        t.querySelectorAll('tr').forEach(tr => {
            const cells = Array.from(tr.querySelectorAll('th,td'))
                .map(c => (c.innerText || '').replace(/\\s+/g, ' ').trim());
            if (cells.length) out.push(cells);
        });
    });
    return out;
}"""


def click_tab(page, label: str) -> bool:
    key = re.sub(r"\s+", "", label)
    return bool(page.evaluate(
        """(key) => {
            const norm = s => (s || '').replace(/\\s+/g, '');
            const els = Array.from(document.querySelectorAll('a,button,li,span,div,td,th,p'));
            const hits = els.filter(e => norm(e.textContent) === key);
            if (!hits.length) return false;
            hits.sort((a, b) => a.getElementsByTagName('*').length - b.getElementsByTagName('*').length);
            hits[0].click();
            return true;
        }""",
        key,
    ))


def parse_row(cells, tab):
    """요금 줄 하나를 해석한다. 요금 줄이 아니면 None."""
    joined = " | ".join(cells)
    if "KRW" not in joined or "요금구분" in joined:
        return None

    cap = None
    m = re.search(r"정원\s*(\d+)", joined) or re.search(r"(\d+)\s*인실", joined)
    if m:
        cap = int(m.group(1))

    fare_m = re.search(r"[\d,]+\s*KRW", joined)

    return {
        "tab": tab,
        "name": cells[0] if cells else "?",
        "cap": cap,
        "fare": fare_m.group(0) if fare_m else "?",
        "sold": any(w in joined for w in SOLD_OUT),
        "blocked": any(w in joined for w in BLOCKED),
        "raw": joined[:300],
    }


def is_two_person_target(r) -> bool:
    if r["sold"] or r["blocked"]:
        return False
    if TEST_MODE:
        return True
    if r["cap"] == 2:
        return True
    return any(k in r["name"] for k in TWO_PERSON_KEYWORDS)


def main() -> int:
    from playwright.sync_api import sync_playwright

    log(f"확인 시작 — {TARGET_DATE} / {PASSENGERS}인 / 2인 전용 객실 감시"
        + (" [시험모드]" if TEST_MODE else ""))
    rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="ko-KR", viewport={"width": 1600, "height": 1400})
        page = ctx.new_page()
        page.set_default_timeout(15_000)
        try:
            submit_search(page)

            body = page.inner_text("body")
            (DEBUG / "step2_text.txt").write_text(body, encoding="utf-8")
            if "STEP.2" not in body and "요금" not in body:
                save(page, "error_step2")
                summary("⚠️ STEP2 진입 실패 — debug 확인")
                return 2

            for tab in ALL_TABS:
                if not click_tab(page, tab):
                    log(f"[{tab}] 탭 없음")
                    continue
                page.wait_for_timeout(1_500)
                found = [parse_row(c, tab) for c in page.evaluate(ROWS_JS)]
                found = [r for r in found if r]
                if not found:
                    log(f"[{tab}] 요금 줄 없음")
                for r in found:
                    if not any(x["tab"] == r["tab"] and x["name"] == r["name"] for x in rows):
                        rows.append(r)

            save(page, "final")

        except Exception as e:  # noqa: BLE001
            log(f"[error] {type(e).__name__}: {e}")
            save(page, "error")
            summary(f"⚠️ 조회 실패 ({type(e).__name__}) — debug 확인")
            return 2
        finally:
            ctx.close()
            browser.close()

    if not rows:
        summary("⚠️ 요금표를 하나도 못 읽음 — debug 확인")
        return 2

    # 전체 현황을 로그와 요약에 남긴다
    log("─" * 60)
    lines = []
    for r in rows:
        cap = f"정원 {r['cap']}" if r["cap"] else "정원 ?"
        if r["sold"]:
            st = "매진"
        elif r["blocked"]:
            st = "인원 조건 불가"
        else:
            st = "예약 가능"
        line = f"{r['name']} / {cap} / {r['fare']} → {st}   [{r['tab']}]"
        lines.append(line)
        log(line)
    log("─" * 60)
    (DEBUG / "rows.txt").write_text("\n".join(lines), encoding="utf-8")

    hits = [r for r in rows if is_two_person_target(r)]

    if not hits:
        summary(f"😴 2인 전용 객실 자리 없음 — {TARGET_DATE}\n\n```\n" + "\n".join(lines) + "\n```")
        return 0

    detail = "\n".join(
        f"• {r['name']} / 정원 {r['cap'] or '?'} / {r['fare']}  [{r['tab']} 탭]" for r in hits
    )
    msg = (f"🚨 뉴카멜리아 {TARGET_DATE} 부산 출발 — 자리 떴습니다\n\n"
           f"{detail}\n\n지금 바로 예약하세요\n{URL}")
    log("자리 있음 — 알림 발송")
    telegram(msg)
    summary(f"🚨 **자리 있음** — {TARGET_DATE}\n\n{detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
