# ADR-001：统一 Tool 授权网关

- 状态：Proposed
- 日期：2026-09-03
- 适用项目：SellerForge / Multi-Agent Ops Platform

## 1. 背景

当前 Tool 权限判断分散在多个层次：

- API 通过 `effective_access()` 计算用户可用 Tool；
- Agent Runtime 通过 `resolve_agent_tool_allowlist()` 应用 Agent 白名单；
- `ToolRegistry.visible_to()` 过滤角色、租户和 `allowed_tool_names`；
- `ApprovalGuard` 处理人工审批；
- `ConnectorAccessGuard` 检查 Connection 是否位于委派范围；
- Connector Runtime 和具体 Tool 再检查 `resource_scope`；
- SubAgent 提交时重新计算 Tool、Connection 和资源范围。

现有逻辑能够提供基础保护，但存在以下问题：

1. API、Agent、SubAgent 和直接 Tool 接口重复实现授权判断；
2. 不同入口的授权流程可能逐渐产生差异；
3. `ToolExecutor` 的用户授权依赖调用方正确填写 Context，缺少不可绕过的统一入口；
4. 用户、Agent、Tool、Connection 和资源范围没有形成一份完整决策；
5. `admin` 当前可得到 `allowed_tools=None`，等价于跳过租户 Tool 授权；
6. `SYSTEM_DEFAULT_TOOL_NAMES` 自动包含委派、网页搜索和沙箱写入等能力，默认范围偏大；
7. 审批目前表现为独立 Guard，还没有作为授权决策的执行义务统一表达；
8. 权限决策缺少稳定的 `decision_id`、策略版本和统一审计记录。

## 2. 决策

在当前模块化单体内增加 `ToolAuthorizationGateway`，统一管理 Tool 的发现、授权、范围收窄、审批义务和执行分发。

第一阶段不拆成独立微服务，也不推翻现有 RBAC 表。网关通过适配器复用现有 `PostgresAccessControlStore`、Agent Registry、Tool Registry、Tool Binding、Connection Registry 和 Approval Service。

统一架构：

```text
API / Coordinator / Analyst / SubAgent Worker
                     │
                     ▼
          ToolAuthorizationGateway
                     │
       ┌─────────────┼──────────────┐
       ▼             ▼              ▼
 PolicyDecision   ScopeResolver   ApprovalService
      PDP          数据范围          审批义务
       │             │              │
       └─────────────┼──────────────┘
                     ▼
          AuthorizationDecision
                     │
              allowed = true
                     ▼
            ToolExecutionRouter
       ┌─────────────┼─────────────┐
       ▼             ▼             ▼
   Local Tool    Connector Tool   MCP/SubAgent
```

网关是统一策略决策点（PDP），Tool 执行前的网关拦截是唯一策略执行点（PEP）。

## 3. 核心原则

### 3.1 两次授权

模型发现 Tool 时检查一次，Tool 真正执行前再次检查：

```text
discover：过滤模型可见的 Tool Schema
execute：基于实时身份、参数、资源范围和审批状态重新授权
```

模型可见性不是最终安全边界。任何 Tool Call 在 handler 执行前都必须重新授权。

### 3.2 默认拒绝

- 显式 Deny 优先；
- 未命中 Allow 时拒绝；
- 生产环境未配置权限体系时拒绝；
- 开发环境可以通过显式兼容配置保留旧行为；
- `admin` 负责管理策略，但不自动绕过数据和 Tool 权限。

### 3.3 用户和 Agent 身份分离

- 用户是授权主体，决定“谁拥有权限”；
- Agent 是工作负载身份，决定“谁代表用户执行”；
- Agent 白名单只能缩小用户权限，不能扩大用户权限；
- SubAgent 必须继承并收窄父 Agent 的授权范围。

### 3.4 授权与审批分离

审批不是 Tool 权限，而是允许执行后的附加义务：

```text
allowed = true
obligation = require_approval
```

没有基础执行权限的请求不能通过审批获得权限。

## 4. 权限计算规则

最终可执行范围为：

```text
用户授权
∩ Agent Tool 白名单
∩ Tool 启用状态
∩ Tool role/tenant 限制
∩ Connection 授权
∩ Resource Scope
∩ 父 Agent 委派范围
∩ 当前会话授权快照
```

授权输出必须同时给出：

- 是否允许；
- 拒绝原因码；
- 命中的策略；
- 收窄后的 Connection IDs；
- 收窄后的 Resource Scope；
- 审批、脱敏、行数或超时限制等执行义务；
- 策略版本。

## 5. 建议接口

### 5.1 Gateway

```python
class ToolAuthorizationGateway:
    def discover(
        self,
        context: AuthorizationContext,
    ) -> list[ToolDefinition]:
        """返回当前主体真正可见的 Tool。"""

    def authorize(
        self,
        request: AuthorizationRequest,
    ) -> AuthorizationDecision:
        """只做授权决策，不执行 Tool。"""

    def execute(
        self,
        call: ToolCall,
        context: AuthorizationContext,
    ) -> ToolResult:
        """重新授权、收窄范围、处理审批并分发执行。"""
```

