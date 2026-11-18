"""Offline tests: no real model requests and no writes to the user workspace."""
import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TEST_ROOT = tempfile.TemporaryDirectory()
os.environ["WORKDIR"] = TEST_ROOT.name
os.environ["LLM_API_KEY"] = "offline-test-key"
os.environ["LLM_MODEL_ID"] = "offline-model"
os.environ["AGENT_PERMISSION_MODE"] = "unattended"
os.environ["QDRANT_URL"] = ""
os.environ["EMBEDDING_URL"] = ""
os.environ["LANGFUSE_TRACING_ENABLED"] = "0"
os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
os.environ.pop("LANGFUSE_SECRET_KEY", None)

from anthropic.types import TextBlock
from src.goal import GoalController, GoalEvaluation, CheckedGoalEvaluator
from src.permissions import PermissionPolicy
from src.storage.sqlite_store import SQLiteStore
from src.runtime import AgentRuntime
from src.workflow import run_workflow_async, WORKFLOWS, RunnerOutput, WorkflowInputError


class Evaluator:
    def __init__(self, *values):
        self.values = iter(values)
        self.calls = 0

    async def evaluate(self, *args):
        self.calls += 1
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


class GoalTests(unittest.IsolatedAsyncioTestCase):
    async def test_block_then_achieve(self):
        evaluator = Evaluator(GoalEvaluation(False, "tests missing"), GoalEvaluation(True, "passed"))
        goal = GoalController(evaluator)
        goal.set_goal("pass tests")
        self.assertEqual((await goal.evaluate_after_turn([])).action, "block")
        self.assertEqual((await goal.evaluate_after_turn([])).action, "achieved")
        self.assertIsNone(goal.active)

    async def test_error_and_limit_keep_goal(self):
        goal = GoalController(Evaluator(RuntimeError("offline"), GoalEvaluation(False, "missing"), GoalEvaluation(False, "missing")), block_cap=1)
        goal.set_goal("finish")
        self.assertEqual((await goal.evaluate_after_turn([])).action, "error")
        self.assertEqual((await goal.evaluate_after_turn([])).action, "block")
        self.assertEqual((await goal.evaluate_after_turn([])).action, "limit")
        self.assertIsNotNone(goal.active)

    async def test_background_does_not_evaluate(self):
        evaluator = Evaluator()
        goal = GoalController(evaluator)
        goal.set_goal("finish")
        self.assertEqual((await goal.evaluate_after_turn([], True)).action, "defer")
        self.assertEqual(evaluator.calls, 0)

    async def test_deterministic_check_skips_model_and_records_exit(self):
        evaluator = Evaluator()
        checked = CheckedGoalEvaluator(evaluator, [{"command": "pytest -q", "exit_code": 0}],
                                       lambda _: ("2 passed", 0), False)
        messages = []
        self.assertTrue((await checked.evaluate("pass", messages)).ok)
        self.assertIn("Exit code: 0", messages[0]["content"])
        self.assertEqual(evaluator.calls, 0)


class PermissionTests(unittest.TestCase):
    def test_safe_worker_commands(self):
        values = []
        worker = threading.Thread(target=lambda: values.append(PermissionPolicy("strict").check("git status")))
        worker.start(); worker.join()
        self.assertEqual(values, [None])

    def test_unattended_never_asks(self):
        policy = PermissionPolicy("unattended")
        for command in ["rm file", "git reset --hard", "python -c evil", "git status; rm file", "shutdown /s"]:
            self.assertIsNotNone(policy.check(command, lambda _: self.fail("must not ask")))


