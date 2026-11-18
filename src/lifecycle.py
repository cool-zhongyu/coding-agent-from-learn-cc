"""Turn state/checkpoints remain separate from the low-level model/tool loop."""
import asyncio
import json
import time
import uuid
from datetime import datetime
from src.config import MODEL, WORKDIR
from src.runtime import get_runtime
from src.goal import GoalController, PromptGoalEvaluator, CheckedGoalEvaluator
from src.planner import ExecutionPlan, plan_request
from src.storage.sqlite_store import dumps


class TurnSession:
    def __init__(self, request, context, run=None):
        self.runtime = get_runtime()
        self.request = request
        self.run_id = run["id"] if run else "run_" + uuid.uuid4().hex
        self.context = context
        self.session_id = run["session_id"] if run else self.runtime.session_id
        self.tool_calls = run.get("tool_calls", 0) if run else 0
        self.tokens = run.get("tokens", 0) if run else 0
        self.team_names = run.get("team_names", []) if run else []
        saved = self.runtime.db.get("working_states", self.run_id, {}) if run else {}
        self.evidence = saved.get("evidence", [{"role": "user", "content": request}])
        self.working = saved.get("working", {"completed": [], "changed_files": [],
                                            "failed_attempts": [], "important_outputs": [], "next_steps": []})
        self.started_at = run["started_at"] if run else datetime.now().isoformat(timespec="seconds")
        self.dispatched = bool(run and run.get("dispatched"))
        self.context["started_at"] = self.started_at
        if run:
            self.plan = ExecutionPlan(**run["plan"])
        else:
            try:
                self.plan = plan_request(request, self.runtime.client, MODEL)
            except Exception as exc:
                self.runtime.db.event(self.run_id, "planner_error", {"error": str(exc)})
                self.plan = ExecutionPlan(goal=request[:4000])
        self.context["plan"] = self.plan.to_dict()
        evaluator = CheckedGoalEvaluator(PromptGoalEvaluator(self.runtime.client, MODEL),
                                          self.plan.completion_checks, self.check_command, self.plan.open_ended)
        self.goal = GoalController(evaluator)
        self.goal.set_goal(self.plan.goal)
        if saved.get("goal_events"):
            self.goal.events = saved["goal_events"]
        self.goal.begin_query()

    def check_command(self, command):
        # No hidden approval prompts from the evaluator's worker thread.
        from src.permissions import PermissionPolicy
        from src.task import assignment_cwd
        cwd = assignment_cwd("agent")
        denied = PermissionPolicy(self.runtime.permissions.mode, WORKDIR, cwd).check(command)
        if denied:
            return denied, None
        from src.tool_core import _run_bash_process
        output, code = _run_bash_process(command, cwd)
        self.evidence.append({"role": "user", "content":
                              f"[Completion check] {command}\nExit code: {code}\n{output}"})
        return output, code

    def record(self, status):
        return {"id": self.run_id, "session_id": self.session_id, "request": self.request,
                "plan": self.plan.to_dict(), "started_at": self.started_at, "status": status,
                "dispatched": self.dispatched, "tokens": self.tokens, "tool_calls": self.tool_calls,
                "team_names": self.team_names}

    def record_tool(self, block, result):
        name = getattr(block, "name", "unknown")
        args = getattr(block, "input", {})
        failed = str(result).startswith(("Error:", "Permission denied"))
        target = "failed_attempts" if failed else "completed"
        self.working[target].append({"tool": name, "input": args, "result": str(result)[:1000]})
        if not failed and name in {"write_file", "edit_file"} and args.get("path"):
            if args["path"] not in self.working["changed_files"]:
                self.working["changed_files"].append(args["path"])
        self.working["important_outputs"].append(str(result)[:2000])
        for key in ("completed", "failed_attempts", "important_outputs"):
            self.working[key] = self.working[key][-50:]
        self.runtime.db.event(self.run_id, "tool", {"name": name, "input": args, "result": result})

    def waiting_for_team(self):
        from src.team import active_teammates, team_lock
        with team_lock:
            return any(active_teammates.get(name) in {"working", "waiting_approval"} for name in self.team_names)

    def checkpoint(self, messages, status="running"):
        record = self.record(status)
        self.working["next_steps"] = [self.goal.active.last_reason] if self.goal.active and self.goal.active.last_reason else []
        state = {**record, "messages": messages, "evidence": self.evidence, "goal_events": self.goal.events,
                 "goal": self.plan.goal, "working": self.working}
        with self.runtime.db.connect() as db:
            for table, data in (("runs", record), ("working_states", state)):
                db.execute(f"INSERT INTO {table} VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
                           (self.run_id, dumps(data), time.time()))
        state_path = WORKDIR / ".agent" / "STATE.md"
        state_path.write_text(f"# Working state\n\nRun: {self.run_id}\nStatus: {status}\n\nGoal: {self.plan.goal}\n\n"
                              + "```json\n" + dumps(self.working) + "\n```\n", encoding="utf-8")

    def dispatch(self, messages):
        if self.dispatched:
            return
        # Persist before non-idempotent dispatch; a crash requires reconciliation,
        # never silently duplicate a team or workflow launch.
        self.dispatched = True
        self.checkpoint(messages)
        if self.plan.mode == "workflow":
            from src.workflow import run_workflow_async
            result = asyncio.run(run_workflow_async(self.plan.workflow_name, self.plan.workflow_args,
                                  parent_run_id=self.run_id))
            messages.append({"role": "user", "content": "[Workflow result]\n" + dumps(result)})
            self.evidence.append(messages[-1])
        elif self.plan.mode == "team":
            from src.team import spawn_teammate_thread
            from src.task import create_task
            for index, stream in enumerate(self.plan.workstreams):
                name = f"worker-{self.run_id[-8:]}-{index}"
                task = create_task(stream[:100], stream)
                self.team_names.append(name)
                self.checkpoint(messages)
                result = spawn_teammate_thread(name, "focused contributor", stream, task_id=task.id)
                self.runtime.db.event(self.run_id, "team_dispatch", {"name": name, "task_id": task.id, "result": result})
                messages.append({"role": "user", "content": "[Team dispatch]\n" + str(result)})
                self.evidence.append(messages[-1])

    def finish(self, messages, status):
        self.checkpoint(messages, status)
        from src.context import write_transcript, remember_after_turn
        transcript = write_transcript(self.evidence)
        self.runtime.db.event(self.run_id, "transcript", {"path": str(transcript)})
        from src.goal import transcript_text
        self.runtime.vectors.index("transcripts", self.run_id, transcript_text(self.evidence))
        if status == "achieved":
            try:
                remember_after_turn(self.evidence)
                from src.skill import evaluate_candidate
                evaluate_candidate(self.run_id, self.evidence)
            except Exception as exc:
                self.runtime.db.event(self.run_id, "learning_error", {"error": str(exc)})
