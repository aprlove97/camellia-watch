#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
뉴카멜리아 예약 화면 정찰 스크립트  [v3 — 조사 전용]

이 버전은 좌석을 확인하지 않는다. 검색 버튼도 누르지 않는다.
예약 화면에 무엇이 있는지 기록만 하고 끝낸다.

v1 이 실패한 이유
    페이지 우측 상단의 '사이트 내 검색창'에 날짜를 넣고 그 옆 검색 버튼을 눌렀다.
    그래서 게시판 검색 결과 페이지로 이동해버렸고, 예약 엔진은 건드리지도 못했다.

남기는 것 (debug/ 폴더)
    01_page.png              화면 스크린샷
    01_page.html             화면 HTML 원본
    02_inventory.txt         드롭다운/입력칸/버튼/프레임 목록  ← 제일 중요
    03_network.txt           브라우저가 주고받은 통신 기록
    04_body_text.txt         화면에 보이는 글자 전체

항상 종료 코드 0 으로 끝난다 (초록불). 실패가 아니라 조사다.
"""

import json
import os
import sys
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))
URL = "https://www.koreaferry.co.kr/rs/idv/reservation"

DEBUG = Path("debug")
DEBUG.mkdir(exist_ok=True)

# 페이지 상단 '사이트 내 검색' 관련 요소들. 절대 건드리면 안 되는 것들.
BLACKLIST_NAMES = {"stx", "sfl", "sop", "srows", "gr_id"}
BLACKLIST_IDS = {"sch_stx", "sch_submit", "stx", "sfl", "gr_id"}

netlog = []


def log(m: str) -> None:
    print(f"[{datetime.now(KST):%H:%M:%S}] {m}", flush=True)


def norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").strip()


def hook_network(page) -> None:
    def on_request(req):
        try:
            if req.resource_type in ("xhr", "fetch", "document"):
                entry = f"REQ  {req.method:5s} [{req.resource_type}] {req.url}"
                pd = req.post_data
                if pd:
                    entry += f"\n     POST DATA: {pd[:800]}"
                netlog.append(entry)
        except Exception:  # noqa: BLE001
            pass

    def on_response(res):
        try:
            rt = res.request.resource_type
            if rt not in ("xhr", "fetch", "document"):
                return
            ct = (res.headers or {}).get("content-type", "")
            entry = f"RES  {res.status} [{rt}] {ct[:40]} {res.url}"
            if "json" in ct or "text" in ct:
                try:
                    body = res.text()
                    entry += f"\n     BODY(1500): {body[:1500]}"
                except Exception:  # noqa: BLE001
                    entry += "\n     BODY: (읽기 불가)"
            netlog.append(entry)
        except Exception:  # noqa: BLE001
            pass

    page.on("request", on_request)
    page.on("response", on_response)


def wait_until_loaded(page, limit: int = 40) -> None:
    """'Now Loading' 이 사라지고 실제 내용이 나올 때까지 기다린다."""
    for i in range(limit):
        try:
            body = page.inner_text("body")
        except Exception:  # noqa: BLE001
            body = ""
        if body and "Now Loading" not in body and len(body.strip()) > 300:
            log(f"화면 로딩 완료 ({i + 1}초, 글자 {len(body)}자)")
            return
        page.wait_for_timeout(1_000)
    log(f"[warn] {limit}초 대기했지만 로딩이 안 끝난 것으로 보임")


def collect(page) -> str:
    out = [f"URL: {page.url}", f"TITLE: {page.title()}", ""]

    out.append("### FRAMES (예약 엔진이 iframe 안에 있는지) ###")
    for f in page.frames:
        out.append(f"  name={f.name!r} url={f.url}")

    for fi, frame in enumerate(page.frames):
        tag = "MAIN" if frame is page.main_frame else f"FRAME[{fi}]"
        out.append(f"\n{'=' * 55}\n{tag}  {frame.url}\n{'=' * 55}")

        out.append("\n### FORMS ###")
        try:
            forms = frame.evaluate("""() => Array.from(document.forms).map(f => ({
                name: f.name, id: f.id, action: f.action, method: f.method,
                inputs: Array.from(f.elements).slice(0,40).map(e => e.tagName + ':' + (e.type||'') + ':' + (e.name||e.id||''))
            }))""")
            for i, f in enumerate(forms):
                out.append(f"  [{i}] name={f['name']!r} id={f['id']!r} action={f['action']}")
                out.append(f"       elements: {f['inputs']}")
        except Exception as e:  # noqa: BLE001
            out.append(f"  (실패: {e})")

        out.append("\n### SELECT (드롭다운) ###")
        try:
            sels = frame.evaluate("""() => Array.from(document.querySelectorAll('select')).map(s => ({
                id: s.id, name: s.name, cls: s.className,
                visible: !!(s.offsetParent || s.getClientRects().length),
                form: s.form ? (s.form.name || s.form.id || s.form.action) : null,
                options: Array.from(s.options).slice(0,30).map(o => o.value + ' || ' + o.textContent.trim())
            }))""")
            for i, s in enumerate(sels):
                mark = " <<< 헤더검색(무시)" if s["id"] in BLACKLIST_IDS or s["name"] in BLACKLIST_NAMES else ""
                out.append(f"  [{i}] id={s['id']!r} name={s['name']!r} visible={s['visible']} form={s['form']!r}{mark}")
                for o in s["options"]:
                    out.append(f"        {o}")
        except Exception as e:  # noqa: BLE001
            out.append(f"  (실패: {e})")

        out.append("\n### INPUT (입력칸/버튼) ###")
        try:
            ins = frame.evaluate("""() => Array.from(document.querySelectorAll('input')).map(i => ({
                type: i.type, id: i.id, name: i.name, cls: i.className,
                value: (i.value||'').slice(0,40), placeholder: i.placeholder,
                readonly: i.readOnly,
                visible: !!(i.offsetParent || i.getClientRects().length),
                form: i.form ? (i.form.name || i.form.id || i.form.action) : null,
                onclick: (i.getAttribute('onclick')||'').slice(0,150)
            }))""")
            for i, e in enumerate(ins):
                mark = " <<< 헤더검색(무시)" if e["id"] in BLACKLIST_IDS or e["name"] in BLACKLIST_NAMES else ""
                out.append(f"  [{i}] {json.dumps(e, ensure_ascii=False)}{mark}")
        except Exception as e:  # noqa: BLE001
            out.append(f"  (실패: {e})")

        out.append("\n### 클릭 가능한 요소 (조회/검색/확인 등) ###")
        try:
            btns = frame.evaluate("""() => {
                const kw = ['검색','조회','확인','다음','선택','예약'];
                return Array.from(document.querySelectorAll('button, a, input[type=submit], input[type=button], div[onclick], span[onclick], li[onclick]'))
                  .map(e => ({tag:e.tagName, id:e.id, cls:(e.className||'').toString().slice(0,60),
                              text:(e.textContent||'').trim().slice(0,40), value:e.value||'',
                              onclick:(e.getAttribute('onclick')||'').slice(0,150),
                              href:(e.getAttribute('href')||'').slice(0,120),
                              visible: !!(e.offsetParent || e.getClientRects().length)}))
                  .filter(o => kw.some(k => (o.text+o.value).includes(k)));
            }""")
            for i, b in enumerate(btns):
                mark = " <<< 헤더검색(무시)" if b["id"] in BLACKLIST_IDS else ""
                out.append(f"  [{i}] {json.dumps(b, ensure_ascii=False)}{mark}")
        except Exception as e:  # noqa: BLE001
            out.append(f"  (실패: {e})")

        out.append("\n### 날짜처럼 보이는 요소 ###")
        try:
            dates = frame.evaluate("""() => Array.from(document.querySelectorAll('*'))
                .filter(e => e.children.length === 0)
                .map(e => (e.textContent||'').trim())
                .filter(t => /^\\d{1,2}\\s*\\/\\s*\\d{1,2}/.test(t) || /\\d{4}-\\d{2}-\\d{2}/.test(t))
                .slice(0, 40)""")
            out.append(f"  {dates}")
        except Exception as e:  # noqa: BLE001
            out.append(f"  (실패: {e})")

    return "\n".join(out)


def main() -> int:
    from playwright.sync_api import sync_playwright

    log("정찰 시작 — 좌석 확인은 하지 않는다")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="ko-KR", viewport={"width": 1600, "height": 1400})
        page = ctx.new_page()
        page.set_default_timeout(10_000)
        hook_network(page)

        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
            wait_until_loaded(page)
            page.wait_for_timeout(3_000)

            page.screenshot(path=str(DEBUG / "01_page.png"), full_page=True)
            (DEBUG / "01_page.html").write_text(page.content(), encoding="utf-8")

            inv = collect(page)
            (DEBUG / "02_inventory.txt").write_text(inv, encoding="utf-8")

            body = page.inner_text("body")
            (DEBUG / "04_body_text.txt").write_text(body, encoding="utf-8")

            # 로그에도 찍어둔다 — 파일 안 받아도 눈으로 볼 수 있게
            log("=" * 50)
            log("화면에 보이는 글자 (앞부분)")
            log("=" * 50)
            print(body[:1500], flush=True)
            log("=" * 50)
            log("화면 구조 (앞부분)")
            log("=" * 50)
            print(inv[:4000], flush=True)

        except Exception as e:  # noqa: BLE001
            log(f"[error] {type(e).__name__}: {e}")
            try:
                page.screenshot(path=str(DEBUG / "01_page.png"), full_page=True)
                (DEBUG / "01_page.html").write_text(page.content(), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        finally:
            (DEBUG / "03_network.txt").write_text("\n".join(netlog), encoding="utf-8")
            log(f"통신 기록 {len(netlog)}건 저장")
            ctx.close()
            browser.close()

    s = os.environ.get("GITHUB_STEP_SUMMARY")
    if s:
        with open(s, "a", encoding="utf-8") as f:
            f.write("🔍 정찰 완료 — 아티팩트의 02_inventory.txt 확인\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
