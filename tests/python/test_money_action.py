import pathlib
import sys
import threading
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from money_action import (  # noqa: E402
    MoneyCandidate,
    ReceiveTransaction,
    detect_money_marker,
    money_receipt_matches,
    select_money_candidate,
)
from uia_fixed_sender import _FifoSendLock  # noqa: E402


class PriorityBarrierTests(unittest.TestCase):
    def test_priority_waits_for_active_send_then_preempts_queued_normals(self):
        lock = _FifoSendLock()
        a_entered = threading.Event()
        release_a = threading.Event()
        priority_reserved = threading.Event()
        release_priority = threading.Event()
        order = []

        def normal(name, entered=None, release=None):
            with lock:
                order.append(name)
                if entered:
                    entered.set()
                if release:
                    self.assertTrue(release.wait(2))

        def priority():
            cancel = threading.Event()
            with lock.priority(cancel_event=cancel, timeout=2) as acquired:
                self.assertTrue(acquired)
                order.append("R")
                priority_reserved.set()
                self.assertTrue(release_priority.wait(2))

        workers = [
            threading.Thread(target=normal, args=("A", a_entered, release_a)),
            threading.Thread(target=normal, args=("B",)),
            threading.Thread(target=normal, args=("C",)),
        ]
        workers[0].start()
        self.assertTrue(a_entered.wait(1))
        workers[1].start()
        workers[2].start()
        priority_worker = threading.Thread(target=priority)
        priority_worker.start()

        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and lock.priority_waiter_count() != 1:
            time.sleep(0.005)
        self.assertEqual(lock.priority_waiter_count(), 1)

        release_a.set()
        self.assertTrue(priority_reserved.wait(1))
        self.assertEqual(order, ["A", "R"])
        release_priority.set()

        for worker in [*workers, priority_worker]:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(order, ["A", "R", "B", "C"])

    def test_cancelled_priority_waiter_leaves_no_hole(self):
        lock = _FifoSendLock()
        active = threading.Event()
        release = threading.Event()

        def hold_normal():
            with lock:
                active.set()
                release.wait(2)

        holder = threading.Thread(target=hold_normal)
        holder.start()
        self.assertTrue(active.wait(1))

        with lock.priority(
            cancel_event=threading.Event(),
            timeout=0.02,
        ) as acquired:
            self.assertFalse(acquired)
        self.assertEqual(lock.priority_waiter_count(), 0)

        release.set()
        holder.join(2)
        with lock.priority(
            cancel_event=threading.Event(),
            timeout=1,
        ) as acquired:
            self.assertTrue(acquired)


