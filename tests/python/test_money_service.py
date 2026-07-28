import copy
import pathlib
import sys
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from money_service import (
    MoneyActionService,
    MoneyRequestError,
    MoneyUiController,
    WeFlowMoneySource,
)
from uia_fixed_sender import _FifoSendLock
from uia_support import ClientMetrics


CALIBRATION = {
    "schema_version": 1,
    "completed": True,
    "coordinate_space": "client_area_ratio",
    "points": {
        "search_box": {"x": 0.1, "y": 0.1},
        "first_result": {"x": 0.2, "y": 0.2},
        "message_input": {"x": 0.6, "y": 0.8},
        "send_button": {"x": 0.9, "y": 0.9},
    },
    "reference": {
        "client_width": 1200,
        "client_height": 800,
        "aspect_ratio": 1.5,
        "dpi": 96,
    },
}


class FakeDriver:
    def __init__(self):
        self.actions = []
        self.metrics = ClientMetrics(
            hwnd=101,
            left=40,
            top=30,
            width=1200,
            height=800,
            dpi=96,
            visible=True,
            maximized=True,
            foreground=True,
        )

    def find_wechat_window(self):
        return 101

    def get_client_metrics(self, hwnd):
        return self.metrics

    def activate_receive_window(self, hwnd):
        if not self.metrics.foreground:
            self.actions.append(("activate_receive", hwnd))
            self.metrics = replace(self.metrics, foreground=True)

    def click_ratio(self, hwnd, point):
        self.actions.append(("click", hwnd, point))

    def click_receive_ratio(self, hwnd, point, calibration):
        self.actions.append(("receive_click", hwnd, point, calibration))

    def press_key(self, key):
        self.actions.append(("key", key))


class FakeSender:
    def __init__(self):
        self._lock = _FifoSendLock()
        self.driver = FakeDriver()
        self.calibration = copy.deepcopy(CALIBRATION)

    def reserve_receive_priority(self, *, cancel_event, timeout):
        return self._lock.reserve_priority(
            cancel_event=cancel_event,
            timeout=timeout,
        )

    def _select_contact(self, hwnd, contact):
        self.driver.actions.append(("select_contact", hwnd, contact))


class FakeSource:
    def __init__(self):
        self.received = threading.Event()

    def fetch_messages(self, _session_id):
        rows = [
            {
                "serverId": "server-1",
                "localType": "8589934592049",
                "isSend": 0,
                "createTime": 100,
                "content": (
                    "<paysubtype>1</paysubtype>"
                    "<feedesc><![CDATA[￥1.00]]></feedesc>"
                    "<transferid>transfer-1</transferid>"
                    "<transcationid>transaction-1</transcationid>"
                ),
            }
        ]
        if self.received.is_set():
            rows.append(
                {
                    "serverId": "receipt-1",
                    "createTime": 101,
                    "isSend": 1,
                    "content": (
                        "<paysubtype>3</paysubtype>"
                        "<transferid>transfer-1</transferid>"
                        "<transcationid>transaction-1</transcationid>"
                    ),
                }
            )
        return rows


class FakeController:
    def __init__(self, _sender):
        self.actions = []
        self.contacts = []
        self.target_contact = ""

    def capture_frame(self):
        return "data:image/png;base64,AAAA", "a" * 64

    def open_contact(self, contact):
        self.contacts.append(contact)
        self.target_contact = contact

    def reselect_target_contact(self):
        self.contacts.append(self.target_contact)

    def verify_normal_chat_window(self):
        self.actions.append(("verify_normal_chat",))

    def click(self, x, y):
        self.actions.append(("click", x, y))

    def press_escape(self):
        self.actions.append(("escape",))


