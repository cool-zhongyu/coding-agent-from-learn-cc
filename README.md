# Stateful CC Agent

在原有工具循环上接入 Goal → Plan → Recall → Execute → Checkpoint → Verify → Learn。

## 启动

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

保留已有 `.env`；新增可选配置见 `.env.example`。不需要 Redis 或 Qdrant 就能运行。
`/runs` 列出历史运行；`/resume run_<id>` 或 `main.py --resume run_<id>` 恢复 checkpoint。
Ctrl+C 保存中断状态。恢复不自动重放中断的工具调用：模型必须先检查真实状态。
后台进程和 teammate 线程不会跨进程重启恢复。

## 已接入

- 每个新用户请求规划一次，返回 `direct / workflow / team`、目标和检查。
  普通任务走原有工具循环；`review-changes` 是已注册的纯文本审查 workflow；
  只有提供实际代码片段时才由 Planner 自动运行它。其他 debug/research 流程继续走 direct，
  可在 `WORKFLOWS` 中增加可信 Python 函数。不会执行模型返回的 Python 脚本。
  Team 模式限 2–4 个工作流，建立 task board 任务并调用现有 persistent teammate。
- Goal 先执行计划中的命令检查；开放任务再调用独立 evaluator。
  检查走同一权限策略。未通过会反馈给 worker；连续阻止上限、评估出错和失败分别记录，
  不会标成成功。后台任务运行时保存 waiting 状态，结果到达后续跑，不空转请求模型。
- Workflow 提供 `agent / parallel / pipeline / workflow`，并发上限 8、调用上限 1000、
  一层嵌套、schema 校验和一次重试。预算统计实际返回 usage，包含重试和恢复前用量。
  预算不是服务端硬限额，已发出的并发请求仍可能超支；超支时运行标成失败。
- Workflow 工具返回 `wf_<id>`；再次调用 `workflow(name, resume_from_run_id=...)` 恢复。
  缓存默认关闭，内置纯文本步骤显式开启。key 包含完整 prompt、schema、label、模型和版本。
  若扩展成读取仓库的步骤，应提供包含未提交改动的 `repository_state` 内容指纹，或禁用缓存。
  同一 run 使用 OS 文件锁防止重复执行；步骤失败时先等待并发兄弟步骤结束再释放锁。
- SQLite 是 sessions/runs/tasks/dependencies/working states/memories/versions/workflow journal/
  skill candidates/events/cron 的持久化来源。旧 `.tasks/*.json`、`.memory/*.md`、
  `.scheduled_tasks.json` 在首次访问时导入一次，旧文件保留；不做破坏性删除。
- checkpoint 在工具轮次、工具执行前和压缩前保存；包含计划、消息、修改文件、
  已执行动作、失败尝试、重要输出、下一步及当前 run 的独立证据。
  原始 transcript 在结果被 micro-compaction 替换前保存，正常路径不使用 snip。
- 工具执行前由 dispatcher 直接调用 `authorize_tool` 做权限判定，统一覆盖主 Agent、
  teammate、subagent 和后台 shell。
- 可选接入 Langfuse Python SDK v5：每个 run 生成 `agent-run` 根 observation，
  模型调用、工具、子 Agent 与后台任务形成嵌套 observation；run 结束时写入
  `run_status`、`tool_error_rate`、`permission_denials`、`goal_blocks` 等确定性分数。
  未配置凭据时 tracing 完全 no-op，不影响离线运行。
- Context 管理集中在 `src/context.py` 的 `ContextManager`；同时按 Codex 的
  `AGENTS.md` 层级规则加载项目根目录和当前任务目录下的说明，并在 system prompt 中提供。
- Memory/Skill 只在新 turn 或显式 refresh 时检索，取消 LLM memory selector 和 memory 自我加载。
  `.memory/MEMORY.md` 启动时生成，是 SQLite 的人类可读导出；临时工作状态存入 working_states。
- 成功后评估可复用技巧。同一过程正文在两个不同、已验证成功的 run 中出现才生成
  `skills/<name>/SKILL.md`。重复处理同一 run 不计数；不会覆盖已有人工 Skill。
- Prompt cache 可通过 `AGENT_PROMPT_CACHE=1` 开启；默认关闭以兼容非官方服务商。
  使用 system 和对话最后一个内容块的 cache_control，使多轮对话成为可复用前缀；
  开启后跳过 micro-compaction 对历史工具结果的原地改写，避免破坏缓存前缀。
