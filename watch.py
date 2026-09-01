#!/usr/bin/env python3
"""서울대학교 교수회관 웨딩홀 취소표 감시기.

watchlist.json 에 적어둔 날짜와 시간대가 비면 Slack / 이메일로 알린다.
표준 라이브러리만 쓰므로 pip install 없이 python3 watch.py 로 바로 돈다.

사용법:
    python3 watch.py                 한 번 검사하고 새 빈자리가 있으면 알림
    python3 watch.py --dry-run       알림을 보내지 않고 결과만 출력
    python3 watch.py --show 2027-09  그 달의 현황 전체를 표로 출력
    python3 watch.py --test-notify   실제로 알림을 보내 설정 확인
    python3 watch.py --test          내장 테스트 실행

예약 현황은 페이지가 쓰는 것과 같은 JSON API에서 그대로 가져온다.
    GET /ajax/wedding/availability?year=YYYY&month=M
      data    예약 완료된 부   {"2027-09-12": [1, 4]}
      closed  예약 불가한 부   {"2027-09-12": [5]}
      open    평일 특별오픈일  {"2026-10-09": {...}}
      outdoor 야외 타임인 부   {"2027-09-12": 4}
    GET /ajax/wedding/max_open_month  현재 예약이 열린 마지막 달
"""

import argparse
import calendar
import json
import os
import smtplib
import ssl
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

BASE = "https://www.snufacultyclub.com"
PART_TIMES = {1: "11:00", 2: "13:00", 3: "15:00", 4: "17:00", 5: "18:30"}
KST = timezone(timedelta(hours=9))
DOW_KO = "월화수목금토일"
DOW_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
ALL_PARTS = [1, 2, 3, 4, 5]

HERE = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


# ---------------------------------------------------------------- 설정 읽기

def strip_line_comments(text):
    """JSON 에 // 로 시작하는 주석 줄을 쓸 수 있게 걷어낸다."""
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("//"))


def load_config(path, state=None):
    """설정을 읽는다. 실패하면 마지막으로 성공한 설정으로 감시를 이어간다.

    돌려주는 값은 (설정, 오류메시지) 다. 설정 파일을 고치다 오타를 내도 감시가
    멈추면 안 되므로, 직전에 성공한 설정을 상태 파일에 넣어두고 그것으로 버틴다.
    """
    name = Path(path).name
    try:
        text = Path(path).read_text(encoding="utf-8")
        cfg = json.loads(strip_line_comments(text))
        if not cfg.get("targets"):
            raise ValueError("targets 가 비어 있습니다")
        validate_targets(cfg["targets"])            # 전부 실제로 쓸 수 있는지 확인
        if state is not None:
            state["last_good_config"] = cfg
        return cfg, None
    except Exception as exc:
        cached = (state or {}).get("last_good_config")
        if cached:
            return cached, f"{name} 을 읽지 못해 마지막으로 정상이던 설정으로 감시합니다 — {exc}"
        return None, f"{name} 을 읽지 못했습니다 — {exc}"


KEYCHAIN_SERVICE = "snu-wedding-watch"


