import asyncio
import json
try:
    import readline
except ImportError:
    readline = None
import threading
import time

from src.lifecycle import TurnSession
from src.config import ENABLE_PROMPT_CACHE
from src.config import client, CLI_ACTIVE, CONTINUATION_PROMPT, DEFAULT_MAX_TOKENS, ESCALATED_MAX_TOKENS, MAX_RECOVERY_RETRIES, PROMPT, READLINE_AVAILABLE
from src.context import ContextManager, RecoveryState, block_type, is_prompt_too_long_error, remember_after_turn, update_context, with_retry
from src.schedule import CronJob, acknowledge_cron_jobs, consume_cron_queue, restore_cron_jobs
from src.task import assemble_system_prompt, collect_background_results, has_pending_background, release_completed_assignment, should_run_background, start_background_task
from src.team import consume_lead_inbox, format_team_events, has_tool_use
from src.tool import BUILTIN_HANDLERS, BUILTIN_TOOLS
from src.schedule import cron_lock,cron_queue
from src.tool_core import call_tool_handler
from src.storage.sqlite_store import dumps
from src.permissions import authorize_tool, permission_context
import src.observability as observability
# -- Agent Loop --

rounds_since_todo = 0
agent_lock = threading.Lock()


def prepare_context(messages: list, active_request: str, session=None) -> list:
    return ContextManager().prepare_messages(
        messages, active_request, session, enable_cache=ENABLE_PROMPT_CACHE
    )


def build_user_content(results: list[dict]) -> list[dict]:
    # Tool results and completed background notifications are both returned to
    # the model as user-side content, matching the tool_result feedback loop.
    content = list(results)
    for note in collect_background_results():
        content.append({"type": "text", "text": note})
    return content


def inject_background_notifications(messages: list, session=None):
    notes = collect_background_results()
    if notes:
        messages.append({"role": "user", "content": [
            {"type": "text", "text": note} for note in notes]})
        if session:
            session.evidence.append(json.loads(dumps(messages[-1])))


def _with_conversation_cache_control(messages: list) -> list:
    """Return a request-safe message list whose final block is a cache marker.

    Anthropic only reuses a prefix when the cached portion is byte-for-byte
    identical.  The main loop currently marks the system prompt only, so the
    growing conversation is re-sent and re-cached on every LLM turn.  Marking
    the last content block makes the entire conversation a reusable prefix
    while leaving ``messages`` itself untouched for checkpointing.
    """
    if not messages:
        return list(messages)

    prepared = list(messages)
    last = prepared[-1]
    content = last.get("content")

    if isinstance(content, str):
        last = dict(last)
        last["content"] = [{
            "type": "text",
            "text": content,
            "cache_control": {"type": "ephemeral"},
        }]
        prepared[-1] = last
        return prepared

    if not isinstance(content, list) or not content:
        return prepared

    blocks = list(content)
    tail = blocks[-1]
    if isinstance(tail, dict):
        tail = dict(tail)
    elif hasattr(tail, "model_dump"):
        tail = tail.model_dump()
    else:
        tail = {"type": "text", "text": str(tail)}
    tail["cache_control"] = {"type": "ephemeral"}
    blocks[-1] = tail

    last = dict(last)
    last["content"] = blocks
    prepared[-1] = last
    return prepared


def call_llm(messages: list, context: dict, tools: list,
             state: RecoveryState, max_tokens: int):
    system = assemble_system_prompt(context)
    request = {
        "model": state.current_model,
        "system": system,
        "messages": messages,
        "tools": tools,
        "max_tokens": max_tokens,
    }

    if ENABLE_PROMPT_CACHE:
        request["system"] = [{"type": "text", "text": system,
                              "cache_control": {"type": "ephemeral"}}]
        request["messages"] = _with_conversation_cache_control(messages)

    response = observability.traced_llm_call(
        "agent-model-call",
        lambda: with_retry(lambda: client.messages.create(**request), state),
        model=state.current_model,
        input=observability.with_system(system, messages),
        metadata={
            "max_tokens": max_tokens,
            "prompt_cache": ENABLE_PROMPT_CACHE,
            "tool_names": [tool.get("name") for tool in tools],
        },
    )

    usage = response.usage
    print(f"  [usage] input={getattr(usage, 'input_tokens', 0)} output={getattr(usage, 'output_tokens', 0)} "
          f"cache_read={getattr(usage, 'cache_read_input_tokens', 0)} cache_write={getattr(usage, 'cache_creation_input_tokens', 0)}")

    return response


