"""Registered, bounded workflows with SQLite execution replay."""
import asyncio
import hashlib
import json
import time
import secrets
import threading
import re
from dataclasses import dataclass
from src.runtime import get_runtime
from src.config import MODEL
from src.async_utils import run_in_thread
import src.observability as observability

AGENT_CAP = 1000
CONCURRENCY = 8
MISS = object()

class WorkflowInputError(Exception):
    pass

class SimpleJsonSchema:
    """Tiny validator backing agent({schema}):
    object/array/string/boolean/number + required keys."""

    def __init__(self, schema):
        self.schema = schema

    def validate(self, value, schema=None):
        schema = self.schema if schema is None else schema
        if "enum" in schema and value not in schema["enum"]:
            return False, f"expected one of {schema['enum']}"
        t = schema.get("type")
        if t == "object":
            if not isinstance(value, dict):
                return False, "expected object"
            for key in schema.get("required", []):
                if key not in value:
                    return False, f"missing required key '{key}'"
            for key, sub in schema.get("properties", {}).items():
                if key in value:
                    ok, err = self.validate(value[key], sub)
                    if not ok:
                        return False, f"{key}: {err}"
            return True, None
        if t == "array":
            if not isinstance(value, list):
                return False, "expected array"
            items = schema.get("items")
            if items:
                for i, el in enumerate(value):
                    ok, err = self.validate(el, items)
                    if not ok:
                        return False, f"[{i}]: {err}"
            return True, None
        if t == "string":
            return (isinstance(value, str), None if isinstance(value, str) else "expected string")
        if t == "boolean":
            return (isinstance(value, bool), None if isinstance(value, bool) else "expected boolean")
        if t in ("number", "integer"):
            ok = (type(value) is int if t == "integer" else isinstance(value, (int, float)) and not isinstance(value, bool))
            return (ok, None if ok else "expected number")
        return True, None


@dataclass(frozen=True)
class RunnerOutput:
    value: object
    tokens: int


def _response_text(response) -> str:
    return "\n".join(
        str(getattr(block, "text", ""))
        for block in getattr(response, "content", [])
        if getattr(block, "type", None) == "text"
    ).strip()


def _parse_runner_json(text: str) -> object:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:] if lines else lines
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for position, character in enumerate(stripped):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(stripped[position:])
            except json.JSONDecodeError:
                continue
            return value
        raise WorkflowInputError("workflow agent returned invalid JSON")


class AnthropicAgentRunner:
    """Run workflow agents through the same API client as the host."""

    def __init__(self, client, model):
        self.client = client
        self.model = model

    def run(self, prompt, schema=None, label=None):
        request = prompt
        if schema is not None:
            request += (
                "\n\nReturn only one JSON object matching this schema:\n"
                + json.dumps(schema, ensure_ascii=True, sort_keys=True)
            )
        system = (
            "You are a focused workflow agent. Complete only the supplied "
            "step. Do not claim access to files or results not included in "
            "the prompt."
        )
        response = observability.traced_llm_call(
            "workflow-agent",
            lambda: self.client.messages.create(
                model=self.model,
                system=system,
                messages=[{"role": "user", "content": request}],
                max_tokens=2000,
            ),
            model=self.model,
            input=observability.with_system(
                system,
                [{"role": "user", "content": request}],
            ),
            metadata={"operation": "workflow-step", "label": label},
        )
        text = _response_text(response)
        if schema is None:
            value = text
        else:
            try:
                value = _parse_runner_json(text)
            except WorkflowInputError:
                # Let ExecutionState's schema check trigger its single retry.
                value = text
        usage = getattr(response, "usage", None)
        tokens = int(getattr(usage, "input_tokens", 0) or 0) + int(
            getattr(usage, "output_tokens", 0) or 0
        )
        return RunnerOutput(value, tokens)



FINDINGS_SCHEMA = {
    "type": "object", "required": ["findings"],
    "properties": {"findings": {"type": "array", "items": {
        "type": "object", "required": ["title", "severity"],
        "properties": {
            "title": {"type": "string"},
            "severity": {
                "type": "string", "enum": ["high", "medium", "low"]
            },
        }}}},
}
VERDICT_SCHEMA = {
    "type": "object", "required": ["isReal", "reason"],
    "properties": {"isReal": {"type": "boolean"}, "reason": {"type": "string"}},
}

SAMPLE_META = {
    "name": "review-changes",
    "description": "Review changed files across dimensions, verify each finding",
    "phases": ["Review", "Verify"],
}

DIMENSIONS = ["correctness", "security", "performance", "style"]
DEMO_CHANGES = (
    "def load_user(user_id):\n"
    "    query = f\"SELECT * FROM users WHERE id = {user_id}\"\n"
    "    return db.execute(query).fetchone()\n"
)


