# 长期记忆体系

## 功能边界

本系统将长期记忆与会话历史分开：会话历史用于恢复对话，长期记忆是经过权限、同意、质量和生命周期治理后，可在后续任务中检索的结构化事实、偏好、画像、情景经验和程序经验。

已实现的记忆类型：

- `fact` / `preference`：用户事实和偏好。
- `profile`：可聚合为用户画像的属性。
- `episodic`：任务、结果和所用 Tool 的情景经验。
- `procedural`：Agent 可复用的流程和方法。
- `organization` / `agent`：租户组织知识与 Agent 专属记忆。

## 数据流

1. Coordinator 根据当前用户、Agent、作用域和问题检索记忆。
2. 数据库先完成 tenant、owner、scope、status 和过期时间过滤。
3. 排序融合语义分、词项分、实体关系、时效、重要度、质量和用户反馈。
4. 只将达到阈值的小型快照注入 Coordinator；专业 Analyst 使用委派时固化的快照。
5. 上下文中的记忆被标注为不可信数据，不作为指令执行。
6. 对话结束后可生成候选偏好、画像和情景记忆；候选项需确认才进入正式检索。

## 存储与索引

- 主数据：SQLite 用于本地开发，PostgreSQL 用于生产。
- 向量：本地向量/pgvector，或从「连接器」页面配置的 Qdrant / Milvus。
- Embedding：租户策略可选离线 hash 兼容模式或 Sentence Transformers。
- 一致性：主数据库为真实源，向量写入失败进入 outbox，后台维护任务重试。
- 切换 Embedding 模型后，执行维护会对存量记忆重新向量化。

## 治理与合规

- 用户可关闭记忆、关闭自动候选、设置保留时间，以及在每次任务中选择普通、只读或临时会话。
- 候选记忆支持确认、拒绝；冲突记忆支持替换；已生效记忆支持纠正和版本链。
- 用户可查看来源、导出全部记忆、删除单条或擦除全部。
- API Key、密码、银行卡号和身份证号等敏感内容默认禁止保存；审核模式下也不会直接生效。
- 检索、确认、纠正、删除、策略变更和维护均留存不含被擦除原文的审计事件。

## 部署

PostgreSQL 环境首先运行：

```bash
ops-agent-migrate
```

API 之外建议单独运行生命周期 worker：

```bash
ops-agent-memory-worker
```

管理员在页面配置 Qdrant 或 Milvus 连接，再在「长期记忆」中选择向量存储与 Embedding 策略。不配置外部向量库时，系统仍可使用主数据库检索。

## 评测

`ops-agent-memory-eval <cases.json>` 输出 Recall@K、tenant 泄漏数、平均延迟、P95 延迟和注入上下文字符量。数据集可以是旧版 case 数组，也可以使用 `memories + cases` 自包含格式；CLI 会自动播种并在结束后合规擦除样例。

仓库内置虚构跨境电商评测集：

```bash
ops-agent-memory-eval evals/enterprise_memory.json \
  --output evals/enterprise_memory_result.json
```

评测覆盖同义改写、用户/租户/Agent 隔离、候选记忆、过期事实与删除后不可检索。`--keep-seed` 仅用于调试，不建议在共享环境使用。
