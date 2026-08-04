import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "market-events.yml"


class WorkflowContractTests(unittest.TestCase):
    def test_offline_gates_precede_network_collection(self):
        text = WORKFLOW.read_text()
        tests = text.index("Run offline market-event tests")
        registry = text.index("Validate official-public source registry")
        secret = text.index("Require Tavily secret")
        collect = text.index("Collect registered public sources")
        self.assertLess(tests, registry)
        self.assertLess(registry, secret)
        self.assertLess(secret, collect)
        self.assertIn("secrets.TAVILY", text)

    def test_raw_bodies_are_artifacts_and_not_in_commit_allowlist(self):
        text = WORKFLOW.read_text()
        commit = text[text.index("Commit durable market-event state only") :]
        self.assertIn("runner.temp }}/market-events-raw", text)
        self.assertIn("retention-days: 3", text)
        self.assertNotIn("raw_documents.jsonl", commit)
        for state_file in (
            "events.jsonl",
            "changes.jsonl",
            "coverage_history.jsonl",
            "reviews.jsonl",
            "review_queue.jsonl",
        ):
            self.assertIn(state_file, commit)

    def test_schedule_runs_daily_and_enables_weekly_discovery_on_sunday(self):
        text = WORKFLOW.read_text()
        self.assertIn('cron: "47 2 * * *"', text)
        self.assertIn('date -u +%u', text)
        self.assertIn('--mode daily', text)
        self.assertIn('--mode weekly', text)


if __name__ == "__main__":
    unittest.main()