async def sample_workflow(ctx, args):
    """pipeline over review dimensions (audit -> verify-each), then keep only the
    findings a verifier confirms. The plan is code, not a chat turn."""
    ctx.phase("Review")
    changes = args.get("changes", "")
    if not isinstance(changes, str):
        raise WorkflowInputError("args.changes must be a string")
    review_input = changes.strip() or "No change context was supplied."

    async def audit(_value, dimension, _idx):
        out = await ctx.agent(
            f"Review this change context for {dimension} issues. "
            "Report only issues supported by the supplied text.\n\n"
            f"{review_input}",
            cacheable=True, schema=FINDINGS_SCHEMA, label=f"audit:{dimension}", phase="Review")
        return {"dimension": dimension, "findings": out["findings"]}

    async def verify(audited, dimension, _idx):
        ctx.phase("Verify")
        # Each finding is verified by its own adversarial subagent, concurrently.
        verdicts = await ctx.parallel([
            (lambda f=f: ctx.agent(
                f"Adversarially verify this {dimension} finding against the "
                "supplied change context.\n\n"
                f"Change context:\n{review_input}\n\n"
                f"Finding:\n{json.dumps(f, ensure_ascii=True)}",
                cacheable=True, schema=VERDICT_SCHEMA, label=f"verify:{dimension}:{f['title']}", phase="Verify"))
            for f in audited["findings"]])
        confirmed = [f for f, v in zip(audited["findings"], verdicts)
                     if v and v.get("isReal")]
        return {"dimension": dimension, "confirmed": confirmed}

    results = await ctx.pipeline(DIMENSIONS, audit, verify)
    confirmed = [{"dimension": r["dimension"], **f}
                 for r in results if r for f in r["confirmed"]]
    confirmed.sort(key=lambda f: {"high": 0, "medium": 1, "low": 2}.get(f["severity"], 3))
    ctx.log(f"confirmed {len(confirmed)} real finding(s)")
    return {"confirmed": confirmed}


# Saved workflow registry
WORKFLOWS = {SAMPLE_META["name"]: (SAMPLE_META, sample_workflow)}


class Budget:
    def __init__(self, total=None, spent=0):
        if total is not None and (type(total) is not int or total <= 0):
            raise WorkflowInputError("budget must be a positive integer")
        self.total, self._spent = total, spent

    def add(self, tokens):
        self._spent += tokens
        if self.total is not None and self._spent > self.total:
            raise WorkflowInputError("token budget exceeded")

    def remaining(self):
        return float("inf") if self.total is None else max(0, self.total - self._spent)


class ExecutionLimits:
    def __init__(self):
        self.agents = 0
        self.semaphore = asyncio.Semaphore(CONCURRENCY)