### 5.2 AuthorizationRequest

```python
@dataclass(frozen=True)
class AuthorizationRequest:
    tenant_id: str
    user_id: str
    role: str

    agent_id: str
    delegation_depth: int
    parent_session_id: str | None

    action: Literal[
        "tool.discover",
        "tool.execute",
        "tool.delegate",
        "tool.approve",
    ]

    tool_name: str
    arguments: dict[str, Any]

    requested_connection_ids: tuple[str, ...]
    requested_resource_scope: dict[str, tuple[str, ...]]
    approved_call_ids: frozenset[str]
```

### 5.3 AuthorizationDecision

```python
@dataclass(frozen=True)
class AuthorizationDecision:
    decision_id: str
    allowed: bool
    code: str
    reason: str

    policy_ids: tuple[str, ...]
    effective_tool_name: str | None
    connection_ids: tuple[str, ...]
    resource_scope: dict[str, tuple[str, ...]]

    obligations: tuple[AuthorizationObligation, ...]
    policy_version: int
```

### 5.4 Obligations

```python
@dataclass(frozen=True)
class AuthorizationObligation:
    kind: Literal[
        "require_approval",
        "redact_output",
        "limit_rows",
        "limit_timeout",
        "audit",
    ]
    parameters: dict[str, Any]
```

## 6. 执行流程

```python
def execute(self, call, context) -> ToolResult:
    definition = self.registry.require(call.name)
    arguments = definition.arguments_model.model_validate(call.arguments)

    decision = self.authorize(
        AuthorizationRequest.from_call(
            call=call,
            arguments=arguments.model_dump(),
            context=context,
        )
    )
    self.audit.record_decision(call, context, decision)

    if not decision.allowed:
        return ToolResult.denied(
            call_id=call.call_id,
            tool_name=call.name,
            code=decision.code,
            reason=decision.reason,
        )

    if decision.requires_approval:
        return self.approvals.request_or_resume(
            call=call,
            context=context,
            decision=decision,
        )

    narrowed_context = context.with_authorization(decision)
    return self.router.execute(definition, arguments, narrowed_context)
```

Tool handler 不得成为 API、Agent 或 Worker 的公开调用入口。生产调用统一经过 Gateway。

## 7. Tool 分发

授权通过后由 Router 根据 Tool 来源分发：

```python
class ToolExecutionRouter:
    def execute(self, definition, arguments, context):
        match definition.source:
            case "local":
                return self.local_executor.execute(definition, arguments, context)
            case source if source.startswith("mcp:"):
                return self.mcp_executor.execute(definition, arguments, context)
            case "subagent":
                return self.subagent_executor.execute(definition, arguments, context)
            case "connector":
                return self.connector_executor.execute(definition, arguments, context)
            case _:
                raise UnknownToolSource(definition.source)
```

Gateway 决定能否执行以及可访问的范围；Router 和 Tool 负责具体执行。

## 8. SubAgent 授权

创建 SubAgent 时生成不可扩大的委派授权：

```python
DelegationGrant(
    tenant_id=parent.tenant_id,
    user_id=parent.user_id,
    parent_agent_id=parent.agent_id,
    child_agent_id="profit-analyst",
    allowed_tools=("profit_report_query",),
    connection_ids=("analytics-prod",),
    resource_scope={"store_names": ("DE", "FR")},
    expires_at=...,
    policy_version=...,
)
```

Worker 执行时使用：

```text
委派授权快照 ∩ 当前实时权限
```

这保证：

- 子 Agent 不能扩大父 Agent 权限；
- 权限撤销后，尚未执行的后台任务失效；
- 每个子任务都可以追踪授权来源；
- child Session 继续保持独立状态和审计记录。

## 9. 数据模型

第一阶段继续使用现有用户、权限组、规则和 Tool Binding 表。完成入口收敛后，再考虑增加：

