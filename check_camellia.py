#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
뉴카멜리아(고려훼리) 좌석 확인 — GitHub Actions 단발 실행용.

한 번 실행해서 목표 등급이 매진인지 아닌지만 보고 끝난다.
반복 실행은 워크플로우의 cron이 담당한다.

종료 코드
    0  아직 매진 (정상, 초록불)
    1  자리 있음  → 텔레그램 발송 + 일부러 실패 처리해서 깃허브 알림도 함께 울림
    2  조회 실패  → 사이트 구조 변경 등. 디버그 파일 확인 필요
"""

import os
import pathlib
import sys
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

URL = "https://www.koreaferry.co.kr/rs/idv/reservation"

TARGET_DATE = os.environ.get("TARGET_DATE", "2026-10-02")
PASSENGERS = int(os.environ.get("PASSENGERS", "2"))
TARGET_TAB = os.environ.get("TARGET_TAB", "1 등 양실 (2 명)")
DEPART_FROM_BUSAN = os.environ.get("DEPART_FROM_BUSAN", "1") == "1"
DUMP = os.environ.get("DUMP", "0") == "1"

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

SOLD_OUT_WORDS = ("매진", "SOLD OUT", "満席")
DEBUG_DIR = pathlib.Path("debug")


def log(msg: str) -> None:
    print(f"[{datetime.now(KST):%Y-%m-%d %H:%M:%S KST}] {msg}", flush=True)


def summary(text: str) -> None:
    """Actions 실행 화면 상단에 요약으로 남긴다."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def telegram(text: str) -> None:
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        log("텔레그램 설정 없음 — 발송 생략")
        return
    try:
        import requests
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": False},
            timeout=20,
        )
        log(f"텔레그램 발송 결과: {r.status_code}")
    except Exception as e:  # noqa: BLE001
        log(f"텔레그램 발송 실패: {e}")


# ─────────────────────────────────────────────────────────────
# 페이지 조작 — 실제 사이트에 맞춰 손볼 가능성이 있는 구간
# ─────────────────────────────────────────────────────────────
def open_step2(page) -> None:
    page.goto(URL, wait_until="networkidle", timeout=60_000)
    page.wait_for_timeout(2_500)  # "Now Loading..." 이 걷힐 때까지

    def try_click(desc, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            log(f"[warn] {desc} 실패: {e}")

    try_click("편도 선택", lambda: page.get_by_text("편도", exact=True).first.click())

    direction = "ＢＵＳＡＮ→ＨＡＫＡＴＡ" if DEPART_FROM_BUSAN else "ＨＡＫＡＴＡ→ＢＵＳＡＮ"
    try_click("방향 선택", lambda: page.get_by_text(direction, exact=False).first.click())

    try_click("인원 선택", lambda: page.locator("select").first.select_option(str(PASSENGERS)))

    def set_date():
        page.locator("input[type='text']").first.fill(TARGET_DATE)
        page.keyboard.press("Escape")
    try_click("날짜 입력", set_date)

    page.get_by_role("button", name="검색").first.click()
    page.wait_for_timeout(3_500)

    # STEP2 상단 날짜 탭에서 목표 날짜를 한 번 더 확정 (예: "10/2(금)")
    md = f"{int(TARGET_DATE[5:7])}/{int(TARGET_DATE[8:10])}"
    try_click(f"날짜 탭 {md}", lambda: page.locator(f"text=/{md}\\s*\\(/").first.click())
    page.wait_for_timeout(1_500)


def read_tab(page, tab_name: str) -> str:
    page.get_by_text(tab_name, exact=False).first.click()
    page.wait_for_timeout(1_500)
    return page.locator("table").first.inner_text()


def dump_all_tabs(page) -> None:
    """디버그용 — 보이는 등급 탭을 하나씩 눌러보며 내용을 기록."""
    DEBUG_DIR.mkdir(exist_ok=True)
    for name in ["2 등실", "1 등 양실 (4 명)", "1 등 양실 (2 명)", "1 등 화실", "기타"]:
        try:
            text = read_tab(page, name)
        except Exception as e:  # noqa: BLE001
            text = f"(읽기 실패: {e})"
        safe = name.replace(" ", "").replace("(", "").replace(")", "")
        (DEBUG_DIR / f"tab_{safe}.txt").write_text(text, encoding="utf-8")
        log(f"--- {name} ---\n{text}\n")


def main() -> int:
    from playwright.sync_api import sync_playwright

    stamp = f"{TARGET_DATE} / {TARGET_TAB} / {PASSENGERS}인"
    log(f"확인 시작 — {stamp}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="ko-KR", viewport={"width": 1600, "height": 1200})
        page = ctx.new_page()
        try:
            open_step2(page)

            if DUMP:
                dump_all_tabs(page)

            text = read_tab(page, TARGET_TAB)

            DEBUG_DIR.mkdir(exist_ok=True)
            (DEBUG_DIR / "target_tab.txt").write_text(text, encoding="utf-8")
            page.screenshot(path=str(DEBUG_DIR / "page.png"), full_page=True)
            (DEBUG_DIR / "page.html").write_text(page.content(), encoding="utf-8")

        except Exception as e:  # noqa: BLE001
            log(f"[error] 조회 실패: {e}")
            DEBUG_DIR.mkdir(exist_ok=True)
            try:
                page.screenshot(path=str(DEBUG_DIR / "page.png"), full_page=True)
                (DEBUG_DIR / "page.html").write_text(page.content(), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            summary(f"⚠️ 조회 실패 — {e}")
            return 2
        finally:
            ctx.close()
            browser.close()

    log(f"읽은 내용: {text.strip()[:400]}")

    if not text.strip():
        summary("⚠️ 표가 비어 있음 — 셀렉터 확인 필요")
        return 2

    if any(w in text for w in SOLD_OUT_WORDS):
        log("아직 매진")
        summary(f"😴 아직 매진 — {stamp}")
        return 0

    if "KRW" not in text and "운임" not in text:
        summary("⚠️ 매진도 요금도 안 보임 — 셀렉터 확인 필요")
        return 2

    # 여기까지 오면 자리가 생긴 것
    msg = (
        f"🚨 뉴카멜리아 자리 떴습니다\n"
        f"{TARGET_DATE} 부산 출발 / {TARGET_TAB} / {PASSENGERS}인\n\n"
        f"{text.strip()[:600]}\n\n"
        f"{URL}"
    )
    log("자리 있음 — 알림 발송")
    telegram(msg)
    summary(f"🚨 **자리 있음** — {stamp}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
