# Langfuse 在线评估

本项目已接入 Langfuse Python SDK v5，用于追踪一次 Agent run，并在这条 run 上记录可解释的质量信号。在线评估由两层组成：

1. **应用侧确定性分数**：不需要 LLM 评委，随每个 run 自动写入。
2. **Langfuse 在线 evaluator**：默认使用确定性 code evaluator（`task-completion-code`），不需要 LLM connection；如果项目有支持 structured output 的模型，可切到可选的 `task-completion` LLM judge。

## 已安装的 Skill

Langfuse Agent Skill 已安装到：

```text
~/.codex/skills/langfuse
```

来源：<https://github.com/langfuse/skills>（`skills/langfuse`，v1.12.0）。

当前会话启动后才安装，所以本会话的 skill 目录没有热加载；实现时直接读取了已安装的 `SKILL.md` 与 `references/`。重启会话后即可通过 skill 名称直接加载。

## 追踪结构

一次用户请求 = 一个 `agent-run` trace：

```text
agent-run (agent)
├── planner (generation)
├── goal-evaluator (generation)
├── agent-model-call (generation)
├── tool-read_file (tool)
├── tool-bash (tool)
├── subagent (agent)
│   ├── subagent-model-call (generation)
│   └── tool-* (tool)
└── teammate-* (agent, 线程内独立 trace)
```

- 根 observation 名称固定为 `agent-run`，是在线 evaluator 的匹配目标。
- 每次模型调用都是 `generation`，带模型名、输入消息、输出、token usage。
- 工具调用是 `tool` observation，嵌套在 `agent-run` 或子 Agent 下。
- `session_id`、`run_id`、`plan_mode`、`plan_goal` 写入 trace/observation 属性。
- 后台 bash 任务使用独立的手动 `tool-bash-background` observation；它由启动它的 run 创建，完成后再结束。

## 确定性分数

这些分数由 `src/observability.py` 在 run 结束时写入根 observation：

| 分数 | 类型 | 含义 |
| --- | --- | --- |
| `run_status` | CATEGORICAL | `achieved / failed / error / waiting / limit / unknown` |
| `tool_error_rate` | NUMERIC | 失败工具调用 / 总工具调用 |
| `permission_denials` | NUMERIC | 被权限层拒绝的工具调用数 |
| `goal_blocks` | NUMERIC | Goal evaluator 判定未完成、要求继续的轮数 |
| `tool_calls` | NUMERIC | 工具调用总数 |
| `run_tokens` | NUMERIC | run 累计 token |
| `run_duration_ms` | NUMERIC | run 墙钟耗时 |
| `user_feedback` | BOOLEAN / NUMERIC | CLI `/feedback` 写入的显式反馈 |

确定性分数不依赖 LLM，也不会产生额外模型成本，适合做告警和筛选。

## 在线 evaluator

Provision 脚本默认创建：

- Evaluator：`task-completion-code`
  - 类型：`code`（Python）
  - 输出：`task_completion`（NUMERIC 0–1）、`answer_present`（BOOLEAN）、`tool_health`（NUMERIC 0–1）
  - 读取根 observation 的 `output` 和 `metadata`，按 `run_status`、最终回答是否存在、`tool_error_rate` 计算。
- Evaluation rule：`agent-run-task-completion`
  - 只匹配根 observation 且 `metadata.feature = coding-agent`
  - 采样率 1.0
  - 自动使用 evaluator 最新版本

这条链路不需要 LLM connection。当前项目已创建：

- Evaluator：`task-completion-code`（已 active）
  `https://us.cloud.langfuse.com/project/cmwedaffv05v0ad0j5cyfntzt/evals/cmwewe63v00foad0ergeyzmkl`
- Rule：`agent-run-task-completion`（enabled，ID `cmwew6vri00fmad0n2wtz3qqr`）
- 验证 trace：`https://us.cloud.langfuse.com/project/cmwedaffv05v0ad0j5cyfntzt/traces/b306eaa9f5681433961ac462aa15ec1c`

该 trace 来自真实 `agent_loop` 路径（离线假模型），包含 `agent-run`、两次 `agent-model-call` generation 和 `tool-read_file`；Langfuse 已自动写回 `task_completion=1`、`answer_present=true`、`tool_health=1`，同时有 7 个应用侧确定性分数。

### 可选 LLM judge

如果项目有支持 structured output 的模型，可以额外运行：

```bash
python scripts/setup_langfuse_online_eval.py --evaluator llm
```