class ToolAuthorizationTests(unittest.TestCase):
    def test_interactive_approval_is_remembered_per_exact_key(self):
        from src import permissions
        permissions._approval_cache.clear()
        try:
            with patch.object(permissions, "_ask_user", return_value=True) as ask:
                self.assertTrue(permissions._approve_once("bash:test:echo hi", "allow?"))
                self.assertTrue(permissions._approve_once("bash:test:echo hi", "allow?"))
            ask.assert_called_once()
        finally:
            permissions._approval_cache.clear()

    def test_bash_authorization_reuses_permission_policy(self):
        from src.permissions import authorize_tool
        self.assertIsNone(authorize_tool(SimpleNamespace(
            name="bash", input={"command": "git status"})))
        self.assertIsNotNone(authorize_tool(SimpleNamespace(
            name="bash", input={"command": "rm -rf /"})))
        self.assertIsNotNone(authorize_tool(SimpleNamespace(
            name="bash", input={"command": 123})))

    def test_file_tools_reject_paths_outside_workspace(self):
        from src.config import WORKDIR
        from src.permissions import authorize_tool
        self.assertIsNone(authorize_tool(SimpleNamespace(
            name="read_file", input={"path": "inside.txt"})))
        self.assertIsNotNone(authorize_tool(SimpleNamespace(
            name="read_file", input={"path": str(WORKDIR.parent / "outside.txt")})))
        self.assertIsNotNone(authorize_tool(SimpleNamespace(
            name="write_file", input={"path": 123})))


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = SQLiteStore(Path(self.temp.name) / "agent.db")
        self.runtime = SimpleNamespace(db=self.db, session_id="test", client=None)
        self.calls = []
        self.runner = SimpleNamespace(run=lambda prompt, schema, label:
            (self.calls.append(label) or RunnerOutput({"ok": True}, 10)))

    def tearDown(self):
        WORKFLOWS.pop("test", None)
        self.temp.cleanup()

    async def test_resume_replays_only_completed_steps(self):
        failing = [True]
        async def flow(ctx, args):
            first = await ctx.agent("first", label="first", cacheable=True)
            if failing[0]:
                raise ValueError("interrupted")
            return [first, await ctx.agent("second", label="second", cacheable=True)]
        WORKFLOWS["test"] = ({}, flow)
        run = await run_workflow_async("test", runtime=self.runtime, runner=self.runner)
        self.assertEqual(run["status"], "failed")
        failing[0] = False
        self.runtime.db = SQLiteStore(self.db.path)  # fresh connection / restart
        resumed = await run_workflow_async("test", resume_from_run_id=run["id"], runtime=self.runtime, runner=self.runner)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(self.calls, ["first", "second"])
        self.assertEqual(resumed["tokens"], 20)
        with self.assertRaises(WorkflowInputError):
            await run_workflow_async("test", {"different": True}, run["id"], runtime=self.runtime)

    async def test_invalid_schema_retry_charged_under_limit(self):
        self.runner.run = lambda *args: RunnerOutput("wrong", 10)
        async def flow(ctx, args):
            return await ctx.agent("x", schema={"type": "object"})
        WORKFLOWS["test"] = ({}, flow)
        run = await run_workflow_async("test", {"budget": 15}, runtime=self.runtime, runner=self.runner)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["tokens"], 20)

    async def test_join_siblings_before_failure(self):
        completed = []
        async def flow(ctx, args):
            async def bad():
                raise ValueError("bad")
            async def slow():
                await asyncio.sleep(.02)
                completed.append(True)
            return await ctx.parallel([bad, slow])
        WORKFLOWS["test"] = ({}, flow)
        run = await run_workflow_async("test", runtime=self.runtime, runner=self.runner)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(completed, [True])


class PersistenceTests(unittest.TestCase):
    def test_resume_closes_unknown_tools_without_replaying(self):
        from src.runtime import get_runtime
        from src.cli import resume_run
        runtime = get_runtime()
        runtime.db.put("runs", "resume-test", {"id": "resume-test", "status": "interrupted", "request": "fix"})
        runtime.db.put("working_states", "resume-test", {"messages": [
            {"role": "user", "content": "fix"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "unknown-call", "name": "bash", "input": {"command": "echo done"}}]}]})
        history = []
        with patch("src.cli.agent_loop") as worker, patch("src.cli.print_turn_assistants"):
            resume_run("resume-test", history, {})
        result = history[-2]["content"][0]
        self.assertEqual(result["tool_use_id"], "unknown-call")
        self.assertTrue(result["is_error"])
        self.assertEqual(worker.call_count, 1)

    def test_task_and_dependency_survive_export_removal(self):
        from src.task import create_task, update_task, load_task, _task_path
        first, second = create_task("first"), create_task("second")
        update_task(second.id, [first.id])
        _task_path(second.id).unlink()
        self.assertEqual(load_task(second.id).blockedBy, [first.id])

    def test_memory_sqlite_authority(self):
        from src.memory import write_memory_file, read_memory_file
        path = write_memory_file("stable", "project", "stable description", "stable body")
        path.write_text("modified export", encoding="utf-8")
        self.assertIn("stable body", read_memory_file(path.name))

    def test_cron_roundtrip(self):
        from src import schedule
        schedule.scheduled_jobs.clear()
        job = schedule.schedule_job("0 9 * * *", "daily", True, True)
        job.pending_delivery = True
        schedule.save_durable_jobs()
        schedule.scheduled_jobs.clear(); schedule.cron_queue.clear()
        schedule.load_durable_jobs(); schedule.load_durable_jobs()
        self.assertEqual([item.id for item in schedule.cron_queue], [job.id])
        schedule.scheduled_jobs.clear(); schedule.cron_queue.clear()

    def test_skill_requires_distinct_verified_runs(self):
        from src.runtime import get_runtime
        from src.skill import save_skill_candidate
        runtime = get_runtime()
        runtime.db.put("runs", "skill-run-1", {"status": "achieved"})
        runtime.db.put("runs", "skill-run-2", {"status": "achieved"})
        first = save_skill_candidate("test-technique", "description", "Procedure", "skill-run-1")
        again = save_skill_candidate("test-technique", "description", "Procedure", "skill-run-1")
        self.assertEqual(len(again["run_ids"]), 1)
        from src.config import SKILLS_DIR
        path = SKILLS_DIR / "test-technique" / "SKILL.md"
        self.assertFalse(path.exists())
        save_skill_candidate("test-technique", "description", "Procedure", "skill-run-2")
        self.assertTrue(path.exists())


