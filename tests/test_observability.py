"""Offline tests for Langfuse integration helpers and evaluator payloads."""
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
os.environ["LANGFUSE_TRACING_ENABLED"] = "0"
os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
os.environ.pop("LANGFUSE_SECRET_KEY", None)

import src.observability as observability
from src.langfuse_eval import (
    CODE_EVALUATOR_SOURCE,
    TASK_COMPLETION_CODE_EVALUATOR,
    TASK_COMPLETION_EVALUATOR,
    TASK_COMPLETION_RULE,
    build_task_completion_code_evaluator,
    build_task_completion_evaluator,
    build_task_completion_rule,
)


class ObservabilityDisabledTests(unittest.TestCase):
    def test_no_credentials_is_a_safe_noop(self):
        with patch.dict(
            os.environ,
            {"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""},
        ):
            self.assertFalse(observability.enabled())
            marker = object()

            def call():
                return marker

            self.assertIs(observability.traced_llm_call("x", call), marker)
            trace = observability.start_agent_run("do work")
            trace.finish({}, [])
            trace.close()
            self.assertFalse(observability.record_user_feedback(True, "ok"))
            self.assertIsNone(observability.last_trace_url())

    def test_with_system_prepends_role_labeled_message(self):
        messages = observability.with_system("system", [{"role": "user", "content": "hi"}])
        self.assertEqual(messages[0], {"role": "system", "content": "system"})
        self.assertEqual(messages[1]["content"], "hi")


class RunSummaryTests(unittest.TestCase):
    def _session(self, status="achieved", failed=None, events=None):
        started = (datetime.now() - timedelta(seconds=1)).isoformat(timespec="seconds")
        return SimpleNamespace(
            runtime=SimpleNamespace(db=SimpleNamespace(get=lambda table, key: {"status": status})),
            run_id="run_test",
            session_id="session_test",
            plan=SimpleNamespace(mode="direct", goal="fix the bug"),
            goal=SimpleNamespace(
                events=events or [
                    {"type": "goal_status", "active": True, "met": False},
                    {"type": "goal_status", "active": False, "met": True},
                ],
                last_status={"reason": "tests pass"},
            ),
            working={
                "failed_attempts": failed or [],
                "changed_files": ["src/app.py"],
            },
            tool_calls=4,
            tokens=120,
            started_at=started,
        )

    def test_summary_builds_deterministic_scores(self):
        session = self._session(
            failed=[
                {"result": "Permission denied: command is blocked"},
                {"result": "Error: file missing"},
            ]
        )
        summary = observability.build_run_summary(
            session,
            [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}],
        )
        self.assertEqual(summary["status"], "achieved")
        self.assertEqual(summary["output"], "done")
        self.assertEqual(summary["metadata"]["tool_error_rate"], 0.5)
        self.assertEqual(summary["metadata"]["permission_denials"], 1)
        self.assertEqual(summary["metadata"]["goal_blocks"], 1)
        names = {name: value for name, value, *_ in summary["scores"]}
        self.assertEqual(names["run_status"], "achieved")
        self.assertEqual(names["tool_error_rate"], 0.5)
        self.assertIn("run_duration_ms", names)

    def test_failed_run_is_error_level_input(self):
        summary = observability.build_run_summary(
            self._session(status="error"),
            [{"role": "assistant", "content": "boom"}],
        )
        self.assertEqual(summary["status"], "error")
        self.assertEqual(summary["output"], "boom")


class EvaluatorPayloadTests(unittest.TestCase):
    def test_task_completion_evaluator_targets_root_observation(self):
        payload = build_task_completion_evaluator()
        self.assertEqual(payload["name"], TASK_COMPLETION_EVALUATOR)
        self.assertEqual(payload["type"], "llm_as_judge")
        self.assertEqual(payload["outputDefinition"]["dataType"], "NUMERIC")
        self.assertEqual(payload["outputDefinition"]["maxValue"], 1)
        variables = {item["variable"] for item in payload["variableMapping"]}
        self.assertEqual(variables, {"input", "output", "metadata"})

    def test_code_evaluator_payload_and_logic(self):
        payload = build_task_completion_code_evaluator()
        self.assertEqual(payload["name"], TASK_COMPLETION_CODE_EVALUATOR)
        self.assertEqual(payload["type"], "code")
        self.assertEqual(payload["sourceCodeLanguage"], "PYTHON")
        compile(payload["sourceCode"], "<langfuse-code-evaluator>", "exec")

        class Score:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class EvaluationResult:
            def __init__(self, scores):
                self.scores = scores

        namespace = {}
        exec(
            CODE_EVALUATOR_SOURCE,
            {"Score": Score, "EvaluationResult": EvaluationResult},
            namespace,
        )
        ctx = SimpleNamespace(
            observation=SimpleNamespace(
                output="done",
                metadata={"run_status": "achieved", "tool_error_rate": 0.25},
            )
        )
        scores = {score.name: score.value for score in namespace["evaluate"](ctx).scores}
        self.assertEqual(scores["task_completion"], 1.0)
        self.assertTrue(scores["answer_present"])
        self.assertEqual(scores["tool_health"], 0.75)

    def test_rule_matches_only_agent_run_root(self):
        payload = build_task_completion_rule("eval_123")
        self.assertEqual(payload["name"], TASK_COMPLETION_RULE)
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["evaluatorAssignments"], [{"evaluatorId": "eval_123"}])
        self.assertIn(
            {
                "type": "stringObject",
                "column": "metadata",
                "key": "feature",
                "operator": "=",
                "value": "coding-agent",
            },
            payload["filter"],
        )


if __name__ == "__main__":
    unittest.main()