class MoneyClassifierTests(unittest.TestCase):
    def test_only_exact_money_markers_are_detected(self):
        self.assertEqual(detect_money_marker({"content": "[红包]"}), "red_packet")
        self.assertEqual(detect_money_marker({"content": "[转账]"}), "transfer")
        self.assertIsNone(detect_money_marker({"content": "发你一个[红包]"}))
        self.assertIsNone(detect_money_marker({"content": "[转账收款]"}))

    def test_selects_exact_incoming_transfer_candidate(self):
        sse = {
            "rawid": "server-1",
            "sessionId": "session-1",
            "timestamp": 100,
            "content": "[转账]",
        }
        candidate = select_money_candidate(
            sse,
            [
                {
                    "serverId": "server-1",
                    "localType": "8589934592049",
                    "isSend": 0,
                    "createTime": 100,
                    "content": (
                        "<msg><appmsg><wcpayinfo>"
                        "<paysubtype>1</paysubtype>"
                        "<feedesc><![CDATA[￥12.34]]></feedesc>"
                        "<paymsgid>pay-1</paymsgid>"
                        "<transferid>transfer-1</transferid>"
                        "<transcationid>transaction-1</transcationid>"
                        "</wcpayinfo></appmsg></msg>"
                    ),
                }
            ],
        )
        self.assertIsInstance(candidate, MoneyCandidate)
        self.assertEqual(candidate.kind, "transfer")
        self.assertEqual(candidate.session_id, "session-1")
        self.assertEqual(candidate.source_server_id, "server-1")
        self.assertEqual(candidate.amount_cny, "12.34")
        self.assertEqual(candidate.correlation_ids["paymsgid"], "pay-1")
        self.assertEqual(
            candidate.correlation_ids["transcationid"],
            "transaction-1",
        )

    def test_sse_local_id_resolves_to_authoritative_server_id(self):
        candidate = select_money_candidate(
            {
                "rawid": "local-1",
                "sessionId": "session-1",
                "timestamp": 100,
                "content": "[红包]",
            },
            [
                {
                    "localId": "local-1",
                    "serverId": "server-1",
                    "localType": "8594229559345",
                    "isSend": 0,
                    "createTime": 100,
                    "content": "<type>2001</type>",
                }
            ],
        )

        self.assertIsInstance(candidate, MoneyCandidate)
        self.assertEqual(candidate.kind, "red_packet")
        self.assertEqual(candidate.source_server_id, "server-1")

    def test_ambiguous_local_or_server_id_match_is_rejected(self):
        sse = {
            "rawid": "shared-id",
            "sessionId": "session-1",
            "timestamp": 100,
            "content": "[红包]",
        }
        rows = [
            {
                "localId": "local-1",
                "serverId": "shared-id",
                "localType": "8594229559345",
                "isSend": 0,
                "content": "<type>2001</type>",
            },
            {
                "localId": "shared-id",
                "serverId": "server-2",
                "localType": "8594229559345",
                "isSend": 0,
                "content": "<type>2001</type>",
            },
        ]

        self.assertIsNone(select_money_candidate(sse, rows))

    def test_rejects_transfer_receipt_as_a_new_candidate(self):
        sse = {
            "rawid": "server-2",
            "sessionId": "session-1",
            "timestamp": 101,
            "content": "[转账]",
        }
        candidate = select_money_candidate(
            sse,
            [
                {
                    "serverId": "server-2",
                    "localType": "8589934592049",
                    "isSend": 1,
                    "content": "<paysubtype>3</paysubtype>",
                }
            ],
        )
        self.assertIsNone(candidate)

    def test_transfer_candidate_requires_both_stable_identifiers(self):
        sse = {
            "rawid": "missing-key",
            "sessionId": "session-1",
            "timestamp": 100,
            "content": "[转账]",
        }
        row = {
            "serverId": "missing-key",
            "localType": "8589934592049",
            "isSend": 0,
            "content": (
                "<paysubtype>1</paysubtype>"
                "<transferid>transfer-1</transferid>"
            ),
        }
        self.assertIsNone(select_money_candidate(sse, [row]))

    def test_group_transfer_requires_configured_account_as_receiver(self):
        sse = {
            "rawid": "group-transfer",
            "sessionId": "room@chatroom",
            "sessionType": "group",
            "timestamp": 100,
            "content": "[转账]",
        }
        row = {
            "serverId": "group-transfer",
            "localType": "8589934592049",
            "isSend": 0,
            "content": (
                "<paysubtype>1</paysubtype>"
                "<receiver_username>wxid_bot</receiver_username>"
                "<transferid>transfer-1</transferid>"
                "<transcationid>transaction-1</transcationid>"
            ),
        }
        self.assertIsNone(select_money_candidate(sse, [row]))
        self.assertIsNone(
            select_money_candidate(
                sse,
                [row],
                account_id="another-account",
            )
        )
        self.assertIsNotNone(
            select_money_candidate(
                sse,
                [row],
                account_id="wxid_bot",
            )
        )

    def test_transfer_receipt_requires_matching_transaction_identifier(self):
        missing_key_candidate = MoneyCandidate(
            kind="transfer",
            session_id="session-1",
            source_server_id="server-1",
            source_timestamp=100,
            correlation_ids={"transferid": "transfer-1"},
        )
        self.assertFalse(
            money_receipt_matches(
                missing_key_candidate,
                [
                    {
                        "serverId": "receipt-missing-key",
                        "createTime": 101,
                        "content": (
                            "<paysubtype>3</paysubtype>"
                            "<transferid>transfer-1</transferid>"
                        ),
                    }
                ],
            )
        )
        candidate = MoneyCandidate(
            kind="transfer",
            session_id="session-1",
            source_server_id="server-1",
            source_timestamp=100,
            correlation_ids={
                "transferid": "transfer-1",
                "transcationid": "transaction-1",
            },
        )
        self.assertTrue(
            money_receipt_matches(
                candidate,
                [
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
                ],
            )
        )
        self.assertFalse(
            money_receipt_matches(
                candidate,
                [
                    {
                        "serverId": "receipt-2",
                        "createTime": 102,
                        "isSend": 1,
                        "content": (
                            "<paysubtype>3</paysubtype>"
                            "<transferid>another-transfer</transferid>"
                            "<transcationid>transaction-1</transcationid>"
                        ),
                    }
                ],
            )
        )
    def test_transfer_amount_requires_one_unambiguous_currency_value(self):
        sse = {
            "rawid": "amount-transfer",
            "sessionId": "session-1",
            "timestamp": 100,
            "content": "[转账]",
        }

        def candidate(feedesc):
            return select_money_candidate(
                sse,
                [
                    {
                        "serverId": "amount-transfer",
                        "localType": "8589934592049",
                        "isSend": 0,
                        "content": (
                            "<paysubtype>1</paysubtype>"
                            f"<feedesc><![CDATA[{feedesc}]]></feedesc>"
                            "<transferid>transfer-amount</transferid>"
                            "<transcationid>transaction-amount</transcationid>"
                        ),
                    }
                ],
            )

        self.assertEqual(candidate("￥1000.00").amount_cny, "1000.00")
        self.assertEqual(
            candidate("手续费1元，转账金额￥1000.00").amount_cny,
            "",
        )

    def test_red_packet_receipt_requires_own_post_action_system_message(self):
        candidate = MoneyCandidate(
            kind="red_packet",
            session_id="session-1",
            source_server_id="server-1",
            source_timestamp=100,
        )
        self.assertTrue(
            money_receipt_matches(
                candidate,
                [
                    {
                        "serverId": "system-1",
                        "createTime": 101,
                        "localType": "10000",
                        "content": "你领取了Alice的红包",
                    }
                ],
            )
        )
        self.assertTrue(
            money_receipt_matches(
                candidate,
                [
                    {
                        "serverId": "system-cdata",
                        "createTime": 103,
                        "localType": "10000",
                        "content": (
                            "<content><![CDATA["
                            "你领取了Alice的红包"
                            "]]></content>"
                        ),
                    }
                ],
            )
        )
        self.assertTrue(
            money_receipt_matches(
                candidate,
                [
                    {
                        "serverId": "system-unresolved-wxid",
                        "createTime": 104,
                        "localType": "10000",
                        "content": "你领取了$wxid_example123$的红包",
                    }
                ],
            )
        )
        self.assertFalse(
            money_receipt_matches(
                candidate,
                [
                    {
                        "serverId": "system-2",
                        "createTime": 102,
                        "localType": "10000",
                        "content": "Bob领取了Alice的红包",
                    }
                ],
            )
        )


