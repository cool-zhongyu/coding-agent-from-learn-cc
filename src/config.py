import ast
import atexit
from src import file_lock as fcntl
import importlib.util
import json
import os
import random
import re
import secrets
import signal
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional
from anthropic import Anthropic 
from dotenv import load_dotenv


try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ENV_FILE = PROJECT_ROOT / ".env"

load_dotenv(ENV_FILE)

WORKDIR = Path(
    os.getenv("WORKDIR", PROJECT_ROOT)
).resolve()
MODEL = os.getenv("LLM_MODEL_ID", "claude-sonnet-4-5")
ENABLE_PROMPT_CACHE = os.getenv("AGENT_PROMPT_CACHE", "0") == "1"
PRIMARY_MODEL = MODEL
FALLBACK_MODEL = os.getenv("LLM_MODEL_ID")

SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"

DEFAULT_MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 16000
MAX_RETRIES = 3
MAX_CONSECUTIVE_529 = 2
MAX_RECOVERY_RETRIES = 2
BASE_DELAY_MS = 500
CONTEXT_LIMIT = 50000
KEEP_RECENT_TOOL_RESULTS = 3
PERSIST_THRESHOLD = 30000
CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."
PROMPT = "\033[36mcc >> \033[0m"
CLI_ACTIVE = False

class ConsoleBroker:
    """Serialize normal prompts and worker permission questions on one stdin."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reader = None

    def ask(self, prompt: str) -> str:
        with self._lock:
            return (self.reader or input)(prompt)


CONSOLE = ConsoleBroker()


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




class myLLM:
    """
    为 Hello Agents 定制的 Anthropic LLM 客户端。

    - 使用 Anthropic Messages API
    - 默认流式输出
    - 支持官方 Anthropic API
    - 支持 Anthropic-compatible 的自定义 base_url
    - 参数优先级：显式参数 > 环境变量 > 默认值
    """

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
    ):
        # -------------------------
        # 1. 读取配置
        # -------------------------

        self.model = (
            model
            or os.getenv("LLM_MODEL_ID")
            or "claude-sonnet-4-5"
        )

        self.api_key = (
            api_key
            or os.getenv("ANTHROPIC_API_KEY")
            or os.getenv("LLM_API_KEY")
        )

        self.base_url = (
            base_url
            or os.getenv("ANTHROPIC_BASE_URL")
            or os.getenv("LLM_BASE_URL")
        )

        self.temperature = temperature

        self.max_tokens = (
            max_tokens
            or int(os.getenv("LLM_MAX_TOKENS", "4096"))
        )

        self.timeout = (
            timeout
            or int(os.getenv("LLM_TIMEOUT", "60"))
        )

        if not self.api_key:
            raise ValueError(
                "API key 未提供。请传入 api_key,"
                "或设置 ANTHROPIC_API_KEY / LLM_API_KEY。"
            )

        # -------------------------
        # 2. 创建 Anthropic Client
        # -------------------------

        client_kwargs = {
            "api_key": self.api_key,
            "timeout": self.timeout,
        }

        # 没有 base_url 时直接使用 Anthropic 官方 API
        if self.base_url:
            client_kwargs["base_url"] = self.base_url

        self.client = Anthropic(**client_kwargs)

    @property
    def messages(self):
        return self.client.messages


    def _prepare_messages(
        self,
        messages: List[Dict[str, str]],
    ):
        """
        将 OpenAI 风格的 messages 转换成 Anthropic Messages API 格式。

        OpenAI:
            [
                {"role": "system", "content": "..."},
                {"role": "user", "content": "..."},
            ]

        Anthropic:
            system="..."

            messages=[
                {"role": "user", "content": "..."}
            ]
        """

        system_messages = []
        anthropic_messages = []

        for message in messages:
            role = message.get("role")
            content = message.get("content", "")

            if role == "system":
                system_messages.append(content)

            elif role in ("user", "assistant"):
                anthropic_messages.append({
                    "role": role,
                    "content": content,
                })

            else:
                raise ValueError(
                    f"Unsupported message role: {role}"
                )

        system_prompt = "\n\n".join(system_messages)

        return system_prompt, anthropic_messages

    def think(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Optional[str]:
        """
        调用 Anthropic Messages API。

        默认进行流式输出，同时收集完整文本并返回。
        """

        temperature = (
            self.temperature
            if temperature is None
            else temperature
        )

        max_tokens = (
            self.max_tokens
            if max_tokens is None
            else max_tokens
        )

        system_prompt, anthropic_messages = self._prepare_messages(
            messages
        )

        print(f"🧠 正在调用 {self.model} 模型...")

        try:
            request_kwargs = {
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": anthropic_messages,
            }

            # Anthropic 的 system prompt 是顶层参数
            if system_prompt:
                request_kwargs["system"] = system_prompt

            collected_content = []

            # -------------------------
            # 流式调用
            # -------------------------

            with self.client.messages.stream(
                **request_kwargs
            ) as stream:

                print("✅ 大语言模型响应成功:")

                for text in stream.text_stream:
                    print(text, end="", flush=True)
                    collected_content.append(text)

            print()

            return "".join(collected_content)

        except Exception as e:
            print(f"❌ 调用 Anthropic API 时发生错误: {e}")
            return None

client=myLLM()