def keychain_password(account):
    """macOS 키체인에 넣어둔 앱 비밀번호를 꺼낸다. 없으면 None.

    .env 에 평문으로 두는 대신 키체인에 맡길 수 있게 한 것이다. 넣는 법은
        security add-generic-password -s snu-wedding-watch -a 계정 -w 앱비밀번호
    """
    try:
        done = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def load_env(path):
    """KEY=VALUE 형식의 .env 를 읽어 환경변수에 채운다. 이미 있는 값은 건드리지 않는다."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        if value:
            os.environ.setdefault(key.strip(), value)

    # .env 에 비밀번호가 없으면 키체인에 물어본다
    user = os.environ.get("SMTP_USER")
    if user and not os.environ.get("SMTP_PASSWORD"):
        found = keychain_password(user)
        if found:
            os.environ["SMTP_PASSWORD"] = found


# ---------------------------------------------------------------- 빈자리 판정

def normalize(payload):
    """API 응답을 정규화한다. PHP 가 빈 맵을 [] 로 내보내므로 dict 로 맞춘다."""
    out = {}
    for key in ("data", "open", "closed", "outdoor"):
        value = payload.get(key)
        out[key] = value if isinstance(value, dict) else {}
    return out


def visible_parts(ymd, avail):
    """그날 예약 화면에 실제로 뜨는 부 목록. 페이지의 렌더링 규칙을 그대로 따른다."""
    if date.fromisoformat(ymd).weekday() >= 5:  # 토, 일
        return ALL_PARTS
    if ymd in avail["open"]:                    # 평일 특별오픈일
        return ALL_PARTS
    return []                                   # 그 밖의 평일은 예약 자체가 열리지 않는다


def free_parts(ymd, avail, today):
    """그날 지금 예약할 수 있는 부 목록."""
    if date.fromisoformat(ymd) < today:
        return []
    reserved = set(avail["data"].get(ymd) or [])
    closed = set(avail["closed"].get(ymd) or [])
    return [p for p in visible_parts(ymd, avail) if p not in reserved and p not in closed]


def candidate_dates(target):
    """target 하나를 감시할 날짜 목록으로 펼친다.

    date 와 month 중 어느 이름을 썼는지가 아니라 값의 생김새로 판단한다.
    "2027-09-12" 면 그 하루, "2027-09" 면 그 달의 해당 요일 전체다.
    키를 헷갈려 적어도 뜻대로 동작해야 예약을 놓치지 않는다.
    """
    value = str(target.get("date") or target.get("month") or "").strip()
    chunks = value.split("-")

    if len(chunks) == 3:
        return [date.fromisoformat(value).isoformat()]

    if len(chunks) == 2:
        year, month = int(chunks[0]), int(chunks[1])
        if not 1 <= month <= 12:
            raise ValueError(f'달이 올바르지 않습니다: "{value}"')
        names = target.get("weekdays", ["sat", "sun"])
        try:
            allowed = {DOW_NAMES[str(w).strip().lower()[:3]] for w in names}
        except KeyError:
            raise ValueError(f"weekdays 는 mon~sun 으로 적어주세요: {names}") from None
        last = calendar.monthrange(year, month)[1]
        return [f"{year}-{month:02d}-{day:02d}" for day in range(1, last + 1)
                if date(year, month, day).weekday() in allowed]

    raise ValueError(f'날짜 형식이 올바르지 않습니다: "{value}"'
                     " (하루는 2027-09-12, 한 달은 2027-09 처럼 적어주세요)")


def validate_targets(targets):
    """설정을 실제로 쓰기 전에 전부 훑어본다. 몇 번째 항목이 문제인지 알려준다."""
    if not isinstance(targets, list):
        raise ValueError("targets 는 목록이어야 합니다")
    for index, target in enumerate(targets, start=1):
        if not isinstance(target, dict):
            raise ValueError(f"{index}번째 항목이 올바르지 않습니다: {target!r}")
        try:
            candidate_dates(target)
        except ValueError as exc:
            raise ValueError(f"{index}번째 항목 — {exc}") from None
        parts = target.get("parts")
        if parts is not None:
            bad = [p for p in parts if p not in PART_TIMES]
            if bad:
                raise ValueError(f"{index}번째 항목 — parts 는 1~5 만 됩니다: {bad}")


def slot_url(ymd, part):
    return f"{BASE}/sub04/sub05_2?date={ymd}&part={part}"


def describe(ymd, part):
    d = date.fromisoformat(ymd)
    return f"{d.year}년 {d.month}월 {d.day}일 ({DOW_KO[d.weekday()]}) {part}부 {PART_TIMES[part]}"


def find_openings(targets, fetch, today, max_open=None, report=None):
    """설정한 조건에 해당하는 예약 가능 슬롯을 모두 찾는다.

    max_open 보다 뒤인 달은 아직 예약이 열리지 않았다. 그런 달은 API 상으로는
    예약이 0건이라 전부 빈자리처럼 보이지만 실제로는 예약할 수 없으므로 건너뛴다.
    """
    cache, seen, found = {}, set(), []
    report = {} if report is None else report
    attempted, failed = report.setdefault("attempted", set()), report.setdefault("failed", {})
    for target in targets:
        wanted = set(target.get("parts") or ALL_PARTS)
        for ymd in candidate_dates(target):
            if max_open and ymd[:7] > max_open:
                continue
            year, month = int(ymd[:4]), int(ymd[5:7])
            if (year, month) not in cache:
                attempted.add(ymd[:7])
                try:
                    cache[(year, month)] = fetch(year, month)
                except Exception as exc:
                    cache[(year, month)] = None
                    failed[ymd[:7]] = str(exc)
            if cache[(year, month)] is None:
                continue
            for part in free_parts(ymd, cache[(year, month)], today):
                if part in wanted and (ymd, part) not in seen:
                    seen.add((ymd, part))
                    found.append({"ymd": ymd, "part": part, "time": PART_TIMES[part],
                                  "url": slot_url(ymd, part), "label": describe(ymd, part)})
    return sorted(found, key=lambda o: (o["ymd"], o["part"]))


# ---------------------------------------------------------------- API 호출

def http_json(url, timeout=20, attempts=3):
    """일시적인 네트워크 오류는 잠깐 쉬었다 다시 시도한다."""
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": UA,
    })
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return json.loads(res.read().decode("utf-8"))
        except Exception:
            if attempt == attempts:
                raise
            time.sleep(2 * attempt)


def fetch_availability(year, month):
    payload = http_json(f"{BASE}/ajax/wedding/availability?year={year}&month={month}")
    if not payload.get("ok"):
        raise RuntimeError(f"{year}-{month:02d} 조회 실패: {payload}")
    return normalize(payload)


def fetch_max_open_month():
    """예약이 열려 있는 마지막 달을 'YYYY-MM' 으로 돌려준다."""
    payload = http_json(f"{BASE}/ajax/wedding/max_open_month")
    if not payload.get("ok") or not payload.get("max"):
        return None
    return f"{payload['max']['year']}-{payload['max']['month']:02d}"


# ---------------------------------------------------------------- 알림 보내기

def send_slack(webhook, title, lines):
    text = title + "\n" + "\n".join(lines)
    body = json.dumps({"text": text, "unfurl_links": False}).encode("utf-8")
    req = urllib.request.Request(webhook, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as res:
        return res.status == 200


def mail_recipients(sender):
    """MAIL_TO 를 받는 사람 목록으로 나눈다. 쉼표든 세미콜론이든 받는다.

    비워두면 보내는 사람 자신에게 보낸다. 주소처럼 보이지 않는 값이 섞여 있으면
    조용히 넘기지 않고 알려준다. 오타 하나로 알림을 못 받으면 안 되기 때문이다.
    """
    raw = os.environ.get("MAIL_TO") or sender
    addrs = [a.strip() for a in raw.replace(";", ",").split(",") if a.strip()]
    wrong = [a for a in addrs if "@" not in a]
    if wrong:
        raise ValueError(f"MAIL_TO 에 메일 주소가 아닌 값이 있습니다: {', '.join(wrong)}")
    if not addrs:
        raise ValueError("받는 사람이 없습니다. MAIL_TO 를 확인해 주세요.")
    return addrs


def send_email(title, lines):
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    recipients = mail_recipients(user)

    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.set_content(title + "\n\n" + "\n".join(lines))

    with smtplib.SMTP(host, port, timeout=20) as server:
        server.starttls(context=ssl.create_default_context())
        server.login(user, password)
        server.send_message(msg, to_addrs=recipients)
    return len(recipients)


def notify(title, lines, log):
    """설정된 채널로 모두 보낸다. 하나라도 성공하면 True."""
    delivered = False
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if webhook:
        try:
            send_slack(webhook, title, lines)
            delivered = True
            log("슬랙 발송 완료")
        except Exception as exc:
            log(f"슬랙 발송 실패: {exc}")
    if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"):
        try:
            count = send_email(title, lines)
            delivered = True
            log(f"메일 발송 완료 ({count}명)")
        except Exception as exc:
            log(f"메일 발송 실패: {exc}")
    if not webhook and not os.environ.get("SMTP_USER"):
        log("알림 채널이 설정되지 않았습니다. .env 를 확인하세요.")
    return delivered


# ---------------------------------------------------------------- 상태 저장

def fresh_state():
    return {"notified": {}, "max_open": None, "first_run": True}


def load_state(path, log=None):
    """상태를 읽는다. 파일이 깨져 있으면 옆으로 치우고 새로 시작한다."""
    p = Path(path)
    if not p.exists():
        return fresh_state()
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("형식이 올바르지 않습니다")
    except Exception as exc:
        broken = p.with_name(p.name + ".corrupt")
        try:
            p.replace(broken)
        except OSError:
            pass
        if log:
            log(f"상태 파일이 손상되어 새로 시작합니다 ({exc}). 원본은 {broken.name} 에 두었습니다.")
        return fresh_state()
    state.setdefault("notified", {})
    state.setdefault("max_open", None)
    state["first_run"] = False
    return state


def save_state(path, state):
    """임시 파일에 먼저 쓰고 바꿔치기한다. 도중에 꺼져도 파일이 깨지지 않는다."""
    payload = {k: v for k, v in state.items() if k != "first_run"}
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def merge_notified(notified, current, failed_months, stamp):
    """현재 빈자리를 기록하되, 조회에 실패한 달의 기존 기록은 그대로 둔다.

    조회 실패를 '빈자리가 사라졌다'로 읽으면, 사이트가 복구된 순간 남아 있던
    자리를 전부 새 취소표로 착각해 알림이 쏟아진다.
    """
    kept = {k: v for k, v in notified.items() if k[:7] in failed_months}
    seen_now = {k: notified.get(k, stamp) for k in current}
    return {**kept, **seen_now}


def report_problem(state, path, message, log):
    """같은 문제로 10분마다 알림이 쏟아지지 않게, 내용이 바뀔 때만 한 번 알린다."""
    log(message)
    if state.get("last_error") == message:
        return
    if notify("⚠️ 취소표 감시기에 문제가 있습니다", [message, "", "감시는 계속 시도합니다."], log):
        state["last_error"] = message
        try:
            save_state(path, state)
        except OSError as exc:
            log(f"상태 저장 실패: {exc}")


# ---------------------------------------------------------------- 실행 흐름

def now_kst():
    return datetime.now(KST)


def make_logger(quiet=False):
    logfile = HERE / "watch.log"
    def log(message):
        stamp = now_kst().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{stamp}] {message}"
        if not quiet:
            print(line, flush=True)
        with logfile.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    return log


def show_month(ym):
    """한 달 현황을 표로 찍는다. 설정 없이 눈으로 확인할 때 쓴다."""
    year, month = (int(x) for x in ym.split("-"))
    avail = fetch_availability(year, month)
    today = now_kst().date()
    days = sorted(set(avail["data"]) | set(avail["closed"]) | set(avail["open"]))
    if not days:
        last = calendar.monthrange(year, month)[1]
        days = [f"{year}-{month:02d}-{d:02d}" for d in range(1, last + 1)
                if date(year, month, d).weekday() >= 5]

    print(f"\n{year}년 {month}월 예약 현황  ({now_kst():%Y-%m-%d %H:%M} 기준)")
    print("-" * 62)
    for ymd in days:
        d = date.fromisoformat(ymd)
        free = free_parts(ymd, avail, today)
        text = "  ".join(f"{p}부 {PART_TIMES[p]}" for p in free) if free else "빈자리 없음"
        print(f"  {d.month:2d}/{d.day:<2d} ({DOW_KO[d.weekday()]})  {text}")
    print("-" * 62)
    total = sum(len(free_parts(ymd, avail, today)) for ymd in days)
    print(f"  예약 가능 슬롯 {total}개\n")


def run_check(args, log):
    load_env(HERE / ".env")
    state_path = args.state
    state = load_state(state_path, log)
    config, config_error = load_config(args.config, state)

    if config_error:
        if args.dry_run:
            log(config_error)
        else:
            report_problem(state, state_path, config_error, log)
    if config is None:
        return 1
    if not config_error:
        state["last_error"] = None

    today = now_kst().date()
    source = os.environ.get("WATCH_SOURCE", "로컬")

    title, lines = None, []

    # 새 달이 열렸는지부터 본다. 10월 예약이 열리는 순간을 잡기 위한 것이다.
    try:
        max_open = fetch_max_open_month()
    except Exception as exc:
        log(f"오픈 월 조회 실패: {exc}")
        max_open = None

    month_opened = False
    if max_open and max_open != state["max_open"]:
        if not state["first_run"] and state["max_open"]:
            month_opened = True
            log(f"예약 오픈 월 변경: {state['max_open']} -> {max_open}")
        state["max_open"] = max_open

    # 감시 대상 슬롯 조회
    report = {}
    try:
        openings = find_openings(config["targets"], fetch_availability, today, max_open, report)
    except Exception as exc:
        message = f"예약 현황을 조회하지 못했습니다 — {exc}"
        if args.dry_run:
            log(message)
        else:
            report_problem(state, state_path, message, log)
        return 1
    attempted, failed = report.get("attempted", set()), report.get("failed", {})
    if attempted and len(failed) == len(attempted):
        # 한 달도 보지 못했다. 상태를 건드리면 복구 후 알림이 쏟아지므로 그대로 둔다.
        message = "예약 현황을 조회하지 못했습니다 — " + "; ".join(
            f"{ym} {err}" for ym, err in sorted(failed.items()))
        if args.dry_run:
            log(message)
        else:
            report_problem(state, state_path, message, log)
        return 1
    for ym, err in sorted(failed.items()):
        log(f"{ym} 조회 실패, 이 달은 이번 검사에서 건너뜁니다 — {err}")

    current = {f"{o['ymd']}#{o['part']}" for o in openings}
    notified = state["notified"]
    fresh = [o for o in openings if f"{o['ymd']}#{o['part']}" not in notified]

    pending = sorted({ymd[:7] for t in config["targets"] for ymd in candidate_dates(t)
                      if max_open and ymd[:7] > max_open})
    log(f"감시 대상 빈자리 {len(openings)}개 (새로 생긴 것 {len(fresh)}개)"
        + (f", 오픈 월 {max_open}" if max_open else "")
        + (f", 미오픈 대기 {', '.join(pending)}" if pending else ""))

    if month_opened:
        year, month = max_open.split("-")
        title = f"📅 {year}년 {int(month)}월 예약이 열렸습니다 — 조건에 맞는 자리 {len(fresh)}건"
        for o in fresh:
            lines.append(f"• {o['label']}")
            lines.append(f"  {o['url']}")
        if not fresh:
            lines.append(f"{BASE}/sub04/sub05")
    elif fresh and state["first_run"]:
        title = f"📋 감시를 시작합니다 — 지금 예약 가능한 자리 {len(fresh)}건"
        for o in fresh:
            lines.append(f"• {o['label']}")
            lines.append(f"  {o['url']}")
    elif fresh:
        title = f"🔔 취소표 발생 — {len(fresh)}건"
        for o in fresh:
            lines.append(f"• {o['label']}")
            lines.append(f"  {o['url']}")
        if len(openings) > len(fresh):
            lines.append("")
            lines.append(f"(이미 알린 빈자리 {len(openings) - len(fresh)}건은 그대로 남아 있습니다)")
    else:
        # 알릴 것이 없어도 사라진 슬롯은 정리해서 다시 나면 알림이 가게 한다
        state["notified"] = merge_notified(notified, current, failed,
                                           now_kst().isoformat(timespec="seconds"))
        state["last_check"] = now_kst().isoformat(timespec="seconds")
        save_state(state_path, state)
        return 0

    lines.append("")
    lines.append(f"— {source} · {now_kst():%m/%d %H:%M} 확인")

    if args.dry_run:
        log("[dry-run] 아래 내용을 보낼 예정이었습니다")
        print("\n" + title + "\n" + "\n".join(lines) + "\n")
        return 0

    if not notify(title, lines, log):
        log("모든 채널 발송에 실패해 상태를 저장하지 않습니다. 다음 실행에서 다시 시도합니다.")
        return 1

    stamp = now_kst().isoformat(timespec="seconds")
    state["notified"] = merge_notified(notified, current, failed, stamp)
    state["last_check"] = stamp
    save_state(state_path, state)
    return 0


def main():
    parser = argparse.ArgumentParser(description="서울대 교수회관 웨딩홀 취소표 감시기")
    parser.add_argument("--config", default=str(HERE / "watchlist.json"))
    parser.add_argument("--state", default=str(HERE / "state.json"))
    parser.add_argument("--dry-run", action="store_true", help="알림을 보내지 않고 결과만 출력")
    parser.add_argument("--show", metavar="YYYY-MM", help="그 달의 현황을 표로 출력")
    parser.add_argument("--test", action="store_true", help="내장 테스트 실행")
    parser.add_argument("--test-notify", action="store_true",
                        help="실제로 알림을 한 번 보내 설정이 맞는지 확인")
    parser.add_argument("--quiet", action="store_true", help="로그 파일에만 기록")
    args = parser.parse_args()

    if args.test:
        return run_tests()
    if args.test_notify:
        load_env(HERE / ".env")
        log = make_logger(False)
        try:
            where = ", ".join(mail_recipients(os.environ.get("SMTP_USER", "")))
        except ValueError as exc:
            where = f"(확인 필요: {exc})"
        log(f"{where} 로 시험 알림을 보냅니다")
        ok = notify("✅ 취소표 감시기 설정 확인",
                    ["이 메시지가 보이면 알림 설정이 정상입니다.",
                     "취소표가 나면 이렇게 알려드립니다.", "",
                     f"— {os.environ.get('WATCH_SOURCE', '로컬')} · {now_kst():%m/%d %H:%M}"], log)
        log("설정이 정상입니다." if ok else "발송에 실패했습니다. 위 오류를 확인해 주세요.")
        return 0 if ok else 1
    if args.show:
        show_month(args.show)
        return 0

    log = make_logger(args.quiet)
    try:
        return run_check(args, log)
    except Exception:
        # 여기까지 온 오류도 이번 검사만 건너뛸 뿐, 다음 스케줄은 예정대로 실행된다.
        log("예상치 못한 오류로 이번 검사를 건너뜁니다:\n" + traceback.format_exc())
        try:
            notify("⚠️ 취소표 감시기에 문제가 있습니다",
                   ["예상치 못한 오류가 났습니다. watch.log 를 확인해 주세요.", "",
                    "다음 검사는 예정대로 진행됩니다."], log)
        except Exception:
            pass
        return 1


# ---------------------------------------------------------------- 내장 테스트

def run_tests():
    """빈자리 판정 로직을 실제 응답 모양 그대로 검증한다."""
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(f"{name}\n    기대: {want}\n    실제: {got}")

    # 2027-09 실제 응답
    sep = normalize({
        "ok": True,
        "data": {"2027-09-04": [1, 4, 3, 5, 2], "2027-09-05": [2, 1, 4],
                 "2027-09-12": [1, 4], "2027-09-18": [5, 2, 3, 4]},
        "open": [],
        "closed": {"2027-09-05": [5], "2027-09-12": [5]},
        "outdoor": {"2027-09-04": 5, "2027-09-12": 4},
    })
    today = date(2026, 9, 1)

    check("전부 예약된 토요일", free_parts("2027-09-04", sep, today), [])
    check("2·3부가 빈 일요일", free_parts("2027-09-12", sep, today), [2, 3])
    check("closed 는 빈자리가 아니다", 5 in free_parts("2027-09-05", sep, today), False)
    check("1부만 빈 토요일", free_parts("2027-09-18", sep, today), [1])
    check("데이터 없는 주말은 전부 빈자리", free_parts("2027-09-11", sep, today), ALL_PARTS)
    check("평일은 열리지 않는다", free_parts("2027-09-06", sep, today), [])
    check("과거 날짜는 제외", free_parts("2027-09-12", sep, date(2027, 10, 1)), [])

    # open 이 dict 로 온 평일 특별오픈
    weekday_open = normalize({
        "data": {"2026-10-09": [4, 1, 3, 2]}, "open": {"2026-10-09": {"4": {"outdoor": 1}}},
        "closed": {"2026-10-09": [5]}, "outdoor": {},
    })
    check("특별오픈 평일은 열린다", free_parts("2026-10-09", weekday_open, date(2026, 9, 1)), [])

    # 빈 응답이 [] 로 와도 죽지 않아야 한다
    check("빈 응답 정규화", normalize({"data": [], "open": [], "closed": [], "outdoor": []}),
          {"data": {}, "open": {}, "closed": {}, "outdoor": {}})

    # 날짜 펼치기
    check("month 는 기본으로 주말만", len(candidate_dates({"month": "2027-10"})), 10)
    check("weekdays 를 지정할 수 있다",
          candidate_dates({"month": "2027-10", "weekdays": ["fri"]}),
          ["2027-10-01", "2027-10-08", "2027-10-15", "2027-10-22", "2027-10-29"])
    check("date 는 그 하루만", candidate_dates({"date": "2027-09-12"}), ["2027-09-12"])
    check("month 에 날짜를 적어도 알아듣는다",
          candidate_dates({"month": "2027-09-04", "parts": [2]}), ["2027-09-04"])
    check("date 에 달을 적어도 알아듣는다",
          len(candidate_dates({"date": "2027-10", "parts": [2]})), 10)

    def rejects(target):
        try:
            validate_targets([target])
            return False
        except ValueError:
            return True

    check("빈 날짜는 거른다", rejects({"parts": [2]}), True)
    check("13월은 거른다", rejects({"month": "2027-13"}), True)
    check("없는 날짜는 거른다", rejects({"date": "2027-02-30"}), True)
    check("6부는 거른다", rejects({"date": "2027-09-12", "parts": [6]}), True)
    check("이상한 요일은 거른다", rejects({"month": "2027-09", "weekdays": ["토"]}), True)
    check("정상 설정은 통과한다", rejects({"month": "2027-09", "parts": [2, 3]}), False)

    # 조건 매칭
    fake = {(2027, 9): sep}
    openings = find_openings([{"date": "2027-09-12", "parts": [2]},
                              {"date": "2027-09-18", "parts": [2, 3]}],
                             lambda y, m: fake[(y, m)], today)
    check("원하는 부만 걸러낸다", [o["ymd"] + "#" + str(o["part"]) for o in openings],
          ["2027-09-12#2"])

    dedup = find_openings([{"date": "2027-09-12", "parts": [2, 3]},
                           {"month": "2027-09", "parts": [2]}],
                          lambda y, m: fake[(y, m)], today)
    check("겹치는 조건은 한 번만", len([o for o in dedup if o["ymd"] == "2027-09-12"]), 2)

    # 아직 열리지 않은 달은 API 상 예약 0건이라 전부 비어 보이지만 잡으면 안 된다
    empty_oct = normalize({"data": [], "open": [], "closed": {"2027-10-03": [5]}, "outdoor": {}})
    both = {(2027, 9): sep, (2027, 10): empty_oct}
    targets = [{"month": "2027-09", "parts": [2]}, {"month": "2027-10", "parts": [2]}]

    check("미오픈 달은 건너뛴다",
          {o["ymd"][:7] for o in find_openings(targets, lambda y, m: both[(y, m)], today, "2027-09")},
          {"2027-09"})
    check("열린 달은 잡는다",
          len(find_openings(targets, lambda y, m: both[(y, m)], today, "2027-10")) > 1, True)
    check("오픈 월을 모르면 모두 본다",
          len(find_openings(targets, lambda y, m: both[(y, m)], today, None)) > 1, True)

    # 한 달 조회가 실패해도 나머지 달은 살아남아야 한다
    def flaky(year, month):
        if month == 10:
            raise RuntimeError("타임아웃")
        return sep

    problems = {}
    survived = find_openings(targets, flaky, today, "2027-10", problems)
    check("실패한 달은 건너뛰고 계속한다", {o["ymd"][:7] for o in survived}, {"2027-09"})
    check("실패는 조용히 넘어가지 않는다", sorted(problems["failed"]), ["2027-10"])
    check("시도한 달을 보고한다", problems["attempted"], {"2027-09", "2027-10"})

    # 조회에 실패한 달의 기록은 살아남아야 한다
    before = {"2027-09-12#2": "t0", "2027-10-02#2": "t0"}
    check("실패한 달의 기록은 보존한다",
          sorted(merge_notified(before, {"2027-09-12#2"}, {"2027-10"}, "t1")),
          ["2027-09-12#2", "2027-10-02#2"])
    check("성공한 달에서 사라진 자리는 지운다",
          sorted(merge_notified(before, set(), {"2027-10"}, "t1")), ["2027-10-02#2"])

    def recipients(value):
        os.environ["MAIL_TO"] = value
        try:
            return mail_recipients("me@gmail.com")
        finally:
            os.environ.pop("MAIL_TO", None)

    check("비워두면 나에게 보낸다", recipients(""), ["me@gmail.com"])
    check("쉼표로 여러 명", recipients("a@x.com, b@y.com,c@z.com"),
          ["a@x.com", "b@y.com", "c@z.com"])
    check("세미콜론도 받는다", recipients("a@x.com; b@y.com"), ["a@x.com", "b@y.com"])
    check("줄바꿈과 빈 칸은 무시", recipients("a@x.com,, b@y.com ,"), ["a@x.com", "b@y.com"])

    try:
        recipients("a@x.com, 오타주소")
        check("주소가 아닌 값은 알려준다", "그냥 통과함", "오류로 알림")
    except ValueError as exc:
        check("주소가 아닌 값은 알려준다", "오타주소" in str(exc), True)

    check("설명 문구", describe("2027-09-12", 2), "2027년 9월 12일 (일) 2부 13:00")

    if failures:
        print(f"\n실패 {len(failures)}건\n")
        for f in failures:
            print("  ✗ " + f + "\n")
        return 1
    print("테스트 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
