from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import tracker as t

ROOT = Path(__file__).resolve().parents[1]
CONFIG = t.load_config(ROOT / "schedule.json")
NOW = datetime(2026, 9, 27, 12, tzinfo=timezone(timedelta(hours=5)))


def session(key="one", day="2026-09-28", start="14:00", end="15:40", **kwargs):
    return dict(id=key, date=day, start=start, end=end, sectionId="s-chess", **kwargs)


def snapshot(*sessions):
    return {"user": {"id": "me", "enrolledInPE": True, "photo": "present", "status": "all"},
            "sections": [{"id": "s-chess", "maxParticipants": 20, "kind": "regular", "venue": {"ru": "Room 16"}}],
            "sessions": list(sessions or [session()]), "bookings": [], "calendar": [],
            "bookingLimits": {"maxPerDay": 1, "maxPerSemester": 15, "semesterStart": "2026-09-21",
                              "semesterEnd": "2026-12-26", "cancelHoursBefore": 8,
                              "oneSectionOnly": False, "consecutiveDaysForbidden": False}}


class Store:
    def __init__(self):
        self.saved = None

    def save(self, state):
        self.saved = deepcopy(state)


class Messenger:
    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    def send(self, text, **kwargs):
        if self.fail:
            raise t.MonitorError("Telegram: simulated timeout")
        self.messages.append(text)


class TimetableTests(unittest.TestCase):
    def test_every_current_weekly_chess_slot_against_screenshot(self):
        expected = {
            ("2026-09-28", "10:00", "11:40"): True,
            ("2026-09-28", "14:00", "15:40"): False,
            ("2026-09-29", "12:00", "13:40"): False,
            ("2026-09-29", "14:00", "15:40"): False,
            ("2026-09-29", "16:00", "17:40"): False,
            ("2026-09-30", "12:00", "13:40"): True,
            ("2026-09-30", "14:00", "15:40"): False,
            ("2026-10-01", "10:00", "11:40"): True,
            ("2026-10-01", "14:00", "15:40"): False,
            ("2026-10-02", "12:00", "13:40"): True,
            ("2026-10-02", "14:00", "15:40"): True,
            ("2026-10-02", "16:00", "17:40"): False,
        }
        for (day, start, end), conflict in expected.items():
            with self.subTest(day=day, start=start):
                self.assertEqual(t.clashes(session(day=day, start=start, end=end), CONFIG), conflict)

    def test_exact_boundaries_and_optional_buffer(self):
        slot = session(start="13:45", end="15:00")
        self.assertFalse(t.clashes(slot, CONFIG))
        self.assertTrue(t.clashes(slot, {**CONFIG, "buffer_minutes": 5}))

    def test_saturday_class_and_unknown_pe_end(self):
        self.assertTrue(t.clashes(session(day="2026-10-03", start="13:00", end="14:00"), CONFIG))
        self.assertTrue(t.clashes(session(day="2026-10-03", start="20:00", end="21:00"), CONFIG))

    def test_dated_exception(self):
        config = {**CONFIG, "extra_busy": [{"date": "2026-09-28", "start": "14:30", "end": "15:00"}]}
        self.assertTrue(t.clashes(session(), config))

    def test_invalid_interval_rejected(self):
        with self.assertRaises(t.MonitorError):
            t.session_interval(session(start="16:00", end="15:00"), CONFIG)

    def test_workflow_covers_cutoffs_and_five_minutes_before(self):
        text = (ROOT / ".github/workflows/monitor.yml").read_text(encoding="utf-8")
        crons = re.findall(r"cron: '([^']+)'", text)

        def matches(field, value):
            if field == "*":
                return True
            for part in field.split(","):
                if "-" in part:
                    low, high = map(int, part.split("-"))
                    if low <= value <= high:
                        return True
                elif int(part) == value:
                    return True
            return False

        slots = [("2026-09-28", "14:00"), ("2026-09-29", "12:00"), ("2026-09-29", "14:00"),
                 ("2026-09-29", "16:00"), ("2026-09-30", "14:00"), ("2026-10-01", "14:00"), ("2026-10-02", "16:00")]
        for day, start in slots:
            dt = datetime.fromisoformat(f"{day}T{start}:00+05:00").astimezone(timezone.utc)
            for ahead in (timedelta(hours=8), timedelta(hours=8, minutes=5)):
                deadline = dt - ahead
                values = [deadline.minute, deadline.hour, deadline.day, deadline.month, (deadline.weekday() + 1) % 7]
                self.assertTrue(any(all(matches(f, v) for f, v in zip(cron.split(), values)) for cron in crons), str(deadline))