class MoneyUiControllerTests(unittest.TestCase):
    def test_capture_and_actions_are_limited_to_validated_wechat_client(self):
        sender = FakeSender()
        captured = []
        controller = MoneyUiController(
            sender,
            capture_fn=lambda rect: captured.append(rect) or b"png-frame",
        )
        data_url, digest = controller.capture_frame()
        self.assertTrue(data_url.startswith("data:image/png;base64,"))
        self.assertEqual(len(digest), 64)
        self.assertEqual(
            (captured[0].left, captured[0].top, captured[0].right, captured[0].bottom),
            (40, 30, 1240, 830),
        )

        controller.click(0.25, 0.75)
        controller.press_escape()
        self.assertEqual(
            sender.driver.actions[0],
            (
                "receive_click",
                101,
                {"x": 0.25, "y": 0.75},
                sender.calibration,
            ),
        )
        self.assertEqual(sender.driver.actions[1][0], "key")

    def test_capture_activates_wechat_before_foreground_validation(self):
        sender = FakeSender()
        sender.driver.metrics = replace(
            sender.driver.metrics,
            foreground=False,
        )
        controller = MoneyUiController(
            sender,
            capture_fn=lambda _rect: b"png-frame",
        )

        controller.capture_frame()

        self.assertEqual(
            sender.driver.actions,
            [("activate_receive", 101)],
        )

    def test_target_contact_is_selected_reselected_and_window_validated(self):
        sender = FakeSender()

        controller = MoneyUiController(
            sender,
            capture_fn=lambda _rect: b"png-frame",
        )

        controller.open_contact("目标联系人")
        controller.reselect_target_contact()
        controller.verify_normal_chat_window()

        self.assertEqual(
            sender.driver.actions,
            [
                ("select_contact", 101, "目标联系人"),
                ("select_contact", 101, "目标联系人"),
            ],
        )

    def test_receive_popup_can_be_captured_clicked_and_closed(self):
        sender = FakeSender()
        sender.driver.metrics = ClientMetrics(
            hwnd=101,
            left=834,
            top=157,
            width=329,
            height=539,
            dpi=96,
            visible=True,
            maximized=False,
            foreground=True,
        )
        captured = []
        controller = MoneyUiController(
            sender,
            capture_fn=lambda rect: captured.append(rect) or b"popup-frame",
        )

        controller.capture_frame()
        controller.click(0.5, 0.5)
        controller.press_escape()

        self.assertEqual(
            (
                captured[0].left,
                captured[0].top,
                captured[0].right,
                captured[0].bottom,
            ),
            (834, 157, 1163, 696),
        )
        self.assertEqual(sender.driver.actions[0][0], "receive_click")
        self.assertEqual(sender.driver.actions[1][0], "key")

    def test_weflow_source_rewarms_an_evicted_session_after_backoff(self):
        calls = []
        candidate = {
            "serverId": "server-1",
            "localType": "8589934592049",
        }
        receipt = {
            "serverId": "receipt-1",
            "localType": "10000",
        }

        class Response:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        ordinary_payloads = [
            [],
            [candidate],
            [],
            [],
            [receipt],
        ]

        def request_get(url, **kwargs):
            calls.append((url, kwargs))
            if "/sessions/" in url:
                return Response(200, {"messages": []})
            return Response(200, {"messages": ordinary_payloads.pop(0)})

        clock = [0.0]
        source = WeFlowMoneySource(
            base_url="http://127.0.0.1:5031",
            access_token="unit",
            request_get=request_get,
            warm_retry_seconds=1.0,
            monotonic=lambda: clock[0],
        )
        self.assertEqual(source.fetch_messages("room/one"), [candidate])
        clock[0] = 0.5
        self.assertEqual(source.fetch_messages("room/one"), [])
        clock[0] = 1.0
        self.assertEqual(source.fetch_messages("room/one"), [receipt])
        self.assertEqual(len(calls), 7)
        self.assertIn("/sessions/room%2Fone/messages", calls[1][0])
        self.assertIn("/sessions/room%2Fone/messages", calls[5][0])
        self.assertNotIn("unit", calls[0][0])
        self.assertEqual(
            calls[0][1]["headers"]["Authorization"],
            "Bearer unit",
        )

    def test_weflow_source_rewarms_a_nonempty_but_stale_session(self):
        calls = []
        candidate = {
            "serverId": "server-1",
            "localType": "8594229559345",
        }
        receipt = {
            "serverId": "receipt-1",
            "localType": "10000",
        }

        class Response:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload

            def json(self):
                return self._payload

        ordinary_payloads = [
            [candidate],
            [candidate],
            [candidate],
            [candidate],
            [candidate, receipt],
        ]

        def request_get(url, **kwargs):
            calls.append((url, kwargs))
            if "/sessions/" in url:
                return Response({"messages": []})
            return Response({"messages": ordinary_payloads.pop(0)})

        clock = [0.0]
        source = WeFlowMoneySource(
            base_url="http://127.0.0.1:5031",
            access_token="unit",
            request_get=request_get,
            warm_retry_seconds=1.0,
            monotonic=lambda: clock[0],
        )

        self.assertEqual(source.fetch_messages("session-1"), [candidate])
        clock[0] = 0.5
        self.assertEqual(source.fetch_messages("session-1"), [candidate])
        clock[0] = 1.0
        self.assertEqual(
            source.fetch_messages("session-1"),
            [candidate, receipt],
        )
        self.assertEqual(
            sum("/sessions/" in url for url, _kwargs in calls),
            2,
        )


