# SellerForge Multi-Agent Operations Platform 项目介绍

## 1. 项目概述

SellerForge 是一套面向企业数据分析和运营协作场景的 Multi-Agent 平台。
它将大模型、专业 Agent、业务 Tool、外部系统连接器、权限治理、知识库和长期记忆
整合到同一个可视化运行平台中，用户可以以自然语言提交复杂任务，由系统完成拆解、并行执行、
数据查询、统计计算、结果汇总和受控的外部操作。

项目不是单一聊天页面，而是一个具备身份、权限、数据边界、审批、审计、记忆与可观测能力的
Agent Runtime 控制面。

## 2. 项目目标

SellerForge 主要解决以下问题：

- 复杂业务问题需要跨多个数据源和业务系统才能回答。
- 大数据量直接输入模型会导致 Token 超限、成本过高和统计口径不稳定。
- 单 Agent 容易同时承担规划、查数、计算和回答，职责过度集中。
- 外部 Tool 缺少用户权限、数据范围和连接凭证的统一治理。
- Agent 长任务需要队列、并行、中断、恢复和运行轨迹，而不是一次性 HTTP 回答。
- 企业知识、用户偏好和跨会话事实需要可审核、可纠错和可删除的持久化管理。

## 3. 总体架构

```text
用户 / 管理员
        │
        ▼
Web 控制台 + FastAPI
        │
        ├── 账户、RBAC、审批、审计
        ├── 会话、任务队列、中断与恢复
        └── Multi-Agent Runtime
                │
                ├── Coordinator
                ├── General Analyst
                └── Specialist Analysts（最多并行 3 个）
                        │
                        ▼
                 Tool Execution Layer
                        │
        ┌─────────────────────────┐
        │              │              │
   业务数据库       外部 OpenAPI      知识与记忆
 PostgreSQL/MySQL  领星/金蝶/钉钉   Qdrant/Milvus/pgvector
```

平台将「Agent 允许使用哪些 Tool」、「用户有权使用哪些 Tool」、「Tool 选择哪个连接」
和「连接允许访问哪些数据」分层建模，最终权限为这些边界的交集。

## 4. Multi-Agent 运行模式

### 4.1 Coordinator

Coordinator 是面向用户的主 Agent，负责理解目标、拆分任务、选择 Analyst、回收子任务结果和生成最终回答。
Coordinator 不直接执行专业数据库查询，以避免决策、查数和计算职责混在同一个 Agent 中。

### 4.2 General Analyst

通用 Analyst 保留原有的通用分析能力，适合无需明确专业分工的问题。管理员可以在页面选择
「通用 Analyst」模式。

### 4.3 Specialist Analysts

专业模式下，Coordinator 可以按业务领域委派多个子 Agent，同一会话最多并行 3 个。当前内置角色包括：

- Amazon Finance Analyst：Amazon 结算、费用、交易类型和 SKU 维度分析。
- Profit Analyst：订单利润、毛利率和本地分析仓查询。
- ERP Analyst：金蝶销售、出库、应收、客户和回款分析。

专业 Agent 使用独立严格 Tool 白名单，不能继续委派，避免无限递归和越权。对用户没有权限的业务 Tool，
页面不展示对应的专业 Analyst。

## 5. Agent Runtime 与任务执行

运行时采用 Function Calling 循环，并将模型输出、Tool 调用、审批和子任务状态持久化为 Session 事件。

核心能力包括：

- 多轮会话与 tenant / user 双重隔离。
- 后台子任务队列和并行 worker。
- `queued` / `running` / `completed` / `failed` / `interrupted` 等完整状态。
- 长任务中断、恢复、取消、超时和 lease 续租。
- 子任务最大深度、超时、尝试次数和 Token 预算限制。
- 流式输出、工具轨迹、思考状态和并行任务可视化。
- 会话可见性与用户身份绑定，不依赖浏览器本地列表作为权限边界。

## 6. 大数据量与 Token 优化

平台不将大量明细行全部输入模型，而是将统计计算下沉到数据库和 Tool：

- Tool 执行过滤、分组、汇总、排序和统计计算。
- 模型只接收列名、统计摘要、少量样例行和 Result Reference。
- 完整结果保存到 Result Store，前端通过分页查看或下载。
- Context Window 保留最近用户轮次，并对历史 Tool 结果进行截断和摘要。
- 运行时使用本回合 Token 预算防止无边界调用。

因此，模型负责理解和解释，数据库负责确定性计算。

