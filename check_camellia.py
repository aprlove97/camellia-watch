#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
뉴카멜리아(고려훼리) 좌석 감시  [v6]

v5 대비 바뀐 점
  * 이 사이트는 탭을 눌러도 모든 등급의 표를 화면 뒤에 전부 들고 있다.
    그래서 탭마다 전체를 다시 읽어 같은 방이 탭 수만큼 중복됐다.
    → 중복 제거를 탭 단위가 아니라 '방 단위'로 바꿨다.
  * 탭 이름표는 신뢰할 수 없으므로 알림에서 뺐다. 대신 표의 '클래스' 칸을 쓴다.
  * 객실명에서 영문 병기를 잘라 알림을 짧게 만들었다.

종료 코드
    0  2인 전용 객실 자리 없음 (정상)
    1  자리 있음 → 텔레그램 발송 + 일부러 실패 처리
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
TEST_MODE = os.environ.get("TEST_MODE", "0") == "1"

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

ALL_TABS = ["2등실", "1등 양실 (4명)", "1등 양실 (2명)", "1등 화실", "기타"]
SOLD_OUT = ("매진", "SOLD OUT", "満席")
BLOCKED = ("입력하신 인원", "예약 하실 수 없", "예약하실 수 없")
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


def shorten(name: str) -> str:
    """'2등 일반실 (실속 할인Ⅰ) 2nd Class Cabin (...)' → '2등 일반실 (실속 할인Ⅰ)'"""
    m = re.search(r"[A-Za-z]", name)
    if not m:
        return name.strip()
    return name[: m.start()].rstrip(" 0123456789").strip()


# ─────────────────────────────────────────────────────────────
def submit_search(page) -> None:
    page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector("#entry-form", timeout=30_000)
    page.wait_for_timeout(1_500)

    page.locator('#entry-form input[name="oufuku"][value="1"]').click()
    page.select_option('#entry-form select[name="ow"]', OW_VALUE)
    page.wait_for_timeout(500)
    if not page.input_value('#entry-form select[name="rt"]'):
        page.select_option('#entry-form select[name="rt"]', "1" if OW_VALUE == "2" else "2")

    page.evaluate(
        """(v) => { const e = document.querySelector('#entry-form input[name="ow_date"]');
                    e.removeAttribute('readonly'); e.value = v;
                    e.dispatchEvent(new Event('change', {bubbles:true})); }""",
        TARGET_DATE,
    )
    page.evaluate(
        """(v) => { const e = document.querySelector('#entry-form input[name="number"]');
                    e.value = v; e.dispatchEvent(new Event('change', {bubbles:true})); }""",
        PASSENGERS,
    )

    page.locator('#entry-form button[type="submit"]').click()
    page.wait_for_load_state("domcontentloaded", timeout=45_000)
    page.wait_for_timeout(3_000)
    log(f"검색 완료 — {TARGET_DATE} / {PASSENGERS}인")


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


def parse_row(cells):
    joined = " | ".join(cells)
    if "KRW" not in joined or "요금구분" in joined:
        return None

    name = shorten(cells[0] if cells else "?")

    # '클래스' 칸 — '1 등 양실 (2 명) 정원 2 이름' 같은 형태
    klass = ""
    for c in cells[1:]:
        if "등실" in c or "등 양실" in c or "등 화실" in c or "정원" in c:
            klass = c
            break

    cap = None
    m = re.search(r"정원\s*(\d+)", joined) or re.search(r"(\d+)\s*인실", joined)
    if m:
        cap = int(m.group(1))

    fare_m = re.search(r"[\d,]+\s*KRW", joined)

    return {
        "name": name,
        "klass": re.sub(r"\s*이름\s*$", "", klass).strip(),
        "cap": cap,
        "fare": fare_m.group(0) if fare_m else "?",
        "sold": any(w in joined for w in SOLD_OUT),
        "blocked": any(w in joined for w in BLOCKED),
    }


def is_target(r) -> bool:
    if r["sold"] or r["blocked"]:
        return False
    if TEST_MODE:
        return True
    if r["cap"] == 2:
        return True
    return any(k in r["name"] for k in TWO_PERSON_KEYWORDS)


def main() -> int:
    from playwright.sync_api import sync_playwright

    log(f"확인 시작 — {TARGET_DATE} / {PASSENGERS}인"
        + (" [시험모드: 예약 가능한 건 전부 알림]" if TEST_MODE else " / 2인 전용 객실만"))
    rows, seen = [], set()

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

            # 탭을 한 번씩 눌러 혹시 숨어 있는 표까지 불러온다.
            # 중복은 '방' 기준으로 걸러내므로 같은 방이 여러 번 잡히지 않는다.
            for tab in ALL_TABS:
                click_tab(page, tab)
                page.wait_for_timeout(1_200)
                for cells in page.evaluate(ROWS_JS):
                    r = parse_row(cells)
                    if not r:
                        continue
                    key = (r["name"], r["klass"], r["fare"])
                    if key in seen:
                        continue
                    seen.add(key)
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

    log("─" * 60)
    lines = []
    for r in rows:
        st = "매진" if r["sold"] else ("인원 조건 불가" if r["blocked"] else "예약 가능")
        cap = f"정원 {r['cap']}" if r["cap"] else "다인실"
        line = f"{r['name']} / {r['klass'] or '?'} / {cap} / {r['fare']} → {st}"
        lines.append(line)
        log(line)
    log("─" * 60)
    (DEBUG / "rows.txt").write_text("\n".join(lines), encoding="utf-8")

    hits = [r for r in rows if is_target(r)]
    table = "```\n" + "\n".join(lines) + "\n```"

    if not hits:
        summary(f"😴 2인 전용 객실 자리 없음 — {TARGET_DATE}\n\n{table}")
        return 0

    detail = "\n".join(
        f"• {r['name']} / {r['klass'] or '?'} / {r['fare']}" for r in hits
    )
    head = "🚨 뉴카멜리아 {} 부산 출발 — {} 자리 떴습니다".format(
        TARGET_DATE, "예약 가능한 방" if TEST_MODE else "2인 전용 객실"
    )
    telegram(f"{head}\n\n{detail}\n\n지금 바로 예약하세요\n{URL}")
    log("자리 있음 — 알림 발송")
    summary(f"🚨 **자리 있음** — {TARGET_DATE}\n\n{detail}\n\n{table}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