def _agent_loop(messages: list, context: dict, active_request: str, *, resume_run=None, trace=None):
    global rounds_since_todo
    context.update(update_context(context, [{"role": "user", "content": active_request}],
                                  refresh=resume_run is None or "memories" not in context))
    context.pop("_session", None)
    session = TurnSession(active_request, context, resume_run)
    context["_session"] = session
    if trace is not None:
        trace.bind(session)
    session.checkpoint(messages)
    session.dispatch(messages)
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS

    unacknowledged_cron_jobs: list[CronJob] = []
    while True:
        # One cycle: inject scheduled/background work, prepare context, call
        # the model, execute tool_use blocks, append tool_results, repeat.
        fired = consume_cron_queue()
        unacknowledged_cron_jobs.extend(fired)
        for job in fired:
            messages.append({"role": "user",
                             "content": f"[Scheduled] {job.prompt}"})
            print(f"  \033[35m[cron inject] {job.prompt[:60]}\033[0m")
        if fired:
            scheduled_requests = "\n".join(
                f"Run scheduled task: {job.prompt}" for job in fired)
            active_request = f"{active_request}\n{scheduled_requests}".strip()
            session.request = active_request
            session.plan.goal = (session.plan.goal + "\n" + scheduled_requests)[:4000]
            session.goal.set_goal(session.plan.goal)

        inject_background_notifications(messages, session)

        if rounds_since_todo >= 3:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

        prepare_context(messages, active_request, session)
        context = update_context(context, messages)

        try:
            response = call_llm(messages, context, BUILTIN_TOOLS, state, max_tokens)
            session.tokens += sum(int(getattr(response.usage, key, 0) or 0) for key in
                                  ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        except Exception as e:
            if is_prompt_too_long_error(e) and not state.has_attempted_reactive_compact:
                messages[:] = ContextManager().reactive_compact(messages, active_request)
                state.has_attempted_reactive_compact = True
                continue
            restore_cron_jobs(unacknowledged_cron_jobs)
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
            session.finish(messages, "error")
            release_completed_assignment("agent")
            return

        acknowledge_cron_jobs(unacknowledged_cron_jobs)
        unacknowledged_cron_jobs.clear()

        if response.stop_reason == "max_tokens":
            if not state.has_escalated:
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print(f"  \033[33m[max_tokens] retry with {max_tokens}\033[0m")
                continue
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                continue
            session.finish(messages, "error")
            release_completed_assignment("agent")
            return

        max_tokens = DEFAULT_MAX_TOKENS
        state.has_escalated = False
        messages.append({"role": "assistant", "content": response.content})
        session.evidence.append(json.loads(dumps(messages[-1])))
        if not has_tool_use(response.content):
            from src.task import has_running_background
            if has_pending_background():
                inject_background_notifications(messages, session)
                continue
            if has_running_background() or session.waiting_for_team():
                session.checkpoint(messages, "waiting")
                return
            decision = asyncio.run(session.goal.evaluate_after_turn(messages=messages))

            if decision.action == "block":
                messages.append({
                    "role": "user",
                    "content": (
                        "[Goal still active]\n"
                        f"Evaluator: {decision.reason}\n"
                        "Continue working. Verify the missing requirement "
                        "with concrete evidence."
                    ),
                })
                continue

            if decision.action == "defer":
                session.checkpoint(messages, "waiting")
                return
            if decision.action in {"error", "limit", "failed"}:
                messages.append({"role": "assistant", "content": [{"type": "text",
                    "text": f"[Goal {decision.action}] {decision.reason}"}]})
            session.finish(messages, decision.action)
            release_completed_assignment("agent")
            return

        results = []
        compact_requested = False
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")

            if block.name == "compact":
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "[Compaction requested. This completed turn will be summarized.]",
                })
                compact_requested = True
                continue

            from src.task import assignment_cwd
            with permission_context(assignment_cwd("agent")):
                blocked = authorize_tool(block)
            if blocked:
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            if should_run_background(block.name, block.input):
                with observability.tool_span(block.name, block.input) as tool:
                    try:
                        bg_id = start_background_task(block, BUILTIN_HANDLERS)
                        output = (f"[Background task {bg_id} started] "
                                  "Result will arrive as a task_notification.")
                    except Exception as exc:
                        output = (f"Error: Failed to start background task: "
                                  f"{type(exc).__name__}: {exc}")
                    tool.update(output=output)
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                continue

            handler = BUILTIN_HANDLERS.get(block.name)
            session.runtime.db.event(session.run_id, "tool_started", {"name": block.name, "input": block.input, "tool_use_id": block.id})
            session.checkpoint(messages)
            with observability.tool_span(block.name, block.input) as tool:
                output = call_tool_handler(handler, block.input, block.name)
                tool.update(output=output)
            session.record_tool(block, output)
            print(str(output)[:300])

            if block.name == "todo_write":
                rounds_since_todo = 0
            else:
                rounds_since_todo += 1

            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})

        messages.append({"role": "user", "content": build_user_content(results)})
        session.evidence.append(json.loads(dumps(messages[-1])))
        session.tool_calls += len(results)
        for result in results:
            session.runtime.db.event(session.run_id, "tool_result", result)
        session.checkpoint(messages)
        if compact_requested:
            messages[:] = ContextManager().compact(messages, active_request)