## 7. Tool 与 Connector 体系

Tool 描述 Agent 能做什么，Connector 描述 Tool 通过哪个外部连接执行。同一类型可创建多个连接实例，
并在 Tool 中选择具体连接。

已适配的连接器：

| 连接器 | 用途 |
| --- | --- |
| PostgreSQL / MySQL | Amazon 财务和利润报表分析 |
| 领星 OpenAPI | 领星 ERP 利润数据查询 |
| 金蝶云星空 | 销售、出库、应收和回款数据 |
| 钉钉 OpenAPI | 单聊、群聊和待办任务推送 |
| Tavily | Coordinator 公开网页搜索（`web_search`） |
| Qdrant | 知识库和向量检索 |
| Milvus | 知识库和向量检索 |

连接凭证与公开配置分离保存，API 只返回脱敏状态。连接器运行时还提供客户端缓存、节流、短暂错误重试、
连续失败熔断和健康状态。钉钉等带外部副作用的写操作禁止自动重试，避免重复发送。

## 8. 账户、权限与治理

平台具备完整的账户与 RBAC 模型：

```text
用户 ↔ 权限组 ↔ Tool 权限
                         │
                         ▼
                    Tool 绑定连接
                         │
                         ▼
                    Connection 数据范围
```

- 支持注册、登录、刷新 Token、退出和密码变更。
- 管理员可以创建带临时密码的账户，用户首次登录强制修改密码。
- 一个用户可属于多个权限组，一个权限组可授权多个业务 Tool。
- 同一 Tool 可以出现在多个权限组，但在同一权限组中不重复。
- 记忆、Skill、沙箱和子 Agent 委派等基础 Tool 不需要业务权限组授权。
- `admin` 默认拥有全部业务 Tool 权限，但仍遵守 Agent 职责和 Connection 数据边界。
- 无权限时返回结构化错误、中文原因和管理员处理建议。
- 高风险或写操作进入审批中心，批准后从原任务继续执行。

非管理员不展示运行概览、审批中心、连接器、用户权限、审计和系统设置等管理页面；长期记忆页仅展示当前用户自己的记忆和隐私控制。

## 9. 长期记忆

记忆系统使用 tenant 隔离的 `memory_items`，并提供：

- `remember_fact`：只在用户明确要求「记住」时写入。
- `search_memory`：按权限、作用域、关键词和语义相似度检索。
- `forget_memory`：删除或合规擦除指定记忆。
- 自动提取候选记忆，候选项不直接生效。
- 新旧事实冲突检测、人工确认、替代链和错误纠正。
- 重要度、置信度、质量分和过期策略。
- 用户事实、用户画像、tenant 组织知识和 Agent 专属记忆。
- 本地向量、PostgreSQL/pgvector、Qdrant 和 Milvus 语义检索。

Coordinator 每轮只读取小型相关快照；Analyst 不直接访问记忆库，而是使用委派时固化的快照。
普通用户可在「长期记忆」页查看、确认、拒绝、纠正、导出或擦除自己的记忆；租户策略和组织记忆仍仅由管理员管理。

## 10. 知识库

知识库使用「向量连接」和「知识空间」两层建模：

- 连接器中管理 Qdrant URL / API Key 或 Milvus URI / Token / Database。
- 知识空间选择连接并配置 Collection、Embedding 模型、维度、Top K 和字段映射。
- 支持 tenant 和 knowledge base 过滤字段。
- 可以真实测试 Collection 是否存在。
- 可以分页查看 Collection 中的知识片段、分类和元数据。
- 连接被知识空间引用时禁止删除。

当前知识库已完成对已存在 Collection 的配置、连通检查和内容浏览。原始文件上传、文档解析、
切片与向量化任务属于后续的知识入库管道，不应与向量数据库连接配置混为一层。

## 11. 模型管理

平台初始化后不内置 Mock 模型，必须由管理员在页面配置模型才能开始任务。

已适配的 Provider：

- OpenAI-compatible
- 智谱 GLM
- 通义千问
- DeepSeek

每个模型独立配置 API Key、Base URL、模型名称、Temperature、Thinking 参数与超时重试。
模型还需要显式声明是否支持图片和语音输入，不支持的输入会在调用前拦截。

## 12. 可观测性与审计

平台对每个任务保留：

