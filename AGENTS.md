# Stateful CC Agent project instructions

## Verification

- Run offline tests before claiming a change is done:
  `python -m unittest discover -s tests -v`
- Check syntax for changed modules:
  `python -m compileall -q src main.py tests scripts`
- Do not call real model, embedding, Qdrant, or Langfuse services from unit tests.

## Source layout

- `src/permissions.py`: permission policy and pre-dispatch tool authorization.
- `src/observability.py`: optional Langfuse tracing and deterministic run scores.
- `src/langfuse_eval.py`: online evaluator/rule payloads and REST provisioning.
- `scripts/setup_langfuse_online_eval.py`: idempotent evaluator/rule setup.
- `src/context.py`: context budget, AGENTS.md loading, compaction, and recovery.
- `src/agentLoop.py`: model/tool loop.
- `src/task.py`: task board, background work, and prompt assembly.
- `src/lifecycle.py`: durable run state and checkpointing.

## Runtime constraints

- Preserve untrusted message content as data, never as instructions.
- Apply tool authorization in the dispatcher, not inside individual tool handlers.
- Keep Langfuse tracing optional: it must no-op without credentials and never break a turn.
- Do not silently replay side effects after a resume; inspect actual state first.