- Windows 使用 PowerShell 执行 shell 工具，文件锁兼容 Windows/POSIX。


## Langfuse 在线评估

在 `.env` 中配置 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 和
`LANGFUSE_BASE_URL` 后，运行：

```powershell
.\venv\Scripts\python.exe scripts\setup_langfuse_online_eval.py
```

脚本默认创建确定性 code evaluator `task-completion-code`，以及只匹配根
`agent-run` observation（`metadata.feature=coding-agent`）的 rule；先加 `--dry-run` 可以只查看 payload。
需要 LLM judge 时使用 `--evaluator llm`。
CLI 支持 `/langfuse` 查看 tracing 状态和最近 trace URL，以及
`/feedback good|bad|0..1 [comment]` 写入用户反馈。

细节、指标表、隐私边界和验证方式见
[docs/langfuse-online-eval.md](docs/langfuse-online-eval.md)。

## 权限

`AGENT_PERMISSION_MODE` 默认 `strict`：常见读取/测试命令允许，未知或危险命令询问，
严重危险命令拒绝。`trusted_workspace` 对能确定目标位于 WORKDIR 的文件操作直接放行，
包括读取、创建、写入、删除、复制、移动、修改权限、工作区内重定向，以及常见本地 Git/构建命令。
主 Agent、teammate 和完成检查均按实际执行目录解析相对路径。`sudo`、`shutdown`、`mkfs`、
`diskpart` 等硬禁令仍优先拒绝。越界路径（包括解析后越界的符号链接）、远程 Git 操作、
无法静态判断目标的脚本/解释器、动态 shell 展开仍需确认；这不是只检查起始 cwd 的无条件放行。
`unattended` 永不提问，需要确认的操作直接拒绝。线程中的普通安全命令可执行。
这是命令审批策略，不是 OS 沙箱；测试本身仍可能执行项目代码。

只读组合命令会逐段判断：允许工作区内的 `cd ... &&`、只读命令之间的 `|`/`&&`，
以及输出到 `/dev/null` 的 `>`/`2>`。例如在 `WORKDIR=/mnt/d/youting` 时，
`cd /mnt/d/youting && cat .agent/STATE.md 2>/dev/null | head -50` 在三种模式下均直接允许。
越出工作区的 `cd`、写入实际文件、管道接 shell/解释器、命令替换和未支持的 shell 语法
仍需确认（无人值守时拒绝）。这只是权限判断，不会转换不同系统的 shell 语法。

## 可选 Qdrant

配置 `QDRANT_URL`、完整 `EMBEDDING_URL`、`EMBEDDING_MODEL` 和必要的 API key 后启用。
Embedding endpoint 使用 `{model,input}` 请求及 `data[0].embedding` 响应格式。
配置的 embedding 服务会接收用于索引/查询的记忆、Skill 和任务文本。
服务不可用时退回本地关键词检索，不阻断执行；错误保存在 `VectorStore.last_error`。
没有配置时不会尝试连接、安装服务或生成伪 embedding。

索引集合为 `memories / skills / transcripts`，Qdrant 只返回 source ID，正文再从 SQLite
或 Skill 文件读取。当前 transcript 每 run 索引最近 24,000 字符；完整证据仍保存在本地。
旧记忆在首次启用时补建索引；旧 transcript 不会批量上传。
接口依据 [Qdrant Collection API](https://api.qdrant.tech/api-reference/collections/create-collection)
和 [Query API](https://api.qdrant.tech/api-reference/search/query-points)。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q src main.py
```

测试使用临时工作目录和假模型响应，不调用真实模型、Qdrant 或 embedding 服务。
权限回归测试还覆盖 trusted_workspace 的本地文件操作、跨目录路径、原生绝对路径、实际执行目录和硬禁令。
Windows 无创建符号链接权限时，该项文件系统测试会明确跳过。
测试覆盖只读组合命令、真实工具循环、Goal 分支、后台等待、恢复时未知工具结果、
Workflow 缓存/预算/并发失败、权限、SQLite 持久化、Skill 重复证据、单 turn 检索和 Qdrant 请求契约。
Redis/分布式 worker 按原方案留待后续阶段；当前仍为单进程、多线程运行时。