- Session 事件流。
- 模型请求状态与 Token 使用。
- Tool 调用参数、结果摘要、耗时和失败原因。
- 子 Agent 队列、执行、完成和失败状态。
- 审批、权限拒绝、登录、账户变更和配置变更审计。
- 运行指标与 OpenTelemetry 导出能力。

对话页面将技术轨迹转换为用户可理解的状态，例如「正在计算销售额」、「等待数据」和「已完成」，
同时保留可展开的 Tool 调用详情供调试和审计。

## 13. 技术栈

| 层级 | 主要技术 |
| --- | --- |
| Web 控制台 | HTML、CSS、Vanilla JavaScript |
| API | FastAPI、Pydantic |
| Agent Runtime | Function Calling、LangGraph、Provider Adapters |
| 持久化 | SQLite、PostgreSQL、Alembic |
| 数据查询 | psycopg、PyMySQL、SQLAlchemy |
| 向量检索 | pgvector、Qdrant、Milvus |
| Embedding | sentence-transformers |
| 身份与安全 | JWT、RBAC、Secret Store |
| 可观测性 | OpenTelemetry、Runtime Metrics、Audit Log |
| 测试 | pytest、FastAPI TestClient、Ruff |

## 14. 主要目录

```text
frontend/                       Web 控制台
src/ops_agent/api/              FastAPI 入口和管理 API
src/ops_agent/runtime/          Agent Runtime、Tool、队列和记忆
src/ops_agent/workflows/        业务查询工作流
src/ops_agent/integrations/     外部系统 API 客户端
src/ops_agent/connections.py    连接器配置和凭证管理
src/ops_agent/access_control.py 用户、权限组和 Tool RBAC
src/ops_agent/accounts.py       账户、密码和 Token
src/ops_agent/knowledge_spaces.py 知识空间配置
alembic/                        PostgreSQL 数据库迁移
scripts/                        启动、数据导入和运维脚本
tests/                          单元测试与 API 回归测试
```

## 15. 部署与启动

开发环境可以使用 SQLite 快速启动；生产环境会拒绝 SQLite、可伪造身份头和内联任务队列，必须使用 PostgreSQL 保存控制面、Session 事件、记忆和队列状态。当前配置注册表尚未进入共享数据库，所以生产只支持单 API 副本。

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
scripts/start_local.sh
```

启动后的初始配置顺序：

1. 使用 `ACCOUNT_BOOTSTRAP_TOKEN` 创建首个管理员账户，成功后轮换并移除该令牌。
2. 在「系统设置」中添加默认模型。
3. 在「连接器」中配置业务数据源。
4. 在「工具」中选择 Tool 使用的连接。
5. 在「用户与权限」中创建权限组并授予业务 Tool。
6. 按需配置知识空间、长期记忆和 Analyst 运行模式。

## 16. 当前完成度与质量保障

当前代码库使用自动化测试、真实 PostgreSQL 集成测试和企业记忆评测持续回归。测试覆盖：

- 多 Agent 角色和 Tool 边界。
- 会话 tenant / user 隔离。
- 子任务队列、并行、中断和恢复。
- 账户、权限组、权限判定和审计。
- 连接器凭证脱敏和资源范围。
- PostgreSQL / MySQL 查询兼容性。
- 钉钉单聊、群聊和待办请求。
- 知识空间和 Qdrant / Milvus 连接配置。
- 长期记忆生命周期。
- 模型 Provider 适配、Thinking 和多模态能力判断。

## 17. 适用场景

- 跨 Amazon、ERP 和本地数据仓库的运营分析。
- 按月份、SKU、店铺和业务线进行销售与利润汇总。
- 自然语言查询财务、订单、应收和回款数据。
- 基于企业知识库的制度、产品和运营问答。
- 分析完成后向钉钉个人、群聊或待办系统推送结果。
- 需要权限隔离、人工审批和全链路审计的企业 Agent 应用。

## 18. 后续规划

1. 知识库入库管道：文件上传、对象存储、解析、切片、Embedding、版本与重建索引。
2. 分类和文档级权限：将知识空间、分类和文档可见性纳入 RBAC。
3. 生产 Secret Store：将本地文件密钥存储替换为 Vault/KMS。
4. 分布式 worker：将后台子任务扩展到独立 worker 集群与消息队列。
5. 持续扩展业务正确性、权限攻防和长任务稳定性评测数据。

---

SellerForge 的核心价值不是让模型可以调用更多工具，而是让多 Agent 在可控的身份、权限、数据、
成本和审计边界中完成真实业务任务。