class MoneyActionServiceTests(unittest.TestCase):
    def setUp(self):
        self.sender = FakeSender()
        self.source = FakeSource()
        self.notices = []
        self.notified = threading.Event()

        def notify(payload):
            self.notices.append(payload)
            self.notified.set()
            return True

        self.service = MoneyActionService(
            sender=self.sender,
            generation=7,
            source=self.source,
            notifier=notify,
            controller_factory=FakeController,
            timeout_seconds=5,
            receipt_poll_seconds=0.01,
        )

    def tearDown(self):
        self.service.stop()

    def start_transfer(self):
        accepted = self.service.handle_sse(
            {
                "rawid": "server-1",
                "sessionId": "session-1",
                "timestamp": 100,
                "content": "[转账]",
                "sourceName": "target-contact",
            }
        )
        self.assertTrue(accepted)
        self.assertTrue(self.notified.wait(1))
        return self.notices[0]

    def test_target_contact_is_opened_before_agent_notice(self):
        notice = self.start_transfer()

        self.assertTrue(notice["request_id"])
        with self.service._lock:
            controller = self.service._active.controller
        self.assertEqual(controller.contacts, ["target-contact"])

    def test_contact_selection_failure_prevents_agent_notice(self):
        class RejectingController(FakeController):
            def open_contact(self, contact):
                raise RuntimeError("selection rejected")

        notices = []
        service = MoneyActionService(
            sender=self.sender,
            generation=15,
            source=self.source,
            notifier=lambda payload: notices.append(payload) or True,
            controller_factory=RejectingController,
            timeout_seconds=5,
            receipt_poll_seconds=0.01,
        )
        try:
            self.assertTrue(
                service.handle_sse(
                    {
                        "rawid": "server-1",
                        "sessionId": "session-1",
                        "timestamp": 100,
                        "content": "[转账]",
                        "sourceName": "target-contact",
                    }
                )
            )
            deadline = time.monotonic() + 1
            public = service.public_status()
            while (
                "last_result" not in public
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
                public = service.public_status()

            self.assertEqual(notices, [])
            self.assertEqual(public["last_result"]["status"], "failed")
            self.assertFalse(public["last_result"]["visual_success"])
            self.assertFalse(public["last_result"]["weflow_success"])
        finally:
            service.stop()

    def test_dual_success_holds_barrier_and_then_completes(self):
        notice = self.start_transfer()
        request_id = notice["request_id"]
        token = notice["capability_token"]
        frame = self.service.get_frame(request_id=request_id, token=token)
        self.assertEqual(frame["target_contact"], "target-contact")
        status = self.service.submit_step(
            token=token,
            payload={
                "request_id": request_id,
                "frame_sha256": frame["frame_sha256"],
                "frame_nonce": frame["frame_nonce"],
                "action": {"type": "click", "x": 0.5, "y": 0.5},
            },
        )
        self.assertFalse(status["visual_success"])
        frame = self.service.get_frame(request_id=request_id, token=token)
        status = self.service.submit_step(
            token=token,
            payload={
                "request_id": request_id,
                "frame_sha256": frame["frame_sha256"],
                "frame_nonce": frame["frame_nonce"],
                "action": {"type": "reselect_contact"},
            },
        )
        self.assertEqual(status["status"], "active")
        with self.service._lock:
            self.assertEqual(
                self.service._active.controller.contacts,
                ["target-contact", "target-contact"],
            )
        frame = self.service.get_frame(request_id=request_id, token=token)
        status = self.service.submit_step(
            token=token,
            payload={
                "request_id": request_id,
                "frame_sha256": frame["frame_sha256"],
                "frame_nonce": frame["frame_nonce"],
                "action": {"type": "done", "normal_chat": True},
            },
        )
        self.assertTrue(status["visual_success"])
        self.assertFalse(status["weflow_success"])
        self.assertEqual(status["status"], "active")

        normal_entered = threading.Event()

        def normal_send():
            with self.sender._lock:
                normal_entered.set()

        normal = threading.Thread(target=normal_send)
        normal.start()
        self.assertFalse(normal_entered.wait(0.05))

        self.source.received.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            status = self.service.get_status(
                request_id=request_id,
                token=token,
            )
            if status["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(status["status"], "completed")
        normal.join(1)
        self.assertFalse(normal.is_alive())
        self.assertTrue(normal_entered.is_set())
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            public = self.service.public_status()
            if public["active"] is False:
                break
            time.sleep(0.01)
        self.assertEqual(
            public,
            {
                "active": False,
                "last_result": {
                    "status": "completed",
                    "visual_success": True,
                    "weflow_success": True,
                    "money_kind": "transfer",
                },
            },
        )
        self.assertNotIn(request_id, repr(public))
        self.assertNotIn(token, repr(public))

    def test_receipt_present_before_first_click_is_only_baseline(self):
        self.source.received.set()
        notice = self.start_transfer()
        request_id = notice["request_id"]
        token = notice["capability_token"]

        frame = self.service.get_frame(request_id=request_id, token=token)
        self.service.submit_step(
            token=token,
            payload={
                "request_id": request_id,
                "frame_sha256": frame["frame_sha256"],
                "frame_nonce": frame["frame_nonce"],
                "action": {"type": "click", "x": 0.5, "y": 0.5},
            },
        )
        frame = self.service.get_frame(request_id=request_id, token=token)
        status = self.service.submit_step(
            token=token,
            payload={
                "request_id": request_id,
                "frame_sha256": frame["frame_sha256"],
                "frame_nonce": frame["frame_nonce"],
                "action": {"type": "done", "normal_chat": True},
            },
        )
        time.sleep(0.05)
        status = self.service.get_status(
            request_id=request_id,
            token=token,
        )
        self.assertEqual(status["status"], "active")
        self.assertTrue(status["visual_success"])
        self.assertFalse(status["weflow_success"])

    def test_worker_start_failure_releases_reserved_priority(self):
        with patch("money_service.threading.Thread") as thread_type:
            thread_type.return_value.start.side_effect = RuntimeError(
                "cannot start"
            )
            self.assertFalse(
                self.service.handle_sse(
                    {
                        "rawid": "start-failure",
                        "sessionId": "session-1",
                        "content": "[红包]",
                        "sourceName": "target-contact",
                    }
                )
            )
        self.assertEqual(self.sender._lock.priority_waiter_count(), 0)
        entered = threading.Event()
        with self.sender._lock:
            entered.set()
        self.assertTrue(entered.is_set())

    def test_stop_during_candidate_fetch_does_not_publish_notice(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingSource:
            def fetch_messages(self, _session_id):
                entered.set()
                release.wait(1)
                return []

        notices = []
        service = MoneyActionService(
            sender=self.sender,
            generation=9,
            source=BlockingSource(),
            notifier=lambda payload: notices.append(payload) or True,
            controller_factory=FakeController,
            timeout_seconds=5,
            receipt_poll_seconds=0.01,
        )
        self.assertTrue(
            service.handle_sse(
                {
                    "rawid": "blocking-fetch",
                    "sessionId": "session-1",
                    "content": "[红包]",
                    "sourceName": "target-contact",
                }
            )
        )
        self.assertTrue(entered.wait(1))
        service.stop(join_timeout=0.01)
        release.set()
        deadline = time.monotonic() + 1
        while (
            self.sender._lock.priority_waiter_count()
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        self.assertEqual(notices, [])

    def test_candidate_confirmation_retries_a_transient_source_error(self):
        class FlakySource(FakeSource):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def fetch_messages(self, session_id):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("transient")
                return super().fetch_messages(session_id)

        self.service.stop()
        self.notices.clear()
        self.notified.clear()
        source = FlakySource()
        self.service = MoneyActionService(
            sender=self.sender,
            generation=11,
            source=source,
            notifier=lambda payload: (
                self.notices.append(payload),
                self.notified.set(),
                True,
            )[-1],
            controller_factory=FakeController,
            timeout_seconds=5,
            receipt_poll_seconds=0.01,
        )
        notice = self.start_transfer()
        self.assertTrue(notice["request_id"])
        self.assertGreaterEqual(source.calls, 2)

    def test_priority_lease_outlives_blocking_click_after_stop(self):
        click_entered = threading.Event()
        release_click = threading.Event()

        class BlockingController(FakeController):
            def click(self, x, y):
                click_entered.set()
                release_click.wait(1)
                super().click(x, y)

        self.service.stop()
        self.notices.clear()
        self.notified.clear()
        self.service = MoneyActionService(
            sender=self.sender,
            generation=8,
            source=self.source,
            notifier=lambda payload: (
                self.notices.append(payload),
                self.notified.set(),
                True,
            )[-1],
            controller_factory=BlockingController,
            timeout_seconds=5,
            receipt_poll_seconds=0.01,
        )
        notice = self.start_transfer()
        frame = self.service.get_frame(
            request_id=notice["request_id"],
            token=notice["capability_token"],
        )

        click_thread = threading.Thread(
            target=lambda: self.service.submit_step(
                token=notice["capability_token"],
                payload={
                    "request_id": notice["request_id"],
                    "frame_sha256": frame["frame_sha256"],
                    "frame_nonce": frame["frame_nonce"],
                    "action": {"type": "click", "x": 0.5, "y": 0.5},
                },
            )
        )
        click_thread.start()
        self.assertTrue(click_entered.wait(1))
        stop_finished = threading.Event()
        stop_thread = threading.Thread(
            target=lambda: (
                self.service.stop(join_timeout=1),
                stop_finished.set(),
            )
        )
        stop_thread.start()
        self.assertFalse(stop_finished.wait(0.05))

        normal_entered = threading.Event()
        normal = threading.Thread(
            target=lambda: (
                self.sender._lock.__enter__(),
                normal_entered.set(),
                self.sender._lock.__exit__(None, None, None),
            )
        )
        normal.start()
        self.assertFalse(normal_entered.wait(0.05))
        release_click.set()
        click_thread.join(1)
        stop_thread.join(1)
        normal.join(1)
        self.assertFalse(click_thread.is_alive())
        self.assertTrue(stop_finished.is_set())
        self.assertTrue(normal_entered.is_set())

    def test_stale_frame_and_wrong_token_are_rejected(self):
        notice = self.start_transfer()
        request_id = notice["request_id"]
        token = notice["capability_token"]
        frame = self.service.get_frame(request_id=request_id, token=token)
        with self.assertRaises(MoneyRequestError) as wrong_token:
            self.service.get_status(request_id=request_id, token="wrong")
        self.assertEqual(wrong_token.exception.http_status, 403)
        with self.assertRaises(MoneyRequestError) as stale:
            self.service.submit_step(
                token=token,
                payload={
                    "request_id": request_id,
                    "frame_sha256": "b" * 64,
                    "frame_nonce": frame["frame_nonce"],
                    "action": {"type": "click", "x": 0.5, "y": 0.5},
                },
            )
        self.assertEqual(stale.exception.http_status, 409)
        self.assertEqual(frame["status"], "active")

    def test_same_digest_from_a_new_frame_does_not_reauthorize_old_nonce(self):
        notice = self.start_transfer()
        request_id = notice["request_id"]
        token = notice["capability_token"]
        first = self.service.get_frame(
            request_id=request_id,
            token=token,
        )
        second = self.service.get_frame(
            request_id=request_id,
            token=token,
        )
        self.assertEqual(first["frame_sha256"], second["frame_sha256"])
        self.assertNotEqual(first["frame_nonce"], second["frame_nonce"])
        with self.assertRaises(MoneyRequestError) as stale:
            self.service.submit_step(
                token=token,
                payload={
                    "request_id": request_id,
                    "frame_sha256": first["frame_sha256"],
                    "frame_nonce": first["frame_nonce"],
                    "action": {"type": "click", "x": 0.5, "y": 0.5},
                },
            )
        self.assertEqual(stale.exception.code, "E_MONEY_STALE_FRAME")

    def test_changed_window_is_rejected_before_click(self):
        class ChangingController(FakeController):
            def __init__(self, sender):
                super().__init__(sender)
                self.capture_count = 0

            def capture_frame(self):
                self.capture_count += 1
                digest = (
                    "a" * 64 if self.capture_count == 1 else "b" * 64
                )
                return "data:image/png;base64,AAAA", digest

        self.service.stop()
        self.notices.clear()
        self.notified.clear()
        controllers = []

        def create_controller(sender):
            controller = ChangingController(sender)
            controllers.append(controller)
            return controller

        self.service = MoneyActionService(
            sender=self.sender,
            generation=10,
            source=self.source,
            notifier=lambda payload: (
                self.notices.append(payload),
                self.notified.set(),
                True,
            )[-1],
            controller_factory=create_controller,
            timeout_seconds=5,
            receipt_poll_seconds=0.01,
        )
        notice = self.start_transfer()
        frame = self.service.get_frame(
            request_id=notice["request_id"],
            token=notice["capability_token"],
        )
        with self.assertRaises(MoneyRequestError) as stale:
            self.service.submit_step(
                token=notice["capability_token"],
                payload={
                    "request_id": notice["request_id"],
                    "frame_sha256": frame["frame_sha256"],
                    "frame_nonce": frame["frame_nonce"],
                    "action": {"type": "click", "x": 0.5, "y": 0.5},
                },
            )
        self.assertEqual(stale.exception.code, "E_MONEY_STALE_FRAME")
        self.assertEqual(controllers[0].actions, [])

    def test_click_fails_closed_when_baseline_omits_candidate(self):
        class IncompleteBaselineSource(FakeSource):
            def fetch_messages(self, session_id):
                if not threading.current_thread().name.startswith(
                    "money-receive-"
                ):
                    return []
                return super().fetch_messages(session_id)

        self.service.stop()
        self.notices.clear()
        self.notified.clear()
        controllers = []

        def create_controller(sender):
            controller = FakeController(sender)
            controllers.append(controller)
            return controller

        self.service = MoneyActionService(
            sender=self.sender,
            generation=12,
            source=IncompleteBaselineSource(),
            notifier=lambda payload: (
                self.notices.append(payload),
                self.notified.set(),
                True,
            )[-1],
            controller_factory=create_controller,
            timeout_seconds=5,
            receipt_poll_seconds=0.01,
        )
        notice = self.start_transfer()
        frame = self.service.get_frame(
            request_id=notice["request_id"],
            token=notice["capability_token"],
        )
        with self.assertRaises(MoneyRequestError) as baseline:
            self.service.submit_step(
                token=notice["capability_token"],
                payload={
                    "request_id": notice["request_id"],
                    "frame_sha256": frame["frame_sha256"],
                    "frame_nonce": frame["frame_nonce"],
                    "action": {"type": "click", "x": 0.5, "y": 0.5},
                },
            )
        self.assertEqual(
            baseline.exception.code,
            "E_MONEY_RECEIPT_BASELINE",
        )
        self.assertEqual(controllers[0].actions, [])


if __name__ == "__main__":
    unittest.main()