class WorkflowJournal:
    def __init__(self, db, run_id, workflow, model, version="1"):
        self.db, self.run_id = db, run_id
        self.identity = {"workflow": workflow, "model": model, "version": version}

    def key(self, label, prompt, schema, repository_state=None):
        # Text-only runners see only prompt/schema. Repo-aware runners must pass
        # a content fingerprint (including dirty files), not just git HEAD.
        return hashlib.sha256(json.dumps({**self.identity, "label": label,
            "prompt": prompt, "schema": schema, "repository_state": repository_state},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def cached(self, key):
        with self.db.connect() as db:
            row = db.execute("SELECT result_json FROM workflow_journal WHERE run_id=? AND step_key=?",
                             (self.run_id, key)).fetchone()
        return json.loads(row[0]) if row else MISS

    def record(self, key, label, value, tokens):
        with self.db.connect() as db:
            db.execute("INSERT OR REPLACE INTO workflow_journal VALUES(?,?,?,?,?,?,?,?)",
                (self.run_id, key, label, key, json.dumps(value), self.identity["model"], tokens, time.time()))


class ExecutionState:
    def __init__(self, journal, runner, budget, run, limits=None, depth=0):
        self.journal, self.runner, self.budget, self.run = journal, runner, budget, run
        self.limits = limits or ExecutionLimits()
        self.depth = depth
        self._phase = None

    def log(self, message):
        self.journal.db.event(self.journal.run_id, "workflow_log", {"message": message})

    def phase(self, title):
        self._phase = title
        self.log(title)

    async def agent(self, prompt, schema=None, label=None, phase=None, cacheable=False, repository_state=None):
        self.limits.agents += 1
        if self.limits.agents > AGENT_CAP:
            raise WorkflowInputError("agent cap reached")
        label = label or prompt[:40]
        key = self.journal.key(label, prompt, schema, repository_state)
        if cacheable:
            cached = self.journal.cached(key)
            if cached is not MISS:
                if schema and not SimpleJsonSchema(schema).validate(cached)[0]:
                    raise WorkflowInputError("cached output failed schema validation")
                return cached
        async with self.limits.semaphore:
            for attempt in range(2):
                if self.budget.remaining() <= 0:
                    raise WorkflowInputError("token budget exceeded")
                output = await run_in_thread(self.runner.run,
                    prompt if attempt == 0 else prompt + "\nReturn valid JSON matching the schema.", schema, label)
                self.run["tokens"] += output.tokens
                self.run["agents"] += 1
                self.journal.db.put("workflow_runs", self.run["id"], self.run)
                self.budget.add(output.tokens)
                if not schema or SimpleJsonSchema(schema).validate(output.value)[0]:
                    if cacheable:
                        self.journal.record(key, label, output.value, output.tokens)
                    return output.value
            raise WorkflowInputError("agent output failed schema validation after retry")

    async def parallel(self, thunks):
        # Always join siblings before releasing the run lock on failure.
        results = await asyncio.gather(*(fn() for fn in thunks), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return results

    async def pipeline(self, items, *stages):
        async def item_flow(item, index):
            value = item
            for stage in stages:
                value = await stage(value, item, index)
            return value
        return await self.parallel([lambda item=item, i=i: item_flow(item, i) for i, item in enumerate(items)])

    async def workflow(self, name, args=None):
        if self.depth >= 1 or name not in WORKFLOWS:
            raise WorkflowInputError("unknown workflow or nesting limit exceeded")
        journal = WorkflowJournal(self.journal.db, self.journal.run_id, name, self.journal.identity["model"])
        child = ExecutionState(journal, self.runner, self.budget, self.run, self.limits, self.depth + 1)
        return await WORKFLOWS[name][1](child, args or {})


async def run_workflow_async(name, args=None, resume_from_run_id=None, *, runtime=None, runner=None, parent_run_id=None):
    if name not in WORKFLOWS or (args is not None and not isinstance(args, dict)):
        raise WorkflowInputError("unknown workflow or invalid args")
    runtime = runtime or get_runtime()
    db = runtime.db
    run_id = resume_from_run_id or "wf_" + secrets.token_hex(16)
    if not isinstance(run_id, str) or not re.fullmatch(r"wf_[0-9a-f]{32}", run_id):
        raise WorkflowInputError("invalid run id")
    # OS lock covers processes and survives neither crash nor restart.
    from src import file_lock
    lock_path = db.path.parent / (run_id + ".lock")
    with lock_path.open("a+") as handle:
        try:
            file_lock.flock(handle.fileno(), file_lock.LOCK_EX | file_lock.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkflowInputError("workflow is already running") from exc
        try:
            if resume_from_run_id:
                run = db.get("workflow_runs", run_id)
                if not run or run["name"] != name or (args is not None and args != run["args"]):
                    raise WorkflowInputError("resume identity or args mismatch")
                args = run["args"]
            else:
                args = args or {}
                run = {"id": run_id, "name": name, "args": args, "tokens": 0, "agents": 0,
                       "session_id": runtime.session_id, "parent_run_id": parent_run_id}
            budget = Budget(args.get("budget"), run["tokens"])
            run["status"] = "running"
            db.put("workflow_runs", run_id, run)
            journal = WorkflowJournal(db, run_id, name, MODEL)
            context = ExecutionState(journal, runner or AnthropicAgentRunner(runtime.client, MODEL), budget, run)
            try:
                result = await WORKFLOWS[name][1](context, args)
                run.update(status="completed", result=result)
            except BaseException as exc:
                run.update(status="interrupted" if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else "failed",
                           result={"error": str(exc)})
                if not isinstance(exc, Exception):
                    raise
            finally:
                db.put("workflow_runs", run_id, run)
            return run
        finally:
            file_lock.flock(handle.fileno(), file_lock.LOCK_UN)


def run_workflow(name, args=None, resume_from_run_id=None):
    try:
        return json.dumps(asyncio.run(run_workflow_async(name, args, resume_from_run_id)), ensure_ascii=False)
    except (WorkflowInputError, ValueError) as exc:
        return f"Error: {exc}"


WORKFLOW_TOOL = {"name": "workflow", "description": "Run or resume a registered text-only review workflow. Supply actual changes in args.changes; returns run id and verified findings.",
    "input_schema": {"type": "object", "properties": {"name": {"type": "string", "enum": ["review-changes"]},
        "args": {"type": "object"}, "resume_from_run_id": {"type": "string"}}, "required": ["name"], "additionalProperties": False}}