```sql
CREATE TABLE ops_authorization_policies (
    tenant_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    effect TEXT NOT NULL,
    action TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    conditions_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    resource_scope_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    version BIGINT NOT NULL,
    PRIMARY KEY (tenant_id, policy_id)
);

CREATE TABLE ops_authorization_decisions (
    decision_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    action TEXT NOT NULL,
    allowed BOOLEAN NOT NULL,
    reason_code TEXT NOT NULL,
    policy_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    effective_scope_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    policy_version BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

如果审计量较大，`ops_authorization_decisions` 应按时间分区或设置保留周期。

## 10. 缓存与一致性

- Tool 发现结果可按 `(tenant, user, role, agent, policy_version)` 短期缓存；
- 执行授权必须重新校验参数和资源范围；
- 审批结果不能被普通授权缓存替代；
- 每次权限变更递增租户 `policy_version`，用于快速失效缓存；
- 长任务和 DB 队列任务执行前重新校验实时权限；
- 审计记录保留本次执行使用的策略版本和有效 Scope。

## 11. 推荐模块结构

```text
src/ops_agent/runtime/authorization/
├── __init__.py
├── models.py
├── policy.py
├── scope.py
├── gateway.py
├── router.py
└── audit.py
```

职责：

| 模块 | 职责 |
|---|---|
| `models.py` | Request、Decision、Obligation、DelegationGrant |
| `policy.py` | PDP 接口及现有 RBAC 适配器 |
| `scope.py` | Connection 和资源范围求交集 |
| `gateway.py` | discover、authorize、execute 主流程 |
| `router.py` | Local、Connector、MCP、SubAgent 分发 |
| `audit.py` | 决策审计和策略版本记录 |

## 12. 渐进迁移计划

### 阶段一：统一入口，不改变现有语义

1. 建立 Authorization Request/Decision 数据模型；
2. 用适配器包装现有 `effective_access()`；
3. 接入 Agent Tool 白名单和 Tool 可见性；
4. 接入 Connection/Resource Scope；
5. Gateway 暂时返回与当前实现相同的结果；
6. 增加 shadow mode，对比新旧决策但仍使用旧结果。

### 阶段二：让 Gateway 成为唯一执行入口

按顺序替换：

1. API `execute_direct_tool`；
2. `AgentRuntime._tools_node`；
3. `SubagentManager` 的权限计算；
4. MCP 和 DB Worker 执行入口；
5. 禁止业务代码直接调用 Tool handler。

### 阶段三：默认拒绝与内置能力拆分

1. 取消 `admin -> allowed_tools=None` 的隐式无限放行；
2. 将 `SYSTEM_DEFAULT_TOOL_NAMES` 拆为真正运行必需能力和用户业务能力；
3. 单独授权沙箱写入、网页搜索和 Agent 委派；
4. 生产环境权限未配置时默认拒绝；
5. 保留显式开发兼容开关。

### 阶段四：资源级策略与执行义务

支持：

- 指定 Connection；
- 店铺、区域、账套、组织等资源范围；
- 最大查询日期窗口；
- 最大返回行数；
- Tool 超时上限；
- 输出脱敏；
- 高风险 Tool 审批。

### 阶段五：按需要服务化

仅在以下条件出现时拆成独立授权服务：

- 多个应用共享同一套授权；
- 多语言 Worker 无法复用 Python 模块；
- 授权模块需要独立扩缩容；
- 合规要求授权决策独立部署；
- 本地策略计算成为明确性能瓶颈。

## 13. 方案对比

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 保持分散判断 | 改动最少 | 重复逻辑和绕过风险持续增加 | 不推荐 |
| 进程内统一 Gateway | 易迁移、无网络开销、适合当前模块化单体 | 需要约束所有入口必须经过网关 | 推荐 |
| 独立授权微服务 | 多系统共享、独立部署 | 网络依赖、缓存一致性和运维复杂 | 暂缓 |
| 立即引入外部策略引擎 | 策略表达能力强 | 学习和集成成本高，现阶段可能过度设计 | 保留替换接口 |

## 14. 风险与缓解

| 风险 | 缓解措施 |
|---|---|
| 迁移改变现有权限语义 | 第一阶段兼容模式；shadow decision 对比 |
| Gateway 成为性能热点 | 进程内调用；按 policy version 缓存 discover |
| Scope 合并导致权限扩大 | 所有 Scope 只允许求交集，禁止求并集扩大 |
| SubAgent 使用过期权限 | Worker 执行前以实时权限再次求交集 |
| admin 改为受控后影响运维 | 建立独立 break-glass 流程并强制审计 |
| 业务代码绕过 Gateway | 收窄 ToolExecutor/handler 可见性并增加架构测试 |

## 15. 测试要求

至少覆盖：

1. discover 与 execute 使用相同授权语义；
2. 无权限 Tool 不出现在模型 Schema 中；
3. 手工构造 Tool Call 仍被执行网关拒绝；
4. 用户权限与 Agent 白名单只能求交集；
5. Connection 和 Resource Scope 不能被子 Agent 扩大；
6. 显式 Deny 优先于 Allow；
7. 无基础权限时审批不能放行；
8. 权限撤销后排队中的 SubAgent 不能继续执行；
9. admin、operator、viewer 的默认行为明确且可测试；
10. 每次授权决策产生可追踪的 decision audit。

## 16. 验收标准

- API、Coordinator、Analyst、SubAgent Worker 和直接 Tool 调用共用 Gateway；
- `ToolExecutor` 不再依赖外部调用方自行完成用户授权；
- Tool Schema 发现与实际执行不存在权限差异；
- 每次 Tool 执行都能关联 `decision_id` 和 `policy_version`；
- 子 Agent 只能继承更小或相等的权限范围；
- 权限、审批、Connection 和资源范围产生一份统一决策；
- 原有功能在兼容迁移阶段无行为回归。

## 17. 后续决策触发点

出现以下情况时重新评估本 ADR：

- 需要接入 OPA、Cedar、Casbin 等外部策略引擎；
- 多个应用需要共享实时授权；
- Tool 数量、租户数或策略量导致本地决策性能不足；
- 出现跨区域部署或独立合规边界；
- 授权策略需要复杂属性表达式或策略模拟环境。
