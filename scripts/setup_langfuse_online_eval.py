#!/usr/bin/env python3
"""Create the Langfuse task-completion evaluator and online rule.

Usage:
    python scripts/setup_langfuse_online_eval.py --dry-run
    python scripts/setup_langfuse_online_eval.py
    python scripts/setup_langfuse_online_eval.py --update

Credentials are read from the project .env or the shell:
    LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL

The evaluator targets the root observation named ``agent-run`` and scores
whether the user request was satisfied.  Re-running without ``--update`` is
idempotent and leaves existing resources untouched.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from src.langfuse_eval import (
    LangfuseEvalClient,
    build_task_completion_code_evaluator,
    build_task_completion_evaluator,
    build_task_completion_rule,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision Langfuse online evaluation for the agent."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print request payloads without calling Langfuse.",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Update an existing evaluator definition and rule.",
    )
    parser.add_argument(
        "--evaluator",
        choices=("code", "llm"),
        default="code",
        help="Use the deterministic code evaluator (default) or an LLM judge.",
    )
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    base_url = (
        os.getenv("LANGFUSE_BASE_URL")
        or os.getenv("LANGFUSE_HOST")
        or "https://cloud.langfuse.com"
    ).strip()

    evaluator_payload = (
        build_task_completion_code_evaluator()
        if args.evaluator == "code"
        else build_task_completion_evaluator()
    )
    rule_payload = build_task_completion_rule("<evaluator-id>")
    if args.dry_run:
        print(
            json.dumps(
                {
                    "base_url": base_url,
                    "evaluator": evaluator_payload,
                    "rule": rule_payload,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if not public_key or not secret_key:
        print(
            "Missing Langfuse credentials.\n"
            "Add these to .env (do not paste keys into chat):\n"
            "  LANGFUSE_PUBLIC_KEY=pk-lf-...\n"
            "  LANGFUSE_SECRET_KEY=sk-lf-...\n"
            "  LANGFUSE_BASE_URL=https://cloud.langfuse.com\n"
            "Then re-run this script.",
            file=sys.stderr,
        )
        return 2

    with LangfuseEvalClient(
        public_key=public_key,
        secret_key=secret_key,
        base_url=base_url,
    ) as client:
        if args.evaluator == "code":
            result = client.ensure_code_task_completion(update=args.update)
        else:
            result = client.ensure_task_completion(update=args.update)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(
        "\nOpen the evaluator in Langfuse and verify that its rule matches "
        "root observations with metadata.feature='coding-agent'."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
