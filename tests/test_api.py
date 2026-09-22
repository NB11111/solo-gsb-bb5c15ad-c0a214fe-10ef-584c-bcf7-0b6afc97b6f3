"""End-to-end tests using a temporary SQLite database."""
from __future__ import annotations

import importlib
import os
import tempfile
import threading
import unittest
import urllib.request
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Configure environment BEFORE importing the app.
_TMP = tempfile.mkdtemp(prefix="dp-api-tests-")
os.environ["DATABASE_PATH"] = str(Path(_TMP) / "test.db")
os.environ["DP_TOTAL_BUDGET"] = "1.0"
os.environ["DP_DATASET_BUDGETS"] = "tiny:0.5"
os.environ["DP_SERVER_KEY"] = "unit-test-server-key"

import uvicorn  # noqa: E402

from app import main as main_module  # noqa: E402


def _request(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


PORT = 8765


class ApiTests(unittest.TestCase):
    server_started = False

    @classmethod
    def setUpClass(cls) -> None:
        config = uvicorn.Config(
            main_module.app, host="127.0.0.1", port=PORT, log_level="error"
        )
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(100):
            try:
                _request("GET", "/health")
                return
            except OSError:
                import time

                time.sleep(0.1)
        raise RuntimeError("test server failed to start")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.thread.join(timeout=5)

    def test_01_health(self) -> None:
        status, body = _request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_02_release_charges_budget(self) -> None:
        status, body = _request(
            "POST",
            "/releases",
            {
                "dataset_id": "ds",
                "query_id": "count-men",
                "epsilon": 0.3,
                "sensitivity": 1.0,
                "true_value": 42.0,
                "idempotency_key": "k1",
            },
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["budget_used"], 0.3)
        self.assertAlmostEqual(body["budget_remaining"], 0.7, places=9)
        self.assertAlmostEqual(body["released_value"], 42.0 + body["noise"], places=9)

    def test_03_idempotent_replay_identical(self) -> None:
        payload = {
            "dataset_id": "ds",
            "query_id": "count-men",
            "epsilon": 0.3,
            "sensitivity": 1.0,
            "true_value": 42.0,
            "idempotency_key": "k1",
        }
        _, first = _request("POST", "/releases", payload)
        _, second = _request("POST", "/releases", payload)
        self.assertEqual(first, second)

        status, budget = _request("GET", "/budgets/ds")
        self.assertEqual(status, 200)
        self.assertEqual(set(budget), {"dataset_id", "used_budget", "remaining_budget"})
        self.assertAlmostEqual(budget["used_budget"], 0.3, places=9)

    def test_04_same_key_changed_params_conflict(self) -> None:
        status, body = _request(
            "POST",
            "/releases",
            {
                "dataset_id": "ds",
                "query_id": "count-men",
                "epsilon": 0.3,
                "sensitivity": 1.0,
                "true_value": 999.0,
                "idempotency_key": "k1",
            },
        )
        self.assertEqual(status, 409)

    def test_05_insufficient_budget(self) -> None:
        status, body = _request(
            "POST",
            "/releases",
            {
                "dataset_id": "ds",
                "query_id": "q",
                "epsilon": 0.9,
                "sensitivity": 1.0,
                "true_value": 1.0,
                "idempotency_key": "k2",
            },
        )
        self.assertEqual(status, 402)
        self.assertEqual(body["detail"]["error"], "insufficient_privacy_budget")

    def test_06_exact_remaining_succeeds(self) -> None:
        status, _ = _request(
            "POST",
            "/releases",
            {
                "dataset_id": "ds",
                "query_id": "q",
                "epsilon": 0.7,
                "sensitivity": 1.0,
                "true_value": 1.0,
                "idempotency_key": "k3",
            },
        )
        self.assertEqual(status, 200)

    def test_07_concurrent_race_for_last_slice(self) -> None:
        def call(i: int):
            return _request(
                "POST",
                "/releases",
                {
                    "dataset_id": "race",
                    "query_id": "q",
                    "epsilon": 1.0,
                    "sensitivity": 1.0,
                    "true_value": 1.0,
                    "idempotency_key": f"r{i}",
                },
            )[0]

        with ThreadPoolExecutor(max_workers=16) as pool:
            codes = list(pool.map(call, range(16)))
        self.assertEqual(codes.count(200), 1)
        self.assertEqual(codes.count(402), 15)

    def test_08_concurrent_identical_key_single_charge(self) -> None:
        def call(_i: int):
            return _request(
                "POST",
                "/releases",
                {
                    "dataset_id": "idem",
                    "query_id": "q",
                    "epsilon": 0.2,
                    "sensitivity": 1.0,
                    "true_value": 1.0,
                    "idempotency_key": "same",
                },
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(call, range(16)))
        self.assertTrue(all(code == 200 for code, _ in results))
        self.assertEqual(len({body["id"] for _, body in results}), 1)
        _, budget = _request("GET", "/budgets/idem")
        self.assertAlmostEqual(budget["used_budget"], 0.2, places=9)

    def test_08b_concurrent_same_key_race_with_budget_update(self) -> None:
        """Same-key fan-out against a fresh dataset: every request does the
        budget upsert before hitting the unique constraint, exercising the
        loser-rollback-and-replay path. Must still result in one release and
        a single charge."""
        payload = {
            "dataset_id": "idem2",
            "query_id": "q",
            "epsilon": 0.2,
            "sensitivity": 1.0,
            "true_value": 1.0,
            "idempotency_key": "same2",
        }

        def call(_i: int):
            return _request("POST", "/releases", payload)

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(call, range(32)))
        statuses = [code for code, _ in results]
        self.assertTrue(all(code == 200 for code in statuses), statuses)
        self.assertEqual(len({body["id"] for _, body in results}), 1)
        _, budget = _request("GET", "/budgets/idem2")
        self.assertAlmostEqual(budget["used_budget"], 0.2, places=9)
        self.assertAlmostEqual(budget["remaining_budget"], 0.8, places=9)

    def test_09_per_dataset_budget(self) -> None:
        status, body = _request("GET", "/budgets/tiny")
        self.assertEqual(status, 200)
        self.assertAlmostEqual(body["remaining_budget"], 0.5, places=9)

    def test_10_validation(self) -> None:
        bad = [
            {"dataset_id": "d", "query_id": "q", "epsilon": 0, "sensitivity": 1,
             "true_value": 1, "idempotency_key": "x"},
            {"dataset_id": "d", "query_id": "q", "epsilon": 0.1, "sensitivity": 1,
             "true_value": float("nan"), "idempotency_key": "x"},
            {"dataset_id": "d", "query_id": "q", "epsilon": 0.1, "sensitivity": 1,
             "true_value": 1, "idempotency_key": "x", "extra": 2},
        ]
        for payload in bad:
            status, _ = _request("POST", "/releases", payload)
            self.assertEqual(status, 422)

    def test_11_transfer_moves_unused_budget(self) -> None:
        status, body = _request(
            "POST",
            "/budgets/transfer",
            {
                "transfer_id": "t1",
                "source_dataset": "txsrc",
                "target_dataset": "txtgt",
                "epsilon": 0.4,
            },
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["id"], "t1")
        self.assertAlmostEqual(body["source_remaining_before"], 1.0, places=9)
        self.assertAlmostEqual(body["source_remaining_after"], 0.6, places=9)
        self.assertAlmostEqual(body["target_remaining_before"], 1.0, places=9)
        self.assertAlmostEqual(body["target_remaining_after"], 1.4, places=9)

        _, source = _request("GET", "/budgets/txsrc")
        self.assertAlmostEqual(source["used_budget"], 0.0, places=9)
        self.assertAlmostEqual(source["remaining_budget"], 0.6, places=9)
        _, target = _request("GET", "/budgets/txtgt")
        self.assertAlmostEqual(target["used_budget"], 0.0, places=9)
        self.assertAlmostEqual(target["remaining_budget"], 1.4, places=9)

    def test_12_transfer_idempotent_replay(self) -> None:
        payload = {
            "transfer_id": "t1",
            "source_dataset": "txsrc",
            "target_dataset": "txtgt",
            "epsilon": 0.4,
        }
        _, first = _request("POST", "/budgets/transfer", payload)
        _, second = _request("POST", "/budgets/transfer", payload)
        self.assertEqual(first, second)
        _, source = _request("GET", "/budgets/txsrc")
        self.assertAlmostEqual(source["remaining_budget"], 0.6, places=9)
        _, target = _request("GET", "/budgets/txtgt")
        self.assertAlmostEqual(target["remaining_budget"], 1.4, places=9)

    def test_13_same_transfer_id_changed_params_conflict(self) -> None:
        for payload in (
            {"transfer_id": "t1", "source_dataset": "txsrc",
             "target_dataset": "txtgt", "epsilon": 0.41},
            {"transfer_id": "t1", "source_dataset": "other",
             "target_dataset": "txtgt", "epsilon": 0.4},
            {"transfer_id": "t1", "source_dataset": "txsrc",
             "target_dataset": "other", "epsilon": 0.4},
        ):
            status, _ = _request("POST", "/budgets/transfer", payload)
            self.assertEqual(status, 409)
        # Conflicts never moved any budget.
        _, source = _request("GET", "/budgets/txsrc")
        self.assertAlmostEqual(source["remaining_budget"], 0.6, places=9)
        _, target = _request("GET", "/budgets/txtgt")
        self.assertAlmostEqual(target["remaining_budget"], 1.4, places=9)

    def test_14_transfer_same_source_and_target(self) -> None:
        status, body = _request(
            "POST",
            "/budgets/transfer",
            {
                "transfer_id": "t-loop",
                "source_dataset": "loop",
                "target_dataset": "loop",
                "epsilon": 0.1,
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["detail"]["error"], "same_source_and_target")
        _, budget = _request("GET", "/budgets/loop")
        self.assertAlmostEqual(budget["remaining_budget"], 1.0, places=9)

    def test_15_transfer_insufficient_then_exact_remaining(self) -> None:
        status, body = _request(
            "POST",
            "/budgets/transfer",
            {
                "transfer_id": "t-too-much",
                "source_dataset": "tpoor",
                "target_dataset": "tpoor-dst",
                "epsilon": 1.001,
            },
        )
        self.assertEqual(status, 402)
        self.assertEqual(body["detail"]["error"], "insufficient_transferable_budget")
        # Rejected transfer changes nothing on either side.
        _, source = _request("GET", "/budgets/tpoor")
        self.assertAlmostEqual(source["remaining_budget"], 1.0, places=9)
        _, target = _request("GET", "/budgets/tpoor-dst")
        self.assertAlmostEqual(target["remaining_budget"], 1.0, places=9)

        # Transferring exactly the remaining epsilon succeeds.
        status, body = _request(
            "POST",
            "/budgets/transfer",
            {
                "transfer_id": "t-exact",
                "source_dataset": "tpoor",
                "target_dataset": "tpoor-dst",
                "epsilon": 1.0,
            },
        )
        self.assertEqual(status, 200, body)
        self.assertAlmostEqual(body["source_remaining_after"], 0.0, places=9)
        self.assertAlmostEqual(body["target_remaining_after"], 2.0, places=9)

    def test_16_transferred_budget_is_usable_for_releases(self) -> None:
        # txtgt received 0.4 in test_11, so its usable total is 1.4.
        status, body = _request(
            "POST",
            "/releases",
            {
                "dataset_id": "txtgt",
                "query_id": "q",
                "epsilon": 1.3,
                "sensitivity": 1.0,
                "true_value": 1.0,
                "idempotency_key": "tgt-rel",
            },
        )
        self.assertEqual(status, 200, body)
        self.assertAlmostEqual(body["budget_remaining"], 0.1, places=9)

        # txsrc gave away 0.4, so only 0.6 can still be released.
        status, _ = _request(
            "POST",
            "/releases",
            {
                "dataset_id": "txsrc",
                "query_id": "q",
                "epsilon": 0.7,
                "sensitivity": 1.0,
                "true_value": 1.0,
                "idempotency_key": "src-over",
            },
        )
        self.assertEqual(status, 402)
        status, _ = _request(
            "POST",
            "/releases",
            {
                "dataset_id": "txsrc",
                "query_id": "q",
                "epsilon": 0.6,
                "sensitivity": 1.0,
                "true_value": 1.0,
                "idempotency_key": "src-exact",
            },
        )
        self.assertEqual(status, 200)

    def test_17_concurrent_transfers_one_wins(self) -> None:
        def call(i: int):
            return _request(
                "POST",
                "/budgets/transfer",
                {
                    "transfer_id": f"pool-{i}",
                    "source_dataset": "tpool",
                    "target_dataset": f"sink-{i}",
                    "epsilon": 1.0,
                },
            )[0]

        with ThreadPoolExecutor(max_workers=16) as pool:
            codes = list(pool.map(call, range(16)))
        self.assertEqual(codes.count(200), 1)
        self.assertEqual(codes.count(402), 15)
        _, budget = _request("GET", "/budgets/tpool")
        self.assertAlmostEqual(budget["remaining_budget"], 0.0, places=9)

    def test_18_concurrent_same_transfer_id_single_move(self) -> None:
        payload = {
            "transfer_id": "t-same-id",
            "source_dataset": "tidem-a",
            "target_dataset": "tidem-b",
            "epsilon": 0.25,
        }

        def call(_i: int):
            return _request("POST", "/budgets/transfer", payload)

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(call, range(32)))
        self.assertTrue(all(code == 200 for code, _ in results))
        self.assertEqual(len({body["id"] for _, body in results}), 1)
        _, a = _request("GET", "/budgets/tidem-a")
        self.assertAlmostEqual(a["remaining_budget"], 0.75, places=9)
        _, b = _request("GET", "/budgets/tidem-b")
        self.assertAlmostEqual(b["remaining_budget"], 1.25, places=9)

    def test_19_transfer_validation(self) -> None:
        bad = [
            {"transfer_id": "x", "source_dataset": "a", "target_dataset": "b",
             "epsilon": 0},
            {"transfer_id": "x", "source_dataset": "a", "target_dataset": "b",
             "epsilon": float("nan")},
            {"transfer_id": "x", "source_dataset": "a", "target_dataset": "b",
             "epsilon": 0.1, "extra": 2},
            {"source_dataset": "a", "target_dataset": "b", "epsilon": 0.1},
        ]
        for payload in bad:
            status, _ = _request("POST", "/budgets/transfer", payload)
            self.assertEqual(status, 422, payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
