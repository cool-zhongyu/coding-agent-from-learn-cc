# Context 与工具权限机制说明

## 目标

项目把上下文管理和工具授权整理为两条清晰的主链路：

1. **Context 生命周期**：集中管理 `AGENTS.md` 层级加载、上下文预算、工具结果瘦身、消息截断和模型摘要压缩。
2. **工具授权**：在 dispatcher 调用工具处理器之前，直接通过 `authorize_tool` 做一次权限判定；不再维护生命周期事件回调或外部策略配置。

修改后保留原有调用接口，不影响主循环和 teammate、subagent、后台任务的基本行为；测试保持离线，不调用真实模型、embedding 或 Qdrant。

## 修改文件

### `src/context.py`

核心改动：

- 新增 `ContextConfig`，把上下文预算相关阈值集中为可替换配置。
- 使用 `AGENTS.md` 分层加载：项目根目录先加载，任务目录越靠近当前工作目录越靠后，后者优先级更高。
- `ContextManager` 负责完整预处理流程，而不是让调用方自行组合多个函数。
- 压缩顺序固定为：

  1. 检查点保存。
  2. 大工具结果持久化到 `.task_outputs/tool-results/`，只在上下文里保留预览。
  3. 旧工具结果 micro-compaction。
  4. 调用配置服务商的 `messages.count_tokens`，统计消息、系统提示和工具定义的输入 token。默认窗口为 1,000,000 token，预留 16,000 输出和 4,000 计数余量后，输入超过 980,000 token 时写入 transcript 并调用模型摘要。
- 关闭 prompt cache 时，只有在真正执行 micro-compaction 或摘要压缩前保存原始 transcript。
- 开启 prompt cache 时继续跳过 micro-compaction，避免修改已缓存的历史前缀。
- 计数端点不可用时，仍处理工具输出，并在模型报告上下文过长时应急压缩。
- 摘要提示词明确把对话视为不可信数据，仅保留当前请求为 authoritative request。

### `src/permissions.py`

核心改动：

- `PermissionPolicy.classify` 继续把 shell 命令分为 `allow / ask / deny`。
- `PermissionPolicy.check` 把分类变成最终结果；`unattended` 或非主线程不会等待交互输入。
- 新增 `authorize_tool(block)`，在工具执行前统一检查：

  - shell 命令策略；
  - `read_file` / `write_file` / `edit_file` 的路径是否位于 `WORKDIR`；

- `authorize_tool` 返回 `None` 表示允许，返回字符串表示拒绝原因。
- 交互式批准按“cwd + 命令”缓存，同一进程内不会重复询问同一操作。

### `src/agentLoop.py`

- 主循环在调用工具处理器前执行 `authorize_tool`，被拒绝时把拒绝原因作为 `tool_result` 返回给模型。
- 移除了工具执行前后的事件回调、输出改写和输出抑制逻辑；工具输出按原样记录和回传。
- Goal 验收仍在模型停止调用工具后执行；连续阻止、评估出错和失败分别记录。

### `src/team.py`

- teammate 和 subagent 的工具调用使用同一个 `authorize_tool`，并按实际任务目录解析相对路径。
- 移除了工具执行前后的事件回调逻辑。

### `src/task.py`

- 后台 bash 任务完成后直接记录结果，不再经过额外的事件回调处理。

### `src/cli.py`

- CLI 在提交用户请求前不再运行事件回调；空输入、退出命令和 `/resume` 行为保持不变。

### `src/async_utils.py`、`src/goal.py`、`src/workflow.py`

- 新增 `run_in_thread`，使用显式线程池执行阻塞调用。
- 替代 `asyncio.to_thread`，避免当前 Python 环境下默认 executor 在 `asyncio.run` 收尾时长时间等待的问题。
- 该改动使 Goal 检查、evaluator 和 workflow 的离线测试可以稳定退出。

## Context 压缩流程

### AGENTS.md 加载

`collect_agents_instruction_paths` 会：

1. 始终优先收集 `PROJECT_ROOT/AGENTS.md`。
2. 从项目根目录向当前任务目录逐级收集 `AGENTS.md`。
3. 对 `PROJECT_ROOT` 与 `WORKDIR` 两个基准去重。
4. 超过文件数或字符上限时停止并截断。

最终 system prompt 中，靠近当前目录的说明排在后面，因此可以覆盖更上层的默认约定。

### 压缩阶段

| 阶段 | 作用 | 是否破坏原始 transcript |
| --- | --- | --- |
| `tool_result_budget` | 把超大工具输出写入磁盘，只保留路径和预览 | 是 |
| `micro_compact` | 把已消费的旧工具结果替换为“可重跑”提示 | 是 |
| `compact_history` | 保存 transcript 后摘要历史，服务端提示超限时分段摘要并合并 | 是 |
| `reactive_compact` | prompt-too-long 时保留最近尾部，压缩更早内容 | 是 |

### Prompt cache 兼容

`AGENT_PROMPT_CACHE=1` 开启时：

- system prompt 使用 `cache_control`。
- 最后一个消息块使用 `cache_control`，使多轮对话成为可复用前缀。
- `prepare_messages` 跳过 micro-compaction，避免改写已经缓存的工具结果。

关闭缓存时，原始 transcript 会在 micro-compaction 或摘要压缩前保存。

## 可信边界

- 摘要提示词把历史对话视为不可信数据。
- 压缩后的消息中，`Authoritative request` 是唯一权威任务来源，`Reference state` 只作为状态参考，不能授权新的副作用。
- 恢复 run 时，未知工具结果会先标记为 outcome unknown，模型必须检查实际状态，避免静默重放副作用。
- 批准缓存只在当前进程内有效，且按精确 key 记忆；重启 agent 后会重新评估，不会把旧批准静默带过进程边界。
- 权限策略是命令审批，不是 OS 沙箱；通过检查的脚本、构建或测试仍按当前进程的系统权限运行。

## 验证

在项目根目录执行：

```bash
python -m compileall -q src main.py tests
python -m unittest discover -s tests -v
```

当前测试结果：

```text
Ran 49 tests in ...s
OK
```

测试覆盖：

- 权限分类、只读组合命令、路径越界。
- 交互式批准缓存。
- 主循环、Goal 分支、后台等待和恢复时未知工具结果。
- 上下文压缩、AGENTS.md 加载和 prompt cache。
- Workflow 缓存、预算与并发失败。
