# SellerForge 内部代码结构与流转分析

本文面向读源码、改 Runtime、排查调用链。产品定位见 [PROJECT_INTRODUCTION.md](PROJECT_INTRODUCTION.md)；记忆体系见 [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md)；工程约束见 [ENGINEERING_RULES.md](ENGINEERING_RULES.md)；前端规则见 [FRONTEND_RULES.md](FRONTEND_RULES.md)。

目录：

- [1. 先读结论](#1-先读结论)
- [2. 仓库地图](#2-仓库地图)
- [3. 进程拓扑与配置](#3-进程拓扑与配置)
- [4. 启动装配](#4-启动装配)
- [5. 分层架构](#5-分层架构)
- [6. HTTP 入口与身份](#6-http-入口与身份)
- [7. 对话主调用栈](#7-对话主调用栈)
- [8. LangGraph 与 RuntimeState](#8-langgraph-与-runtimestate)
- [9. `_model_node` 内部](#9-_model_node-内部)
- [10. `_tools_node` 内部](#10-_tools_node-内部)
- [11. 角色虚拟化](#11-角色虚拟化)
- [12. Tool / MCP / Connector](#12-tool--mcp--connector)
- [13. 子 Agent 队列](#13-子-agent-队列)
- [14. 权限与数据范围](#14-权限与数据范围)
- [15. 存储与物化](#15-存储与物化)
- [16. 直连 BI](#16-直连-bi)
- [17. 模型路由](#17-模型路由)
- [18. 记忆、知识、网页](#18-记忆知识网页)
- [19. 审批、中断、恢复](#19-审批中断恢复)
- [20. 前端](#20-前端)
- [21. API 目录](#21-api-目录)
- [22. Session 事件与 SSE](#22-session-事件与-sse)
- [23. 读码顺序与符号表](#23-读码顺序与符号表)
- [24. 测试、迁移、边界](#24-测试迁移边界)
- [25. 查数端到端对照](#25-查数端到端对照)
- [26. 记忆检索、冲突与维护](#26-记忆检索冲突与维护)
- [27. 沙箱、Skill、附件](#27-沙箱skill附件)
- [28. 查询 SQL / OpenAPI 如何落地](#28-查询-sql--openapi-如何落地)
- [29. 脱敏、观测、成本](#29-脱敏观测成本)
- [30. Catalog 如何拼出来](#30-catalog-如何拼出来)
- [31. 前端 SSE 与消息回放](#31-前端-sse-与消息回放)
- [32. 账户与 JWT](#32-账户与-jwt)
- [33. 知识：GraphRAG 网关 vs 向量空间](#33-知识graphrag-网关-vs-向量空间)
- [34. 本地数据文件与排障](#34-本地数据文件与排障)

---

## 1. 先读结论

SellerForge 的「多智能体」是 **同一套 Agent Runtime 上的角色虚拟化**，不是多张 LangGraph 图，也不是每个角色一个进程。

| 看起来像 | 实际是 |
|---|---|
| Coordinator / Analyst / Specialist | 同一份 `AgentRuntime`、同一张 `model↔tools` 图；差在 `agent_id`、prompt、Tool 白名单、Session |
| 并行多个 Analyst | `SubagentManager.submit` 多次入队；专业模式同一父 Session 活跃任务 ≤ 3 |
| MCP Agent | `MCPClientManager` 把远程工具登记进 `ToolRegistry`，名字 `mcp__{server}__{tool}` |
| 子 Agent 工作流 | 嵌套调用同一个 `AgentRuntime.run()` |
| 直连 BI Agent | `workflows/*/agent.py` 只生成 QueryPlan，执行仍走 `ToolExecutor` |

LangGraph 只负责 function-calling 循环。RBAC、连接器、审批、队列、Result Store、审计都在图外面。

---

## 2. 仓库地图

Python 包根是 `src/ops_agent/`（`pyproject.toml` 里 `where = ["src"]`）。

```text
multi_agent_ops_platform/
├── frontend/                      index.html + app.js + styles.css + guide.js
├── src/ops_agent/
│   ├── api/app.py                 FastAPI 全部路由（约 4000+ 行，单文件控制面）
│   ├── config.py                  Settings：.env + data/runtime_overrides.json
│   ├── persistence.py             JSON 原子写（模型/连接公开配置）
│   ├── agent_roles.py             角色常量、白名单、system prompt
│   ├── agent_registry.py          AgentDefinition（kind=runtime|role|hybrid|workflow）
│   ├── agent_skill_store.py       Agent + Skill 种子与持久化
│   ├── access_control.py          ops_access_* 表；EffectiveAccess
│   ├── accounts.py                注册/登录/JWT/scrypt
│   ├── connections.py             ConnectionDefinition + LocalSecretStore
│   ├── connector_control_plane.py Tool catalog / binding 的 PostgreSQL
│   ├── model_registry.py          页面配置的多模型（Key 与定义分离）
│   ├── model_gateway.py           直连 BI 用的轻量 plan 模型网关
│   ├── knowledge_spaces.py        知识空间注册表
│   ├── knowledge_gateway.py       检索已有 Collection
│   ├── vector_connections.py      记忆用的向量连接解析
│   ├── mysql_connection.py        分析库 MySQL 客户端
│   ├── source_privacy.py          对外文本/JSON 脱敏
│   ├── infrastructure/platform_store.py   ops_runs + ops_audit_events
│   ├── integrations/{kingdee,lingxing,dingtalk,tavily}/
│   ├── workflows/{amazon_finance,lingxing_profit,profit_report,kingdee_cloud}/
│   ├── runtime/                   见下表
│   ├── migrations/                Alembic CLI 包装
│   └── evals/
├── alembic/versions/              0001～0005
├── skills/                        SKILL.md，load_skill 按需注入
├── config/mcp_servers.json
├── tests/
└── scripts/                       mock 数据、备份、冒烟
```

### 2.1 `runtime/` 文件

| 文件 | 关键类型 / 函数 |
|---|---|
| `stack.py` | `RuntimeStack`、`open_runtime_stack()` |
| `agent_loop.py` | `AgentRuntime`、`RuntimeState`、`SessionLiveHub` |
| `domain.py` | `RuntimeAgentRequest/Response`、`ToolCall`、`ModelTurn`、`ToolResult` |
| `tools.py` | `ToolDefinition`、`ToolRegistry`、`ToolExecutor`、两个 Guard |
| `agent_tool_policy.py` | `resolve_agent_tool_allowlist`、`coordinator_delegation_prompt` |
| `model_router.py` | `ModelRouter`、各 Provider Adapter、`ModelConfigurationRequiredAdapter` |
| `model_errors.py` | `ModelProviderError`（带 status_code / Retry-After） |
| `qwen_adapter.py` / `deepseek_adapter.py` | 厂商差异（thinking 等） |
| `subagents.py` | `SubagentManager`、`delegate`、`execute_subagent_task` |
| `subagent_worker.py` | `SubagentQueueWorker.run_forever` |
| `governance.py` | `ToolApprovalRecord`、`SubagentTaskRecord`、claim/lease |
| `connectors.py` | `ToolBinding`、`ConnectorRuntime` |
| `mcp_client.py` | `MCPClientManager` |
| `result_store.py` | `materialize_tool_output`、`result_page` |
| `session_events.py` | `SessionEvent`（payload 会剥掉遗留 `seller_id`） |
| `memory.py` / `memory_worker.py` | 记忆 Tool、快照、候选、维护 |
| `sandbox.py` | `sandbox_read_only` / `sandbox_workspace_write` / danger-full-access |
| `skills.py` | `load_skill` |
| `attachments.py` | `LocalAttachmentStore`（`sha256:` 内容寻址） |
| `observability.py` / `tracing.py` | TurnMetric、OTel span |
| `auth.py` | Runtime 侧 Principal 辅助 |
| `amazon_finance_tool.py` 等 | 把 `workflows/*/query_tool` 登记为 Runtime Tool |

---

## 3. 进程拓扑与配置

生产见 `docker-compose.production.yml`：

```text
浏览器 ──HTTP/SSE──► api (ops-agent-api)
                       │
                       ├─ data 卷：模型 JSON、连接 JSON、附件、沙箱工作区
                       └─ POSTGRES_DSN ──► postgres:16
                                            Session / Result / Governance
                                            Access / Accounts / Memory / 队列

subagent-worker (ops-agent-subagent-worker)
  与 API 共用 DSN 和 data 卷
  claim_next_task → execute_subagent_task → AgentRuntime.run

memory-worker (ops-agent-memory-worker)
  按租户循环 MemoryService.maintenance
```

`Settings.validate_runtime()`（`config.py`）在 `create_app` lifespan 里执行。生产强制：

- `JWT_REQUIRED=true`、`JWT_SECRET` ≥ 32 字符、`JWT_ISSUER` / `JWT_AUDIENCE`
- `SUBAGENT_QUEUE_BACKEND=db`
- `APP_REPLICA_COUNT=1`（模型/连接注册表尚未进共享库）
- `POSTGRES_DSN` 必填

开发默认 `subagent_queue_backend=inline`，API 进程内线程池跑子任务。`analyst_mode` 为 `general` 或 `specialized_parallel`，可被 `data/runtime_overrides.json` 覆盖（页面改 Analyst 模式会写这份文件）。

其它关键默认：

| 配置 | 默认 | 作用 |
|---|---|---|
| `max_tool_steps` | 设置项 | 一轮最多 Tool 步数，图 recursion_limit = 该值×2+4 |
| `run_token_budget` | 约 30000 | 本回合 prompt+completion 超限则清掉 tool_calls |
| `context_tool_max_rows` / `max_chars` | 12 / 4000 | 进模型的 Tool 结果压缩 |
| `subagent_max_depth` | 3 | 防止委派递归 |
| `analyst_parallel_limit` | 3 | 专业模式硬顶 |
| `subagent_lease_seconds` | 30 | Worker 租约；过期可被别人 claim |
| `memory_snapshot_limit` / `max_chars` | 8 / 2400 | 注入 Coordinator 的记忆体积 |

CLI：`ops-agent-api`、`ops-agent-subagent-worker`、`ops-agent-memory-worker`、`ops-agent-migrate`、`ops-agent-eval`、`ops-agent-memory-eval`、`ops-agent-install-mock`。

---

## 4. 启动装配

`create_app()` 的 lifespan 顺序（`api/app.py`）：

1. `settings.validate_runtime()`
2. `create_platform_store`、`create_account_service`
3. 四个直连 BI Agent：`AmazonFinanceAgent` 等，只持有 `ModelGateway` + 空 DSN 的 `QueryTool`（真正查询不走这里的 DSN）
4. `with open_runtime_stack(settings) as stack:` 把字段挂到 `application.state`
5. `_sync_query_agents`：用 Tool catalog 同步 hybrid Agent 的启用状态
6. `stream_slots = BoundedSemaphore(agent_stream_max_concurrency)`

`open_runtime_stack()`（`runtime/stack.py`）是 API 与 Worker **共用**的装配函数，避免两套 Registry 缓存分叉。

装配顺序要注意：

1. 种子 Agent/Skill → `AgentRegistry`
2. `PostgresBindingPersistence` 从 JSON 导入绑定，种子 Tool catalog
3. `ConnectionRegistry`（公开 JSON + secrets 文件）
4. `AccessControlStore`、`ConnectorRuntime`、`ModelRegistry` → `ModelRouter`
5. 空的 `ToolRegistry` 上按序 `register_*`（记忆、四个查询、钉钉、网页、知识、Skill、沙箱）
6. `MCPClientManager.start()`：连不上且 `fail_on_startup_error=false` 时只记 status，不让 API 起不来
7. Session / Result / Metrics / Governance / Attachment
8. `AgentRuntime(..., executor=ToolExecutor(guards=[ApprovalGuard, ConnectorAccessGuard]))`
9. `SubagentManager` → **最后** `register_subagent_tool`（handler 要闭包已有 manager）

退出时 `subagent_manager.shutdown()`、`mcp_manager.stop()`。

`application.state` 上之后会用到的对象：

`settings`、`store`、`account_service`、四个 `*_agent`、`agent_registry`、`model_registry`、`connection_registry`、`connector_runtime`、`tool_bindings`、`access_control`、`result_store`、`memory_service`、`knowledge_spaces`、`knowledge_gateway`、`runtime_tool_registry`、`runtime_tool_executor`、`tool_catalog`、`session_events`、`metrics_store`、`attachment_store`、`skill_registry`、`mcp_manager`、`agent_runtime`、`runtime_governance`、`subagent_manager`、`sandbox_runner`、`stream_slots`。

---

## 5. 分层架构

```text
frontend (Vanilla JS)
        │ HTTP / SSE
api/app.py
        │ Principal + RBAC + 会话归属
        ├──────────────────┐
        ▼                  ▼
AgentRuntime.run    execute_direct_tool     ← 跳过 Coordinator
        │                  │
        ▼                  ▼
LangGraph model↔tools      │
        │                  │
        ▼                  ▼
ToolExecutor（schema → Guard → handler + timeout）
        ├─ delegate_*  → Subagent 队列 → 再 run(agent_id=analyst)
        ├─ 查询/钉钉/Tavily → ConnectorRuntime → 外部系统
        ├─ MCP → MCPClientManager.call → MCP Server
        └─ 沙箱 / Skill / 记忆 → 本地实现
```

四条正交轴：

1. **谁在说话**：`agent_id`
2. **能调哪些 Tool**：角色白名单 ∩ 模式 ∩ 连接就绪 ∩ RBAC ∩ 快照
3. **能看哪些数据**：Connection 范围 ∩ Tool 绑定范围 ∩ 委派快照
4. **结果给谁看**：全量 Result Store；模型只看摘要

---

## 6. HTTP 入口与身份

所有受保护接口先过 `principal_from_headers`。生产 `JWT_REQUIRED` 时必须 Bearer；开发可用 `X-Tenant-ID` / `X-User-ID` / `X-User-Role` 模拟。角色：`viewer` / `operator` / `approver` / `admin`。部分路由再收紧，例如审批只要 `approver`+`admin`，Dashboard 只要 `admin`。

账户（`accounts.py`）：

- 首个注册用户成为该租户 `admin`；之后关闭公开注册
- 生产首次引导要 `X-Bootstrap-Token` = `ACCOUNT_BOOTSTRAP_TOKEN`
- 密码 scrypt；失败锁定；Access Token 短、Refresh 轮换
- 改密/重置会撤销旧会话

对话入口伪代码：

```text
POST /v1/agent/query
  principal = principal_from_headers(...)
  decision = access_control.effective_access(tenant, user, role)
  if configured and not user_enabled → 403 denial_detail()
  if payload.session_id → owned_session_events()  # tenant+user 同时匹配
  result = agent_runtime.run(
      payload,
      tenant_id, user_id, role,
      token_budget=settings.run_token_budget,
      allowed_tools=set(decision.allowed_tools) or None,  # None = 开放兼容
  )
  audit_agent_result(...)
  return RuntimeAgentResponse
```

请求体 `RuntimeAgentRequest`：

| 字段 | 约束 |
|---|---|
| `question` | 2～4000 字 |
| `session_id` | 可选；续聊必填且必须属于当前用户 |
| `model_id` | 可选；否则用会话已选或默认模型 |
| `attachment_ids` | 最多 20，值为 `sha256:...` |
| `memory_mode` | `default` / `read_only` / `disabled` |

响应 `RuntimeAgentResponse.status`：`completed` | `waiting_approval` | `cancelled` | `interrupted` | `budget_exceeded` | `timed_out` | `failed`。

流式 `/v1/agent/query/stream` 同样调 `run(..., on_event=emit, interruption_is_resumable=True)`，后台线程把事件写成 SSE。并发受 `stream_slots` 限制，满了 429。

HTTP 中间件对所有 `/v1/` 的 POST/PUT/PATCH/DELETE 写 `ops_audit_events`（`api.{method}`），JWT 优先于伪造头。

---

## 7. 对话主调用栈

```text
query_agent_runtime
  → AgentRuntime.run
       校验 session 归属（owner == user_id）
       agent_id 默认 COORDINATOR_AGENT_ID
       Agent 必须 enabled
       allowed_tools &= resolve_agent_tool_allowlist(agent)
       ContextVar 挂上 on_event
       span("agent.turn")
       → _run_turn
            session_id = 请求值或 uuid4
            live_hub.begin(session_id, cancellation_event)
            → _execute_turn
                 写 session.created / user.message
                 拼 system prompt + 记忆 + Skill catalog + 权限说明
                 从事件回放 messages
                 组装 RuntimeState
                 graph.invoke(state, recursion_limit=max_tool_steps*2+4)
                 成功则抽记忆候选 / episode
                 写 turn.completed
                 返回 RuntimeAgentResponse
            live_hub.end
       metrics_store.record(TurnMetric)
```

`run()` 也可被 Worker 直接调用：传入 `agent_id`、`delegation_depth`、`parent_session_id`、冻结的 `connection_ids` / `resource_scope` / `memory_snapshot`。这就是子 Agent 的入口，不再经过 FastAPI。

异常映射：

- `ModelProviderError` → HTTP 该错误的 status_code + Retry-After
- session 不存在或不属于你 → `KeyError` → 404
- Agent disabled / 参数非法 → `ValueError` → 400
- 超时 → `timed_out`；用户中断且可恢复 → `interrupted`；否则 `cancelled`

---

## 8. LangGraph 与 RuntimeState

`_build_graph()`：

```text
START → model
model  --pending_calls 非空--> tools
model  --否则--> END
tools  --waiting_approval--> END
tools  --否则--> model
```

没有 Supervisor 节点，没有按角色拆开的图节点。子 Agent 是再 `invoke` 一次这张图。

`RuntimeState`（`agent_loop.py`）是图在内存里的整包状态：

| 字段 | 含义 |
|---|---|
| `messages` | OpenAI 风格 messages（system + 历史 + tool） |
| `session_id` / `tenant_id` / `user_id` / `role` | 身份 |
| `model_id` | 本回合模型 |
| `required_modalities` | `{text}` 或 `{text, image}` |
| `pending_calls` | 本轮要从 tools 节点执行的 ToolCall |
| `tool_results` | 已完成的 ToolResult |
| `tool_steps` | 已走完的 tools 次数 |
| `allowed_tools` | 本 Session 冻结的可见名；None 表示不额外限制 |
| `agent_id` / `delegation_depth` | 角色与嵌套深度 |
| `connection_ids` / `resource_scope` | 数据范围快照 |
| `connection_scope_enforced` | 有 ConnectionRegistry 则为 True |
| `approved_call_ids` | 审批通过、本回合允许执行的 call_id |
| `waiting_approval` / `pending_approval_ids` | 停在审批 |
| `cancellation_event` / `deadline` | 中断与墙钟超时 |
| `token_budget` / `tokens_used` | 本回合预算 |
| `explicit_memory_consent` / `forget` | 用户话里是否说了「记住/忘记」 |
| `memory_snapshot` | 已检索的小型记忆 |
| `status` / `answer` / `provider` / `model` | 终态 |

`_context(state)` 把它压成不可变的 `ToolExecutionContext`，交给 Registry 与 Executor。`visible_to()` 用 `role`、`allowed_tool_names`、`allowed_tenants` 过滤 schema。

续聊时 `_restore_messages(events)` 把事件还原成 messages：`user.message` → user（含图片 data URL）、`model.response` → assistant（含 tool_calls）、`tool.completed` → tool（再 compact）。**不**把 `session.created`、审批、subagent.* 变成模型消息，那些只给 UI/审计。

`SessionLiveHub`：进行中的 `session_id` 可被新开的浏览器 SSE 订阅，`interrupt()` 对同一 Event 置位。

---

## 9. `_model_node` 内部

顺序：

1. `_check_control`：取消或 deadline → 抛错
2. `registry.schemas(context)`：本轮模型可见的 function schema
3. `router.route(model_id, required_modalities)`
4. 写 `model.request`（tools 名列表、模态）
5. `router.invoke(messages, schemas, on_token=...)`  
   - 流式：`token` / `reasoning` 经 `StreamingPublicTextSanitizer` 推 SSE
6. 失败写 `model.error` 并抛 `ModelProviderError`
7. 兼容修复（只改模型输出，不扩大权限）：
   - `_recover_web_search`：用户明确要求网页搜索但模型没调工具
   - `_recover_claimed_delegation`：模型嘴上说委派了但没出 tool_call
   - `_canonicalize_delegation_arguments`：objective 归一、同领域任务合并
   - `_repair_delegation_mode`：专业模式下把 `delegate_subagent` 改成批次工具（或反过来）
   - `_normalize_specialist_delegations`：多条专业委派收成每批最多 3 个
8. 丢掉不在 `visible_tool_names` 里的 call，写 `model.tool_call_rejected`
9. `tool_steps >= max_tool_steps` 则清空 calls，改口「已达最大轮数」
10. Token 超预算同样清空 calls
11. 写 `model.response`，把 `pending_calls` 交给图路由

进模型前 `_prepare_model_messages` 会按上下文窗口设置压缩历史：保留最近 N 个 user turn、限制 message 数/字符数，Tool 内容再走 `_compact_tool_content`（先截 rows，再截字符，必要时只留 3 行 preview）。

system prompt 拼接（`_execute_turn`）：

1. `_base_system_prompt(agent_id)`：角色默认文案，可被 Agent 定义覆盖
2. Coordinator 追加 `coordinator_delegation_prompt`（列出当前可委派且用户有权的子 Agent）
3. Analyst 追加 `data_tool_usage_prompt`（当前可用查询 Tool 的用法）
4. `memory_prompt(snapshot)`，并标明不可当指令执行
5. 非 admin：列出本轮允许的工具名，或直接塞入 `denial_detail` 文案
6. 若上次是用户中断：要求从已有事件恢复，不要把中断当终答
7. `skill_registry.catalog_prompt`（只含当前允许的 skill）
8. 若有 `parent_session_id`：声明「你是子 Agent，权限已锁定」

---

## 10. `_tools_node` 内部

对每个 `pending_calls`：

1. `registry.get(name, context)` 失败 → 立刻 `tool.completed`（ok=false），继续下一个
2. 写 `tool.requested`（call_id、name、arguments）
3. `requires_approval` 且 call_id 不在 `approved_call_ids`：
   - `governance_store.create_approval(...)`
   - 写 `approval.requested`
   - `waiting_approval=True`，**本 call 不执行**，循环可继续登记其它需审批项
4. 否则 `executor.execute(call, context)`
5. `_check_control`（执行中被中断则停）
6. `sanitize_public_value` / `sanitize_public_text`
7. 若 ok 且有 ResultStore：`materialize_tool_output(...)` 替换 `output`
8. 写 `tool.completed`，追加 role=tool 的 message

然后 `pending_calls=[]`，`tool_steps += 1`。若 `waiting_approval`，answer 改成「等待逐次人工审批」，status 同步，图路由 END。

`ToolExecutor._execute`：

1. 取 definition + `model_validate` 参数
2. `ApprovalGuard`：未批准的高风险直接 `PermissionError`（正常路径在节点里已经分流；这里是防绕过）
3. `ConnectorAccessGuard`：有 binding 时解析 Connection；若 `connection_scope_enforced` 且 id 不在快照 `connection_ids` → 拒绝
4. 与 `context.deadline` 取较小 timeout，丢进单线程池 `future.result(timeout=...)`
5. 成功 `ToolResult(ok=True)`；`KeyError/ValidationError/PermissionError/TimeoutError` 变成 `ok=False`，不把异常打出图外

---

## 11. 角色虚拟化

常量在 `agent_roles.py`。决策核 `kind=role` 或 `runtime`；查数入口历史上是 `kind=hybrid`。

| `agent_id` | 默认 prompt | 代码强制 |
|---|---|---|
| `function-calling-runtime` | Coordinator：拆任务、委派、知识/网页、禁止自己查库 | 去掉全部 `DATA_QUERY_TOOL_NAMES`；general 只留 `delegate_subagent`，专业模式只留 `delegate_specialists` |
| `analyst` | 只用列出的查询 Tool，禁止再委派 | 去掉记忆/知识/网页/delegate |
| `amazon-finance-analyst` | 只用 `amazon_finance_query` | 同上 + 严格 allowlist |
| `profit-analyst` | `lingxing_profit_query` 优先，失败可切 `profit_report_query` | 不可用 Amazon/ERP |
| `erp-analyst` | 只用 `kingdee_cloud_query` | 不可用 Amazon/利润 |

`resolve_agent_tool_allowlist` 还会：

- 减去 `inactive_data_query_tools`（Agent 未启用或连接未配）
- Coordinator 并上钉钉三个写 Tool 与记忆/知识/网页（仍要连接就绪才会真正出现在 schema 里）
- 用户没有某专业核所需业务 Tool 时，该核不进委派列表，前端也不展示

`AgentDefinition.strict_tool_allowlist=True` 时以定义里的 `allowed_tools` 为上限，再套上面的强制规则。页面 `PATCH /v1/agents/{id}` 可改 prompt 和启用状态，不能靠改名变出一个新专业核——工程规则要求专业核必须有独立白名单、输入输出合同和评测。

`DELEGATABLE_AGENT_IDS` = analyst + 三个 specialist。Coordinator 不能被委派。

---

## 12. Tool / MCP / Connector

### 12.1 Tool 合同

`ToolDefinition`：`name`、`description`、`arguments_model`、`handler(args, context)`、`risk`、`requires_approval`、`timeout_seconds`、`source`（`local` / `mcp:...` / `subagent`）、`builtin`、`allowed_roles`、`allowed_tenants`、可选 `parameters_schema`（MCP 用上游 JSON Schema）。

内置查询 Tool 与连接类型：

| Tool | Connector | 计划模型 | 实现 |
|---|---|---|---|
| `amazon_finance_query` | analytics | `AmazonFinanceQueryPlan`（metric/日期/limit） | 参数化聚合，仅 RELEASED |
| `profit_report_query` | analytics | 利润表计划 | 本地仓 `lingxing_profit_order_transactions` |
| `lingxing_profit_query` | lingxing | OpenAPI 计划 | 分页拉取后 Runtime 做数值统计 |
| `kingdee_cloud_query` | kingdee | 单据查询计划 | WebAPI `execute_bill_query` |
| `web_search` | tavily | query/max_results | Coordinator 只读，不审批 |
| `dingtalk_*` | dingtalk | 用户/群/待办 | `requires_approval`，禁止自动重试 |
| `search_knowledge` | GraphRAG `KnowledgeGateway` | query/space_id/top_k | Coordinator；未配 API 返回中文提示而非编造 |
| `load_skill` | 无 | skill 名 | 把 SKILL.md 注入上下文 |
| `sandbox_*` | 无 | 命令/路径 | Analyst 向；full-access 要审批 |
| `delegate_*` | 无 | objective / tasks | 见第 13 节 |

`workflows/` 与 `runtime/*_tool.py`：domain 定义计划 → query_tool 执行 → runtime 包装成 Tool；`workflows/*/agent.py` 给直连 API 做 `plan()`。

`AmazonFinanceQueryPlan.metric`：`overview` | `daily` | `transaction_type` | `fee` | `sku` | `settlement`。limit 1～100。模型不得输出 SQL/表名/DSN；来源用业务名。

### 12.2 MCP

配置 `config/mcp_servers.json`：`stdio` 或 `streamable-http`。独立线程跑 asyncio loop，session 保活。`list_tools` 后登记；调用走队列到该 loop 的 `session.call_tool`。公开名超 64 字符会 hash 截断。对 `_model_node` 与本地 Tool 无区别。

### 12.3 ConnectorRuntime

`ToolBinding(tool_name, connector_type, operation, resource_scope_field)`。默认绑定见 `connector_control_plane.DEFAULT_TOOL_BINDINGS`。

`execute_tool(tenant_id, tool_name, operation)`：

1. 解析绑定 Connection（显式绑定 > 该类型默认）
2. 熔断：连续失败 ≥ `failure_threshold`(3) 则冷却
3. `min_interval_seconds` 节流（Tavily 等非 0）
4. 缓存客户端；更新/删除 Connection 会 `invalidate`
5. 读操作瞬时错误可有限重试；钉钉等 `retry_transient=False`

凭证：`data/connections.json` 只存 `secret_ref`；密钥在 `data/connection_secrets.json` mode `0600`。`ConnectorType`：`analytics` | `lingxing` | `kingdee` | `dingtalk` | `qdrant` | `milvus` | `tavily`。analytics 的 `database_type` 为 postgresql 或 mysql。

资源范围字段因类型而异：利润 `store_names`、领星 `sids`、钉钉三类 id 列表。空范围 = 该类推送默认禁止。Amazon 查询以绑定的 analytics Connection 为边界，**不再传播 `seller_id`**（事件 payload 读时也会剥掉遗留字段）。

---

## 13. 子 Agent 队列

### 13.1 入队 `SubagentManager.submit`

加锁后：

1. `depth > subagent_max_depth` → 失败
2. `_resolve_target_agent`：必须在 `DELEGATABLE_AGENT_IDS` 且 enabled
3. `_enforce_parallel_limit`：专业模式下同一 parent 的 queued/running/cancel_requested 个数
4. `_resolved_tools`：请求列表 ∩ 目标 Agent 白名单 ∩ 当前用户授权
5. `_resolved_connection_scope`：请求范围 ∩ 当前 Connection
6. 若有 MemoryService：按 **子 Agent id + objective** 再 build 一份 snapshot（不是父的原文照搬；父也可在 Tool 参数里传入）
7. 新 `task_id`、`child_session_id`
8. `SubagentTaskRecord(status=queued)` 写入 Governance
9. 父 Session 写 `subagent.started`

`delegate`（通用）：默认同步，`timeout` 缺省 min(默认超时, 170s)；`run_in_background=true` 立即返回投影（answer 截断 0）。`delegate_specialists`：一次 1～3 个任务，全部 `wait`，每任务 answer 字符按个数摊（约 500～1400）。

`task_projection` 把记录收成给模型看的精简 JSON（status、answer 截断、error），避免把子 Session 全量事件灌回父模型。

### 13.2 执行 `execute_subagent_task`

```text
update running + 父事件 subagent.running
runtime.run(
  question=objective,
  session_id=child_session_id,
  agent_id=record.agent_id,
  allowed_tools=set(record.allowed_tools),
  connection_ids / resource_scope / memory_snapshot = 快照,
  parent_session_id, delegation_depth=depth,
  timeout_seconds, token_budget,
  cancellation_event,
)
映射 response.status → 任务终态
写 subagent.finished（父 Session）
```

`queue_backend`：

- `inline`（开发）：`submit` 后立刻 `ThreadPoolExecutor.submit(_run_inline)`。`wait()` 轮询 Governance 记录直到终态或超时。`cancel` 对 `_cancellations[task_id]` 置位。进程退出 `shutdown(wait=False)`。
- `db`（生产）：`submit` 只 `create_task`。`wait()` 同样轮询库。真正执行在独立进程 `claim_next_task`。API 副本和 Worker 必须共享 DSN；配置变更后要滚动 Worker，否则它仍用旧的模型/连接 JSON。

生产禁止「只入队无 Worker」。`health` 会带出当前 `subagents` 后端名。

Worker 循环：`requeue_expired_leases` → `claim_next_task(worker_id, lease)` → 心跳线程 `renew_lease`；lease 丢了或 `cancel_requested` 则 set cancellation。尝试次数用尽 → failed。

状态机：

```text
queued → running → completed | failed | timed_out | waiting_approval | budget_exceeded
queued|running → cancel_requested → cancelled
lease 过期 → queued（attempt+1）或 failed
```

前端必须把同一 `task_id` 的 queued→running 渲染成一条任务，不能显示两条。

---

## 14. 权限与数据范围

### 14.1 RBAC 表

`PostgresAccessControlStore`：`ops_access_users`、`ops_permission_groups`、用户↔组多对多、组↔业务 Tool 多对多（组内 Tool 唯一）。规则明细只读。

`EffectiveAccess`：

- 租户还没有任何用户：`configured=False`，`allowed_tools=None`（开放兼容）
- 一旦有用户：未登记 / 停用 / 无组 / 无规则 → 对应 `denial_detail` code
- `admin` 绕过组规则，仍受 Agent 职责和 Connection 范围约束
- 基础 Tool（`SYSTEM_DEFAULT_TOOL_NAMES`）自动并入已启用用户，不进组候选

对话里最终可见：

```text
decision.allowed_tools
  ∩ resolve_agent_tool_allowlist(agent)
  ∩ 连接就绪
  ∩ session.created 快照（续聊时 _merge_session_tool_snapshot）
```

schema 可见 ≠ 执行授权：Executor 再跑 Guard，防重放和绕过模型路径的直接调用。

### 14.2 数据范围

```text
Connection.resource_scopes
  ∩ Tool binding 的子集（PUT /v1/tools/{name}/connection 可带 resource_scopes）
  ∩ 委派/会话快照
```

`execute_direct_tool` 也会 `tool_bindings.execution_scope` + `connection_scope_enforced=True`。

---

## 15. 存储与物化

| 存储 | 表/文件 | 开发 | 生产 |
|---|---|---|---|
| Session 事件 | `agent_session_events`（sequence 单调） | SQLite 文件 | PostgreSQL |
| Result | `agent_tool_results` | 同 Session 后端 | PostgreSQL |
| Governance | 审批 + `subagent_tasks` | SQLite/PG | PostgreSQL |
| 审计 / 旧 run | `ops_audit_events` / `ops_runs` | PG | PG |
| RBAC / 账户 | `ops_access_*`、账户表 | PG | PG |
| 记忆 | `memory_items` + 向量 | SQLite+本地向量 | PG/pgvector 或 Qdrant/Milvus |
| 模型/连接/知识空间 | `data/*.json` | 本地 | 本地（故单副本） |
| Tool catalog/binding | PG（0005） | PG | PG |
| 分析库 | `amazon_finance_*`、`lingxing_profit_*` | 独立 PG/MySQL | 只读账号 |

`materialize_tool_output`：output 是带 `rows` 的 dict 才物化。计算 `statistics.numeric_columns`（count/sum/min/max/avg）和 `data_quality`。`result_ref = result-{uuid}`。投影去掉全量 rows，留 `preview_rows`、`rows_truncated`、`result_endpoint`。控制台 `GET /v1/agent/results/{ref}?offset&limit`（limit≤200），还校验 `record.user_id == principal.user_id`。删 Session 会删父子 Session 的结果。

分析库 DSN **不是** `POSTGRES_DSN`。管理员在连接器页录入；导入脚本用环境变量 `IMPORT_DATABASE_DSN`。

Alembic：`0001_baseline` 事件/治理/指标；`0002` 记忆；`0003` 成熟记忆控制面；`0004` 生产控制面（账户等）；`0005` connector tool entities。

---

## 16. 直连 BI

`POST /v1/amazon-finance/query`（领星/利润表/金蝶同构）：

1. hybrid Agent enabled + analytics/对应连接就绪，否则 503 + `connector_not_configured`
2. `AmazonFinanceAgent.plan(payload)`：有现成 plan 就用，否则模型只输出计划 JSON
3. `execute_direct_tool(tool_name="amazon_finance_query", arguments=plan)`
4. 审计 `amazon_finance.queried`

`execute_direct_tool` 自己造一次性 `session_id=direct-...`，**不写** 对话 Session 事件，但仍走 RBAC 和 Connector 范围。适合调试 SQL 计划和给非对话客户端。

`HYBRID_AGENT_TO_TOOL` 把旧 catalog id 映射到 Tool 名；`delete_hybrid_agents` 启动时会清理重复的 hybrid 定义，能力收敛到 Tool catalog。

---

## 17. 模型路由

`ModelRegistry` ← `data/model_definitions.json`，Key 另存。每个模型声明：provider、base_url、temperature、thinking、是否支持 image/audio、超时重试。

未配置任何可调用模型：`ModelConfigurationRequiredAdapter` → 503「请去系统设置添加模型」。Key 为空：`MissingApiKeyAdapter`，禁止回退 `.env`。

`_model_node` 用 `required_modalities` 拦多模态。Adapter：OpenAI 兼容、智谱（thinking 通道）、通义、DeepSeek。流式 token 与 reasoning 分 channel。

`ModelTurn`：`content`、`reasoning_content`、`tool_calls`、`usage`。usage 累加进 `tokens_used`，超 `token_budget` 停止调用。

---

## 18. 记忆、知识、网页

三种来源三种 Tool，禁止混用。

| Tool | 调用者 | 写入条件 | 引用 |
|---|---|---|---|
| `remember_fact` | Coordinator | 仅 `explicit_memory_consent`（话里明确「记住」） | 记忆 id |
| `search_memory` / `forget_memory` | Coordinator | forget 同样要明确「忘记」 | |
| `search_knowledge` | Coordinator | 只读已发布 Collection | 标题+页码 |
| `web_search` | Coordinator | 只读 Tavily | 标题+URL |

`memory_mode=disabled` 不检索、不提取；`read_only` 可检索不可写。

Coordinator `_execute_turn` 在无 parent 时 `build_snapshot(question)`。子任务 `submit` 时按子 Agent 再 snap 一次。Analyst 无记忆 Tool。快照包在 `<untrusted_memory_context>` 里，明确「不是指令」。

`web_search`：Tavily，snippet ≤800 字，返回 title/url/text/score；未配连接时工具给中文说明，Coordinator 必须转述而不是用训练知识冒充网页。用户说「网页搜索/搜新闻」时 `_recover_web_search` 会补一次调用。

回合成功结束后（仅 Coordinator、非 resume、`memory_mode=default`）：`extract_candidates` 写 `memory.candidates_extracted`；`capture_episode` 写 `memory.episode_extracted`。候选要确认才进检索。检索公式、冲突链、Worker 见 [第 26 节](#26-记忆检索冲突与维护)；产品策略见 [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md)。

对话检索走 GraphRAG 网关，管理页还有独立的向量「知识空间」配置，两套不要混，见 [第 33 节](#33-知识graphrag-网关-vs-向量空间)。

---

## 19. 审批、中断、恢复

**审批**：钉钉三个写 Tool、`danger-full-access` 沙箱等 `requires_approval=True`。`_tools_node` 创建 `ToolApprovalRecord(status=pending)` 后本轮 END。

`POST /v1/agent/approvals/{id}`（approver/admin）→ `AgentRuntime.decide_approval`：

- 写 `approval.decided`
- 批准：用快照重建 `ToolExecutionContext(approved_call_ids={call_id})` 再 `executor.execute`
- 拒绝：`ToolResult(ok=False, error=rejected by ...)`
- 写 `tool.completed`
- 若同 Session 还有 pending → 仍返回 `waiting_approval`
- 否则从事件 `_restore_messages` **继续 graph**（把 tool 结果喂回 model 写终答）

批准执行仍走 ConnectorAccessGuard；不能把未授权 call 变成授权。写操作禁止因 resume/lease 重试而重复发送。

**中断**：`POST /v1/agent/sessions/{id}/interrupt` → `live_hub.interrupt` + 事件 `turn.interrupt_requested`；正在跑的 Tool/子任务看 `cancellation_event`。流式路径 `interruption_is_resumable=True` 时写 `turn.interrupted` 而非 cancelled。

**恢复**：`POST /v1/agent/query/resume` 或 `continue_session`。`turn_is_open`（有 user.message 尚无 turn.completed）才真正续跑。`resume=True` 不重复写 user.message；`_fill_missing_tool_results` 补齐助手已发出但未落地的 tool 结果。中断恢复会在 prompt 里要求重新委派失败/取消的子任务。

附件：`POST /v1/agent/attachments` 存 sha256；对话 `attachment_ids` 变成 data URL。工作区文件下载：`GET /v1/agent/workspace/file`（沙箱产出的 csv 等）。

---

## 20. 前端

无构建。`frontend/index.html` 导航：

| `data-page` | 角色 | 后端 |
|---|---|---|
| `guide` | 全员 | 本地文案 |
| `agent-chat` | 全员 | `/v1/agent/query/stream`、sessions、results、subagents |
| `approvals` | admin/approver | `/v1/agent/approvals` |
| `knowledge` | admin | `/v1/knowledge/spaces` |
| `dashboard` | admin | `/v1/dashboard/summary` |
| `agents` | 全员（专业核按权限藏） | `/v1/agents` |
| `skills` | admin | `/v1/agent/skills` |
| `tools` | 全员 | catalog + tool-bindings |
| `connectors` | admin | `/v1/connections` |
| `memory` | 全员（普通用户只看自己） | `/v1/memory/*` |
| `access` | admin | `/v1/access-control` |
| `audit` | admin | `/v1/audit-events` |
| `settings` | admin | `/v1/configuration`、models、context-window、analyst-runtime |

`applyRoleVisibility()` 按 `data-role-allow` 藏菜单。对话把 `queued/running/waiting_approval` 等翻成中文状态；`result_ref` 打开分页表。Auth：access+refresh 存在 localStorage，开发才显示租户/角色头模拟。

---

## 21. API 目录

按域分组（完整列表以 `app.py` 为准）：

**账户**：`/v1/auth/register|login|refresh|logout|me|change-password`

**对话 Runtime**：`POST /v1/agent/query`、`/query/stream`、`/query/resume`；`GET/DELETE /v1/agent/sessions`；`GET .../events`；`POST .../interrupt`；`GET /v1/agent/results/{ref}`；`GET /v1/agent/metrics`

**审批 / 子任务**：`GET/POST /v1/agent/approvals`；`POST/GET /v1/agent/subagents`；`POST .../cancel`

**附件 / Skill / 工作区**：attachments、skills CRUD、workspace file

**直连 BI**：`/v1/amazon-finance/query`、`/v1/lingxing-profit/query`、`/v1/profit-report/query`、`/v1/kingdee-cloud/query`

**连接 / 工具绑定 / 知识空间**：`/v1/connections`、`/health`、`/v1/tool-bindings`、`PUT /v1/tools/{name}/connection`、`/v1/knowledge/spaces`

**记忆**：用户侧 `/v1/memory/*`；管理侧 `/v1/memories/*`（确认/拒绝/纠正/合规删除）

**RBAC**：`/v1/access-control` 用户/组/规则/绑定

**配置**：`/v1/catalog`、`/v1/models`、`/v1/configuration`、models CRUD、context-window、analyst-runtime

**健康**：`/health`、`/health/live`、`/health/ready`（ready 检查 runtime + 可调用模型 + session store）

---

## 22. Session 事件与 SSE

`SessionEvent`：`session_id`、单调 `sequence`、`tenant_id`、`user_id`、`event_type`、`payload`、`created_at`。

| event_type | 谁写 | payload 要点 |
|---|---|---|
| `session.created` | `_execute_turn` | agent_id、allowed_tools、connection_ids、resource_scope、token_budget、memory_snapshot（仅子 Session）、memory_mode |
| `user.message` | 同上 | content、attachment_ids |
| `model.request` / `model.response` / `model.error` | `_model_node` | provider、tools、content、tool_calls、usage |
| `model.tool_call_rejected` / `model.tool_call_recovered` | 修复逻辑 | reason、tool_names |
| `delegation.*` | 归一/并行化 | 批次数、从哪张 Tool 改到哪张 |
| `tool.requested` / `tool.completed` | `_tools_node` | 参数；完成后是脱敏 ToolResult |
| `approval.requested` / `approval.decided` | tools / decide_approval | approval_id、risk、approved |
| `subagent.started` / `running` / `cancel_requested` / `finished` | Manager / Worker | task_id、child_session_id、status |
| `turn.completed` / `turn.interrupted` / `turn.interrupt_requested` | 回合收尾 | answer、status、tokens_used |
| `memory.candidates_extracted` / `episode_extracted` | 回合成功后 | memory_ids |

SSE `type` 包括：`session`、`user.message`、`token`、`reasoning`、上述事件名、最后 `done`（整份 RuntimeAgentResponse）或 `error`。客户端应按 sequence 幂等投影；刷新后可挂 `SessionLiveHub` 或重拉 `/events`。

列表会话 `list_sessions(tenant, user)` 必须双键过滤。子 Session 不出现在用户会话列表（`parent_session_id` 标记）。

---

## 23. 读码顺序与符号表

1. `agent_roles.py`
2. `runtime/stack.py` → `create_app` lifespan
3. `api/app.py`：`query_agent_runtime`、`execute_direct_tool`、`decide_runtime_approval`
4. `runtime/domain.py` 请求/响应
5. `runtime/agent_loop.py`：`RuntimeState`、`run`、`_build_graph`、`_model_node`、`_tools_node`、`decide_approval`
6. `runtime/tools.py`
7. `runtime/agent_tool_policy.py`
8. `runtime/subagents.py` + `governance.py` + `subagent_worker.py`
9. `runtime/connectors.py` + `amazon_finance_tool.py` + `workflows/amazon_finance/query_tool.py`
10. `runtime/result_store.py`
11. `access_control.py`、`connections.py`、`accounts.py`
12. `runtime/mcp_client.py`、`memory.py`

| 符号 | 文件 |
|---|---|
| `query_agent_runtime` | `api/app.py` |
| `execute_direct_tool` | `api/app.py` |
| `open_runtime_stack` | `runtime/stack.py` |
| `AgentRuntime.run` | `runtime/agent_loop.py` |
| `_build_graph` / `_model_node` / `_tools_node` | 同上 |
| `decide_approval` | 同上 |
| `ToolExecutor.execute` | `runtime/tools.py` |
| `resolve_agent_tool_allowlist` | `runtime/agent_tool_policy.py` |
| `SubagentManager.submit` / `delegate` | `runtime/subagents.py` |
| `execute_subagent_task` | 同上 |
| `ConnectorRuntime.execute_tool` | `runtime/connectors.py` |
| `materialize_tool_output` | `runtime/result_store.py` |
| `MemoryService.search` / `build_snapshot` | `runtime/memory.py` |
| `KnowledgeGateway` | `knowledge_gateway.py` |
| `SandboxRunner` | `runtime/sandbox.py` |
| `sendAgentMessage` | `frontend/app.js` |

---

## 24. 测试、迁移、边界

相关测试：`test_runtime.py`、`test_multi_agent_roles.py`、`test_agent_tool_policy.py`、`test_subagent_queue.py`、`test_governance.py`、`test_result_store.py`、`test_memory.py`、`test_connector_runtime.py`、`test_connections.py`、`test_api.py`、`test_access_control.py`、`test_accounts.py`、`test_web_search_tool.py`、`test_dingtalk.py`、`test_mysql_query_tools.py`。PG 集成：`RUN_POSTGRES_TESTS=1`。

评测：`ops-agent-eval`、`ops-agent-memory-eval evals/enterprise_memory.json`。

读代码时不要误解：

1. 不是 LangGraph 多 Agent 图。
2. Worker 不是另一套 Runtime，只是再 `open_runtime_stack`。
3. MCP 不是 LangGraph MCP 组件。
4. `workflows/*/agent.py` 不做 SQL。
5. `POSTGRES_DSN` ≠ 分析库。
6. 生产多 API 副本在 JSON 注册表迁走前不可用。
7. 知识空间不能当文档入库系统。
8. Coordinator 终答前必须拿到子任务终态；「已委派请稍等」不算完成。
9. `admin` 不能绕过 Connection 范围和审批。

---

## 25. 查数端到端对照

用户：「2026 年 7 月 Amazon 结算净额按 SKU Top 10」。`analyst_mode=specialized_parallel`。

| 步 | 函数 | 关键数据 |
|---|---|---|
| 1 | `query_agent_runtime` | Principal；`allowed_tools` 含或不含 `amazon_finance_query` 只影响能否看见专业核 |
| 2 | `AgentRuntime.run` Coordinator | schema 有 `delegate_specialists`，无 `amazon_finance_query` |
| 3 | `build_snapshot` | 例如用户偏好店铺；注入 prompt，不可当指令 |
| 4 | `_model_node` | tool_call：`amazon-finance-analyst` + 写清日期/Top10/SKU 的 objective |
| 5 | `_normalize_specialist_delegations` | 保证 ≤3 路 |
| 6 | `delegate_specialists` → `submit` | 冻结 tools/connections/scope/memory；`child_session_id` |
| 7 | Worker `execute_subagent_task` | 第二次 `run(agent_id=amazon-finance-analyst)` |
| 8 | 子 `_model_node` | 只能调 `amazon_finance_query`；参数是 `metric=sku` 等 |
| 9 | `AmazonFinanceQueryTool.execute` | 参数化 `GROUP BY sku`，`transactionStatus=RELEASED` |
| 10 | `materialize_tool_output` | 全量 rows 入库；子模型看 preview + statistics + result_ref |
| 11 | 子 `_model_node` | 中文结论 +「数据来源：…」；不输出 SQL |
| 12 | `task_projection` | 截断 answer 回父 tools 消息 |
| 13 | 父 `_model_node` | 终答；把下载链接/`result_ref` 转给用户 |
| 14 | 控制台 | SSE `done`；表格走 `GET /v1/agent/results/{ref}` |

失败：查询 Tool `ok=False` → Coordinator 必须披露，禁止编造净额。三路里一路失败：只能基于成功路作答并写明缺口。钉钉若在同轮被调用，会先 `waiting_approval`，数字结论要等批准或拒绝后再续。

---

## 26. 记忆检索、冲突与维护

`MemoryItem` 字段：`scope`（user / profile / tenant / agent）、`kind`（fact / preference / profile / organization / agent / episodic / procedural）、`status`（candidate / active / conflicted / superseded / deleted）、`key`、`importance` / `confidence` / `quality_score`、版本链 `supersedes_id` / `correction_of` / `conflict_group_id`、`expires_at`。embedding 存在主库，`exclude=True` 不随 API dump 出给模型。

### 写入

`remember_fact` 先查 `context.explicit_memory_consent`（正则见 `explicit_remember_requested`：记住/请记下/remember this…）。租户/Agent 范围要 admin。`MemoryService.create`：

1. 租户策略 + 用户偏好都 enabled
2. `sensitive_categories` 命中身份证/银行卡等：策略 `block` 或用户未允许敏感 → 拒绝
3. 用户 kind 必须在 `allowed_kinds`
4. 同 key + 同 scope + 同 owner 已有 active 且内容不同 → 打 `conflict_group_id`，新记录 `conflicted`
5. 主库 put 后 `_index`：向量 upsert；失败进 outbox，不回滚主数据（主库是真实源）

自动候选：`extract_candidates` 用启发式或 `model_candidate_extractor(router)`。候选项默认不 `active`。高置信且策略 `auto_activate_confidence` 才可能自动生效。

### 检索公式

`search` / `build_snapshot` 同一套：

```text
先 SQL：tenant + owner + scope + status∈可检索 + 未过期
semantic：pgvector 或 Qdrant/Milvus；否则本地 embedding 余弦
lexical：query token ∩ content token
recency：1 - age_days / memory_decay_after_days
entity：控制面实体表命中 +0.1
feedback_penalty：metadata.quality_flag ∈ {incorrect, stale} 则 -0.25

score = 0.55*semantic + 0.2*lexical + 0.1*importance
      + 0.1*quality + 0.05*recency + entity - penalty

evidence = 0.8*semantic + 0.2*lexical + entity
保留 evidence ≥ max(threshold, best_evidence*0.72)
截断到 snapshot_limit（默认 8）
写 memory_retrieval_logs（query 只存 hash）
```

Embedding：策略 `hash` 用确定性 token 哈希向量（离线可复现）；`sentence_transformers` 用租户指定模型。切模型后 Worker `maintenance` 会按 metadata 里的 provider/model/维度重嵌并重索引。

### 擦除与维护

`forget` 同样要 `explicit_memory_forget`。合规删除清空 content 和向量，留不含原文的 `memory_events`。Worker：过期 active → superseded；重试 failed outbox；重嵌。

记忆控制表：`memory_user_preferences`、`memory_tenant_policies`、`memory_events`、`memory_retrieval_logs`、`memory_sources`、`memory_relations`、向量 outbox。

---

## 27. 沙箱、Skill、附件

### 沙箱

`SandboxRunner`：受限模式按平台探测后端，失败则 `restricted_available=False`，只读/工作区写 Tool **不会注册**（health 里 sandbox=unavailable），**不会降级成裸跑**。

| 平台 | 后端 | health `sandbox` |
|---|---|---|
| macOS | Seatbelt `/usr/bin/sandbox-exec` | `seatbelt` |
| Windows 8+ | AppContainer（无 capability = 默认无网；只读/写用两个 profile，避免 ACL 泄漏） | `appcontainer` |
| Linux 及其它 | 无 | `unavailable`（除非打开 `sandbox_full_access`） |

Windows 不依赖 Codex 那套受限用户 + WFP 安装服务。Job Object 负责超时杀进程树；workspace-write 只给写 profile 的 SID 开工作区/`TEMP` 的 ACL。AppContainer 仍可能写入自己的 package 目录，这比 Seatbelt 的「除 `/dev/null` 外一律禁写」略松。

| Tool | 模式 | 审批 | 角色 |
|---|---|---|---|
| `sandbox_read_only` | 无网、禁写 | 否 | operator/admin |
| `sandbox_workspace_write` | 无网，仅 workspace 与 tmp 可写 | 是 | operator/admin |
| `sandbox_full_access` | 无隔离 | 是，且要 `sandbox_full_access_enabled` | 仅 admin |

argv 数组，不经 shell。POSIX 上 `RLIMIT_CPU` / `RLIMIT_FSIZE`；Windows 上 Job Object 限制 CPU 时间。环境白名单：POSIX 为 PATH/HOME/TMPDIR/LANG/LC_ALL，Windows 另留 SYSTEMROOT/COMSPEC/TEMP 等，不传入密钥。cwd 必须落在 workspace 下。workspace-write 成功后可把 `echo/printf` 最后参数物化成文件，制表符再出一份 `.csv`（Analyst 给下载链接用）。跳过扫描 `.venv` / `node_modules` / `.git` / `data`。

### Skill

仓库 `skills/*/SKILL.md` 启动时 `seed_skills_from_paths` 进 `agent_skill_store`。页面 CRUD 走同一 store。`SkillRegistry.catalog_prompt` 只列出 `model_invocable` 且当前允许的名字。`load_skill` 返回 `{name, content}` 全文，模型未 load 前不应猜步骤。内置 skill 不能删。名字 `^[a-z0-9]+(?:-[a-z0-9]+)*$`。

查询类 skill 与 Tool 的对应：`agent_tool_policy.TOOL_SKILL_NAMES`（如 `amazon_finance_query` → `amazon-settlement-analysis`）。Coordinator 委派后 Analyst 才该 load 对应 skill。

### 附件

`LocalAttachmentStore`：tenant 目录、内容 sha256、Pillow 解码校验格式/像素/字节。对话消息若带图，`required_modalities` 含 image；不支持图像的模型在 route 时拦截。续聊 `_restore_messages` 会按 attachment_ids 再拼 data URL。

---

## 28. 查询 SQL / OpenAPI 如何落地

### Amazon（分析库）

`AmazonFinanceQueryTool._statement` 用 `psycopg.sql` 拼 **标识符**，值全部 `%s` 参数。永远带 `transaction_status = 'RELEASED'`。`end_date` 转成 `< end+1day`。metric 决定 SELECT/GROUP BY：

- overview：count / sum(total_amount) / min-max posted_at / currency
- daily：UTC 日
- transaction_type / fee / sku / settlement：对应维度 + LIMIT

引擎 mysql 时走 `mysql_read_only_connection`，语句超时 `analytics_statement_timeout_ms`。只读。

### 利润表分析仓

`profit_report` query_tool 同模式，表业务名对外是「领星利润分析数据（分析仓）」，物理名 `lingxing_profit_order_transactions` 会被 `sanitize_public_text` 替换掉。

### 领星实时 OpenAPI

`lingxing_profit_tool`：Connector 拿签名客户端分页拉；Runtime 对数值列做确定性 count/sum/min/max/avg（与 Result Store 的 `calculate_result_profile` 同类）。模型仍只看摘要。

### 金蝶

`kingdee_cloud_query` → `KingdeeQueryTool` → WebAPI `execute_bill_query`。单据类型白名单（销售订单、出库、应收、费用应收）。账套来自 Connection，不是模型参数。

所有查询 Tool 返回结构约定：`plan`、`columns`、`rows`、`summary`、`data_source`（业务名）、`calculation.{engine,operation,metric,grouped_by}`。有 `rows` 才会进 Result Store。

---

## 29. 脱敏、观测、成本

`source_privacy.py`：表名 → 业务来源。`StreamingPublicTextSanitizer` 处理流式切词导致的跨 token 表名。`_tools_node` 对 output/error 再 `sanitize_public_value`。事件 payload 读时剥 `seller_id`。

OTel：`configure_tracing`。`otel_exporter=console|otlp`。span：`agent.turn`、`model.invoke`、`tool.execute`。属性带 tenant/user/agent，禁止把密钥和全量表格打进 span。

`TurnMetric` 在每轮 `run` 结束写入 metrics store。Dashboard `runtime` 块：turn_count、failure_rate、tokens、estimated_cost_usd（本地示意费率，不是账单）、avg_latency、by_status/by_model、近 14 日 daily。费率表在 `observability._INPUT_OUTPUT_RATES`。`usage_from_events` 从 session 的 model.response / tool.completed 汇总。

---

## 30. Catalog 如何拼出来

`GET /v1/catalog`：

1. 用当前 Principal 建 `ToolExecutionContext`，`allowed_tool_names=decision.allowed_tools`
2. `runtime_tool_registry.catalog_for(context)`：已按角色/可见性过滤的 Tool 列表
3. `snapshot_agents(...)`：各 hybrid 是否 active（连接+catalog 启用）
4. 若有 Postgres Tool catalog，**丢掉 kind=hybrid 的 Agent**，避免和 Tool 卡片重复
5. `tool_bindings.catalog` 再按 allowed_tools 过滤
6. `tool_capabilities` 来自 PG catalog（display_name、connector_type、enabled、system_prompt）

助手页、工具页、委派候选都读这一个接口，不要各算各的可见性。

---

## 31. 前端 SSE 与消息回放

`sendAgentMessage`（`frontend/app.js`）：

1. 本地先插入一条 `user.message` 并清空输入，避免等待首包
2. `POST /v1/agent/query/stream`，`consumeAgentSse` 按 `\n\n` 拆块
3. `handleAgentStreamEvent`：
   - `session`：记下 session_id，写入会话列表
   - `token` / `reasoning`：追加到 `agentChat.stream`，直接改 DOM（不一定整页 rerender）
   - 其它带 `payload` 的 type：幂等追加到 `events`（同 type+payload 不重复）
   - `model.response`：丢掉 stream 缓冲，改由事件渲染最终文本
   - `done`：toast 终态（等待审批 / 已中断 / 完成）
   - `error`：抛错，`finishAgentStream` 处理 429 cooldown
4. 结束后拉 sessions / subagents / 必要时 events，防止漏事件

`renderAgentMessages` 把 `events` 投影成气泡：user、assistant（含 tool 轨迹折叠）、审批、子任务。中断态禁用发送，只显示「继续执行」。`isInterruptedAgentTurn` 看是否有 `turn.interrupted` 且其后没有 `turn.completed`。

刷新页面：用 `GET /v1/agent/sessions/{id}/events` 重建 `events`，不依赖 localStorage 当权限边界（localStorage 只记 apiKey/token/上次 sessionId）。

---

## 32. 账户与 JWT

`accounts.py`：`validate_password`（≥10、大小写+数字）。临时密码 `Ark-...-7a`。JWT claims：tenant_id、user_id、role、token 类型。Access 短 TTL，Refresh 旋转。`principal_from_headers` 在 JWT 模式下忽略伪造的 X-User-*（中间件审计同样优先 Bearer）。

连续失败锁定、改密撤会话、管理员重置只回一次临时密码。开发无 JWT 时才允许头模拟，且 `isDevEnvironment()` 前端才显示那三个输入框。

---

## 33. 知识：GraphRAG 网关 vs 向量空间

两套并存，职责不同：

| | GraphRAG `KnowledgeGateway` | `KnowledgeSpaceRegistry` |
|---|---|---|
| 配置 | `.env` 的 `KNOWLEDGE_API_BACKEND` / `URL` / 可选 `TOKEN` | 连接器 Qdrant/Milvus + `data/knowledge_spaces.json` |
| 谁用 | Coordinator 的 `search_knowledge` | 管理页「知识空间」测试连接、看 Collection 内容 |
| 检索 | HTTP 调用 `/v1/retrieve`，融合 OpenSearch 混合检索、LightRAG、Neo4j 和规则证据 | 直连向量库字段映射 |
| 未配置 | Tool 返回「尚未连接知识检索服务」，不编造制度 | 空间列表为空 |

`search_knowledge`：GraphRAG 后端映射为单一虚拟空间 `ecommerce-graphrag`，
调用时传入完整问题。网关只接收统一检索返回的可信 `citation_ids`，按证据优先级
排序，再转为 title、page、chunk、source、evidence_id 和 ≤800 字 snippet。
`KNOWLEDGE_API_BACKEND=wenshu` 仍可回退旧的多知识空间协议。

不要把 Qdrant 空间配置理解成 `search_knowledge` 的唯一后端；Coordinator
制度问答默认以电商 GraphRAG 统一检索为准。向量空间仍用于控制面的 Collection
浏览/连接测试，以及记忆的可选索引。

---

## 34. 本地数据文件与排障

| 路径 | 内容 | 备注 |
|---|---|---|
| `.env` | DSN、JWT、队列、知识网关 | 不放模型 Key、不放分析库 DSN |
| `data/runtime_overrides.json` | context window、analyst_mode | 页面 PATCH 写入 |
| `data/model_definitions.json` + secrets | 多模型 | 空则对话 503 |
| `data/connections.json` | 公开连接 | 无密钥 |
| `data/connection_secrets.json` | 0600 | 勿提交 |
| `data/tool_bindings.json` | 兼容导入；运行时以 PG 为准 | `import_json_bindings` |
| `data/knowledge_spaces.json` | 向量空间 | |
| `data/attachments/` | 图片 | tenant 子目录 |
| `data/platform.auth-secret` | 本地 HMAC 材料 | |
| 沙箱 workspace | csv 等 | `SANDBOX_WORKSPACE_ROOT` |
| SQLite（若仍有） | 仅非 PG 测试路径 | 当前 Settings 已要求 POSTGRES_DSN |

排障口诀：

1. `/health/ready` 的 `model=false` → 先配模型，不是 Runtime 坏了
2. 能聊天不能查数 → 连接器 + 工具绑定 + 权限组，再看 Analyst 模式
3. 委派出去没有 Worker 消费 → 生产必须起 `ops-agent-subagent-worker`
4. 子任务权限比父大 → 不应发生；查 submit 时的 snapshot 与 ConnectorAccessGuard
5. 模型看到表名 → 查 sanitize 是否漏了新表，补 `_PRIVATE_IDENTIFIERS`
6. 流式卡死 → `stream_slots` 429，或模型 Adapter 没打 token 也没结束
7. 沙箱 Tool 消失 → 非 macOS/Windows，或 Seatbelt / AppContainer 探测失败
8. 知识问答空洞 → `KNOWLEDGE_API_*` 未配，不是 Qdrant 空间没建
