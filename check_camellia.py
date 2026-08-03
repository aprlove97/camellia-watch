#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
뉴카멜리아(고려훼리) 좌석 확인  [v4]

v3 정찰로 알아낸 실제 폼 구조를 그대로 쓴다.

    form#entry-form  →  POST /rs/idv/reservation/index_post
      oufuku   radio    1=편도, 2=왕복
      ow       select   2=ＢＵＳＡＮ→ＨＡＫＡＴＡ, 1=ＨＡＫＡＴＡ→ＢＵＳＡＮ
      rt       select   ow 를 바꾸면 자동으로 반대편이 채워진다 (비어 있으면 검증 실패)
      ow_date  text     readonly 달력. 값을 직접 넣어야 한다. YYYY-MM-DD
      number   text     인원 1~11

종료 코드
    0  아직 매진
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
TARGET_TAB = os.environ.get("TARGET_TAB", "1등 양실 (2명)")
OW_VALUE = "2" if os.environ.get("DEPART_FROM_BUSAN", "1") == "1" else "1"

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

ALL_TABS = ["2등실", "1등 양실 (4명)", "1등 양실 (2명)", "1등 화실", "기타"]
SOLD_OUT = ("매진", "SOLD OUT", "満席")

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
        log(f"텔레그램 발송: {r.status_code}")
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
    log(f"STEP1 도착 — {page.url}")

    # 편도
    page.locator('#entry-form input[name="oufuku"][value="1"]').click()

    # 출발편 (드롭다운). 이걸 바꾸면 리턴편(rt)이 자동으로 반대편으로 채워진다.
    page.select_option('#entry-form select[name="ow"]', OW_VALUE)
    page.wait_for_timeout(500)

    rt_val = page.input_value('#entry-form select[name="rt"]')
    if not rt_val:
        page.select_option('#entry-form select[name="rt"]', "1" if OW_VALUE == "2" else "2")
        log("리턴편 수동 지정")

    # 출발일 — readonly 달력이라 값을 직접 밀어넣는다
    page.evaluate(
        """(v) => {
            const el = document.querySelector('#entry-form input[name="ow_date"]');
            el.removeAttribute('readonly');
            el.value = v;
            el.dispatchEvent(new Event('change', {bubbles:true}));
        }""",
        TARGET_DATE,
    )

    # 인원
    page.evaluate(
        """(v) => {
            const el = document.querySelector('#entry-form input[name="number"]');
            el.value = v;
            el.dispatchEvent(new Event('change', {bubbles:true}));
        }""",
        PASSENGERS,
    )

    state = page.evaluate("""() => {
        const f = document.getElementById('entry-form');
        const g = n => { const e = f.querySelector('[name="' + n + '"]'); return e ? e.value : null; };
        return {oufuku: (f.querySelector('[name=oufuku]:checked')||{}).value,
                ow: g('ow'), rt: g('rt'), ow_date: g('ow_date'), number: g('number')};
    }""")
    log(f"입력값 확인 — {state}")
    save(page, "10_step1_filled")

    page.locator('#entry-form button[type="submit"]').click()
    page.wait_for_load_state("domcontentloaded", timeout=45_000)
    page.wait_for_timeout(3_000)
    log(f"검색 실행 — 도착 {page.url}")

    err = page.evaluate(
        "() => { const e = document.querySelector('#error'); "
        "return (e && e.offsetParent) ? e.innerText.trim() : ''; }"
    )
    if err:
        log(f"[warn] 폼 오류 메시지: {err}")

    save(page, "20_step2")


# ─────────────────────────────────────────────────────────────
# STEP2 — 등급 탭 읽기
# ─────────────────────────────────────────────────────────────
def click_tab(page, label: str) -> bool:
    """공백 차이를 무시하고 탭을 찾아 누른다. ('1 등 양실' vs '1등 양실')"""
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


def read_fare_table(page) -> str:
    return page.evaluate("""() => {
        const ts = Array.from(document.querySelectorAll('table'));
        const hit = ts.filter(t => /운임|요금구분|클래스/.test(t.innerText));
        return (hit.length ? hit : ts).map(t => t.innerText).join('\\n---\\n');
    }""")


def main() -> int:
    from playwright.sync_api import sync_playwright

    stamp = f"{TARGET_DATE} / {TARGET_TAB} / {PASSENGERS}인"
    log(f"확인 시작 — {stamp}")
    target_text = ""

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="ko-KR", viewport={"width": 1600, "height": 1400})
        page = ctx.new_page()
        page.set_default_timeout(15_000)
        try:
            submit_search(page)

            body = page.inner_text("body")
            (DEBUG / "21_step2_text.txt").write_text(body, encoding="utf-8")
            if "STEP.2" not in body and "요금" not in body:
                log("[error] STEP2 로 넘어가지 못한 것 같다")
                summary("⚠️ STEP2 진입 실패 — debug 확인")
                return 2

            for name in ALL_TABS:
                if click_tab(page, name):
                    page.wait_for_timeout(1_500)
                    t = read_fare_table(page)
                else:
                    t = "(탭을 못 찾음)"
                (DEBUG / f"30_tab_{re.sub(r'[^0-9가-힣]', '', name)}.txt").write_text(t, encoding="utf-8")
                flag = "매진" if any(w in t for w in SOLD_OUT) else "여유?"
                log(f"[{name}] {flag} — {t[:150].replace(chr(10), ' | ')}")
                if re.sub(r"\s+", "", name) == re.sub(r"\s+", "", TARGET_TAB):
                    target_text = t

            save(page, "40_final")

        except Exception as e:  # noqa: BLE001
            log(f"[error] {type(e).__name__}: {e}")
            save(page, "90_error")
            summary(f"⚠️ 조회 실패 ({type(e).__name__}) — debug 확인")
            return 2
        finally:
            ctx.close()
            browser.close()

    if not target_text.strip() or target_text.startswith("(탭"):
        summary(f"⚠️ '{TARGET_TAB}' 탭을 못 읽음 — debug 확인")
        return 2

    if any(w in target_text for w in SOLD_OUT):
        log("아직 매진")
        summary(f"😴 아직 매진 — {stamp}")
        return 0

    if "KRW" not in target_text and "운임" not in target_text:
        summary("⚠️ 매진도 요금도 안 보임 — 구조 확인 필요")
        return 2

    msg = (f"🚨 뉴카멜리아 자리 떴습니다\n{TARGET_DATE} 부산 출발 / {TARGET_TAB} / "
           f"{PASSENGERS}인\n\n{target_text.strip()[:600]}\n\n{URL}")
    log("자리 있음 — 알림 발송")
    telegram(msg)
    summary(f"🚨 **자리 있음** — {stamp}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
