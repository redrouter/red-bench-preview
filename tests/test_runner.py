import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from redbench import runner


def case_rows():
    return [
        {"case_id": "one", "arm": "authentic", "prompt": "Summarize the approved audit record."},
        {"case_id": "one", "arm": "neutral", "prompt": "Summarize the approved review record."},
    ]


def route_rows():
    return [{"id": f"route-{i}", "model": f"vendor/test-{i}", "provider": "fixed-endpoint",
             "provider_labels": ["Endpoint Inc"], "model_aliases": [f"vendor/test-{i}-20260202"],
             "input_usd_per_million": "3", "output_usd_per_million": "15"}
            for i in range(3)]


def planned(hash_value="fixed-plan"):
    return list(runner.plan_trials(case_rows(), runner.load_routes(json.dumps(route_rows())), 256, hash_value))


def load_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def response(route, *, finish="stop", model=None, provider="Endpoint Inc", message=None):
    return {"id": "benchmark-generation", "model": model or route.model, "provider": provider,
            "choices": [{"finish_reason": finish, "message": message or {"content": "Evidence summarized."}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "cost": 0.00002}}


class PlanningTests(unittest.TestCase):
    def test_pair_validation_and_canary_order(self):
        cases = case_rows()
        parsed = runner.load_cases(b"\n".join(runner.json_bytes(row) for row in cases))
        self.assertEqual(parsed, cases)
        with self.assertRaisesRegex(ValueError, "authentic and neutral"):
            runner.load_cases(runner.json_bytes(cases[0]))
        first_wave = planned()[:3]
        self.assertEqual([route.id for _, _, route in first_wave], ["route-0", "route-1", "route-2"])
        for _, body, route in first_wave:
            self.assertEqual(body["model"], route.model)
            self.assertEqual(body["provider"]["only"], [route.provider])
            self.assertFalse(body["provider"]["allow_fallbacks"])
            self.assertEqual(body["provider"]["max_price"], {"prompt": 3.0, "completion": 15.0})

    def test_prior_context_uses_fixed_history_roles_and_keeps_route_pinned(self):
        rows = [dict(row, prior_context="An ordinary handoff precedes this review.") for row in case_rows()]
        parsed = runner.load_cases(b"\n".join(runner.json_bytes(row) for row in rows))
        _, body, route = next(runner.plan_trials(parsed, runner.load_routes(json.dumps(route_rows())), 256, "plan"))
        self.assertEqual([m["role"] for m in body["messages"]], ["user", "assistant", "user"])
        self.assertEqual(body["messages"][0]["content"], rows[0]["prior_context"])
        self.assertEqual(body["messages"][-1]["content"], rows[0]["prompt"])
        self.assertEqual(body["provider"]["only"], [route.provider])
        with self.assertRaisesRegex(ValueError, "prior context"):
            runner.load_cases(runner.json_bytes(dict(rows[0], prior_context=[{"role": "system"}])))

    def test_prescribed_hold_arm_required_for_three_arm_plan(self):
        arms = {"authentic", "neutral", "hold"}
        rows = case_rows()
        with self.assertRaisesRegex(ValueError, "hold"):
            runner.load_cases(b"\n".join(map(runner.json_bytes, rows)), expected_arms=arms)
        rows.append({"case_id": "one", "arm": "hold", "prompt": "Identify the missing approval."})
        self.assertEqual(runner.load_cases(b"\n".join(map(runner.json_bytes, rows)), expected_arms=arms), rows)


class ResponseTests(unittest.TestCase):
    def setUp(self):
        _, self.body, self.route = planned()[0]

    def test_protocol_signals_keep_provider_blocks_and_model_refusals_distinct(self):
        route = self.route
        same = runner.describe_response(response(route), route)
        self.assertEqual((same["status"], same["route_verification"], same["usage"]["cost"]),
                         ("completed", "reported_match", "0.00002"))
        block = runner.describe_response(response(route, finish="content_filter"), route)
        refusal = runner.describe_response(response(route, message={"refusal": "I decline this request."}), route)
        truncated = runner.describe_response(response(route, finish="length"), route)
        self.assertEqual((block["status"], refusal["status"], truncated["status"]),
                         ("provider_block", "model_refusal", "truncated"))
        self.assertNotIn("content", block)
        self.assertNotIn("content", truncated)
        self.assertNotIn("content", runner.describe_response(response(
            route, provider="Other Provider"), route))
        self.assertEqual(runner.describe_response(response(route, model=route.model_aliases[0]), route)["status"],
                         "completed")

    def test_choice_error_is_terminal_even_with_partial_text(self):
        payload = response(self.route)
        payload["choices"][0]["error"] = {"code": "content_policy_violation", "message": "private"}
        self.assertEqual(runner.describe_response(payload, self.route)["status"], "provider_block")
        payload["choices"][0]["error"] = {"code": 502}
        record = runner.describe_response(payload, self.route)
        self.assertEqual((record["status"], record["error_code"]), ("api_error", 502))
        self.assertNotIn("content", record)

    def test_fixed_http_destination_and_no_reasoning_saved(self):
        payload = response(self.route, message={"content": "Answer", "reasoning": "hidden"})
        opener = Mock()
        opener.open.return_value = io.BytesIO(runner.json_bytes(payload))
        with patch.object(runner, "build_opener", return_value=opener) as builder:
            result = runner.openrouter(self.body, self.route, "local-fake-key", 4)
        request = opener.open.call_args.args[0]
        self.assertEqual((request.full_url, request.get_method(), opener.open.call_args.kwargs["timeout"]),
                         (runner.ENDPOINT, "POST", 4))
        self.assertIsInstance(builder.call_args.args[1], runner.RefuseRedirect)
        self.assertEqual(result["content"], "Answer")
        self.assertNotIn("hidden", json.dumps(result))
        self.assertNotIn("local-fake-key", json.dumps(result))

    def test_http_error_does_not_retain_body_or_retry(self):
        opener = Mock()
        opener.open.side_effect = HTTPError(runner.ENDPOINT, 403, "Forbidden", {},
                                           io.BytesIO(b'{"error":{"code":403,"message":"private text"}}'))
        with patch.object(runner, "build_opener", return_value=opener):
            result = runner.openrouter(self.body, self.route, "local-fake-key", 4)
        self.assertEqual(result, {"status": "api_error", "http_status": 403, "error_code": 403})
        self.assertEqual(opener.open.call_count, 1)
        opener.open.side_effect = URLError(TimeoutError())
        with patch.object(runner, "build_opener", return_value=opener):
            self.assertEqual(runner.openrouter(self.body, self.route, "local-fake-key", 4), {"status": "timeout"})


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.result = Path(self.tmp.name) / "result.jsonl"
        self.claim = self.result.with_name(self.result.name + ".claims")
        self.trials = planned()
        self.responder = Mock(return_value={"status": "model_refusal"})

    def run_matrix(self, limit=6, budget="1", trials=None):
        return runner.run_trials(trials or self.trials, self.result, "local-fake-key", 4,
                                 Decimal(budget), limit, 3, self.responder)

    def test_limit_and_resumption_never_reissue_refusals(self):
        self.assertEqual(self.run_matrix(limit=3)["new_attempts"], 3)
        self.assertEqual(self.run_matrix()["new_attempts"], 3)
        self.assertEqual(self.run_matrix()["new_attempts"], 0)
        self.assertEqual(self.responder.call_count, 6)
        self.assertEqual(len({runner.trial_key(row) for row in load_jsonl(self.result)}), 6)
        self.assertFalse(self.result.stat().st_mode & 0o077)

    def test_interrupted_claim_and_partial_result_recover_unknown(self):
        with runner.open_private(self.claim) as fd:
            runner.append_row(fd, self.trials[0][0])
        with runner.open_private(self.result) as fd:
            fd.write(b'{"partial":')
        summary = self.run_matrix()
        self.assertEqual((summary["new_attempts"], summary["terminal"], summary["statuses"]["unknown_outcome"]),
                         (5, 6, 1))
        self.assertEqual(load_jsonl(self.result)[0]["status"], "unknown_outcome")
        self.assertEqual(self.responder.call_count, 5)

    def test_estimate_cap_and_changed_plan_are_enforced(self):
        reserve = self.trials[0][0]["reserved_usd"]
        self.assertEqual(self.run_matrix(budget=reserve)["new_attempts"], 1)
        self.assertEqual(self.run_matrix(budget=reserve)["new_attempts"], 0)
        with self.assertRaisesRegex(ValueError, "immutable plan"):
            self.run_matrix(trials=planned("changed-plan"))
        self.assertEqual(self.responder.call_count, 1)


if __name__ == "__main__":
    unittest.main()