class LoopTests(unittest.TestCase):
    def test_context_recall_only_once_and_system_stable(self):
        from src.context import update_context
        from src.task import assemble_system_prompt
        with patch("src.memory.load_memories", return_value="records") as recall, \
             patch("src.skill.retrieve_skills", return_value="skill") as skills:
            context = update_context({"started_at": "fixed"}, [{"role": "user", "content": "request"}], refresh=True)
            first = assemble_system_prompt(context)
            for _ in range(4):
                context = update_context(context, [])
            self.assertEqual(first, assemble_system_prompt(context))
        self.assertEqual(recall.call_count, 1)
        self.assertEqual(skills.call_count, 1)

    def test_tool_evidence_is_durable_and_run_scoped(self):
        from anthropic.types import ToolUseBlock
        from src import agentLoop as loop
        from src.planner import ExecutionPlan
        responses = [SimpleNamespace(content=[ToolUseBlock(type="tool_use", id="call-1", name="read_file", input={"path": "test.txt"})],
                                     stop_reason="tool_use", usage=SimpleNamespace(input_tokens=1, output_tokens=1)),
                     SimpleNamespace(content=[TextBlock(type="text", text="done")], stop_reason="end_turn",
                                     usage=SimpleNamespace(input_tokens=1, output_tokens=1))]
        history = [{"role": "user", "content": "old task"}, {"role": "assistant", "content": "old success"},
                   {"role": "user", "content": "read"}]
        context = {}
        with patch("src.lifecycle.plan_request", return_value=ExecutionPlan(goal="read")), \
             patch.object(loop, "call_llm", side_effect=responses), \
             patch.object(loop, "BUILTIN_HANDLERS", {"read_file": lambda **kwargs: "actual evidence"}), \
             patch("src.goal.PromptGoalEvaluator.evaluate", return_value=GoalEvaluation(True, "done")), \
             patch("src.context.remember_after_turn"), patch("src.skill.evaluate_candidate"):
            loop.agent_loop(history, context, "read")
        session = context["_session"]
        saved = session.runtime.db.get("working_states", session.run_id)
        self.assertNotIn("old success", json.dumps(saved["evidence"]))
        self.assertIn("actual evidence", json.dumps(saved["evidence"]))
        self.assertEqual(saved["working"]["completed"][0]["tool"], "read_file")

    def test_real_loop_verifies_and_finalizes(self):
        from src import agentLoop as loop
        from src.planner import ExecutionPlan
        from src.runtime import get_runtime
        response = SimpleNamespace(content=[TextBlock(type="text", text="done")], stop_reason="end_turn",
                                   usage=SimpleNamespace(input_tokens=10, output_tokens=5))
        history = [{"role": "user", "content": "explain"}]
        context = {}
        with patch("src.lifecycle.plan_request", return_value=ExecutionPlan(goal="explain")), \
             patch.object(loop, "call_llm", return_value=response), \
             patch("src.goal.PromptGoalEvaluator.evaluate", return_value=GoalEvaluation(True, "explanation present")), \
             patch("src.context.remember_after_turn"), patch("src.skill.evaluate_candidate"):
            loop.agent_loop(history, context, "explain")
        run = get_runtime().db.get("runs", context["_session"].run_id)
        self.assertEqual(run["status"], "achieved")

    def test_background_yields_instead_of_spinning(self):
        from src import agentLoop as loop
        from src.planner import ExecutionPlan
        response = SimpleNamespace(content=[TextBlock(type="text", text="waiting")], stop_reason="end_turn",
                                   usage=SimpleNamespace(input_tokens=1, output_tokens=1))
        with patch("src.lifecycle.plan_request", return_value=ExecutionPlan(goal="wait")), \
             patch.object(loop, "call_llm", return_value=response) as model, \
             patch("src.task.has_running_background", return_value=True):
            context = {}
            loop.agent_loop([{"role": "user", "content": "wait"}], context, "wait")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(context["_session"].runtime.db.get("runs", context["_session"].run_id)["status"], "waiting")