它会创建 `task-completion` LLM-as-a-Judge evaluator，使用 `input`、`output`、`metadata` 三个变量。注意：DeepSeek 的 `deepseek-flash` / `deepseek-v4-pro` 当前不支持 Langfuse judge 需要的 `json_schema` structured output，因此不能作为 judge；OpenAI/Anthropic 官方或其他支持 structured output 的 provider 才可用。

确定性 code evaluator 和 LLM judge 可以同时存在；需要切换 rule 时运行 `--update`。

### 指标选择理由

| 优先级 | 指标 | 证据 | 决策 | 可测性 |
| --- | --- | --- | --- | --- |
| P0 | 任务完成度 `task_completion` | run_status + 最终回答是否存在 + tool_error_rate | 快速定位失败/空回答/工具异常 | code evaluator，0–1 |
| P0 | `run_status` | SQLite run 记录 | 快速定位失败/阻塞 | 确定性 |
| P1 | `tool_error_rate` | 工具执行结果 | 发现工具或环境问题 | 确定性 |
| P1 | `permission_denials` | 权限层返回 | 发现策略过严或危险尝试 | 确定性 |
| P1 | `goal_blocks` | Goal evaluator 事件 | 发现反复未完成/循环 | 确定性 |
| P2 | `task_completion` LLM judge | 用户请求 + 最终回答 + metadata | 需要语义判断时补充 | 需要 structured-output 模型 |
| P2 | `user_feedback` | CLI 显式反馈 | 校准 judge 与真实用户体验 | 用户输入 |

## 配置与运行

### 1. 创建 Langfuse 项目并写入凭据

在 Langfuse 项目 **Settings → API Keys** 创建 key pair，然后写入项目 `.env`（不要把密钥粘贴到聊天或提交到仓库）：

```dotenv
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_TRACING_ENABLED=1
LANGFUSE_ENVIRONMENT=development
```

自托管时把 `LANGFUSE_BASE_URL` 换成实例 URL。美国区 Cloud 使用 `https://us.cloud.langfuse.com`。

### 2. 安装依赖

```bash
python -m pip install -r requirements.txt
```

### 3. 先检查 payload

```bash
python scripts/setup_langfuse_online_eval.py --dry-run
```

### 4. 创建 evaluator 和 rule

默认创建确定性 code evaluator：

```bash
python scripts/setup_langfuse_online_eval.py
```

脚本按名称幂等：已存在则保留，不重复创建。需要更新 evaluator 定义或切换 rule 绑定时：

```bash
python scripts/setup_langfuse_online_eval.py --update
```

如需 LLM judge：

```bash
python scripts/setup_langfuse_online_eval.py --evaluator llm --update
```

LLM judge 需要 Langfuse 项目里有支持 structured output 的 LLM connection。Code evaluator 不需要。

### 5. 运行应用并查看

```bash
python main.py
```

- `/langfuse`：查看 tracing 是否启用、最近一次 trace URL。
- `/feedback good` / `/feedback bad`：给最近一次 run 写 `user_feedback`。
- `/feedback 0.8 回答基本正确，但漏了一个测试`：写数值反馈和 comment。

在 Langfuse 的 **Tracing** 页面打开 `agent-run`，应能看到树形结构；在 **Evaluators** 页面查看 `task-completion-code` 和 `agent-run-task-completion` rule 的执行结果。

## 数据与隐私

启用 tracing 后，以下内容会发送到 Langfuse：

- 用户请求与对话消息；
- 模型输入/输出、模型名与 token usage；
- 工具名、工具参数、工具输出摘要；
- run 状态、goal reason、计划、变更文件路径。

如果工作区包含敏感信息，请在 Langfuse 项目侧配置 masking / 访问控制，或设置 `LANGFUSE_TRACING_ENABLED=0` 关闭 tracing。离线测试和本地无凭据运行不会发起 Langfuse 网络请求。

## 验证

```bash
python -m compileall -q src main.py tests scripts
python -m unittest discover -s tests -v
```

新增测试覆盖：

- 无凭据时所有 tracing/评分 helper 安全 no-op；
- run summary 的确定性分数与 metadata；
- evaluator/rule payload 的目标 observation 和字段映射。

本轮已用真实 Langfuse 项目验证：发送一条带 `metadata.feature=coding-agent` 的 `agent-run` trace 后，rule 自动触发 code evaluator，并写回 `task_completion=1`、`answer_present=true`、`tool_health=1`；同一 trace 上还有 7 个应用侧确定性分数。
