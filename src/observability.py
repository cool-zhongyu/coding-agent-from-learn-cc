"""Optional Langfuse tracing and run-level quality signals.

Tracing is opt-in: it activates only when both ``LANGFUSE_PUBLIC_KEY`` and
``LANGFUSE_SECRET_KEY`` are set and ``LANGFUSE_TRACING_ENABLED`` is not false.
Without credentials every helper is a no-op, so offline tests and local runs
never contact Langfuse.

The application records deterministic outcome scores itself (run status, tool
error rate, permission denials, goal blocks, tokens, latency).  The optional
online evaluator created by ``scripts/setup_langfuse_online_eval.py`` reads the
root ``agent-run`` observation and scores task completion with an LLM judge.
"""
from __future__ import annotations

import atexit
import json
import os
import threading
from contextlib import ExitStack, contextmanager
from datetime import datetime
from typing import Any

_lock = threading.Lock()
_client = None
_client_error: str | None = None
_last_trace: dict[str, str | None] = {}
_atexit_registered = False


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def enabled() -> bool:
    if not _truthy(os.getenv("LANGFUSE_TRACING_ENABLED"), True):
        return False
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def _warn(message: str) -> None:
    print(f"  \033[33m[langfuse] {message}\033[0m")


def _get_client():
    global _client, _client_error, _atexit_registered
    if not enabled():
        return None
    with _lock:
        if _client is not None:
            return _client
        if _client_error is not None:
            return None
        try:
            from langfuse import Langfuse

            base_url = (
                os.getenv("LANGFUSE_BASE_URL")
                or os.getenv("LANGFUSE_HOST")
                or "https://cloud.langfuse.com"
            )
            _client = Langfuse(
                public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
                secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
                base_url=base_url,
                environment=(
                    os.getenv("LANGFUSE_ENVIRONMENT")
                    or os.getenv("LANGFUSE_TRACING_ENVIRONMENT")
                    or "development"
                ),
                release=os.getenv("LANGFUSE_RELEASE") or None,
            )
            if not _atexit_registered:
                atexit.register(shutdown)
                _atexit_registered = True
            return _client
        except Exception as exc:  # pragma: no cover - depends on external SDK
            _client_error = f"{type(exc).__name__}: {exc}"
            _warn(f"disabled: {_client_error}")
            return None


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return _jsonable(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "to_dict"):
        try:
            return _jsonable(value.to_dict())
        except Exception:
            pass
    return str(value)


def with_system(system: Any, messages: list) -> list:
    """Return a role-labeled message list with the system prompt first."""
    result = []
    if system:
        if isinstance(system, list):
            text = "\n".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in system
            )
        else:
            text = str(system)
        result.append({"role": "system", "content": text})
    result.extend(_jsonable(messages))
    return result


def _response_output(response: Any) -> Any:
    content = getattr(response, "content", None)
    if content is not None:
        return _jsonable(content)
    return _jsonable(response)


def _usage_details(response: Any) -> dict[str, int] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    result: dict[str, int] = {}
    pairs = (
        ("input", "input_tokens"),
        ("output", "output_tokens"),
        ("cache_read_input_tokens", "cache_read_input_tokens"),
        ("cache_creation_input_tokens", "cache_creation_input_tokens"),
    )
    for key, attr in pairs:
        if isinstance(usage, dict):
            value = usage.get(attr, usage.get(key))
        else:
            value = getattr(usage, attr, None)
        if value is None:
            continue
        try:
            result[key] = int(value)
        except (TypeError, ValueError):
            continue
    return result or None


