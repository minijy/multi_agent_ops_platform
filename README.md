# Multi-Agent Ops Platform

> 完整的项目定位、总体架构、模块说明、部署流程与能力边界请参阅
> [ArkFlow 项目介绍](docs/PROJECT_INTRODUCTION.md)。

面向生产化演进的 **Agent Runtime 平台**：通用 Function Calling 对话、工具审批、Subagent 外部队列、沙箱执行、可观测性，以及跨境电商 BI 查询 Agent（Amazon 结算、领星利润、金蝶云星空）。项目提供商用控制台形态的 Dashboard、审批中心、Agent 对话、知识库、审计和设置页面。

项目支持两种本地形态：`.env.example` 使用 SQLite + mock 模型，完全离线即可运行；生产形态可将控制面与 Session 事件切换到 PostgreSQL。分析类 Agent 可连接 PostgreSQL 或 MySQL 分析库；虚构样本数据由脚本生成后导入，不随仓库分发。

## 系统架构

```mermaid
flowchart LR
    UI["管理控制台 / Agent 对话"] --> API["FastAPI / SSE"]
    API --> RT["Function Calling Runtime"]
    RT --> TR["ToolRegistry / ToolExecutor"]
    TR --> GOV["逐次工具审批"]
    RT --> SE[("Session Event Store")]
    TR --> RS[("Result Store")]
    RS --> UI
    API --> BI["BI Query Agents"]
    BI --> DB[("PostgreSQL / MySQL Analytics")]
    CP[("Control Plane / Audit")] --- API
```

Agent Runtime 负责模型路由、工具循环与 Session 事件记录；高风险沙箱或完全访问工具进入逐次审批。各 BI Agent 与 Runtime 隔离，模型只生成受约束查询计划，SQL / API 由代码白名单生成并参数化执行。

## 大结果与统计计算

Connector Tool 返回表格数据时，Runtime 会把完整结果写入独立的 Result Store，
只向模型和 Session 事件传递统计摘要、数据质量、计算口径、少量预览行和
`result_ref`。因此当次 Tool 调用也受管理页面中“工具结果预览行数”和
“工具结果最大字符数”约束，不会等到下一轮对话才压缩。

统计计算遵循“数据引擎计算、模型解释”的边界：

- 数据库 Tool 使用对应引擎的参数化聚合 SQL 完成 `SUM`、`COUNT`、`GROUP BY` 等计算。
- OpenAPI Tool 完成授权分页读取，Runtime 对返回结果执行确定性的数值列
  `count/sum/min/max/avg` 和空值统计。
- 模型只接收已计算指标、计算引擎与分组口径，不读取全量明细重新计算。
- 控制台可通过 `result_ref` 分页查看完整明细；单页最多 200 行。

Result Store 与 Session Event Store 使用相同后端：本地为 SQLite，生产为
PostgreSQL。删除 Session 时会同步删除父、子 Session 对应的物化结果。

## 内置 Agent

| Agent ID | 说明 | 默认状态 |
|---|---|---|
| `function-calling-runtime` | 通用 Function Calling Runtime | 启用 |
| `amazon-finance-query` | Amazon 结算只读查询 | 需在连接器页配置 PostgreSQL 或 MySQL 并绑定工具 |
| `lingxing-profit-report` | 领星 OpenAPI 利润报表 | 需配置领星凭证 |
| `profit-report-query` | 已导入分析数据库的领星利润表查询 | 需在连接器页配置 PostgreSQL 或 MySQL 并绑定工具 |
| `kingdee-cloud` | 金蝶云星空 WebAPI 单据查询 | 默认禁用，需配置金蝶凭证 |

## 快速开始（SQLite + mock 模型）

需要 Python 3.11 或更高版本。

```bash
cd multi_agent_ops_platform
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
pytest -q
ops-agent-api
```

服务默认监听 `http://127.0.0.1:8100`：

- 管理控制台：`http://127.0.0.1:8100/`
- 交互式 API 文档：`http://127.0.0.1:8100/docs`

SQLite 文件会创建在 `data/`，该目录已被 Git 忽略。

发起一次 Agent 对话：