class AvailabilityTests(unittest.TestCase):
    def inspect(self, snap):
        return t.inspect_snapshot(snap, CONFIG, NOW)[0]

    def test_cancelled_booking_frees_seat_and_override_capacity(self):
        snap = snapshot(session(maxParticipants=2))
        snap["bookings"] = [{"sessionId": "one", "studentId": "other"},
                            {"sessionId": "one", "studentId": "cancelled", "cancelledAt": "2026-09-27"}]
        self.assertEqual(self.inspect(snap)["one"]["free"], 1)

    def test_full_and_overbooked_never_negative(self):
        snap = snapshot(session(maxParticipants=1))
        snap["bookings"] = [{"sessionId": "one", "studentId": str(i)} for i in range(2)]
        self.assertEqual(self.inspect(snap)["one"]["free"], 0)

    def test_same_day_existing_other_sport(self):
        snap = snapshot(session(), session("basket", start="16:00", end="17:40"))
        snap["sessions"][1]["sectionId"] = "s-basket"
        snap["bookings"] = [{"sessionId": "basket", "studentId": "me"}]
        self.assertEqual(self.inspect(snap)["one"]["reason"], "daily_limit")

    def test_own_booking_not_notified(self):
        snap = snapshot()
        snap["bookings"] = [{"sessionId": "one", "studentId": "me"}]
        self.assertEqual(self.inspect(snap)["one"]["reason"], "already_booked")

    def test_semester_quota_includes_past_bookings(self):
        snap = snapshot(session(), session("past", day="2026-09-21"))
        snap["bookings"] = [{"sessionId": "past", "studentId": "me"}]
        snap["bookingLimits"]["maxPerSemester"] = 1
        self.assertEqual(self.inspect(snap)["one"]["reason"], "semester_limit")

    def test_holidays_vacations_and_cancellation(self):
        for kind in ("holiday", "vacation"):
            snap = snapshot()
            snap["calendar"] = [{"date": "2026-09-28", "type": kind}]
            self.assertEqual(self.inspect(snap)["one"]["reason"], "holiday")
        self.assertEqual(self.inspect(snapshot(session(cancelled=True)))["one"]["reason"], "cancelled")

    def test_future_only_and_timetable_validity(self):
        snap = snapshot(session("past", day="2026-09-21"), session("nextyear", day="2027-01-05"))
        self.assertEqual(self.inspect(snap), {})

    def test_partial_api_response_fails_closed(self):
        snap = snapshot()
        del snap["bookings"]
        with self.assertRaises(t.MonitorError):
            self.inspect(snap)

    def test_missing_own_session_fails_closed(self):
        snap = snapshot()
        snap["bookings"] = [{"sessionId": "missing", "studentId": "me"}]
        with self.assertRaises(t.MonitorError):
            self.inspect(snap)

    def test_missing_photo_and_enrollment(self):
        for key, expected in (("photo", "photo_required"), ("enrolledInPE", "not_enrolled")):
            snap = snapshot()
            snap["user"][key] = None
            self.assertEqual(self.inspect(snap)["one"]["reason"], expected)

    def test_changed_cancellation_rule_requires_schedule_review(self):
        snap = snapshot()
        snap["bookingLimits"]["cancelHoursBefore"] = 12
        with self.assertRaises(t.MonitorError):
            self.inspect(snap)