class PromptCacheTests(unittest.TestCase):
    def test_conversation_cache_control_does_not_mutate_history(self):
        from src import agentLoop as loop
        messages = [{"role": "user", "content": "hello"}]
        prepared = loop._with_conversation_cache_control(messages)
        self.assertEqual(messages[0]["content"], "hello")
        self.assertEqual(prepared[0]["content"][0]["type"], "text")
        self.assertEqual(prepared[0]["content"][0]["text"], "hello")
        self.assertEqual(prepared[0]["content"][0]["cache_control"], {"type": "ephemeral"})

    def test_call_llm_marks_last_message_when_cache_enabled(self):
        from src import agentLoop as loop
        from src.context import RecoveryState
        response = SimpleNamespace(usage=SimpleNamespace(input_tokens=1, output_tokens=1))
        history = [{"role": "user", "content": "hello"}]
        session = SimpleNamespace(request="hello", checkpoint=lambda messages: None)
        with patch.object(loop, "assemble_system_prompt", return_value="system"), \
             patch.object(loop, "ENABLE_PROMPT_CACHE", True), \
             patch.object(loop, "client") as client, \
             patch("src.context.client") as counter_client:
            client.messages.create.return_value = response
            model = RecoveryState()
            tools = [{"name": "test", "input_schema": {"type": "object"}}]
            counter = counter_client.client.with_options.return_value.messages.count_tokens
            counter.return_value = SimpleNamespace(input_tokens=100)
            loop.call_llm(history, {}, tools, model, 10, session=session)
        counter_client.client.with_options.assert_called_once_with(max_retries=0)
        counter.assert_called_once_with(
            model=model.current_model, system="system", messages=history, tools=tools, timeout=15,
        )
        kwargs = client.messages.create.call_args.kwargs
        self.assertEqual(kwargs["system"], [{
            "type": "text", "text": "system",
            "cache_control": {"type": "ephemeral"},
        }])
        self.assertEqual(
            kwargs["messages"][-1]["content"][-1]["cache_control"],
            {"type": "ephemeral"},
        )

    def test_prepare_context_keeps_consumed_results_stable_for_cache(self):
        from src import agentLoop as loop
        long_result = "x" * 200
        messages = [{"role": "user", "content": "start"}]
        for tool_id in ("a", "b", "c", "d", "e"):
            messages.append({"role": "assistant", "content": [
                {"type": "tool_use", "id": tool_id, "name": "read_file",
                 "input": {"path": tool_id}},
            ]})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id,
                 "content": long_result if tool_id == "a" else "recent"},
            ]})
        with patch.object(loop, "ENABLE_PROMPT_CACHE", True):
            loop.prepare_context(messages, "start")
        self.assertEqual(messages[2]["content"][0]["content"], long_result)
        with patch.object(loop, "ENABLE_PROMPT_CACHE", False):
            loop.prepare_context(messages, "start")
        self.assertIn("Earlier tool result compacted", messages[2]["content"][0]["content"])