class ReceiveTransactionTests(unittest.TestCase):
    def make_transaction(self):
        return ReceiveTransaction(
            request_id="request-1",
            generation=4,
            source_ref="server-1",
            deadline=time.monotonic() + 2,
        )

    def test_visual_first_still_requires_weflow_success(self):
        transaction = self.make_transaction()
        self.assertTrue(
            transaction.mark_visual_success(
                request_id="request-1",
                generation=4,
            )
        )
        self.assertFalse(transaction.completed)
        self.assertTrue(
            transaction.mark_weflow_success(
                request_id="request-1",
                generation=4,
                source_ref="server-1",
            )
        )
        self.assertTrue(transaction.completed)
        self.assertEqual(transaction.status, "completed")

    def test_weflow_first_still_requires_visual_success(self):
        transaction = self.make_transaction()
        self.assertTrue(
            transaction.mark_weflow_success(
                request_id="request-1",
                generation=4,
                source_ref="server-1",
            )
        )
        self.assertFalse(transaction.completed)
        self.assertTrue(
            transaction.mark_visual_success(
                request_id="request-1",
                generation=4,
            )
        )
        self.assertTrue(transaction.completed)

    def test_stale_or_wrong_source_signal_is_rejected(self):
        transaction = self.make_transaction()
        self.assertFalse(
            transaction.mark_visual_success(
                request_id="old-request",
                generation=4,
            )
        )
        self.assertFalse(
            transaction.mark_weflow_success(
                request_id="request-1",
                generation=3,
                source_ref="server-1",
            )
        )
        self.assertFalse(
            transaction.mark_weflow_success(
                request_id="request-1",
                generation=4,
                source_ref="other-server",
            )
        )
        self.assertFalse(transaction.completed)


if __name__ == "__main__":
    unittest.main()
