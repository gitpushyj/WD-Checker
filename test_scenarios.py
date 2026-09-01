#!/usr/bin/env python3
"""요구사항 다섯 가지를 실제 실행 흐름으로 확인한다.

watch.py 의 run_check 를 가짜 API와 함께 여러 번 돌려서, 시간이 흐르는 동안
알림이 정확히 언제 오고 언제 오지 않는지를 본다.

    python3 test_scenarios.py
"""

import json
import shutil
import tempfile
from datetime import date
from pathlib import Path

import watch

TODAY = date(2026, 9, 1)
sent = []           # 이번 검사에서 나간 알림
world = {}          # 가짜 예약 현황
max_open = ["2027-09"]


def fake_availability(year, month):
    if world.get("boom"):
        raise RuntimeError("서버 응답 없음")
    return watch.normalize(world.get((year, month), {"data": {}, "closed": {}, "open": {}, "outdoor": {}}))


def fake_max_open():
    return max_open[0]


def fake_notify(title, lines, log):
    sent.append(title)
    return True


class Args:
    def __init__(self, config, state):
        self.config, self.state, self.dry_run, self.quiet = config, state, False, True


def setup():
    watch.fetch_availability = fake_availability
    watch.fetch_max_open_month = fake_max_open
    watch.notify = fake_notify
    watch.now_kst = lambda: __import__("datetime").datetime(2026, 9, 1, 12, 0,
                                                            tzinfo=watch.KST)
    watch.load_env = lambda path: None


def run(tmp, config_text):
    """한 번의 스케줄 실행. 나간 알림 제목 목록을 돌려준다."""
    sent.clear()
    (tmp / "watchlist.json").write_text(config_text, encoding="utf-8")
    code = watch.run_check(Args(str(tmp / "watchlist.json"), str(tmp / "state.json")),
                           lambda m: None)
    return list(sent), code


def main():
    setup()
    tmp = Path(tempfile.mkdtemp())
    failures = []

    def expect(label, got, want):
        mark = "✓" if got == want else "✗"
        print(f"  {mark} {label}")
        if got != want:
            print(f"      기대: {want}\n      실제: {got}")
            failures.append(label)

    WATCH_23 = '{"targets": [{"month": "2027-09", "parts": [2, 3]},' \
               '             {"month": "2027-10", "parts": [2, 3]}]}'

    # 9월 주말은 전부 찼고, 10월은 아직 열리지 않은 상태에서 시작한다
    full = {"data": {f"2027-09-{d:02d}": [1, 2, 3, 4, 5]
                     for d in (4, 5, 11, 12, 18, 19, 25, 26)}, "closed": {}, "open": {}, "outdoor": {}}
    world[(2027, 9)] = full
    world[(2027, 10)] = {"data": {}, "closed": {}, "open": {}, "outdoor": {}}

    print("\n[1] 원하는 자리에 빈자리가 생기면 알린다")
    out, _ = run(tmp, WATCH_23)
    expect("빈자리가 없으면 조용하다", out, [])

    world[(2027, 9)] = {**full, "data": {**full["data"], "2027-09-12": [1, 3, 4, 5]}}
    out, _ = run(tmp, WATCH_23)
    expect("12일 2부가 취소되자 알린다", out, ["🔔 취소표 발생 — 1건"])

    out, _ = run(tmp, WATCH_23)
    expect("같은 자리로 또 알리지 않는다", out, [])

    print("\n[2] 다음 달이 열리면 한 번만 알린다")
    max_open[0] = "2027-10"
    out, _ = run(tmp, WATCH_23)
    expect("열린 순간 알린다", out[0].startswith("📅 2027년 10월 예약이 열렸습니다"), True)

    out, _ = run(tmp, WATCH_23)
    expect("두 번째 검사에서는 알리지 않는다", out, [])
    out, _ = run(tmp, WATCH_23)
    expect("세 번째도 조용하다", out, [])

    print("\n[3] 남이 채간 자리가 다시 취소되면 또 알린다")
    world[(2027, 9)] = full                                    # 12일 2부를 누가 예약했다
    out, _ = run(tmp, WATCH_23)
    expect("채간 순간에는 알림이 없다", out, [])

    world[(2027, 9)] = {**full, "data": {**full["data"], "2027-09-12": [1, 3, 4, 5]}}
    out, _ = run(tmp, WATCH_23)
    expect("다시 취소되자 또 알린다", out, ["🔔 취소표 발생 — 1건"])

    print("\n[4] 조건을 고치면 다음 검사에 바로 반영된다")
    world[(2027, 9)] = {**full, "data": {**full["data"], "2027-09-18": [1, 2, 3, 4]}}
    out, _ = run(tmp, WATCH_23)
    expect("5부는 감시 대상이 아니라 조용하다", out, [])

    out, _ = run(tmp, '{"targets": [{"month": "2027-09", "parts": [5]}]}')
    expect("5부를 넣자 바로 알린다", out, ["🔔 취소표 발생 — 1건"])

    out, _ = run(tmp, '{"targets": [{"date": "2027-09-18", "parts": [5]}]}')
    expect("날짜를 콕 집어도 동작한다", out, [])

    print("\n[5] 오류가 나도 다음 스케줄은 살아 있다")
    BROKEN = '{"targets": [{"month": "2027-09", "parts": [2,3]},]}'   # 쉼표가 하나 더 있다
    run(tmp, WATCH_23)                                   # 정상 설정을 한 번 읽어둔다

    out, _ = run(tmp, BROKEN)
    expect("설정 오타를 알려준다", out, ["⚠️ 취소표 감시기에 문제가 있습니다"])
    expect("같은 오타로 두 번 알리지 않는다", run(tmp, BROKEN)[0], [])

    world[(2027, 9)] = {**full, "data": {**full["data"], "2027-09-05": [1, 3, 4, 5]}}
    out, _ = run(tmp, BROKEN)
    expect("오타 상태에서도 직전 설정으로 감시를 이어간다", out, ["🔔 취소표 발생 — 1건"])

    world["boom"] = True                                 # 사이트가 응답하지 않는다
    out, code = run(tmp, WATCH_23)
    expect("서버가 죽어도 예외로 끝나지 않는다", code, 1)
    expect("서버 장애를 알려준다", out, ["⚠️ 취소표 감시기에 문제가 있습니다"])

    world["boom"] = False                                # 사이트가 돌아왔다
    out, _ = run(tmp, WATCH_23)
    expect("복구되어도 알림이 쏟아지지 않는다", out, [])

    world[(2027, 9)] = {**full, "data": {**full["data"], "2027-09-05": [1, 3, 4, 5],
                                         "2027-09-11": [1, 3, 4, 5]}}
    out, _ = run(tmp, WATCH_23)
    expect("복구 뒤 새로 난 취소표는 그대로 알린다", out, ["🔔 취소표 발생 — 1건"])

    (tmp / "state.json").write_text("{깨진 파일", encoding="utf-8")
    out, code = run(tmp, WATCH_23)
    expect("상태 파일이 깨져도 죽지 않는다", code, 0)
    expect("깨진 파일은 따로 보관한다", (tmp / "state.json.corrupt").exists(), True)

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if failures:
        print(f"실패 {len(failures)}건: {failures}")
        return 1
    print("다섯 가지 요구사항 모두 확인했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