class ContextManagementTests(unittest.TestCase):
    def test_agents_instructions_are_ordered_root_first(self):
        from src.context import load_agents_instructions
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            nested = root / "nested"
            nested.mkdir()
            (root / "AGENTS.md").write_text("root instruction", encoding="utf-8")
            (nested / "AGENTS.md").write_text("nested instruction", encoding="utf-8")
            with patch("src.context.PROJECT_ROOT", root), \
                 patch("src.context.WORKDIR", root):
                text = load_agents_instructions(nested)
            self.assertLess(text.index("root instruction"), text.index("nested instruction"))

    def test_context_manager_keeps_long_history_within_token_budget(self):
        from src.context import ContextManager
        original = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}"}
            for i in range(70)
        ]
        messages = list(original)
        prepared = ContextManager({"limit_tokens": 980_000}).prepare_messages(
            messages, "continue", enable_cache=True, count_tokens=lambda _: 979_999,
        )
        self.assertIs(prepared, messages)
        self.assertEqual(messages, original)

    def test_summary_preserves_oversized_transcript_tail_by_splitting(self):
        from anthropic import BadRequestError
        from httpx import Request, Response
        from src.context import summarize_history

        messages = [{"role": "user", "content": "前" * 85_000 + "important tail"}]
        too_long = BadRequestError(
            "prompt is too long",
            response=Response(400, request=Request("POST", "https://example.invalid")),
            body=None,
        )
        replies = [
            SimpleNamespace(content=[TextBlock(type="text", text=text)])
            for text in ("early facts", "recent facts", "combined facts")
        ]
        with patch("src.context.client") as client:
            create = client.messages.create
            create.side_effect = [too_long, *replies]
            self.assertEqual(summarize_history(messages), "combined facts")
        payloads = [call.kwargs["messages"][0]["content"] for call in create.call_args_list]
        self.assertEqual(len(payloads), 4)
        self.assertEqual(payloads[1] + payloads[2], payloads[0])
        self.assertIn("important tail", payloads[2])
        self.assertEqual(json.loads(payloads[3]), ["early facts", "recent facts"])

    def test_context_manager_compacts_when_over_limit(self):
        from src.context import ContextManager
        messages = [{"role": "user", "content": "short text"}]
        manager = ContextManager({"limit_tokens": 10})
        with patch("src.context.write_transcript"), patch(
            "src.context.compact_history",
            return_value=[{"role": "user", "content": "summary"}],
        ) as compact:
            manager.prepare_messages(
                messages, "request", enable_cache=True, count_tokens=lambda _: 10,
            )
            compact.assert_not_called()
            prepared = manager.prepare_messages(
                messages, "request", enable_cache=True, count_tokens=lambda _: 11,
            )
        self.assertIs(prepared, messages)
        self.assertEqual(compact.call_count, 1)
        self.assertEqual(messages[0]["content"], "summary")

    def test_unsupported_token_counter_does_not_block_model_request(self):
        from anthropic import NotFoundError
        from httpx import Request, Response
        from src import agentLoop as loop
        from src.context import RecoveryState

        history = [{"role": "user", "content": "x" * 60_000}]
        session = SimpleNamespace(request="continue", checkpoint=lambda messages: None)
        with patch.object(loop, "assemble_system_prompt", return_value="system"), \
             patch.object(loop, "ENABLE_PROMPT_CACHE", False), \
             patch.object(loop, "client") as client, \
             patch("src.context.client") as counter_client, \
             patch("src.context.compact_history") as compact:
            counter = counter_client.client.with_options.return_value.messages.count_tokens
            counter.side_effect = NotFoundError(
                "unsupported", response=Response(404, request=Request("POST", "https://example.invalid")),
                body=None,
            )
            client.messages.create.return_value = SimpleNamespace(usage=SimpleNamespace())
            loop.call_llm(history, {}, [], RecoveryState(), 10, session=session)
        compact.assert_not_called()
        self.assertIs(client.messages.create.call_args.kwargs["messages"], history)


class VectorTests(unittest.TestCase):
    def test_qdrant_query_contract(self):
        from src.storage.vector_store import VectorStore
        vector = VectorStore()
        vector.enabled, vector.url = True, "http://qdrant"
        with patch.object(vector, "embed", return_value=[1., 0.]), \
             patch.object(vector, "request", return_value={"result": {"points": [{"payload": {"source_id": "one"}}]}}) as request:
            self.assertEqual(vector.search("memories", "question"), ["one"])
        self.assertTrue(request.call_args.args[0].endswith("/points/query"))
        self.assertEqual(request.call_args.args[1]["query"], [1., 0.])

    def test_service_outage_falls_back(self):
        from src.storage.vector_store import VectorStore
        vector = VectorStore()
        vector.enabled = True
        with patch.object(vector, "embed", side_effect=OSError("offline")):
            self.assertEqual(vector.search("memories", "question"), [])
        self.assertEqual(vector.last_error, "offline")


if __name__ == "__main__":
    unittest.main()
