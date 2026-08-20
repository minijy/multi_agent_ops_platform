window.SellerForgeGuide = {
  path: [
    {n: "1", title: "账号与权限", topic: "access", text: "管理员创建用户、权限组，并勾选该组能用的业务工具。"},
    {n: "2", title: "连接器与工具", topic: "connectors", text: "先配好数据库、领星、金蝶或搜索凭证，再到工具页绑定连接和数据范围。"},
    {n: "3", title: "知识与模型", topic: "knowledge", text: "对接文枢知识空间、上传文档完成解析与向量化；在系统设置里配置可用模型。"},
    {n: "4", title: "发任务", topic: "agent-chat", text: "在任务页用自然语言提问。协调助手拆解，分析助手查数，高风险操作会进入审批。"}
  ],
  groups: [
    {id: "work", title: "日常工作", hint: "登录后最常用的三件事", items: ["agent-chat", "approvals", "knowledge"]},
    {id: "manage", title: "平台管理", hint: "把系统跑通、把边界管住", items: ["dashboard", "agents", "tools", "connectors", "memory", "access", "audit", "settings"]}
  ],
  topics: {
    "agent-chat": {
      title: "任务",
      minutes: "4 分钟",
      audience: "所有角色",
      page: "agent-chat",
      pageLabel: "打开任务",
      summary: "用自然语言提交运营问题。协调助手拆任务，分析助手查数，结果在同一会话里汇总。",
      intro: "任务页是 SellerForge 的工作台。左侧是会话和后台子任务，中间是对话，底部输入。系统不会把整表明细塞进模型，而是让数据库做汇总，模型只解释摘要。",
      blocks: [
        {
          heading: "什么时候用",
          items: [
            "问 Amazon 结算、费用、SKU 或站点维度的结果。",
            "问领星利润、毛利或已导入分析库的报表。",
            "问金蝶销售、出库、应收和回款。",
            "问内部制度、操作手册——走知识库检索。",
            "问公开网页上的最新说明——走网页搜索（需配置 Tavily）。"
          ]
        },
        {
          heading: "怎么发一条任务",
          steps: [
            {title: "确认模型和会话", body: "右上角选择已启用的模型。左侧点「新会话」开始一条独立任务；历史会话按标题搜索。不要把不相关的问题堆在同一个会话里，便于回溯和删除。"},
            {title: "写清楚目标与范围", body: "说明时间、站点、店铺、SKU、币种或单据类型。例如「上月欧洲站 FBA 仓储费按 SKU 汇总」，比「查一下费用」更容易一次查准。"},
            {title: "发送", body: "Enter 发送，Shift+Enter 换行。支持图片的模型可点「添加图片」。发送后会看到思考、工具轨迹和流式回答。"},
            {title: "看结果，而不是只看一句话", body: "查数工具会把完整表格存进结果库，对话里通常只有摘要和预览。需要明细时，从工具结果打开分页查看（单页最多 200 行）。"},
            {title: "中断与继续", body: "长任务可点「停止」。若停在审批或中断状态，处理后点「继续」从原会话恢复，不必重问一遍。"}
          ]
        },
        {
          heading: "系统实际怎么跑",
          items: [
            "协调助手（Coordinator）理解目标、拆子任务，自己不直接打专业数据库。",
            "分析助手按委派策略查数：通用模式走一个通用分析助手；专业模式最多并行 3 个（Amazon / 利润 / ERP）。",
            "没有权限的业务工具不会出现在对应分析助手上，提问时会得到中文原因和管理员建议。",
            "知识检索调用文枢；公开网页搜索调用 Tavily。内部制度和外部网页不要混为一谈。"
          ]
        }
      ],
      callouts: [
        {kind: "tip", title: "写问题的习惯", body: "带上时间范围、站点和计算口径（求和、计数、按什么分组）。口径越清楚，工具生成的查询越稳。"},
        {kind: "warn", title: "高风险操作会暂停", body: "沙箱或有外部副作用的工具会进入审批中心。未批准前会话不会继续执行该步。"}
      ],
      related: ["approvals", "knowledge", "agents", "tools"]
    },
    approvals: {
      title: "审批中心",
      minutes: "3 分钟",
      audience: "管理员、审批员",
      roles: ["admin", "approver"],
      page: "approvals",
      pageLabel: "打开审批中心",
      summary: "高风险或写操作不会自动执行。批准后从原任务继续，拒绝必须写原因。",
      intro: "审批是治理层，不是聊天功能。平台把「模型想调用什么」和「人是否允许这次调用」分开，避免自动对外发消息、改数据或进入高权限沙箱。",
      blocks: [
        {
          heading: "什么会进来",
          items: [
            "高风险工具，例如完全访问沙箱。",
            "带外部副作用的写操作（例如向钉钉推送）。这类调用禁止自动重试，避免批一次发两次。",
            "卡片上能看到工具中文名、调用说明、参数和来源会话。"
          ]
        },
        {
          heading: "怎么处理",
          steps: [
            {title: "核对说明和参数", body: "先读操作说明，再展开「调用参数」。确认对象、范围和内容是你要的，而不是模型猜的。"},
            {title: "回到原任务核对上下文", body: "点「在任务中打开」查看完整对话，避免只看一行 JSON 就批准。"},
            {title: "批准或拒绝", body: "批准后运行时从该步恢复。拒绝必须填写备注，这条原因会进入审计，便于事后说明。"}
          ]
        }
      ],
      callouts: [
        {kind: "tip", title: "角标", body: "侧栏审批中心旁的数字是当前待处理件数。运行概览顶部也会提示待审批。"}
      ],
      related: ["agent-chat", "audit", "tools"]
    },
    knowledge: {
      title: "知识库",
      minutes: "5 分钟",
      audience: "管理员",
      roles: ["admin"],
      page: "knowledge",
      pageLabel: "打开知识库",
      summary: "制度、手册、官方说明放进文枢知识空间。任务里检索的是切片，不是整份文件。",
      intro: "SellerForge 的知识页是外壳：分类、上传、检索和状态展示走运营平台，解析、切片、向量化在文枢完成。不要在连接器里给知识文档手工对接 Collection。",
      blocks: [
        {
          heading: "和长期记忆的区别",
          items: [
            "知识库：团队共享的文档与制度，适合「报销怎么走」「VAT 怎么报」。",
            "长期记忆：跨会话的事实与偏好，适合「记住我们欧洲站默认用 EUR」。",
            "网页搜索：公开互联网。内部规定不要依赖搜索引擎。"
          ]
        },
        {
          heading: "日常操作",
          steps: [
            {title: "选择或新建空间", body: "右上角「新建空间」只需名称和 Embedding 模型。Qdrant Collection 由文枢自动创建。下拉框按「名称 · 篇数」区分空间。"},
            {title: "分类", body: "左侧树是当前空间的分类。点分类只列出该类文档。可新建一级或子分类。未分类单独统计。"},
            {title: "上传文档", body: "「上传文档」选择文件、标题、类型、标签和分类。重复内容可按策略跳过。上传后看解析、向量化两列是否变绿。"},
            {title: "看切片", body: "点一行打开文档。可重新解析、重新向量化或删除。列表底部分页，每页 20 篇；换空间或分类会回到第 1 页。"},
            {title: "检索切片", body: "顶栏输入具体条款、错误码或业务词，例如 AUTH-1003。命中的是切片而不是文件名。点「返回文档」回到列表。"},
            {title: "分类与重建", body: "结构调整后可用「分类与重建」按新结构重做已有切片向量。库很大时会排队数分钟，确认后再执行。"}
          ]
        }
      ],
      callouts: [
        {kind: "warn", title: "没向量化就检索不到", body: "解析完成只代表正文出来了。任务里的 search_knowledge 依赖向量化成功。两列都要绿。"},
        {kind: "tip", title: "文枢工作台", body: "大批量导入、复杂解析问题可到文枢（默认 8000 端口）处理，结果仍会反映在本页。"}
      ],
      related: ["agent-chat", "connectors", "settings"]
    },
    dashboard: {
      title: "运行概览",
      minutes: "2 分钟",
      audience: "管理员",
      roles: ["admin"],
      page: "dashboard",
      pageLabel: "打开运行概览",
      summary: "看待审批、失败率、Token、延迟和近 14 日趋势，用来发现异常而不是替代审计。",
      intro: "概览汇总当前租户的运行指标。卡片是累计值；趋势图按 UTC 自然日补齐近 14 天，没有任务的日期为 0。",
      blocks: [
        {
          heading: "卡片含义",
          items: [
            "待审批：需要人点头的工具调用。非 0 时顶部会有提示条。",
            "审计：控制面操作条数，点卡片可进审计日志。",
            "对话回合 / 失败率：完成回合与失败、超时、取消、超预算的占比。",
            "用量：Token（输入+输出）、平均延迟、工具调用次数、按模型费率估算的美元成本（示意，不是账单）。"
          ]
        },
        {
          heading: "图怎么读",
          items: [
            "对话回合：每天完成了多少轮。",
            "回合状态：完成与失败等累计分布。",
            "Token：近 14 日用量曲线。",
            "模型分布：各模型承担了多少回合。若近 14 日无新回合，状态和模型仍可能显示历史累计。"
          ]
        }
      ],
      callouts: [
        {kind: "tip", title: "成本数字", body: "估算按内置的百万 Token 单价示意，用于对比模型用量，不能当结算依据。"}
      ],
      related: ["audit", "approvals", "settings"]
    },
    agents: {
      title: "助手",
      minutes: "4 分钟",
      audience: "所有角色（策略由管理员保存）",
      page: "agents",
      pageLabel: "打开助手",
      summary: "协调助手负责拆任务，分析助手负责查数。专业模式下最多并行 3 个领域助手。",
      intro: "不要让同一个助手既决策又打全部数据库。页面上的委派策略决定 Coordinator 如何找分析助手。",
      blocks: [
        {
          heading: "两种委派策略",
          steps: [
            {title: "通用分析助手", body: "适合口径简单、不必按业务线拆开的问题。Coordinator 把查数交给通用 Analyst。"},
            {title: "并行专业分析", body: "按领域委派：Amazon 财务、利润、ERP（金蝶）。同一会话最多 3 个并行。专业助手有独立工具白名单，不能再委派，避免递归越权。"},
            {title: "保存", body: "改完下拉框后点「保存」。未保存不会生效。"}
          ]
        },
        {
          heading: "卡片上能改什么",
          items: [
            "名称、角色说明、是否启用、系统提示。",
            "协调器只保留委派和检索类工具；查数必须给分析助手。",
            "用户没有某业务工具权限时，对应专业分析助手不会展示给他。"
          ]
        }
      ],
      callouts: [
        {kind: "warn", title: "改提示词要克制", body: "系统提示影响拆任务和是否乱调用工具。先小范围验证，再给全员使用。"}
      ],
      related: ["agent-chat", "tools", "access"]
    },
    tools: {
      title: "工具",
      minutes: "4 分钟",
      audience: "所有角色可看，绑定连接需有权限配置",
      page: "tools",
      pageLabel: "打开工具",
      summary: "工具描述「能做什么」。业务工具必须绑连接和数据范围；系统内置工具默认可用。",
      intro: "最终能不能调用，是「助手职责 ∩ 用户权限组 ∩ 工具绑定的连接 ∩ 连接上的数据范围」的交集。",
      blocks: [
        {
          heading: "两组工具",
          items: [
            "业务工具：Amazon 结算、领星利润、利润表、金蝶、网页搜索等。需要连接器，并在本页选择连接、必要时填写数据范围（逗号分隔，* 表示连接允许的全部）。",
            "系统内置：记忆、沙箱、委派等。不走业务权限组，管理员以外的用户也可以按运行时规则使用。"
          ]
        },
        {
          heading: "绑定步骤",
          steps: [
            {title: "先有连接", body: "没有可选连接时，按提示去连接器页创建。停用的连接仍会出现但不应再绑给生产任务。"},
            {title: "选连接并保存", body: "下拉选择后点「保存连接与范围」。只改下拉不保存不会生效。"},
            {title: "看风险标记", body: "低 / 中 / 高。高风险或写入更可能进审批。只读查询一般不会拦在审批里。"}
          ]
        }
      ],
      callouts: [
        {kind: "tip", title: "范围只能缩小", body: "工具上的数据范围不能超出连接器里配置的范围。想查更多库或店铺，先改连接再改工具。"}
      ],
      related: ["connectors", "access", "agent-chat"]
    },
    connectors: {
      title: "连接器",
      minutes: "5 分钟",
      audience: "管理员",
      roles: ["admin"],
      page: "connectors",
      pageLabel: "打开连接器",
      summary: "凭证和可达范围放在这里。同一类型可建多个实例，例如测试库和生产库。",
      intro: "连接器不是知识文档入库。Qdrant/Milvus 连接给记忆等向量后端用；文档解析与检索走文枢。",
      blocks: [
        {
          heading: "各类型怎么配",
          items: [
            "数据分析数据库：只读 PostgreSQL 或 MySQL DSN，供 Amazon 结算和利润表查询。",
            "领星 OpenAPI：App ID / Secret，并把出口 IP 加入领星白名单。",
            "金蝶云星空：Web API 第三方登录，服务地址以 /K3Cloud 结尾。",
            "钉钉：用于待办或消息推送，属于写操作，审批和禁止乱重试更严格。",
            "Tavily：Coordinator 的 web_search。保存 API Key 后任务里才能搜公开网页。",
            "Qdrant / Milvus：Agent 个人记忆等向量后端，不要在这里手工对接文枢 Collection。"
          ]
        },
        {
          heading: "安全习惯",
          steps: [
            {title: "命名分环境", body: "名称写成「生产-结算只读」比「数据库1」清楚，工具绑定时不容易点错。"},
            {title: "凭证不回显", body: "保存后页面只给脱敏状态。改密码或 Secret 时重新填写。"},
            {title: "停用而不是误删", body: "被知识空间或工具引用的连接往往不能删。先解绑或停用。"}
          ]
        }
      ],
      callouts: [
        {kind: "warn", title: "只配连接还不能查数", body: "必须再到工具页把对应业务工具绑到这条连接，用户还要在权限组里拥有该工具。"}
      ],
      related: ["tools", "knowledge", "access"]
    },
    memory: {
      title: "长期记忆",
      minutes: "4 分钟",
      audience: "管理员",
      roles: ["admin"],
      page: "memory",
      pageLabel: "打开长期记忆",
      summary: "跨会话记住事实和偏好。自动抽出的内容先当候选，确认后才生效。",
      intro: "记忆按租户隔离。协调助手每轮只带一小段相关快照；分析助手不直接翻记忆库，只用委派时固化的快照。",
      blocks: [
        {
          heading: "范围",
          items: [
            "用户：该人自己的事实。",
            "用户画像：相对稳定的偏好或角色信息。",
            "组织知识：租户内共享、又还不值得做成知识库文档的条目。",
            "助手：某个 Agent 自己的工作记忆。"
          ]
        },
        {
          heading: "状态与操作",
          steps: [
            {title: "筛选", body: "按关键词或语义、状态、范围、用户筛选。"},
            {title: "确认 / 拒绝 / 纠正", body: "候选和冲突不要直接当事实。确认后生效；拒绝丢弃；纠正会留下替代关系。"},
            {title: "合规删除", body: "用户要求遗忘或监管删除时走删除，而不是改一句提示词假装没了。"},
            {title: "对话里怎么写入", body: "用户明确说「记住…」时，运行时才调用记住事实。不要指望闲聊全部自动入库。"}
          ]
        }
      ],
      callouts: [
        {kind: "tip", title: "制度放知识库", body: "会改版的手册、PDF、官方页面放知识库。记忆只放短事实，避免两套真相。"}
      ],
      related: ["knowledge", "agent-chat", "audit"]
    },
    access: {
      title: "用户与权限",
      minutes: "5 分钟",
      audience: "管理员",
      roles: ["admin"],
      page: "access",
      pageLabel: "打开用户与权限",
      summary: "用户加入权限组，组内勾选业务工具。管理员默认具备全部业务工具，仍受连接数据范围约束。",
      intro: "角色管的是能进哪些页面；权限组管的是能调哪些业务工具。两套不要混用。",
      blocks: [
        {
          heading: "角色",
          items: [
            "管理员：全部管理页 + 全部业务工具。",
            "操作员：做任务、看助手和工具，不进连接器、权限、审计等管理页。",
            "审批员：额外进入审批中心。",
            "只读：以查看为主，不能当日常改配置的账号。"
          ]
        },
        {
          heading: "怎么授权",
          steps: [
            {title: "添加用户", body: "指定账号、显示名、角色。可生成临时密码，对方首次登录必须改密。会话在重置密码后立即失效。"},
            {title: "添加权限组", body: "按岗位建组，例如「财务只读」「运营查询」。在组内勾选业务工具，可多选。"},
            {title: "用户加入组", body: "一行用户可以加入多个组。工具授权取并集。点标签上的 × 可移出。"},
            {title: "看规则明细", body: "页面下方只读列出当前生效关系，用来核对「为什么某人能调这个工具」。"}
          ]
        }
      ],
      callouts: [
        {kind: "warn", title: "系统工具不在组里勾", body: "记忆、沙箱、委派等默认开放。不要指望不建组就能查 Amazon；那是业务工具，必须进组。"}
      ],
      related: ["tools", "agents", "audit"]
    },
    audit: {
      title: "审计日志",
      minutes: "2 分钟",
      audience: "管理员",
      roles: ["admin"],
      page: "audit",
      pageLabel: "打开审计日志",
      summary: "登录、权限、连接、模型、会话删除和查询类关键操作都留痕，详情按字段展示。",
      intro: "审计回答「谁在什么时候对什么做了什么」。运行概览回答「整体是否健康」。排故障两份一起看。",
      blocks: [
        {
          heading: "怎么用",
          steps: [
            {title: "筛选", body: "顶栏按操作者、动作或资源关键字过滤。"},
            {title: "读状态", body: "成功、失败、已删除、已记录。失败行优先看详情里的原因。"},
            {title: "读详情标签", body: "连接类型、事件数、模型参数等拆成胶囊。过长内容悬停可看全文。"}
          ]
        }
      ],
      callouts: [
        {kind: "tip", title: "删会话也会记", body: "删除任务会话会记一条审计，并清理对应的结果库数据。"}
      ],
      related: ["dashboard", "access", "agent-chat"]
    },
    settings: {
      title: "系统设置",
      minutes: "4 分钟",
      audience: "管理员",
      roles: ["admin"],
      page: "settings",
      pageLabel: "打开系统设置",
      summary: "配置对话模型和发给模型的上下文窗口。没有可用模型时，任务发不出去。",
      intro: "生产环境关闭自助注册，账号由管理员下发。密钥不会出现在设置页的返回结果里。",
      blocks: [
        {
          heading: "模型",
          steps: [
            {title: "添加模型", body: "选择提供方（OpenAI 兼容、智谱、通义、DeepSeek 等），填写名称、模型 ID、Base URL 和密钥。"},
            {title: "声明能力", body: "是否支持图片必须如实勾选。不支持却传图，会在调用前被拦截。"},
            {title: "设默认并启用", body: "任务页下拉只出现已启用模型。改默认后新会话走新默认，已打开的会话仍以当前选择为准。"}
          ]
        },
        {
          heading: "滑动窗口",
          items: [
            "只把最近若干用户轮次发给模型，系统提示始终保留。完整事件仍在事件库。",
            "可限制最大消息条数、字符数，以及旧工具结果的行数和字符。这是为了控 Token，不是删历史。",
            "改完必须点保存。"
          ]
        }
      ],
      callouts: [
        {kind: "warn", title: "预算", body: "本回合 Token 预算超限会停止该回合，避免工具死循环。调大前先看运行概览里的用量。"}
      ],
      related: ["agent-chat", "dashboard", "connectors"]
    }
  }
};
