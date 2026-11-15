#!/usr/bin/env python3
"""API-level integration tests for the incremental roll-up.

Run with:  python3 -I tests/test_app.py

Each test starts from a fresh seeded database so the rules are checked against
a known state, not against whatever a previous run left behind.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import build_server  # noqa: E402


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.sqlite3")
        self.server = build_server("127.0.0.1", 0, self.db_path)
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    # -- helpers ----------------------------------------------------
    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def post(self, path, payload, expect_status=200):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def submit(self, payload, expect=200):
        status, body = self.post("/api/batches", payload, expect)
        self.assertEqual(status, expect, body)
        return body

    @staticmethod
    def zone(summary, key):
        for row in summary:
            if row["zone_key"] == key:
                return row
        return None

    # -- seed -------------------------------------------------------
    def test_seed_matches_rules(self):
        state = self.get("/api/state")
        self.assertEqual(state["snapshot_version"], 1)
        north = self.zone(state["summary"], "Z-NORTH")
        self.assertEqual(north["order_count"], 4)
        self.assertEqual(north["amount_count"], 3)
        self.assertEqual(north["null_count"], 1)
        self.assertEqual(north["zero_count"], 1)
        self.assertEqual(north["total_cents"], 19250)
        self.assertEqual(north["unit_price_cents"], 6417)
        unmatched = self.zone(state["summary"], "__unmatched__")
        self.assertEqual(unmatched["order_count"], 2)
        self.assertTrue(all(item["is_match"] for item in state["reconciliation"]))

    # -- duplicate keys ---------------------------------------------
    def test_duplicate_keys_are_rejected_and_remainder_applies(self):
        result = self.submit({
            "label": "dup",
            "ops": [
                {"type": "insert", "sale_id": "SO-1001", "store_id": "S-101", "amount": "1.00"},
                {"type": "insert", "sale_id": "SO-9001", "store_id": "S-201", "amount": "2.00"},
                {"type": "insert", "sale_id": "SO-9001", "store_id": "S-202", "amount": "3.00"},
                {"type": "insert", "sale_id": "SO-9002", "store_id": "S-301", "amount": None},
            ],
        })
        self.assertEqual(result["status"], "partial")
        codes = sorted(item["code"] for item in result["rejected"])
        self.assertEqual(codes, ["DUPLICATE_KEY", "DUPLICATE_KEY"])
        east = self.zone(result["snapshot"]["summary"], "Z-EAST")
        self.assertEqual(east["order_count"], 4)  # SO-9001 landed once
        south = self.zone(result["snapshot"]["summary"], "Z-SOUTH")
        self.assertEqual(south["order_count"], 3)
        self.assertTrue(result["all_match"])

    def test_strict_mode_rejects_whole_batch(self):
        result = self.submit({
            "label": "strict dup",
            "strict": True,
            "ops": [
                {"type": "insert", "sale_id": "SO-9001", "store_id": "S-201", "amount": "2.00"},
                {"type": "insert", "sale_id": "SO-9001", "store_id": "S-202", "amount": "3.00"},
            ],
        })
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.get("/api/state")["snapshot_version"], 1)

    # -- idempotency ------------------------------------------------
    def test_same_payload_twice_does_not_double_count(self):
        payload = {
            "label": "idem",
            "ops": [{"type": "insert", "sale_id": "SO-9401", "store_id": "S-101", "amount": "5.00"}],
        }
        first = self.submit(payload)
        self.assertEqual(first["status"], "committed")
        second = self.submit(payload)
        self.assertEqual(second["status"], "replayed")
        state = self.get("/api/state")
        north = self.zone(state["summary"], "Z-NORTH")
        self.assertEqual(north["order_count"], 5)  # 4 + 1, not 4 + 1 + 1
        self.assertEqual(north["total_cents"], 19750)

    def test_labels_do_not_change_identity(self):
        ops = [{"type": "insert", "sale_id": "SO-9402", "store_id": "S-101", "amount": "5.00"}]
        self.submit({"label": "a", "ops": ops})
        again = self.submit({"label": "b", "ops": ops})
        self.assertEqual(again["status"], "replayed")

    # -- null / zero / missing amounts ------------------------------
    def test_null_zero_and_positive_are_distinct(self):
        result = self.submit({
            "label": "amounts",
            "ops": [
                {"type": "insert", "sale_id": "SO-9501", "store_id": "S-301", "amount": None},
                {"type": "insert", "sale_id": "SO-9502", "store_id": "S-301", "amount": "0"},
                {"type": "insert", "sale_id": "SO-9503", "store_id": "S-301", "amount": "12.30"},
            ],
        })
        south = self.zone(result["snapshot"]["summary"], "Z-SOUTH")
        self.assertEqual(south["order_count"], 5)
        # zero is a present amount, so it counts as non-null; null does not.
        self.assertEqual(south["amount_count"], 4)
        self.assertEqual(south["null_count"], 1)
        self.assertEqual(south["zero_count"], 1)
        self.assertEqual(south["total_cents"], 17545 + 1230)

    def test_empty_string_amount_counts_as_null(self):
        result = self.submit({
            "label": "empty string",
            "ops": [{"type": "insert", "sale_id": "SO-9504", "store_id": "S-401", "amount": ""}],
        })
        west = self.zone(result["snapshot"]["summary"], "Z-WEST")
        self.assertEqual(west["null_count"], 1)
        self.assertEqual(west["amount_count"], 2)

    def test_invalid_amount_is_rejected(self):
        result = self.submit({
            "label": "bad amount",
            "ops": [{"type": "insert", "sale_id": "SO-9505", "store_id": "S-401", "amount": "abc"}],
        })
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["rejected"][0]["code"], "INVALID_AMOUNT")

    # -- updates retract then add -----------------------------------
    def test_update_retracts_old_before_adding_new(self):
        result = self.submit({
            "label": "update",
            "ops": [{"type": "update", "sale_id": "SO-1005", "amount": "10.00"}],
        })
        batch_id = result["batch"]["batch_id"]
        impact = self.get(f"/api/batches/{batch_id}/ops/0/impact")
        phases = [entry["phase"] for entry in impact["entries"]]
        self.assertEqual(phases, ["retract", "add"])
        self.assertEqual(impact["entries"][0]["total_delta_cents"], -29990)
        self.assertEqual(impact["entries"][1]["total_delta_cents"], 1000)
        east = self.zone(result["snapshot"]["summary"], "Z-EAST")
        self.assertEqual(east["total_cents"], 42990 - 29990 + 1000)

    def test_update_moves_region_by_retract_then_add(self):
        result = self.submit({
            "label": "move sale",
            "ops": [{"type": "update", "sale_id": "SO-1002", "store_id": "S-201"}],
        })
        north = self.zone(result["snapshot"]["summary"], "Z-NORTH")
        east = self.zone(result["snapshot"]["summary"], "Z-EAST")
        self.assertEqual(north["order_count"], 3)
        self.assertEqual(north["total_cents"], 19250 - 6400)
        self.assertEqual(east["order_count"], 4)
        self.assertEqual(east["total_cents"], 42990 + 6400)

    def test_store_zone_change_moves_all_its_sales(self):
        result = self.submit({
            "label": "move store",
            "ops": [{"type": "move_store", "store_id": "S-301", "zone_id": "Z-WEST"}],
        })
        south = self.zone(result["snapshot"]["summary"], "Z-SOUTH")
        west = self.zone(result["snapshot"]["summary"], "Z-WEST")
        self.assertEqual(south["order_count"], 1)
        self.assertEqual(south["total_cents"], 2345)
        self.assertEqual(west["order_count"], 3)
        self.assertEqual(west["total_cents"], 38770 + 15200)
        batch_id = result["batch"]["batch_id"]
        impact = self.get(f"/api/batches/{batch_id}/ops/0/impact")
        zone_keys = {zone["zone_key"] for zone in impact["zones"]}
        self.assertEqual(zone_keys, {"Z-SOUTH", "Z-WEST"})

    def test_store_move_to_unassigned_goes_unmatched(self):
        result = self.submit({
            "label": "unassign store",
            "ops": [{"type": "move_store", "store_id": "S-102", "zone_id": None}],
        })
        north = self.zone(result["snapshot"]["summary"], "Z-NORTH")
        unmatched = self.zone(result["snapshot"]["summary"], "__unmatched__")
        self.assertEqual(north["order_count"], 2)
        self.assertEqual(unmatched["order_count"], 4)

    # -- no group vs zero sum ---------------------------------------
    def test_region_deleted_to_nothing_disappears(self):
        result = self.submit({
            "label": "empty south",
            "ops": [
                {"type": "delete", "sale_id": "SO-1008"},
                {"type": "delete", "sale_id": "SO-1009"},
            ],
        })
        self.assertIsNone(self.zone(result["snapshot"]["summary"], "Z-SOUTH"))
        keys = {row["zone_key"] for row in result["snapshot"]["summary"]}
        self.assertNotIn("Z-SOUTH", keys)

    def test_region_with_only_null_and_zero_keeps_zero_sum_group(self):
        result = self.submit({
            "label": "zero sum north",
            "ops": [
                {"type": "delete", "sale_id": "SO-1001"},
                {"type": "delete", "sale_id": "SO-1002"},
            ],
        })
        north = self.zone(result["snapshot"]["summary"], "Z-NORTH")
        self.assertIsNotNone(north)
        self.assertEqual(north["order_count"], 2)
        # the zero-amount sale is a present amount, so it stays in amount_count
        self.assertEqual(north["amount_count"], 1)
        self.assertEqual(north["null_count"], 1)
        self.assertEqual(north["zero_count"], 1)
        self.assertEqual(north["total_cents"], 0)
        self.assertEqual(north["unit_price_cents"], 0)
        self.assertTrue(all(item["is_match"] for item in result["snapshot"]["reconciliation"]))

    def test_region_with_only_null_sales_has_undefined_unit_price(self):
        result = self.submit({
            "label": "only null north",
            "ops": [
                {"type": "delete", "sale_id": "SO-1001"},
                {"type": "delete", "sale_id": "SO-1002"},
                {"type": "delete", "sale_id": "SO-1004"},
            ],
        })
        north = self.zone(result["snapshot"]["summary"], "Z-NORTH")
        self.assertIsNotNone(north)
        self.assertEqual(north["order_count"], 1)
        self.assertEqual(north["amount_count"], 0)
        self.assertEqual(north["total_cents"], 0)
        # undefined is not zero: no non-null amount to divide by
        self.assertIsNone(north["unit_price_cents"])

    def test_all_sales_removed_leaves_zero_groups_not_a_fake_row(self):
        sales = self.get("/api/state")["sales"]
        active = [s["sale_id"] for s in sales if s["status"] == "active"]
        result = self.submit({
            "label": "delete everything",
            "ops": [{"type": "delete", "sale_id": sid} for sid in active],
        })
        self.assertEqual(result["snapshot"]["summary"], [])
        self.assertEqual(result["snapshot"]["full"], [])
        self.assertEqual(result["snapshot"]["reconciliation"], [])
        self.assertTrue(result["all_match"])

    # -- crash / recovery -------------------------------------------
    def test_mid_batch_failure_rolls_back_everything(self):
        before = self.get("/api/state")
        payload = {
            "label": "crash",
            "fail_after": 2,
            "ops": [
                {"type": "insert", "sale_id": "SO-9601", "store_id": "S-101", "amount": "11.00"},
                {"type": "insert", "sale_id": "SO-9602", "store_id": "S-102", "amount": None},
                {"type": "insert", "sale_id": "SO-9603", "store_id": "S-401", "amount": "0"},
            ],
        }
        result = self.submit(payload)
        self.assertEqual(result["status"], "rolled_back")
        after = self.get("/api/state")
        self.assertEqual(after["snapshot_version"], before["snapshot_version"])
        self.assertEqual(len(after["summary"]), len(before["summary"]))
        self.assertNotIn("SO-9601", {s["sale_id"] for s in after["sales"]})
        self.assertEqual(after["batches"], before["batches"])
        attempts = after["attempts"]
        self.assertEqual(attempts[0]["status"], "rolled_back")
        self.assertEqual(attempts[0]["applied_ops"], 2)

        retry = dict(payload)
        retry.pop("fail_after")
        retry["label"] = "crash retry"
        recovered = self.submit(retry)
        self.assertEqual(recovered["status"], "committed")
        state = self.get("/api/state")
        north = self.zone(state["summary"], "Z-NORTH")
        self.assertEqual(north["order_count"], 6)
        self.assertEqual(north["null_count"], 2)

    def test_fail_after_zero_rolls_back_before_any_write(self):
        before = self.get("/api/state")
        result = self.submit({
            "label": "fail immediately",
            "fail_after": 0,
            "ops": [{"type": "insert", "sale_id": "SO-9604", "store_id": "S-101", "amount": "1.00"}],
        })
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["detail"]["applied_ops_before_failure"], 0)
        self.assertEqual(self.get("/api/state")["snapshot_version"], before["snapshot_version"])

    # -- reconciliation / recompute -----------------------------------
    def test_recompute_from_same_snapshot_matches(self):
        self.submit({
            "label": "mixed",
            "ops": [
                {"type": "insert", "sale_id": "SO-9701", "store_id": "S-999", "amount": "8.00"},
                {"type": "move_store", "store_id": "S-301", "zone_id": "Z-WEST"},
                {"type": "update", "sale_id": "SO-1013", "store_id": "S-501", "amount": None},
                {"type": "delete", "sale_id": "SO-1007"},
            ],
        })
        latest = self.get("/api/state")["snapshot_version"]
        audit = self.get(f"/api/snapshots/{latest}/recompute")
        self.assertGreater(len(audit["comparison"]), 0)
        self.assertTrue(audit["all_match"])
        self.assertTrue(all(item["is_match"] for item in audit["stored_full_matches"]))

    def test_every_committed_snapshot_recomputes_identically(self):
        self.submit({
            "label": "step 1",
            "ops": [{"type": "update", "sale_id": "SO-1005", "store_id": "S-102"}],
        })
        self.submit({
            "label": "step 2",
            "ops": [{"type": "move_store", "store_id": "S-202", "zone_id": "Z-SOUTH"}],
        })
        self.submit({
            "label": "step 3",
            "ops": [{"type": "delete", "sale_id": "SO-1003"},
                    {"type": "insert", "sale_id": "SO-9801", "store_id": "S-501", "amount": "7.00"}],
        })
        state = self.get("/api/state")
        versions = sorted({batch["snapshot_version"] for batch in state["batches"]})
        for version in versions:
            audit = self.get(f"/api/snapshots/{version}/recompute")
            self.assertTrue(audit["all_match"], f"snapshot v{version} mismatch")

    # -- validation edge cases --------------------------------------
    def test_missing_store_and_unknown_store_are_unmatched_not_rejected(self):
        result = self.submit({
            "label": "unmatched",
            "ops": [
                {"type": "insert", "sale_id": "SO-9901", "store_id": "S-999", "amount": "4.00"},
                {"type": "insert", "sale_id": "SO-9902", "store_id": None, "amount": None},
            ],
        })
        unmatched = self.zone(result["snapshot"]["summary"], "__unmatched__")
        self.assertEqual(unmatched["order_count"], 4)
        self.assertEqual(unmatched["null_count"], 1)
        self.assertEqual(len(result["warnings"]), 2)

    def test_delete_twice_is_reported(self):
        self.submit({"label": "delete once", "ops": [{"type": "delete", "sale_id": "SO-1004"}]})
        # A differing payload so the idempotency guard does not mask the check.
        second = self.submit({
            "label": "delete twice",
            "ops": [
                {"type": "delete", "sale_id": "SO-1004"},
                {"type": "insert", "sale_id": "SO-9008", "store_id": "S-201", "amount": "1.00"},
            ],
        })
        self.assertEqual(second["status"], "partial")
        self.assertEqual(second["rejected"][0]["code"], "ALREADY_DELETED")

    def test_update_deleted_sale_is_rejected(self):
        self.submit({"label": "delete", "ops": [{"type": "delete", "sale_id": "SO-1004"}]})
        result = self.submit({
            "label": "update deleted",
            "ops": [{"type": "update", "sale_id": "SO-1004", "amount": "9.00"}],
        })
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["rejected"][0]["code"], "NOT_FOUND")

    def test_move_to_same_zone_is_no_change(self):
        result = self.submit({
            "label": "no change",
            "ops": [{"type": "move_store", "store_id": "S-101", "zone_id": "Z-NORTH"}],
        })
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["rejected"][0]["code"], "NO_CHANGE")

    def test_bad_payloads(self):
        status, body = self.post("/api/batches", {"ops": []})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_PAYLOAD")
        status, body = self.post("/api/batches", {"ops": [{"type": "nope", "sale_id": "X"}]})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(body["rejected"][0]["code"], "INVALID_OP")
        status, body = self.post("/api/batches", {"ops": "notalist"})
        self.assertEqual(status, 400)

    # -- persistence -------------------------------------------------
    def test_state_survives_reopening_the_database(self):
        self.submit({
            "label": "persist",
            "ops": [{"type": "insert", "sale_id": "SO-9950", "store_id": "S-201", "amount": "6.00"}],
        })
        version = self.get("/api/state")["snapshot_version"]
        self.server.shutdown()
        self.server.server_close()
        from core.service import Service

        reopened = Service(self.db_path)
        state = reopened.state()
        self.assertEqual(state["snapshot_version"], version)
        east = self.zone(state["summary"], "Z-EAST")
        self.assertEqual(east["total_cents"], 42990 + 600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