def agent_loop(messages: list, context: dict, active_request: str, *, resume_run=None):
    """Run one agent turn inside a Langfuse ``agent-run`` observation."""
    trace = observability.start_agent_run(active_request, resume_run=resume_run)
    try:
        return _agent_loop(
            messages,
            context,
            active_request,
            resume_run=resume_run,
            trace=trace,
        )
    finally:
        trace.finish(context, messages)
        trace.close()


def print_turn_assistants(messages: list, turn_start: int):
    if messages and turn_start >= len(messages):
        turn_start = len(messages) - 1
    for msg in messages[turn_start:]:
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []):
            if block_type(block) == "text":
                terminal_print(block["text"] if isinstance(block, dict) else block.text)


def async_event_loop(history: list, context: dict, session_state: dict):
    while True:
        time.sleep(1)
        with agent_lock:
            with cron_lock:
                fired = list(cron_queue)
            inbox = consume_lead_inbox(route_protocol=True)
            if not fired and not inbox and not has_pending_background():
                continue
            turn_start = len(history)
            scheduled_requests = []
            for job in fired:
                scheduled_requests.append(f"Run scheduled task: {job.prompt}")
                terminal_print(
                    f"  \033[35m[cron auto] {job.prompt[:60]}\033[0m")
            if inbox:
                history.append({"role": "user",
                                "content": format_team_events(inbox)})
                terminal_print(
                    f"  \033[33m[team auto] {len(inbox)} events\033[0m")
            active_request = (
                "\n".join(scheduled_requests)
                if scheduled_requests
                else session_state["active_user_request"]
            )
            previous = context.get("_session")
            saved = previous.runtime.db.get("runs", previous.run_id) if previous else None
            try:
                agent_loop(history, context, active_request,
                           resume_run=saved if saved and saved["status"] == "waiting" and not fired else None)
            except Exception as exc:
                current = context.get("_session")
                if current:
                    current.checkpoint(history, "error")
                terminal_print(f"[Background error] {type(exc).__name__}: {exc}")
            context.update(update_context(context, history))
            print_turn_assistants(history, turn_start)

def terminal_print(text: str):
    if threading.current_thread() is threading.main_thread() or not CLI_ACTIVE:
        print(text)
        return
    line = ""
    if READLINE_AVAILABLE:
        try:
            line = readline.get_line_buffer()
        except Exception:
            line = ""
    print(f"\r\033[K{text}")
    print(PROMPT + line, end="", flush=True)