```bash
curl -X POST http://127.0.0.1:8100/v1/agent/query \
  -H 'Content-Type: application/json' \
  -d '{"question": "你好，请介绍当前 Runtime"}'
```

如果 `.env` 配置了 `APP_API_KEY`，受保护接口还需增加 `X-API-Key` 请求头。

## 生成并导入 Mock 分析数据（PostgreSQL）

仓库不包含样本数据文件。用 `scripts/generate_mock_profit_data.py` 在本地生成虚构跨境电商数据（手机壳 / 配件，品牌 CoverNest、ShieldPro 等），**与真实订单、店铺无任何关联**。生成结果默认写到 `fixtures/mock_data/`（Git 忽略），再导入 PostgreSQL。

### 前置条件

1. 本机已安装 PostgreSQL，并创建分析库（例如 `wenshu`）。
2. 安装 `psql` 客户端（Amazon 导入脚本通过 `psql` 执行 COPY）。
3. 导入数据时在当前 shell 准备一个 DSN 变量（只供导入脚本使用）：

```dotenv
export IMPORT_DATABASE_DSN=postgresql://user:password@127.0.0.1:5432/wenshu
ANALYTICS_STATEMENT_TIMEOUT_MS=5000
```

应用运行时不读取该 DSN。启动服务后，由管理员在“连接器”
页面录入 PostgreSQL 连接，再在“工具”页为查询工具选择连接。

MySQL 使用相同的“数据分析数据库”连接器：在页面将数据库类型选为
`MySQL`，并填写 `mysql://user:password@host:3306/database`。查询账号
应只授予 `SELECT` 权限；MySQL 库需包含与现有 Amazon 结算或领星利润仓
相同的表和字段。同一 Tool 可直接切换绑定到 PostgreSQL 或 MySQL 连接，
不需要重建权限组或专业 Analyst。

### 1. 生成虚构数据

默认生成 2026-01 ~ 2026-07：每月 5 页 × 500 条 Amazon JSON，以及每月约 7476 行领星 XLSX。

```bash
python scripts/generate_mock_profit_data.py
```

常用参数：

```bash
# 只生成 7 月、更小体量，便于快速试跑
python scripts/generate_mock_profit_data.py \
  --year 2026 \
  --months 7 \
  --pages-per-month 1 \
  --records-per-page 50 \
  --xlsx-rows-per-month 200

# 指定输出目录
python scripts/generate_mock_profit_data.py --output-dir fixtures/mock_data
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--year` | `2026` | 数据年份 |
| `--months` | `1-7` | 月份范围，如 `1-7` 或 `7` |
| `--pages-per-month` | `5` | 每月 Amazon JSON 页数 |
| `--records-per-page` | `500` | 每页交易条数 |
| `--xlsx-rows-per-month` | `7476` | 每月领星 XLSX 行数 |
| `--output-dir` | `fixtures/mock_data` | 输出根目录 |
| `--seed` | `20260817` | 随机种子，相同种子可复现 |

生成后的目录：

| 路径 | 内容 |
|---|---|
| `fixtures/mock_data/api_pages/` | Amazon `listTransactions` 分页 JSON |
| `fixtures/mock_data/xlsx/` | 领星利润报表导出格式（`mock-利润报表-订单-Transaction-YYYY-MM.xlsx`） |

### 2. 导入 PostgreSQL

```bash
python scripts/install_mock_data.py --reset
# 或
ops-agent-install-mock --reset
```

生成并导入可以一步完成：

```bash
python scripts/install_mock_data.py --generate --reset
```

`--reset` 会在导入前清空相关分析表。也可按需跳过部分数据：

```bash
python scripts/install_mock_data.py --skip-amazon    # 仅导入领星 XLSX
python scripts/install_mock_data.py --skip-lingxing  # 仅导入 Amazon JSON
```

导入完成后，以下 Agent 即可使用分析库：

- `amazon-finance-query` → 表 `amazon_finance_*`
- `profit-report-query` → 表 `lingxing_profit_order_transactions`

也可以直接导入单个文件：

