# Multi-Agent Ops Platform

面向生产化演进的 **Agent Runtime 平台**：通用 Function Calling 对话、工具审批、Subagent 外部队列、沙箱执行、可观测性，以及跨境电商 BI 查询 Agent（Amazon 结算、领星利润、金蝶云星空）。项目提供商用控制台形态的 Dashboard、审批中心、Agent 对话、知识库、审计和设置页面。

项目支持两种本地形态：`.env.example` 使用 SQLite + mock 模型，完全离线即可运行；生产形态可将控制面与 Session 事件切换到 PostgreSQL。分析类 Agent 需要 PostgreSQL 分析库；虚构样本数据由脚本生成后导入，不随仓库分发。

## 系统架构

```mermaid
flowchart LR
    UI["管理控制台 / Agent 对话"] --> API["FastAPI / SSE"]
    API --> RT["Function Calling Runtime"]
    RT --> TR["ToolRegistry / ToolExecutor"]
    TR --> GOV["逐次工具审批"]
    RT --> SE[("Session Event Store")]
    API --> BI["BI Query Agents"]
    BI --> PG[("PostgreSQL Analytics")]
    CP[("Control Plane / Audit")] --- API
```

Agent Runtime 负责模型路由、工具循环与 Session 事件记录；高风险沙箱或完全访问工具进入逐次审批。各 BI Agent 与 Runtime 隔离，模型只生成受约束查询计划，SQL / API 由代码白名单生成并参数化执行。

## 内置 Agent

| Agent ID | 说明 | 默认状态 |
|---|---|---|
| `function-calling-runtime` | 通用 Function Calling Runtime | 启用 |
| `amazon-finance-query` | Amazon 结算只读查询 | 需配置 `ANALYTICS_DSN` |
| `lingxing-profit-report` | 领星 OpenAPI 利润报表 | 需配置领星凭证 |
| `profit-report-query` | 已导入 PostgreSQL 的领星利润表查询 | 需配置 `ANALYTICS_DSN` |
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
3. 在 `.env` 中配置分析库 DSN：

```dotenv
ANALYTICS_DSN=postgresql://user:password@127.0.0.1:5432/wenshu
ANALYTICS_STATEMENT_TIMEOUT_MS=5000
```

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
python scripts/import_amazon_finance.py fixtures/mock_data/api_pages/2026-07_page_001.json --database-url "$ANALYTICS_DSN"
python scripts/import_lingxing_profit_xlsx.py fixtures/mock_data/xlsx/mock-利润报表-订单-Transaction-2026-07.xlsx --dsn "$ANALYTICS_DSN"
```

## 多模型配置

系统设置页可管理多个模型定义（持久化到 `data/model_definitions.json`，Git 忽略）。对话窗口可按 `model_id` 选择模型。

- `GET /v1/models` — 列出可用模型
- `GET/POST/PATCH/DELETE /v1/configuration/models` — CRUD 模型定义

本地默认 `MODEL_PROVIDER=mock`。启用 OpenAI 或智谱时，在 `.env` 或控制台中配置对应 API Key 与模型名称。

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

```dotenv
# OpenAI
MODEL_PROVIDER=openai
MODEL_NAME=gpt-5.6-sol
OPENAI_API_KEY=your-api-key

# 智谱官方 SDK
MODEL_PROVIDER=zhipu
ZAI_API_KEY=your-api-key
ZHIPU_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
ZHIPU_MODEL_NAME=glm-5.2
```

智谱 Runtime Adapter 使用官方 `zai-sdk` 和原生 Function Calling。参考：[智谱官方 Python SDK](https://docs.bigmodel.cn/cn/guide/develop/python/introduction)。

## 金蝶云星空 Agent

在管理控制台 **Agents & 工具** 中启用 `kingdee-cloud`，配置以下凭证字段：

- `server_url` — 私有云 WebAPI 地址
- `acct_id` — 账套 ID
- `app_id` / `app_secret` — 应用密钥
- `username` / `lcid` — 登录用户与语言

支持查询：销售订单、销售出库、应收单、费用应收单。

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
