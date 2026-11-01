import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gachaops.planner import ConfigError, command, load, plan
from gachaops.supervisor import simulate
from gachaops.windows_process import CleanupError

ROOT = Path(__file__).resolve().parents[1]


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.config = load(ROOT / "config/simulation.json")
        self.config["tasks"] = self.config["tasks"][:2]

    def test_inventory_is_honest_about_missing_installations(self):
        result = plan(load(ROOT / "config/inventory.example.json"))
        self.assertEqual(len(result["tasks"]), 6)
        self.assertTrue(all(t["status"] == "blocked" for t in result["tasks"]))

    def test_routes_only_supported_healthy_capability(self):
        task = self.config["tasks"][0]
        primary = self.config["runners"][task["runners"][0]]
        fallback = copy.deepcopy(primary)
        fallback.update(readiness="validated", exe="mock.exe", cwd="mock", runner_version="1",
                        resource_version="1", completion_contract="mock-event", health={"daily": "healthy"})
        self.config["runners"]["fallback"] = fallback
        task["runners"].append("fallback")
        self.assertEqual(plan(self.config)["tasks"][0]["selected_runner"], "fallback")
        fallback["capabilities"] = ["navigation"]
        self.assertIsNone(plan(self.config)["tasks"][0]["selected_runner"])

    def test_cross_game_runner_is_rejected(self):
        self.config["tasks"][0]["runners"] = ["ok-ww"]
        self.assertIn("capability_not_supported", plan(self.config)["tasks"][0]["candidates"][0]["blockers"])

    def test_unknown_runner_rejected(self):
        self.config["tasks"][0]["runners"] = ["typo"]
        with self.assertRaises(ConfigError):
            plan(self.config)

    def test_duplicate_task_rejected(self):
        self.config["tasks"].append(self.config["tasks"][0])
        with self.assertRaises(ConfigError):
            plan(self.config)

    def test_invalid_timeouts_rejected(self):
        for value in [0, -1, float("nan"), float("inf"), True, "30m"]:
            with self.subTest(value=value), self.assertRaises(ConfigError):
                self.config["tasks"][0]["timeout_seconds"] = value
                plan(self.config)

    def test_retry_requires_replay_safety(self):
        self.config["tasks"][0]["replay_safe"] = False
        with self.assertRaises(ConfigError):
            plan(self.config)

    def test_ok_argument_array_preserves_spaces(self):
        self.assertEqual(command({"adapter": "ok", "exe": "C:/Game Tools/ok.exe", "task_index": 1}),
                         ["C:/Game Tools/ok.exe", "-t", "1", "-e"])

    def test_unverified_adapter_contract_cannot_be_generated(self):
        with self.assertRaises(ConfigError):
            command({"adapter": "maa"})

    def test_success_and_atomic_report(self):
        with tempfile.TemporaryDirectory() as out:
            report, directory = simulate(self.config, out)
            self.assertEqual(report["status"], "success")
            self.assertEqual(json.loads((directory / "summary.json").read_text()), report)
            self.assertFalse((Path(out) / ".simulation.lock").exists())

    def test_retry_then_success(self):
        tid = self.config["tasks"][0]["id"]
        with tempfile.TemporaryDirectory() as out:
            report, _ = simulate(self.config, out, {tid: ["failure", "success"]})
            row = report["tasks"][0]
            self.assertEqual(row["status"], "success")
            self.assertEqual([a["exit_code"] for a in row["attempts"]], [7, 0])

    def test_timeout_continues_next_task(self):
        task = self.config["tasks"][0]
        task["timeout_seconds"] = 0.1
        with tempfile.TemporaryDirectory() as out:
            report, _ = simulate(self.config, out, {task["id"]: ["timeout"]})
            self.assertEqual(report["tasks"][0]["status"], "timeout")
            self.assertEqual(len(report["tasks"][0]["attempts"]), 2)
            self.assertEqual(report["tasks"][1]["status"], "success")

    def test_slow_fixture_startup_uses_configured_budget(self):
        # A cold interpreter can take over a second on a shared Windows runner.
        # Keep a real child process so the old one-second cap fails this check.
        self.config["tasks"] = self.config["tasks"][:1]
        self.config["tasks"][0].update(timeout_seconds=5, retries=0)
        with tempfile.TemporaryDirectory() as out:
            root = Path(out)
            (root / "mock_runner.py").write_text(
                "import runpy, time\ntime.sleep(1.1)\n"
                f"runpy.run_path({str(ROOT / 'gachaops/mock_runner.py')!r}, run_name='__main__')\n",
                encoding="utf-8")
            with patch("gachaops.supervisor.__file__", str(root / "supervisor.py")):
                report, _ = simulate(self.config, root / "reports")
            self.assertEqual(report["status"], "success")
            self.assertEqual(report["tasks"][0]["attempts"][0]["exit_code"], 0)

    def test_zero_exit_without_completion_is_not_success(self):
        tid = self.config["tasks"][0]["id"]
        with tempfile.TemporaryDirectory() as out:
            report, _ = simulate(self.config, out, {tid: ["ambiguous"]})
            self.assertEqual(report["tasks"][0]["status"], "completion_unconfirmed")
            self.assertEqual(report["status"], "partial_failure")

    def test_lock_prevents_duplicate_chain(self):
        with tempfile.TemporaryDirectory() as out:
            (Path(out) / ".simulation.lock").write_text("existing")
            with self.assertRaises(ConfigError):
                simulate(self.config, out)
            self.assertEqual((Path(out) / ".simulation.lock").read_text(), "existing")

    def test_unknown_scenario_is_rejected_before_process_launch(self):
        with tempfile.TemporaryDirectory() as out, self.assertRaises(ConfigError):
            simulate(self.config, out, {"typo": ["success"]})

    def test_cleanup_failure_stops_chain_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as out:
            with patch("gachaops.supervisor.attempt", side_effect=CleanupError("owned tree still active")) as call:
                report, directory = simulate(self.config, out)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(report["status"], "cleanup_failed")
            self.assertEqual(report["tasks"][1]["status"], "not_run")
            self.assertEqual(json.loads((directory / "summary.json").read_text()), report)
            self.assertFalse((Path(out) / ".simulation.lock").exists())


if __name__ == "__main__":
    unittest.main()
