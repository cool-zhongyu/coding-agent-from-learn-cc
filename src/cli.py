"""Interactive agent; /runs and /resume restore durable turn checkpoints."""
import argparse
import json
import threading
from src import config
from src.agentLoop import agent_loop, async_event_loop, print_turn_assistants, agent_lock
from src.schedule import start_runtime_services
from src.runtime import get_runtime
from src.memory import rebuild_memory_index
import src.observability as observability


def resume_run(run_id, history, context):
    runtime = get_runtime()
    state = runtime.db.get("working_states", run_id)
    run = runtime.db.get("runs", run_id)
    if not state or not run:
        raise ValueError("Unknown run id")
    if run["status"] == "achieved":
        raise ValueError("Run is already achieved")
    history[:] = state["messages"]
    from src.goal import _block_value, _block_type
    pending = {}
    for message in history:
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if _block_type(block) == "tool_use":
                pending[_block_value(block, "id")] = block
            elif _block_type(block) == "tool_result":
                pending.pop(_block_value(block, "tool_use_id"), None)
    if pending:
        history.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": key, "is_error": True,
             "content": "Interrupted; outcome unknown. Inspect actual state before retrying side effects."}
            for key in pending]})
    history.append({"role": "user", "content":
                    "Resume the saved request. Background processes and teammates do not survive restart. "
                    "Inspect durable workflow runs and actual workspace state before retrying side effects."})
    start = len(history)
    agent_loop(history, context, run["request"], resume_run=run)
    print_turn_assistants(history, start)
    return run["request"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", help="resume a saved run id")
    options = parser.parse_args()
    config.CLI_ACTIVE = True
    runtime = get_runtime()
    rebuild_memory_index()
    start_runtime_services()
    history, context = [], {}
    session_state = {"active_user_request": "(no active user request)"}
    print("Stateful agent: /runs, /resume <run_id>, /feedback, /langfuse, q to quit")
    if options.resume:
        session_state["active_user_request"] = resume_run(options.resume, history, context)
    threading.Thread(target=async_event_loop, args=(history, context, session_state), daemon=True).start()
    while True:
        try:
            query = config.CONSOLE.ask(config.PROMPT).strip()
            if query.lower() in {"q", "exit"}:
                break
            if not query:
                continue
            with agent_lock:
                if query == "/runs":
                    for run in runtime.db.all("runs"):
                        print(run["id"], run["status"], run["request"][:100])
                    continue
                if query.startswith("/resume "):
                    session_state["active_user_request"] = resume_run(query.split(maxsplit=1)[1], history, context)
                    continue
                if query == "/langfuse":
                    print(json.dumps(observability.status(), indent=2, ensure_ascii=False))
                    continue
                if query.startswith("/feedback"):
                    parts = query.split(maxsplit=2)
                    if len(parts) < 2:
                        print("Usage: /feedback good|bad|0..1 [comment]")
                        continue
                    raw = parts[1].lower()
                    if raw in {"good", "up", "yes", "true"}:
                        value = True
                    elif raw in {"bad", "down", "no", "false"}:
                        value = False
                    else:
                        try:
                            value = float(raw)
                        except ValueError:
                            print("Feedback value must be good, bad, or a number from 0 to 1.")
                            continue
                        if not 0 <= value <= 1:
                            print("Feedback number must be between 0 and 1.")
                            continue
                    comment = parts[2] if len(parts) > 2 else None
                    if observability.record_user_feedback(value, comment):
                        print("Feedback recorded in Langfuse.")
                    else:
                        print("Feedback not recorded: configure Langfuse keys and run a turn first.")
                    continue
                turn_start = len(history)
                session_state["active_user_request"] = query
                history.append({"role": "user", "content": query})
                agent_loop(history, context, query)
                print_turn_assistants(history, turn_start)
        except EOFError:
            break
        except KeyboardInterrupt:
            session = context.get("_session")
            if session:
                session.checkpoint(history, "interrupted")
                print(f"\nSaved {session.run_id}; use /resume to continue.")
        except Exception as exc:
            session = context.get("_session")
            if session:
                session.checkpoint(history, "error")
            print(f"Error: {type(exc).__name__}: {exc}")
    observability.shutdown()


if __name__ == "__main__":
    main()