```bash
python scripts/import_amazon_finance.py fixtures/mock_data/api_pages/2026-07_page_001.json --database-url "$IMPORT_DATABASE_DSN"
python scripts/import_lingxing_profit_xlsx.py fixtures/mock_data/xlsx/mock-利润报表-订单-Transaction-2026-07.xlsx --dsn "$IMPORT_DATABASE_DSN"
```

## 多模型配置

系统设置页可管理多个模型定义（持久化到 `data/model_definitions.json`，Git 忽略）。对话窗口可按 `model_id` 选择模型。

- `GET /v1/models` — 列出可用模型
- `GET/POST/PATCH/DELETE /v1/configuration/models` — CRUD 模型定义

首次启动时模型列表为空，系统不会自动生成 Mock 模型，Agent 对话也不可用。
管理员需先在“系统设置 → 模型配置”中填写真实模型的 Provider、
模型名称、Base URL 和 API Key；这些配置不在 `.env` 重复维护。
每个模型还需显式声明是否支持图片输入和语音输入；默认均为关闭。
对话页和后端路由会同时校验能力，防止将多模态内容发送给仅文本模型。

## API 概览

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/health` | 查看当前模型、知识库与持久化后端 |
| `GET` | `/v1/dashboard/summary` | Dashboard 指标与 Runtime 观测 |
| `GET` | `/v1/catalog` | Runtime / 工作流、Agent 与工具目录 |
| `GET` | `/v1/models` | 列出可用模型 |
| `GET` | `/v1/configuration` | 返回经过脱敏的运行配置 |
| `GET/POST/PATCH/DELETE` | `/v1/configuration/models` | 模型定义 CRUD |
| `POST` | `/v1/agent/query` | 通用 Function Calling Agent Runtime |
| `POST` | `/v1/agent/query/stream` | 流式返回 token 与事件 |
| `POST` | `/v1/agent/query/resume` | 从开放或已中断的会话检查点继续执行 |
| `POST` | `/v1/agent/sessions/{session_id}/interrupt` | 中断当前执行并保留可恢复检查点 |
| `GET` | `/v1/agent/results/{result_ref}` | 分页读取物化的完整 Tool 结果 |
| `GET` | `/v1/agent/approvals` | 查询待审批的工具调用 |
| `POST` | `/v1/agent/approvals/{id}` | 批准或拒绝单次工具调用 |
| `POST` | `/v1/amazon-finance/query` | Amazon 结算只读查询 |
| `POST` | `/v1/profit-report/query` | 领星利润表只读查询 |
| `POST` | `/v1/lingxing-profit/query` | 领星 OpenAPI 利润报表 |
| `POST` | `/v1/kingdee-cloud/query` | 金蝶云星空单据查询 |

开发环境使用 `X-Tenant-ID`、`X-User-ID`、`X-User-Role` 请求头模拟身份（`viewer` / `operator` / `approver` / `admin`）。正式部署必须使用 OIDC/JWT。

## PostgreSQL 控制面

将 `.env.postgres.example` 复制为 `.env`，修改 `POSTGRES_DSN` 并切换持久化后端：

```dotenv
CONTROL_PLANE_BACKEND=postgres
SESSION_EVENT_BACKEND=postgres
POSTGRES_DSN=postgresql://user:password@127.0.0.1:5432/ops_agent
```

```bash
python scripts/check_postgres.py
ops-agent-migrate
RUN_POSTGRES_TESTS=1 pytest -q tests/test_postgres_integration.py
```

也可使用项目自带的 Docker Compose：

```bash
docker compose -f docker-compose.postgres.yml up -d
```

## 使用真实模型

使用管理员账号进入“系统设置 → 模型配置”，新建 OpenAI、智谱、
通义千问或 DeepSeek 模型，填写 API Key 并设为默认模型。保存后立即生效，
无需修改 `.env` 或重启服务。

## 金蝶云星空 Agent

在管理控制台 **Agents & 工具** 中启用 `kingdee-cloud`，配置以下凭证字段：

- `server_url` — 私有云 WebAPI 地址
- `acct_id` — 账套 ID
- `app_id` / `app_secret` — 应用密钥
- `username` / `lcid` — 登录用户与语言

支持查询：销售订单、销售出库、应收单、费用应收单。

## Connector 与租户资源范围

外部账号按 tenant 保存为 Connection；公开配置写入
`data/connections.json`，凭证仅通过 `secret_ref` 从独立的
`data/connection_secrets.json` 读取。本地 Secret 文件会设置为 `0600`；
生产环境建议将 `LocalSecretStore` 替换为 Vault/KMS 实现。

### 钉钉连接与推送

管理员可在“连接器”页创建“钉钉”连接，配置企业内部应用的
`AppKey`、`AppSecret`、`RobotCode` 和可选的默认待办创建者
`UnionId`。`AppSecret` 只写入独立 Secret Store，接口和页面只显示脱敏状态。

连接必须显式配置三类可访问目标：

- `dingtalk_user_ids`：允许机器人主动发送单聊的用户 `UserId`。
- `dingtalk_conversation_ids`：允许发送群消息的 `openConversationId`。
- `dingtalk_union_ids`：允许用于待办创建者、执行人和参与人的 `UnionId`。

页面留空某一类范围时，该类推送默认禁止。创建连接后，在“工具”页分别为
`dingtalk_send_direct_message`、`dingtalk_send_group_message`、
`dingtalk_create_todo` 选择连接，并可进一步缩小各 Tool 的目标范围。
三个 Tool 都是业务写操作：需在权限组中明确授予，且每次执行都会进入
审批中心等待“仅批准本次”。钉钉瞬时网络错误不会自动重试，避免超时后重复发送。

钉钉开放平台侧还需完成：发布企业内部应用机器人、授予机器人主动发送单聊/群聊消息权限、
将机器人加入目标群，以及授予待办应用读写权限。

- `GET /v1/connections` — 列出当前 tenant 的脱敏 Connection。
- `POST /v1/connections` — 创建独立连接；同一连接类型可创建多个实例。
- `PATCH/DELETE /v1/connections/{connection_id}` — 更新或删除独立 Connection。
- `GET /v1/connections/health` — 查看连接的就绪、失败与熔断状态。
- `PUT /v1/connections/{analytics|lingxing|kingdee}` — 管理员更新连接、凭证和 `resource_scopes`。
- `GET /v1/tool-bindings` — 查看每个 Connector Tool 可选及当前选中的 Connection。
- `PUT /v1/tools/{tool_name}/connection` — 为 Tool 选择具体 Connection。
- `store_names` / `sids` 范围在 Tool 执行前强制校验；Amazon 查询以绑定的
  Analytics Connection 为数据边界，不再接收或传播 `seller_id`。

`ConnectorRuntime` 按 Connection 缓存客户端，并统一处理最小调用间隔、
瞬时错误重试和连续失败熔断；更新 Connection 时对应缓存与状态会立即失效。
四个 BI 直连 API 只负责生成受约束计划，实际查询全部进入同一个
`ToolExecutor`。`GET /v1/catalog` 的 `tool_bindings` 可查看 Tool 到 Connector、
operation、资源范围和所选 Connection 的映射。绑定配置独立持久化在
`TOOL_BINDINGS_PATH`，未显式绑定的旧 Tool 会继续使用对应类型的默认连接。

Coordinator 委派任务时会把当时可见的 `connection_ids` 和
`resource_scope` 固化到子任务及子 Session。Analyst 执行 Connector Tool 时，
`ConnectorAccessGuard` 会校验 Connection 是否属于该权限快照；具体资源范围
同时取“当前 Connection 权限”和“委派快照”的交集，避免委派后权限扩大。

Analyst 支持两种可持久化运行模式，可在 Agents 页面或
`PATCH /v1/configuration/analyst-runtime` 切换：

- `general`：保留原通用 `analyst`，覆盖全部只读数据查询工具。
- `specialized_parallel`：按领域委派 `amazon-finance-analyst`、
  `profit-analyst`、`erp-analyst`；每个角色有独立提示词和严格 Tool 白名单。

专业模式下，同一父 Session 同时处于 queued/running/cancel_requested 的
Analyst 任务最多 3 个。该限制在任务入队时加锁校验，不依赖模型遵守提示词；
已入队任务会继续使用创建时的 Agent、Connection 和资源范围快照。
专业模式只向 Coordinator 暴露 `delegate_specialists`：一次提交最多三个专业
Agent，任务并发执行并在同一调用中返回精简结论。Runtime 还会把旧会话或模型
生成的 `delegate_subagent` 自动转换为每批最多三个的并行委派，避免并行能力
依赖模型是否正确选中工具。同一领域的月份、产品等维度应优先合并为一次带
`group_by` 的查询；确需拆分时也允许多个相同专业角色并行运行。

旧版 Agent `integration` 和 `ANALYTICS_DSN` 不再自动生成 Connection。
连接凭证必须在“连接器”页面保存，并在“工具”页选择对应连接。

## 用户与权限体系

控制面提供 tenant 级 RBAC：`User -> Permission Group -> Permission Rule -> Tool`。
规则只做 Tool 白名单授权；数据范围在 Tool 的 Connection 绑定中配置，
不会让同一用户规则和外部数据源边界耦合。
用户与权限组保持多对多；权限组与权限规则是一对多，每条规则只有一个
`group_id`，移动规则会自动解除其原权限组归属。删除权限组会级联删除组内规则。
权限组与业务 Tool 是多对多：在权限组上直接多选并保存 Tool 权限，同一 Tool 可出现在多个权限组中；同一租户、同一权限组内的 Tool 不会重复。规则明细仅用于只读展示，不在明细上移动或改变归属；
创建规则时控制台只显示尚未分配的 Tool，API 和数据库唯一约束会拒绝重复归属。
`remember_fact`、`search_memory`、`forget_memory`、`load_skill`、沙箱工具和
子 Agent 委派工具是 Runtime 基础能力，自动授予所有已登记且启用的用户，
不进入权限规则候选；Agent 职责白名单与沙箱策略仍会限制其实际使用范围。

控制台同时提供 tenant 级账户认证。首次进入可在注册页创建账户；租户的
第一个注册账户自动成为 `admin`；已初始化的租户关闭公开注册，新用户由管理员在
“添加用户”中选择角色并填写或生成临时密码，临时密码只返回一次，用户首次
登录必须修改后才能访问业务接口。密码使用 scrypt 加盐存储，连续失败默认
5 次锁定 15 分钟；Access Token 默认 15 分钟，Refresh Token 默认 7 天并在
刷新时轮换。管理员重置密码或用户主动改密都会撤销该账户的旧会话。

- `POST /v1/auth/register`、`/login`、`/refresh`、`/logout` — 账户和会话生命周期。
- `GET /v1/auth/me`、`POST /v1/auth/change-password` — 当前账户与首次/主动改密。
- `POST /v1/access-control/users/{user_id}/reset-password` — 管理员重置临时密码。
- PostgreSQL 部署应配置共享的 `JWT_SECRET`，保证多副本签发与校验一致。

- `GET /v1/access-control` — 返回用户、权限组、规则及 Tool 目录。
- `PUT/DELETE /v1/access-control/users/{user_id}` — 管理用户与启用状态。
- `POST/DELETE /v1/access-control/groups` — 管理权限组。
- `POST/DELETE /v1/access-control/rules` — 管理 Tool 白名单规则。
- `PUT/DELETE .../users/{user_id}/groups` 和 `.../groups/{group_id}/rules`
  — 管理两层关联。
- `PUT /v1/tools/{tool_name}/connection` 可同时提交 `resource_scopes`；
  绑定范围必须是 Connection 范围的子集。

最终执行权限是“身份角色 ∩ Agent Tool 白名单 ∩ RBAC Tool 规则”，
最终数据权限是“Connection 范围 ∩ Tool 绑定范围 ∩ 会话/委派快照”。
其中 `admin` 默认绕过用户、权限组和权限规则的 Tool 白名单，
但仍然遵守 Agent 职责边界与 Connection 数据范围，避免 Coordinator
直接查库或越过外部数据源授权。非 Admin 被拒绝时，API 返回结构化
错误码、中文原因与管理员处理建议。
这两个结果在任务创建时固化，后续配置变更不会放大运行中任务的权限。
为了兼容旧环境，tenant 中尚未创建任何用户时使用开放兼容模式；
一旦创建首个用户，未登记、已停用或没有规则的用户默认无 Tool 权限。
本地 SQLite 和多副本 PostgreSQL 控制面均有对应关系表实现。

## 知识库与向量数据库

知识库的 Qdrant 和 Milvus 不再从全局配置文件读取。管理员先在
「连接器」页面创建租户级向量数据库连接，再在「知识库」页面
创建一个或多个知识空间。

- Qdrant 连接：URL 和可选 API Key。
- Milvus 连接：URI、可选 Token 和 Database。
- 知识空间：选择连接并配置 Collection、Embedding 模型、向量维度、
  Top K、向量/文本字段以及 tenant / knowledge base 过滤字段。
- 「测试连接」会访问真实向量数据库并确认 Collection 存在。
- 向量连接的凭证与公开配置分离保存；连接被知识空间引用时不允许删除。

## 长期记忆

Runtime 内置 tenant 隔离的 `memory_items` 存储和
`remember_fact` / `search_memory` / `forget_memory` 三个 Tool。Coordinator 每轮
只读取与当前问题相关的小型快照；Analyst 不直接访问记忆库，
而是在委派时固化当时可见的快照，避免越权和任务中途语义漂移。

- 显式写入：只有本轮用户明确说“记住”时 `remember_fact` 才可执行。
- 自动提取：偏好和画像只会以 `candidate` 产生；与同 key 生效记忆不同时进入 `conflicted`，需管理员确认或拒绝。
- 检索：默认使用本地确定性向量；PostgreSQL 可选 `pgvector`，也可选 Qdrant。排序结合语义、词项、重要度和质量分。
- 作用域：用户记忆、自动聚合的用户画像、tenant 组织知识和 Agent 专属记忆分开授权。
- 生命周期：支持重要度、置信度、质量分、过期时间、版本链、纠错代替与内容擦除。
- 合规：单条遗忘与用户级合规删除都会清空内容和向量，并保留不含原文的审计记录。

管理控制台的“长期记忆”页面可查看、语义检索、审核候选、
纠正、删除和执行用户级合规擦除。PostgreSQL 部署先执行
`ops-agent-migrate`；迁移会尝试建立 pgvector HNSW 索引，扩展不可用时保留 JSON 向量回退。

## 项目结构

```text
multi_agent_ops_platform/
├── fixtures/mock_data/        # 本地生成，Git 忽略
├── scripts/
│   ├── generate_mock_profit_data.py  # 生成虚构 Amazon JSON + 领星 XLSX
│   ├── install_mock_data.py          # 将生成结果导入 PostgreSQL
│   ├── import_amazon_finance.py
│   └── import_lingxing_profit_xlsx.py
├── src/ops_agent/
│   ├── api/app.py             # FastAPI、Runtime、审批与 BI 查询
│   ├── model_registry.py      # 多模型 CRUD
│   ├── runtime/               # Runtime、MCP/Skills、Subagent、沙箱、审批
│   ├── workflows/
│   │   ├── amazon_finance/    # Amazon 结算查询
│   │   ├── lingxing_profit/   # 领星 OpenAPI
│   │   ├── profit_report/     # 领星利润表 SQL 查询
│   │   └── kingdee_cloud/     # 金蝶云星空
│   └── integrations/kingdee/  # 金蝶 WebAPI 客户端
├── frontend/                  # 无构建依赖的管理控制台
├── skills/                    # Agent Skills（按需加载）
├── config/mcp_servers.json    # MCP Server 配置
└── tests/                     # 单元与集成测试
```

## 常用命令

```bash
ops-agent-api                  # 启动 API 服务
ops-agent-migrate              # Alembic 数据库迁移
ops-agent-eval                 # 离线 golden 评测
python scripts/generate_mock_profit_data.py   # 生成本地 Mock 分析数据
ops-agent-install-mock --reset                # 将生成结果导入 PostgreSQL
ops-agent-subagent-worker      # Subagent 外部队列 Worker
pytest -q                      # 运行测试
```

## 安全说明

- `.env`、`data/` 与 `fixtures/mock_data/` 已被 Git 忽略，请勿提交 API Key、模型凭证或生成出的样本文件。
- 分析库账号建议使用只读权限。
- 沙箱 `danger-full-access` 工具每次调用均需人工审批。
