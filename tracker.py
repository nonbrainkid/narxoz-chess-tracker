"""Read-only Narxoz availability monitor. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SPORT = "https://sport.narxoz.kz"
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


class MonitorError(Exception):
    """Messages must be safe for public logs: no URLs, payloads, or secrets."""


def request_json(url, *, payload=None, headers=None, service="API", attempts=3):
    encoded = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Accept": "application/json", "User-Agent": "NarxozChessMonitor/1.0"}
    if encoded is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, data=encoded, headers=request_headers)
            with urllib.request.urlopen(req, timeout=25) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise MonitorError(f"{service}: invalid response")
            return result
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt + 1 == attempts:
                raise MonitorError(f"{service}: HTTP {exc.code}") from None
        except (OSError, ValueError) as exc:
            if attempt + 1 == attempts:
                raise MonitorError(f"{service}: {type(exc).__name__}") from None
        time.sleep(2 ** attempt)
    raise MonitorError(f"{service}: request failed")


def env_required(key):
    value = os.environ.get(key, "").strip()
    if not value:
        raise MonitorError(f"Missing secret: {key}")
    return value


def fetch_sport():
    username = env_required("SPORT_USERNAME")
    body = {"email" if "@" in username else "studentNumber": username,
            "password": env_required("SPORT_PASSWORD")}
    # The live login endpoint returns the same authenticated snapshot as /state.
    snapshot = request_json(SPORT + "/api/auth/login", payload=body, service="Sport Hub")
    if not isinstance(snapshot.get("user"), dict) or not snapshot["user"].get("id"):
        raise MonitorError("Sport Hub: authentication failed")
    return snapshot


def minute(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d|24:00", value):
        raise MonitorError("Invalid timetable time")
    h, m = map(int, value.split(":"))
    return h * 60 + m


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if config.get("timezone") != "Asia/Almaty" or config.get("utc_offset_minutes") != 300:
        raise MonitorError("Timetable timezone must be reviewed")
    if date.fromisoformat(config["valid_from"]) > date.fromisoformat(config["valid_through"]):
        raise MonitorError("Invalid timetable date range")
    if not isinstance(config.get("buffer_minutes"), int) or config["buffer_minutes"] < 0:
        raise MonitorError("Invalid timetable buffer")
    for item in config["weekly_classes"]:
        if item["weekday"] not in range(7) or minute(item["end"]) <= minute(item["start"]):
            raise MonitorError("Invalid weekly class")
    for item in config["extra_busy"]:
        date.fromisoformat(item["date"])
        if minute(item["end"]) <= minute(item["start"]):
            raise MonitorError("Invalid extra busy interval")
    return config


def local_tz(config):
    # Kazakhstan uses UTC+05:00 throughout the configured fall 2026 semester.
    return timezone(timedelta(minutes=config["utc_offset_minutes"]))


def session_interval(session, config):
    day = date.fromisoformat(session["date"])
    start, end = minute(session["start"]), minute(session["end"])
    if start >= end or start >= 1440:
        raise MonitorError("Sport Hub: invalid session interval")
    midnight = datetime.combine(day, datetime.min.time(), local_tz(config))
    return midnight + timedelta(minutes=start), midnight + timedelta(minutes=end)


def clashes(session, config):
    day = date.fromisoformat(session["date"])
    start, end = minute(session["start"]), minute(session["end"])
    busy = [x for x in config["weekly_classes"] if x["weekday"] == day.weekday()]
    busy += [x for x in config["extra_busy"] if x["date"] == session["date"]]
    buffer = config["buffer_minutes"]
    return any(start < minute(x["end"]) + buffer and end > minute(x["start"]) - buffer for x in busy)


def localized(value):
    if isinstance(value, dict):
        return str(value.get("ru") or value.get("en") or value.get("kk") or "")
    return str(value or "")


def inspect_snapshot(snapshot, config, now):
    for key in ("sections", "sessions", "bookings", "calendar"):
        if not isinstance(snapshot.get(key), list):
            raise MonitorError(f"Sport Hub: missing {key}")
    user, limits = snapshot.get("user"), snapshot.get("bookingLimits")
    if not isinstance(user, dict) or not user.get("id") or not isinstance(limits, dict):
        raise MonitorError("Sport Hub: missing user or booking limits")
    required_limits = ("maxPerDay", "maxPerSemester", "semesterStart", "semesterEnd", "cancelHoursBefore")
    if any(k not in limits for k in required_limits):
        raise MonitorError("Sport Hub: booking rules changed")
    if limits["cancelHoursBefore"] != 8:
        raise MonitorError("Sport Hub: cancellation cutoff changed; update deadline schedules")
    section = next((x for x in snapshot["sections"] if x.get("id") == config["section_id"]), None)
    if section is None:
        raise MonitorError("Sport Hub: chess section missing")
    sessions = {x["id"]: x for x in snapshot["sessions"]}
    if not sessions or len(sessions) != len(snapshot["sessions"]):
        raise MonitorError("Sport Hub: invalid session list")
    active = [b for b in snapshot["bookings"] if not b.get("cancelledAt")]
    if any(not b.get("sessionId") or not b.get("studentId") for b in active):
        raise MonitorError("Sport Hub: invalid bookings")
    counts = Counter(b["sessionId"] for b in active)
    my_bookings = [b for b in active if b["studentId"] == user["id"]]
    if any(b["sessionId"] not in sessions for b in my_bookings):
        raise MonitorError("Sport Hub: a booked session is missing")
    mine = [sessions[b["sessionId"]] for b in my_bookings]
    my_ids = {s["id"] for s in mine}
    my_days = Counter(s["date"] for s in mine)
    used = sum(limits["semesterStart"] <= s["date"] <= limits["semesterEnd"] for s in mine)
    holidays = {x["date"] for x in snapshot["calendar"] if x.get("type") in ("holiday", "vacation")}
    candidates, reasons = {}, Counter()
    checked = 0
    for session in snapshot["sessions"]:
        if session.get("sectionId") != config["section_id"]:
            continue
        start, end = session_interval(session, config)
        if start <= now or not (config["valid_from"] <= session["date"] <= config["valid_through"]):
            continue
        if not (limits["semesterStart"] <= session["date"] <= limits["semesterEnd"]):
            continue
        checked += 1
        capacity = session.get("maxParticipants", section.get("maxParticipants"))
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
            raise MonitorError("Sport Hub: invalid capacity")
        free = max(0, capacity - counts[session["id"]])
        reason = None
        if session.get("cancelled"):
            reason = "cancelled"
        elif session["date"] in holidays:
            reason = "holiday"
        elif clashes(session, config):
            reason = "class_conflict"
        elif session["id"] in my_ids:
            reason = "already_booked"
        elif my_days[session["date"]] >= limits["maxPerDay"]:
            reason = "daily_limit"
        elif used >= limits["maxPerSemester"]:
            reason = "semester_limit"
        elif not user.get("enrolledInPE"):
            reason = "not_enrolled"
        elif not isinstance(user.get("photo"), str) or not user["photo"].strip():
            reason = "photo_required"
        elif limits.get("oneSectionOnly") and any(x["sectionId"] != session["sectionId"] for x in mine):
            reason = "one_section"
        elif limits.get("consecutiveDaysForbidden") and any(abs((date.fromisoformat(x["date"]) - start.date()).days) == 1 for x in mine):
            reason = "consecutive_days"
        elif section.get("kind") == "lfk" and user.get("status") != "lfk":
            reason = "lfk_only"
        elif any(start < session_interval(x, config)[1] and end > session_interval(x, config)[0] for x in mine):
            reason = "booking_conflict"
        if reason:
            reasons[reason] += 1
        venue = localized(session.get("venue", section.get("venue")))
        fingerprint = hashlib.sha256(json.dumps([session["date"], session["start"], session["end"], venue], ensure_ascii=False).encode()).hexdigest()[:16]
        candidates[session["id"]] = {
            "id": session["id"], "date": session["date"], "start": session["start"], "end": session["end"],
            "venue": venue, "free": free if reason is None else 0, "capacity": capacity,
            "fingerprint": fingerprint, "cancel_hours": limits["cancelHoursBefore"], "reason": reason,
        }
    return candidates, {"future_chess": checked, "eligible": sum(x["reason"] is None for x in candidates.values()),
                        "available": sum(x["free"] > 0 for x in candidates.values()),
                        "active_bookings": len(mine), "semester_used": used,
                        "semester_limit": limits["maxPerSemester"], "excluded": dict(reasons)}


def empty_state():
    return {"version": 1, "initialized": False, "observations": {}, "failures": 0, "failure_alerted": False}


def validate_state(state):
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("observations"), dict):
        raise MonitorError("Monitor state is missing or corrupt; refusing to reset notification history")
    if not isinstance(state.get("initialized"), bool):
        raise MonitorError("Invalid monitor initialization state")
    for item in state["observations"].values():
        if not isinstance(item, dict) or not isinstance(item.get("free"), int) or not item.get("fingerprint"):
            raise MonitorError("Invalid notification history")
    return state


class FileStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        return validate_state(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, state):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temp.replace(self.path)


class GitHubStore:
    def __init__(self):
        repo = env_required("GITHUB_REPOSITORY")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise MonitorError("Invalid GitHub repository")
        self.url = f"https://api.github.com/repos/{repo}/contents/state.json"
        self.headers = {"Authorization": "Bearer " + env_required("GITHUB_TOKEN"), "X-GitHub-Api-Version": "2022-11-28"}
        self.sha = None

    def load(self):
        data = request_json(self.url + "?ref=monitor-state", headers=self.headers, service="GitHub state")
        self.sha = data["sha"]
        return validate_state(json.loads(base64.b64decode(data["content"])))

    def save(self, state):
        raw = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        body = {"message": "Update monitor state [skip ci]", "branch": "monitor-state",
                "sha": self.sha, "content": base64.b64encode(raw).decode()}
        # PUT with an expected SHA refuses concurrent overwrites; do not retry an ambiguous write.
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="PUT", headers={
            **self.headers, "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                self.sha = json.load(response)["content"]["sha"]
        except Exception:
            raise MonitorError("GitHub state: persistence failed") from None


class Telegram:
    def __init__(self):
        self.token = env_required("TELEGRAM_BOT_TOKEN")
        self.chat = env_required("TELEGRAM_CHAT_ID")
        if not re.fullmatch(r"[1-9]\d*", self.chat):
            raise MonitorError("Telegram recipient must be a verified private chat")

    def send(self, text, *, link=False):
        payload = {"chat_id": self.chat, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if link:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": "Открыть шахматы — записаться", "url": SPORT + "/SectionDetail?id=s-chess"}]]}
        result = request_json("https://api.telegram.org/bot" + self.token + "/sendMessage", payload=payload,
                              service="Telegram", attempts=1)
        if result.get("ok") is not True:
            raise MonitorError("Telegram: message was not accepted")


def changed_candidates(candidates, state):
    events = []
    previous = state["observations"]
    for key, item in candidates.items():
        old = previous.get(key)
        if item["free"] > 0 and (not old or old["free"] <= 0 or item["free"] > old["free"] or old["fingerprint"] != item["fingerprint"]):
            events.append(item)
    return sorted(events, key=lambda x: (x["date"], x["start"], x["id"]))


def observation(item):
    return {"free": item["free"], "fingerprint": item["fingerprint"]}


def format_events(events, config, now):
    rows = ["♟ <b>Есть места на шахматы без пересечений с парами</b>"]
    for item in events:
        start, _ = session_interval(item, config)
        cutoff = start - timedelta(hours=item["cancel_hours"])
        warning = "⚠️ Срок отмены уже прошёл." if now >= cutoff else f"Отмена до {cutoff:%d.%m %H:%M}."
        rows.append(f"<b>{start:%d.%m.%Y}, {WEEKDAYS[start.weekday()]} · {item['start']}–{item['end']}</b>\n"
                    f"Свободно: {item['free']} из {item['capacity']}\n{html.escape(item['venue'][:180])}\n{warning}")
    rows.append("Время Алматы (UTC+5). Места проверены сейчас; запись через Sport Hub.")
    return "\n\n".join(rows)


def run_monitor(snapshot, config, state, store, messenger, now):
    candidates, summary = inspect_snapshot(snapshot, config, now)
    events = changed_candidates(candidates, state)
    # Record closed/decreased/blocked slots before sending. Preserve unsent openings.
    pending = {x["id"] for x in events}
    next_observations = {key: observation(item) for key, item in candidates.items() if key not in pending}
    for key in pending:
        if key in state["observations"]:
            next_observations[key] = state["observations"][key]
    state["observations"] = next_observations
    store.save(state)
    for offset in range(0, len(events), 8):
        batch = events[offset:offset + 8]
        messenger.send(format_events(batch, config, now), link=True)
        for item in batch:
            state["observations"][item["id"]] = observation(item)
        store.save(state)
    if not state["initialized"]:
        messenger.send("✅ <b>Мониторинг шахмат запущен</b>\n\n"
                       "Проверки каждые 30 минут круглосуточно; дополнительно за 8 ч 5 мин и за 8 ч до подходящих потоков. "
                       "GitHub Actions иногда задерживает запуски.\n\n"
                       "Пн 14:00; вт 12:00, 14:00, 16:00; ср/чт 14:00; пт 16:00.\n"
                       "Учитываются пары, праздники Sport Hub, действующие записи и лимиты сайта. "
                       "Уведомления приходят при появлении/повторном открытии места или увеличении числа мест.\n\n"
                       f"Сейчас подходящих занятий с местами: {summary['available']}. "
                       f"Действующих записей за семестр: {summary['semester_used']}/{summary['semester_limit']}.\n"
                       f"Расписание проверено до {config['valid_through']}. Время Алматы (UTC+5).", link=True)
    elif state.get("failure_alerted"):
        messenger.send("✅ Проверки Sport Hub восстановлены. Мониторинг шахмат работает.")
    state.update(initialized=True, failures=0, failure_alerted=False, last_success=now.isoformat(), summary=summary)
    store.save(state)
    return {**summary, "notified_sessions": len(events)}


def record_failure(state, store, messenger):
    state["failures"] = state.get("failures", 0) + 1
    if state["failures"] >= 3 and not state.get("failure_alerted"):
        messenger.send("⚠️ Три проверки шахмат подряд завершились ошибкой. Мониторинг временно не подтверждает наличие мест. "
                       "Проверь последний запуск GitHub Actions. После восстановления придёт сообщение.")
        state["failure_alerted"] = True
    store.save(state)


def disable_expired_workflow():
    repo = env_required("GITHUB_REPOSITORY")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise MonitorError("Invalid GitHub repository")
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/workflows/monitor.yml/disable",
        method="PUT", headers={"Authorization": "Bearer " + env_required("GITHUB_TOKEN"),
                               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=25):
            pass
    except Exception:
        raise MonitorError("GitHub: could not disable expired workflow") from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="schedule.json")
    parser.add_argument("--dry-run", action="store_true", help="Live read-only check; no messages or persistence")
    parser.add_argument("--state-file", help="Use an explicitly initialized local state file instead of GitHub")
    args = parser.parse_args()
    state = store = messenger = None
    try:
        config = load_config(args.config)
        now = datetime.now(local_tz(config))
        if not args.dry_run:
            messenger = Telegram()
            store = FileStore(args.state_file) if args.state_file else GitHubStore()
            state = store.load()
        if now.date() > date.fromisoformat(config["valid_through"]):
            if state is not None and not state.get("schedule_expired"):
                messenger.send("📅 Расписание на семестр закончилось. Уведомления о местах приостановлены: обнови schedule.json и расписание GitHub Actions.")
                state["schedule_expired"] = True
                store.save(state)
            if not args.dry_run and os.environ.get("GITHUB_ACTIONS") == "true":
                disable_expired_workflow()
            print("Timetable expired; no availability notifications sent.")
            return 0
        snapshot = fetch_sport()
        if args.dry_run:
            candidates, summary = inspect_snapshot(snapshot, config, now)
            summary["available_sessions"] = [{k: x[k] for k in ("date", "start", "end", "free")} for x in candidates.values() if x["free"]]
        else:
            summary = run_monitor(snapshot, config, state, store, messenger, now)
        print(json.dumps(summary, ensure_ascii=True, sort_keys=True))
        return 0
    except Exception as exc:
        safe = str(exc) if isinstance(exc, MonitorError) else type(exc).__name__
        print("Monitor failed: " + safe, file=sys.stderr)
        if state is not None:
            try:
                record_failure(state, store, messenger)
            except Exception:
                print("Failure status could not be delivered or saved.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