def traced_llm_call(
    name: str,
    call,
    *,
    model: str | None = None,
    input: Any = None,
    metadata: Any = None,
):
    """Run one model call and record it as a Langfuse generation when enabled."""
    client = _get_client()
    if client is None:
        return call()

    observation = None
    try:
        observation = client.start_observation(
            as_type="generation",
            name=name,
            model=model,
            input=_jsonable(input),
            metadata=_jsonable(metadata),
        )
    except Exception as exc:  # pragma: no cover - depends on external SDK
        _warn(f"generation {name!r} not traced: {type(exc).__name__}: {exc}")
        return call()

    try:
        response = call()
        try:
            observation.update(
                output=_response_output(response),
                usage_details=_usage_details(response),
                model=model,
            )
        except Exception as exc:  # pragma: no cover
            _warn(f"generation update failed: {type(exc).__name__}: {exc}")
        return response
    except Exception as exc:
        try:
            observation.update(
                level="ERROR",
                status_message=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            pass
        raise
    finally:
        try:
            observation.end()
        except Exception:
            pass


class _NoopObservation:
    trace_id = None
    id = None

    def update(self, **kwargs):
        return self

    def end(self, **kwargs):
        return self

    def score(self, **kwargs):
        return None


_NOOP = _NoopObservation()


@contextmanager
def tool_span(name: str, input: Any = None):
    """Context manager for a tool observation; yields a no-op when disabled."""
    client = _get_client()
    if client is None:
        yield _NOOP
        return

    observation = None
    try:
        observation = client.start_observation(
            as_type="tool",
            name=f"tool-{name}",
            input=_jsonable(input),
            metadata={"tool": name},
        )
    except Exception as exc:  # pragma: no cover
        _warn(f"tool span {name!r} not traced: {type(exc).__name__}: {exc}")

    if observation is None:
        yield _NOOP
        return

    try:
        yield observation
    except Exception as exc:
        try:
            observation.update(
                level="ERROR",
                status_message=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            pass
        raise
    finally:
        try:
            observation.end()
        except Exception:
            pass


@contextmanager
def agent_span(name: str, input: Any = None, metadata: Any = None):
    """Context manager for a subagent/teammate execution."""
    client = _get_client()
    if client is None:
        yield _NOOP
        return

    cm = None
    try:
        cm = client.start_as_current_observation(
            as_type="agent",
            name=name,
            input=_jsonable(input),
            metadata=_jsonable(metadata),
        )
        span = cm.__enter__()
    except Exception as exc:  # pragma: no cover
        _warn(f"agent span {name!r} not traced: {type(exc).__name__}: {exc}")
        yield _NOOP
        return

    try:
        yield span
    except Exception as exc:
        try:
            span.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        raise
    finally:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass


def start_tool_observation(name: str, input: Any = None, *, metadata: Any = None):
    """Create a manual tool observation whose lifecycle the caller owns."""
    client = _get_client()
    if client is None:
        return _NOOP
    try:
        return client.start_observation(
            as_type="tool",
            name=f"tool-{name}",
            input=_jsonable(input),
            metadata=_jsonable(metadata),
        )
    except Exception as exc:  # pragma: no cover
        _warn(f"manual tool span {name!r} not traced: {type(exc).__name__}: {exc}")
        return _NOOP


def _parse_started_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _final_answer(messages: list) -> str:
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
    return ""


def build_run_summary(session, messages: list) -> dict[str, Any]:
    """Build trace output/metadata and deterministic scores for one run."""
    run = None
    if session is not None:
        try:
            run = session.runtime.db.get("runs", session.run_id)
        except Exception:
            run = None
    status = str((run or {}).get("status") or "unknown")
    final_answer = _final_answer(messages)
    output = final_answer or f"(no assistant answer; status={status})"

    working = getattr(session, "working", {}) if session is not None else {}
    failed = working.get("failed_attempts", []) if isinstance(working, dict) else []
    permission_denials = sum(
        1
        for item in failed
        if str(item.get("result", "")).startswith("Permission denied")
    )
    tool_calls = int(getattr(session, "tool_calls", 0) or 0)
    tool_error_rate = (len(failed) / tool_calls) if tool_calls else 0.0
    tokens = int(getattr(session, "tokens", 0) or 0)

    goal = getattr(session, "goal", None) if session is not None else None
    events = getattr(goal, "events", []) if goal is not None else []
    goal_blocks = sum(
        1
        for event in events
        if event.get("type") == "goal_status"
        and event.get("active")
        and not event.get("met")
    )
    goal_reason = ""
    if goal is not None and getattr(goal, "last_status", None):
        goal_reason = str(goal.last_status.get("reason", ""))

    plan = getattr(session, "plan", None) if session is not None else None
    plan_mode = str(getattr(plan, "mode", "") or "")
    plan_goal = str(getattr(plan, "goal", "") or "")

    started_at = _parse_started_at(getattr(session, "started_at", None))
    duration_ms = None
    if started_at is not None:
        duration_ms = max(0, int((datetime.now() - started_at).total_seconds() * 1000))

    changed_files = working.get("changed_files", []) if isinstance(working, dict) else []
    metadata = {
        "run_id": getattr(session, "run_id", None) if session is not None else None,
        "session_id": getattr(session, "session_id", None) if session is not None else None,
        "run_status": status,
        "plan_mode": plan_mode,
        "plan_goal": plan_goal[:1000],
        "goal_reason": goal_reason[:1000],
        "tokens": tokens,
        "tool_calls": tool_calls,
        "tool_errors": len(failed),
        "tool_error_rate": round(tool_error_rate, 4),
        "permission_denials": permission_denials,
        "goal_blocks": goal_blocks,
        "changed_files_count": len(changed_files),
        "changed_files": list(changed_files)[:20],
        "duration_ms": duration_ms,
    }
    scores = [
        ("run_status", status, "CATEGORICAL", "Final durable run status."),
        ("tool_error_rate", round(tool_error_rate, 4), "NUMERIC", "Failed tool calls divided by total tool calls."),
        ("permission_denials", permission_denials, "NUMERIC", "Tool calls denied by the permission layer."),
        ("goal_blocks", goal_blocks, "NUMERIC", "Turns where the goal evaluator kept the task active."),
        ("tool_calls", tool_calls, "NUMERIC", "Total tool calls in the run."),
        ("run_tokens", tokens, "NUMERIC", "Total model tokens reported for the run."),
    ]
    if duration_ms is not None:
        scores.append(("run_duration_ms", duration_ms, "NUMERIC", "Wall-clock run duration."))
    return {"status": status, "output": output, "metadata": metadata, "scores": scores}


class AgentTrace:
    """Root ``agent-run`` observation plus its trace-level attributes."""

    def __init__(
        self,
        *,
        client=None,
        context_manager=None,
        span=None,
        metadata: dict[str, Any] | None = None,
    ):
        self.client = client
        self._cm = context_manager
        self.span = span
        self._attrs = ExitStack()
        self._closed = False
        self._metadata = dict(metadata or {})

    @classmethod
    def disabled(cls) -> "AgentTrace":
        return cls()

    def _enter_attributes(self, *, session_id=None, run_id=None, metadata=None):
        if self.client is None:
            return
        try:
            from langfuse import propagate_attributes

            attrs_metadata = {
                key: value
                for key, value in {**self._metadata, **(metadata or {})}.items()
                if value is not None
            }
            self._attrs.enter_context(
                propagate_attributes(
                    session_id=session_id,
                    metadata=attrs_metadata,
                    tags=["agent", "coding-agent"],
                    trace_name="agent-run",
                )
            )
        except Exception:
            pass

    def bind(self, session) -> None:
        """Attach durable run identifiers after TurnSession is created."""
        if self.span is None:
            return
        self._metadata.update(
            {
                "run_id": getattr(session, "run_id", None),
                "plan_mode": getattr(getattr(session, "plan", None), "mode", None),
                "plan_goal": str(getattr(getattr(session, "plan", None), "goal", ""))[:1000],
            }
        )
        try:
            self.span.update(metadata=self._metadata)
        except Exception:
            pass
        self._enter_attributes(
            session_id=getattr(session, "session_id", None),
            run_id=getattr(session, "run_id", None),
            metadata=self._metadata,
        )

    def finish(self, context: dict, messages: list) -> None:
        if self.span is None:
            return
        session = context.get("_session") if isinstance(context, dict) else None
        summary = build_run_summary(session, messages)
        level = "ERROR" if summary["status"] in {"error", "failed", "limit"} else "DEFAULT"
        try:
            self.span.update(
                output=summary["output"],
                metadata=summary["metadata"],
                level=level,
            )
        except Exception:
            pass
        for name, value, data_type, comment in summary["scores"]:
            try:
                self.span.score(
                    name=name,
                    value=value,
                    data_type=data_type,
                    comment=comment,
                )
            except Exception:
                pass
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._attrs.close()
        except Exception:
            pass
        if self._cm is not None:
            try:
                self._cm.__exit__(None, None, None)
            except Exception:
                pass
        if self.client is not None:
            try:
                self.client.flush()
            except Exception:
                pass


def start_agent_run(active_request: str, *, resume_run=None) -> AgentTrace:
    """Start the root agent observation; returns a no-op trace when disabled."""
    client = _get_client()
    if client is None:
        return AgentTrace.disabled()

    session_id = None
    if resume_run is not None:
        session_id = resume_run.get("session_id")
    if not session_id:
        try:
            from src.runtime import get_runtime

            session_id = get_runtime().session_id
        except Exception:
            session_id = None

    metadata = {
        "feature": "coding-agent",
        "resume": bool(resume_run),
        "run_id": (resume_run or {}).get("id"),
    }
    try:
        cm = client.start_as_current_observation(
            as_type="agent",
            name="agent-run",
            input={"request": active_request},
            metadata=metadata,
        )
        span = cm.__enter__()
    except Exception as exc:  # pragma: no cover
        _warn(f"agent trace not started: {type(exc).__name__}: {exc}")
        return AgentTrace.disabled()

    trace = AgentTrace(
        client=client,
        context_manager=cm,
        span=span,
        metadata=metadata,
    )
    trace._enter_attributes(session_id=session_id, metadata=metadata)
    try:
        _last_trace.update(
            {
                "trace_id": getattr(span, "trace_id", None),
                "observation_id": getattr(span, "id", None),
            }
        )
    except Exception:
        pass
    return trace


def record_user_feedback(value, comment: str | None = None) -> bool:
    """Attach explicit user feedback to the most recent run."""
    client = _get_client()
    trace_id = _last_trace.get("trace_id")
    if client is None or not trace_id:
        return False
    data_type = "BOOLEAN" if isinstance(value, bool) else "NUMERIC"
    try:
        client.create_score(
            name="user_feedback",
            value=value,
            trace_id=trace_id,
            observation_id=_last_trace.get("observation_id"),
            data_type=data_type,
            comment=comment,
        )
        client.flush()
        return True
    except Exception as exc:  # pragma: no cover
        _warn(f"feedback not recorded: {type(exc).__name__}: {exc}")
        return False


def last_trace_url() -> str | None:
    client = _get_client()
    trace_id = _last_trace.get("trace_id")
    if client is None or not trace_id:
        return None
    try:
        return client.get_trace_url(trace_id=trace_id)
    except Exception:
        return None


def shutdown() -> None:
    if _client is None:
        return
    try:
        _client.flush()
    except Exception:
        pass


def status() -> dict[str, Any]:
    """Small, secret-free status object for CLI diagnostics."""
    client = _get_client()
    return {
        "enabled": enabled(),
        "initialized": client is not None,
        "error": _client_error,
        "last_trace_url": last_trace_url(),
    }