class NotificationTests(unittest.TestCase):
    def test_compressed_api_response(self):
        import gzip
        import io
        response = io.BytesIO(gzip.compress(b'{"ok":true}'))
        response.headers = {"Content-Encoding": "gzip"}
        with patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(t.request_json("https://example.test"), {"ok": True})

    def test_partial_batch_failure_retries_only_undelivered_sessions(self):
        snap = snapshot(*[session(str(i), day="2026-09-28", start="14:00", end="15:40") for i in range(10)])
        state, store = t.empty_state(), Store()
        class FailSecond(Messenger):
            def send(self, text, **kwargs):
                if self.messages:
                    raise t.MonitorError("Telegram: simulated failure")
                super().send(text, **kwargs)
        with self.assertRaises(t.MonitorError):
            t.run_monitor(snap, CONFIG, state, store, FailSecond(), NOW)
        self.assertEqual(len(store.saved["observations"]), 8)
        result = t.run_monitor(snap, CONFIG, store.saved, store, Messenger(), NOW)
        self.assertEqual(result["notified_sessions"], 2)

    def test_no_repeat_until_reopening_or_increase(self):
        snap = snapshot(session(maxParticipants=2))
        state, store, messenger = t.empty_state(), Store(), Messenger()
        def check():
            return t.run_monitor(snap, CONFIG, state, store, messenger, NOW)["notified_sessions"]
        self.assertEqual(check(), 1)
        self.assertEqual(check(), 0)
        snap["bookings"] = [{"sessionId": "one", "studentId": "one"}]
        self.assertEqual(check(), 0)
        snap["bookings"] = []
        self.assertEqual(check(), 1)
        snap["bookings"] = [{"sessionId": "one", "studentId": str(i)} for i in range(2)]
        self.assertEqual(check(), 0)
        snap["bookings"].pop()
        self.assertEqual(check(), 1)
        self.assertEqual(check(), 0)

    def test_delivery_failure_does_not_mark_opening_delivered(self):
        snap = snapshot()
        state, store = t.empty_state(), Store()
        with self.assertRaises(t.MonitorError):
            t.run_monitor(snap, CONFIG, state, store, Messenger(fail=True), NOW)
        messenger = Messenger()
        result = t.run_monitor(snap, CONFIG, store.saved, store, messenger, NOW)
        self.assertEqual(result["notified_sessions"], 1)

    def test_recovery_only_after_three_failures(self):
        state, store, messenger = t.empty_state(), Store(), Messenger()
        state["initialized"] = True
        for _ in range(2):
            t.record_failure(state, store, messenger)
        self.assertEqual(len(messenger.messages), 0)
        t.record_failure(state, store, messenger)
        t.record_failure(state, store, messenger)
        self.assertEqual(len(messenger.messages), 1)
        t.run_monitor(snapshot(session(maxParticipants=1)), CONFIG, state, store, messenger, NOW)
        self.assertFalse(state["failure_alerted"])
        self.assertEqual(state["failures"], 0)

    def test_corrupt_state_cannot_silently_reset(self):
        with self.assertRaises(t.MonitorError):
            t.validate_state({})

    def test_notification_escapes_venue_and_shows_passed_deadline(self):
        candidates, _ = t.inspect_snapshot(snapshot(), CONFIG, NOW)
        item = candidates["one"]
        item["venue"] = "<script>&test"
        message = t.format_events([item], CONFIG, datetime.fromisoformat("2026-09-28T07:00:00+05:00"))
        self.assertIn("&lt;script&gt;&amp;test", message)
        self.assertIn("Срок отмены уже прошёл", message)

    def test_safe_http_error_never_includes_bot_token(self):
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("https://example/secret", 401, "secret", {}, None)):
            with self.assertRaises(t.MonitorError) as error:
                t.request_json("https://example/secret", service="Telegram")
        self.assertEqual(str(error.exception), "Telegram: HTTP 401")


if __name__ == "__main__":
    unittest.main()
