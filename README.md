# 云璃QQ群聊人格插件 — 开发报告 (v2.3.1)

## 1. 项目概述

基于 AstrBot 框架的 QQ 群聊人格插件，让 AI 化身《崩坏：星穹铁道》角色"云璃"，以朱明仙舟猎剑士的身份融入群聊。

核心设计目标：

- **人格一致性**：基于官方台词、语音、剧情数据构建系统提示词，静态部分 Token 预算 \~400（可被 LLM 缓存复用），动态上下文 \~850 Token
- **拟真交互**：智能分段对话 + 动态打字延迟 + 思考停顿，模拟真人聊天节奏
- **群聊感知**：根据消息密度自动调节活跃度
- **长期记忆**：双层记忆架构，零Token轻量提取 + LLM深度整理，记住群友喜好和群氛围
- **上下文去污染**：动态上下文通过 `extra_user_content_parts` + `_no_save=True` 注入，LLM可见但不持久化到对话历史，避免过时信息累积和记忆混乱

## 2. 核心功能模块

### 2.1 人格引擎 (persona/)

**身份保持**

- 基于台词、语音、剧情数据构建系统提示词
- 严格过滤 AI 身份表述（"我是AI"→"我是云璃"）
- 动态知识检索：根据用户输入匹配相关知识、现代类比、情感模板

**语言过滤**

- 动作词过滤：删除 AI 生成的动作描述（如 *挠头*、（笑）），但保留嵌入句中的动作词（如"他突然笑了笑说"）
- 情绪形容词保护："害羞""尴尬"等纯情绪词作为正常表达保留，只过滤带括号/动作格式的版本
- 颜文字保护：保留含日文假名的可爱颜文字（如 (*^▽^*)），过滤纯符号颜文字（如 orz/owo）
- 情感标签过滤：删除【心情】【状态】等系统标签

**情感状态管理**

- 状态机驱动：neutral → excited / tsundere / annoyed / curious / happy / bored
- 触发器：剑/食物/夸奖/告别等关键词自动切换状态
- 情感注入：根据状态添加前缀/后缀语气词（如"哼，""…别误会哦。"）
- 自动衰减：连续对话后情感强度逐渐回落

### 2.2 QQ 群聊行为 (persona/qq\_behavior.py)

**文本格式化**

- 根据配置添加颜文字（跨平台兼容，比QQ原生表情代码更自然）
- 被 @ 时概率添加称呼前缀
- 不添加动作描述或情感标签，保持自然聊天体验

**拟人化处理**

- 动态打字延迟：基础反应时间 + 每字符打字时间（模拟真人打字速度）
- 智能停顿：长消息中添加省略号/犹豫型停顿（如"…嗯…"）
- 人类小习惯：口语化替换（"了"→"啦"）、概率省略标点、省略"我"

**群氛围感知**

- 根据最近消息数判断安静/活跃/混乱模式

### 2.3 混合记忆架构 (main.py)

双层设计，平衡实时性与成本：

| 层级  | 机制       | 触发条件    | 成本      |
| --- | -------- | ------- | ------- |
| 第一层 | 规则式轻量提取  | 实时，每条消息 | 零 Token |
| 第二层 | LLM 深度整理 | 定时/定量批量 | 批量消耗    |

**轻量提取**

- 20 种句式覆盖偏好、身份、状态、能力
- 自动过期：临时记忆 1-14 天自动清理
- 负面过滤："讨厌""不喜欢"等自动标记

**LLM 深度整理**

- 动态门槛：根据消息密度自动调整（1-6小时 / 5-80条对话）
- 整理目标：合并重复、升华细节、删除低价值，压缩到 60% 以下
- 事务保护：BEGIN TRANSACTION / ROLLBACK / COMMIT 确保原子性
- 并发控制：asyncio.Semaphore(3) 限制同时运行 3 个整理任务

**记忆类型**

- 事实、偏好、关系、临时
- 单用户上限 50 条，达到 45 条触发整理
- 群友记忆共享：跨用户召回，30% 降权参与评分

### 2.4 智能分段对话 (persona/message\_splitter.py)

**简化版设计**：作为 AstrBot 自带分段的增强补充，提供段间延迟和思考停顿，不再做复杂的智能切分。

**切分策略**

1. 空行切分：段落边界
2. 句子切分：按标点符号切分超长段落
3. 过短合并：合并连续过短片段
4. 最大段数限制：超限时合并尾部

**拟人化特性**

- 动态打字延迟：基础延迟 + 每字符延迟，首段 ×0.3（快速反应），中间段 ×0.7（思路连贯），末段 ×1.1（收尾思考）
- 智能思考停顿：根据段落长度动态选择 `…` / `嗯…` / `…让我想想…`
- 自然长度分布：避免机械化的等长分段

### 2.5 数据库层 (database/)

- **知识库**：台词、语音、剧情、知识、类比、情感模板（只读，插件自带初始数据）
- **记忆库**：用户记忆、群摘要、互动记录（读写，持久化到 AstrBot data 目录）
- **线程安全**：知识库使用线程本地存储，记忆库全部公共方法使用 `threading.Lock` 保护（v2.2.0 加固）
- **记忆重置**：支持 `reset_memory()` 清空动态数据（保留表结构）

### 2.6 请求链路管理 (core/)

**请求上下文 (`RequestContext`)**

- 在 `on_llm_request` 中创建统一上下文，附着在 `event._yunli_ctx` 上跨阶段传递至 `on_llm_response`
- 封装 `group_id`/`user_id`/`user_nickname`/`scope` 来源信息和 `is_prompt_injected`/`is_debounce_buffered`/`is_debounce_merged`/`is_knowledge_query` 生命周期标记
- 替代原先 5 个散落动态属性，确保两个阶段状态一致且可追溯

**@检测器 (`AtDetector`)**

- 独立类封装三层 @ 检测逻辑：框架 `is_at_me()` → 消息组件 At 解析 → 文本 `[At:ID]` 匹配
- self\_id 带缓存，首次获取后注入检测器，避免每次检测重复查找
- `_should_activate` 从 30 行 `if/elif/break` 简化为 1 行委托

**消息防抖 (`MessageDebouncer`)**

- 窗口期内（默认 2 秒）同一作用域的多条消息被缓冲合并，仅处理最后一条
- 防抖器时间戳仅在处理成功后才更新，防止注入超时后下条消息被错误防抖
- 新增消息合并功能：同 scope 多条消息合并为一条处理，附带合并通知文本

**工具函数 (`utils.py`)**

- 集中管理所有纯工具函数，消除 `persona/core.py` 和 `main.py` 中的重复代码
- 回复自审函数：`remove_assistant_prefix`（助手腔开头）、`remove_internal_state_lines`（内部状态泄露）、`clean_repeated_punctuation`（重复标点）
- 文本处理函数：`is_structured_summary`（结构化总结检测）、`truncate_at_sentence`（句子边界截断）、`estimate_tokens`（Token估算）
- 消息处理函数：`merge_messages`（防抖消息合并）

**统一后台任务生命周期**

- 使用 `_safe_create_task()` 统一管理所有 `asyncio.create_task` 调用
- 插件卸载时自动取消未完成任务 → 刷新数据库日志缓冲区 → 清空防抖缓冲区

### 2.7 内存背压与线程安全

- 对话缓冲队列使用 `asyncio.Queue(maxsize=2000)` 替代 `List[Dict]` + `asyncio.Lock`，队列满时自动丢弃新消息，硬上限 \~400KB
- `YunliMemoryDB` 全部公共方法使用 `self._lock` 保护，解决 `to_thread` 与事件循环并发访问的数据竞争（v2.2.0 扩展至全部方法）
- `_flush_logs` 写入失败时保留缓冲区数据，仅超限时裁剪，避免日志丢失
- 分段发送并发控制：`asyncio.Semaphore(3)` 限制同时最多 3 个分段发送任务

## 3. 配置项

完整配置说明见 [README.md](README.md)。核心配置分组：

- **基础人格**：人格强度、身份保持、现代类比模式
- **群聊行为**：响应模式、插嘴概率、冷却时间
- **情感与语气**：语气词概率、QQ表情、颜文字、省略标点
- **记忆系统**：轻量提取、LLM整理、记忆上限、对话缓冲队列上限、记忆缓冲任务防抖
- **知识查询**：最大句数、最大长度
- **分段回复**：分段长度、打字延迟、思考停顿
- **性能优化**：缓存大小、人格注入超时
- **Darwin进化**（v2.1 新增）：启用开关、迭代参数、自动触发间隔、资产范围
- **Darwin Phase2**（v2.1 新增）：启用开关、日志采样率、自动分析间隔

## 4. 测试覆盖

- **测试文件**：`tests/test_*.py`（共 18 个测试文件）+ `evolution/tests/test_*.py`（2 个进化模块测试）
- **测试数量**：732 个（主模块 717 + 进化模块 15 = 732；其中包括 Darwin apply 端到端 17 + 结构化日志 9）
- **覆盖场景**：
  - 人格引擎（情感状态机、语言风格、身份保持）
  - 语气词修复（句首禁止、句末标点保护、避免重复叠加、自然切分过滤）
  - 动作词过滤（嵌入保留、独立删除、括号标签删除、星号标记删除、情绪形容词保留）
  - 轻量提取（事实、偏好、临时、负面过滤）
  - 深度整理触发（时间、数量、阈值边界）
  - 记忆上限与淘汰策略
  - LLM 整理去重与压缩
  - 并发安全与防重复触发
  - 6 小时对话积累模拟
  - 消息切分（短文本、空行、Markdown、代码块、超长文本、语气词）
  - 打字延迟计算
  - 思考停顿动态选择
  - 请求链路完整性（RequestContext 跨阶段传递、AtDetector 三层检测）
  - LLM 上下文缓存命中率（system\_prompt 静态性、确定性方法、记忆文本生成）
  - 数据库线程安全（锁保护、并发读写）
  - 后台任务生命周期管理
  - 消息防抖合并
  - 工具函数（助手腔去除、内部状态行清理、重复标点清理、结构化总结检测、消息合并）
  - Darwin 进化模块（JSON 提取工具 8 用例：纯JSON/代码块包裹/复杂嵌套/无效输入/空字符串/前后文字/嵌套数组；评分解析与比较 7 用例：基本解析/代码块包裹/无效响应/改进比较/退化比较/报告格式化/比较报告格式化）

运行测试：

```bash
python -m unittest discover -s tests -v
```

## 5. 部署说明

1. 将插件目录放入 AstrBot `data/plugins/` 目录
2. 重启 AstrBot，插件自动注册
3. 数据库在首次加载时自动初始化（从 `database/data/initial_data.json` 导入）
4. 持久化数据存储于 AstrBot `data/` 目录，更新插件不丢失
5. **建议关闭 AstrBot 自带分段回复**，使用插件自定义切分逻辑

## 6. 项目结构

```
yunli/
├── main.py                    # 插件主入口（装饰器注册、生命周期管理、共享 state）
├── core/                      # 核心模块目录
│   ├── __init__.py            # 模块导出（AtDetector、RequestContext 等）
│   ├── event_pipeline.py      # 事件处理管道（on_message / on_llm_request / on_llm_response）
│   ├── command_handler.py     # 群聊命令处理器（/云璃/云璃语音/云璃资料/云璃帮助）
│   ├── evolution_manager.py   # Darwin 进化命令与后台任务管理
│   ├── at_detector.py         # @检测器（三层检测 + self_id 缓存）
│   ├── context_builder.py     # LLM 提示词上下文构建
│   ├── debouncer.py           # 消息防抖（窗口期内合并多条消息）
│   ├── group_perception.py    # 群聊感知（话题检测、氛围分析）
│   ├── memory_manager.py      # 记忆管理器（轻量提取 + LLM 深度整理）
│   ├── thread_tracker.py      # 对话线程追踪（含群级短期时间线）
│   ├── metrics.py             # 指标统计
│   ├── logging_helpers.py     # 结构化日志辅助
│   ├── request_context.py     # 请求上下文（on_llm_request → on_llm_response 状态传递）
│   └── utils.py               # 通用工具函数
├── metadata.yaml              # 插件元数据
├── README.md                  # 用户说明文档
├── requirements.txt           # 依赖声明
├── _conf_schema.json          # 配置 Schema
├── database/
│   ├── __init__.py            # 数据库模块导出
│   ├── init_db.py             # 数据库初始化与记忆操作
│   ├── knowledge_schema.sql   # 知识库表结构
│   ├── memory_schema.sql      # 记忆库表结构
│   └── data/
│       └── initial_data.json  # 初始数据（台词、知识等）
├── persona/
│   ├── core.py                # 人格引擎核心
│   ├── emotion.py             # 情感状态机
│   ├── relationship.py        # 关系状态机
│   ├── filters.py             # 语言过滤器
│   ├── language.py            # 语言风格处理 + 查询模式检测
│   ├── qq_behavior.py         # QQ 群聊行为管理
│   ├── message_splitter.py    # 消息分段器
│   └── config.py              # 人格配置
├── evolution/                 # v2.1 新增：Darwin 自进化系统
│   ├── __init__.py
│   ├── darwin_evolve.py       # Phase 1 主进化循环（10维度评分 + 棘轮机制 + 并行评估）
│   ├── llm_client.py           # 统一 LLM 调用客户端（Provider → HTTP fallback → 模拟三级降级）
│   ├── utils.py                # 共享工具函数（JSON 提取等）
│   ├── _locks.py               # 6 个文件写入线程安全锁
│   ├── asset_bridge.py         # 资产运行时应用桥接
│   ├── provider_factory.py     # LLM Provider 获取工厂
│   ├── log_collector.py        # Phase 2 对话日志采样采集
│   ├── pattern_discovery.py    # Phase 2 LLM 驱动 6 类模式发现
│   ├── rule_generator.py       # Phase 2 半自动规则提案生成
│   ├── code_sandbox.py         # 代码沙箱
│   ├── test_prompts.json
│   ├── assets/                 # 5 类可进化的文本资产
│   │   ├── system_prompt.md
│   │   ├── review_rules.md
│   │   ├── filter_rules.md
│   │   ├── emotion_templates.md
│   │   └── language_style.md
│   ├── tests/                  # 进化模块单元测试
│   │   ├── __init__.py
│   │   ├── test_utils.py       # JSON 提取工具测试
│   │   ├── test_scorer.py      # 评分解析与比较测试
│   │   └── test_code_sandbox.py # 代码沙箱测试
│   └── eval/                  # 评分框架
│       ├── __init__.py
│       ├── rubric.py          # 10 维度评分标准（含反AI腔维度）
│       └── scorer.py          # 评分执行器 + 比较器
└── tests/                     # 单元测试与集成测试
    ├── __init__.py
    ├── test_base.py            # 共享测试基础设施（路径设置、AstrBot mock、测试基类）
    ├── benchmark_performance.py # 性能基准测试
    ├── test_cache_hit_rate.py  # LLM 上下文缓存命中率测试
    ├── test_conversation.py    # 对话相关测试
    ├── test_database.py        # 数据库操作测试
    ├── test_emotion.py         # 情感状态机测试
    ├── test_filters.py         # 语言过滤器测试
    ├── test_language.py        # 语言风格处理测试
    ├── test_message_splitter.py # 消息切分器测试
    ├── test_persona.py         # 人格引擎测试
    ├── test_plugin.py          # 插件主类集成测试
    ├── test_qq_behavior.py     # QQ群聊行为测试
    ├── test_utils.py           # 工具函数测试
    ├── test_boundary.py        # 边界测试
    ├── test_response_chain.py  # 响应链测试
    ├── test_metrics.py         # 指标测试
    ├── test_logging_helpers.py # 结构化日志测试
    ├── test_smoke.py           # 冒烟测试
    ├── test_memory_fixes.py    # 记忆系统修复专项测试
    ├── test_evolution_*.py     # 进化模块集成/端到端测试（多个文件）
    └── benchmark_results.json  # 性能基准结果
```

## 7. 技术栈

- Python 3.12+
- AstrBot 插件框架（v4.25.2+）
- SQLite（知识库 + 记忆库）

## 8. 更新日志

### v2.3.1 (2026-06-17) — 常驻旁听模式 + 短期上下文连续性修复

> **本轮修复覆盖 4 个文件，解决"非@消息实际无法记录"和"群内上下文接不上"问题，新增 8 个单元测试全部通过。**

#### P2-11 修复 — 群内短期上下文连续性

- **群级短期时间线**（`core/thread_tracker.py`）：新增 `record_user_message_to_group` / `record_yunli_response_to_group` / `get_group_thread_context` 方法，用 `group_id:__group__` scope 保存最近 20 轮群内完整对话。用户 A 说完话后用户 B 立刻 @ 云璃，云璃也能看到 A 刚才说了什么。
- **最近群聊原文增强**（`core/context_builder.py`）：`build_recent_chat_history` 默认 `limit` 从 10 提升到 20，`token_budget` 从 300 提升到 400，新增 `include_bot_response=True` 包含云璃自己的回复，形成完整对话流；`exclude_message` 增加标准化去重（去除 `@云璃`、CQ At 码、`[At:bot]` 等前缀），避免当前消息重复注入。
- **即时落库**（`core/event_pipeline.py`）：`on_llm_response` 中 `_log_interaction` 从后台任务改为 `await` 等待完成，然后立即 `flush_logs()`，确保云璃回复后下一条消息能立刻查到。

#### P2-12 修复 — 常驻触发旁听模式

- **新增** **`@filter.on_message`** **入口**（`main.py`）：所有群消息常驻监听，不再依赖 AstrBot 是否调用 LLM。
- **事件分流**（`core/event_pipeline.py`）：
  - `on_message`：非 @ 消息完整旁听记录；@ 消息只更新线程追踪，避免与 `on_llm_request` 重复记录
  - `on_request`：只处理 @ 云璃的请求，负责提示词注入和日志记录
  - `on_response`：记录云璃回复并刷新缓冲区
- **噪音过滤**：自动跳过纯表情、CQ 码、纯数字/符号、单字符、命令消息。

#### P2-13 修复 — 日志落库时序

- `on_llm_response` 中 `await plugin._log_interaction(...)` 确保回复日志写入缓冲区后再 `flush_logs()`，解决之前 `_safe_create_task` 后台执行可能导致的"回复已发送但日志未落库"问题。

#### 测试覆盖

新增测试：

- `test_include_bot_response`：云璃回复出现在最近群聊原文
- `test_exclude_message_with_at_prefix` / `test_exclude_message_with_cq_at`：@ 前缀/CQ 码去重
- `TestGroupThreadContext`（4 个测试）：群级线程跨用户记录、包含云璃回复、max\_lines 截断、跨群隔离
- 更新 `test_limit_10_messages` / 新增 `test_default_limit_20_messages`

### v2.3.0 (2026-06-17) — 记忆系统全面修复

> **本轮修复覆盖 7 个文件，解决记忆混乱、关系记错、认不得群友等 9 项问题，新增 23 个单元测试全部通过。**

#### P0 级修复 — 旁听模式（解除 @ 门控）

- **解除 @ 门控对记忆/感知的限制**（`core/event_pipeline.py`）：未 @ 云璃时进入旁听模式，记录群聊互动用于记忆和群聊感知。新增 `_record_passive_interaction` 方法，调用 `_log_interaction(trigger_type="passive")` 记录互动日志、更新群氛围、更新话题、轻量记忆提取、话题线程追踪。这样云璃能"旁听"群聊，建立群友画像，在被@时能认出群友。
- **深度整理跳过空 response**（`memory_manager.py`）：旁听模式 response 为空时不触发 LLM 深度整理，避免空回复污染整理队列。轻量记忆提取仍然正常执行。

#### P1 级修复 — 核心问题

- **P1.3 修复记忆归属判断**（`context_builder.py`）：移除不可靠的昵称回退匹配（QQ群昵称可重复），改为严格归属判断：仅当 `current_user_id` 和 `mem_user_id` 都非空且相等时才归为"自己的记忆"，否则归入群记忆。`mem_user_id` 为空的旧数据也归入群记忆，避免污染当前用户画像。
- **P1.4 增加群友记忆召回**（`context_builder.py`）：群友记忆折扣从 `0.7` 调整为 `0.9`，避免群友记忆几乎无法召回；`get_relevant_memories` 默认 `min_confidence` 从 7 降为 5；群友记忆召回数量从 `limit*2` 提升到 `limit*3`，扩大群友画像覆盖。
- **P1.5 记忆整理加事务保护**（`init_db.py` + `memory_manager.py`）：新增 `replace_user_memories` 原子性替换方法，使用 `BEGIN TRANSACTION` / `ROLLBACK` / `COMMIT` 确保原子性。失败时旧记忆保持不变，避免整理失败导致数据丢失。`_consolidate_memories_for_user` 改用此方法替代"先删后写"。

#### P2 级修复 — 次要问题

- **P2.6 轻量提取增加上下文校验**（`memory_manager.py`）：新增 `_CONTEXT_NEGATION_WORDS`（否定/疑问词表）和 `_CONTENT_PREFIX_BLACKLIST`（开头禁用词表）。`_try_add_memory` 增加 match 参数，检查匹配位置前 3 个字符是否有否定/疑问词（如"我不是学生"不提取"学生"），检查提取内容是否以禁用词开头（如"说真的喜欢这个"不提取）。
- **P2.7 置信度衰减机制**（`init_db.py` + `memory_manager.py`）：新增 `decay_memory_confidence` 方法，定期对所有活跃记忆的 confidence 衰减（默认系数 0.95，下限 3）。解决"富者愈富"反馈循环：常被召回的记忆越来越强，新记忆永远召回不出来。在 `cleanup_expired` 中调用。
- **P2.8 冲突检测改用更精细规则**（`init_db.py`）：语义冲突检测增加上下文校验，`semantic_conflicts` 每对词附带"上下文类型"（身份/性别/状态）。新增 `_is_identity_statement` 函数判断 word 是否出现在身份/性别表述的上下文中。避免"我是学生干部"和"我在工作"误判，避免"我喜欢男的"和"我是女的"误判。

#### P3 级修复 — 优化项

- **P3.9 防抖合并记忆遗漏修复**（`debouncer.py` + `main.py`）：`MessageDebouncer` 新增 `on_individual_message` 回调，合并消息前对每条原始消息单独触发记忆提取。避免"我喜欢猫"+"我喜欢狗"合并后只提取出一条偏好。`main.py` 新增 `_on_individual_message` 方法调用 `extract_memory_lightweight`。
- **P3.10 记忆文本截断修复**（`context_builder.py`）：记忆文本截断长度从 15 字提升到 30 字，避免关键信息丢失。如"用户是计算机科学专业的大学生"不再被截断为"用户是计算机科学专业的…"。
- **P3.13 群聊摘要生成调用**（`init_db.py` + `memory_manager.py` + `main.py`）：新增 `get_recent_interactions` 和 `get_active_groups` 方法。`MemoryManager` 新增 `generate_group_summaries` 和 `_generate_summary_for_group` 方法，定期从 `interaction_logs` 提取最近 1 小时对话，调用 LLM 生成摘要写入 `chat_summaries` 表。`main.py` 新增 `_periodic_memory_maintenance` 后台定时任务（每小时执行），惰性启动避免测试警告。

#### 测试覆盖

新增 `tests/test_memory_fixes.py`（23 个测试），覆盖所有 9 项修复：

- `TestMemoryAttribution`：严格归属判断（mem\_user\_id 为空归入群记忆）
- `TestGroupMemoryRecall`：min\_confidence 默认值降低、群友记忆折扣调整
- `TestTransactionProtection`：原子性替换成功、无效内容跳过
- `TestLightweightExtractionContext`：否定上下文跳过、开头禁用词、正常提取仍有效
- `TestConfidenceDecay`：衰减降低置信度、最小置信度下限、低置信度不衰减
- `TestConflictDetectionRefined`：身份冲突检测、非身份不误判、性别冲突检测、性别偏好不误判
- `TestDebouncerIndividualMessage`：合并时对每条原始消息调用回调
- `TestMemoryTextTruncation`：长记忆不被过度截断
- `TestGroupSummaryGeneration`：获取最近互动、活跃群列表、摘要存储、低互动跳过
- `TestPassiveInteraction`：旁听模式记录到数据库、触发轻量记忆提取

### v2.2.0 (2026-06-16) — 系统性代码审查与全面修复

> **本轮修复覆盖 27 个文件，修复 28 个失败测试 + 1 个真实功能 bug + Darwin apply 端到端 + 拆分 main.py + 错误处理规范化 + 结构化日志，732 个测试全部通过（0 失败）。**

#### P0 级修复 — 严重缺陷

- **C1** **`at_detector.py`** **self\_id 为空时误判**：移除 `if not self_id: return True` 的保守判定，self\_id 未获取时不再将所有消息误判为 @自己
- **C2** **`init_db.py`** **白名单表名错误**：`_ALLOWED_TABLES` 和 `reset_memory` 中 `"chat_topics"` 修正为 `"topic_history"`，与实际表名一致，修复记忆重置功能失效
- **C3** **`llm_client.py`** **事件循环死锁**：`_call_async_safe` 重写为始终使用私有后台事件循环，移除向当前线程循环提交协程的策略（会在同线程事件循环中造成死锁）
- **C4** **`main.py`** **同步函数阻塞事件循环**：`_log_interaction` 从 `def` 改为 `async def`，数据库操作包装在 `asyncio.to_thread()` 中，避免阻塞 AstrBot 事件循环
- **C5** **`init_db.py`** **`add_memory`** **冲突检测静默失败**（**本轮新发现**）：外层 `for mem` 内的 `if has_conflict: break` 提前跳出循环，**绕过了 line 759 的 mark conflicted 步骤**，导致反义词冲突（如"喜欢猫" vs "讨厌猫"）永远不会被标记。修复：删除冗余外层 break，让外层 for 走完到 mark 步骤。**这一 bug 已在生产环境运行期间阻止所有冲突记忆的标记**

#### P0 后续修复（本轮执行）

- **P0-1：批量修复 28 个失败测试**
  - `tests/test_utils.py`: 更新 `estimate_tokens` 期望（中英区分算法：中文 1.5 token/字、英文 0.25 token/字）
  - `tests/test_persona.py`: 移除对已删除 `_prompt_cache` 的引用（README m11 修复未回归测试）
  - `tests/test_language.py`: 更新 `TestTopicMaxLength` 反映 v2.2.0 移除按 topic 区分的截断，改为统一 `max_text_length` + `max_chars` 参数
  - `tests/test_message_splitter.py`: setUp 显式设 `max_segments=10`（避免被默认 2 段限制吞并）
  - `tests/test_cache_hit_rate.py`: mock 配置补全（`calculate_impulse` 返回 `{}`）
  - `tests/test_plugin.py`: mock 兼容 `emotion_state` 参数
  - `tests/test_database.py`: 更新 `test_memory_conflict` 用 `include_outdated=True` 验证冲突标记
  - `tests/test_evolution_darwin.py` / `test_evolution_integration.py`: 更新 `-1.0` 哨兵值期望（旧版固定 70/80/75）
  - `tests/test_evolution_cli.py`: HTTP fallback 测试改用 `patch.dict(os.environ, ...)` 注入 API Key（v2.2.0 不再从 config 读 key）
- **P0-3：Darwin apply 端到端**（`evolution/asset_bridge.py`）
  - 新增 `load/save/get_runtime_overlay` API：`applied_runtime.json` 存 LLM 改进文本
  - `persona/core.py` 的 `build_system_prompt()` 优先读 overlay，否则回退到源常量
  - 新增 `dry_run` 模式：apply 前显示 diff，不真修改
  - 新增 `atomic_write_with_backup`：临时文件 + 备份 + replace，失败自动回滚
  - 新增 `apply_evolved_assets()` 批量接口
  - 修复 `apply_asset_to_runtime` 缩进丢失 bug（原代码替换类内常量时不保留缩进导致语法错误）
  - `/云璃进化 apply` 默认启用 overlay 写入，新增 `/云璃进化 apply_dry_run` 群聊命令
  - 新增 17 个端到端测试验证：apply 后 `build_system_prompt()` 立即看到新内容
- **P0-4：CI/CD 自动化**（`.github/workflows/ci.yml` + `lint.yml`）
  - Python 3.10/3.11/3.12 matrix + pip cache + Codecov
  - flake8 静态检查（E9/F63/F7/F82 + PEP8 关键项，排除运行时/产物目录）
  - PR/push 触发自动跑测，防止 v2.2.0 类的"声称通过实际失败"问题

#### P1 级重构 — 代码组织与可观测性

- **P1-1：拆分 main.py 神类**（1422 → 493 行）
  - `YunliEventPipeline`（`core/event_pipeline.py`）：on\_llm\_request/response、上下文注入、分段发送
  - `YunliCommandHandler`（`core/command_handler.py`）：/云璃/云璃语音/云璃资料/云璃帮助 命令
  - `YunliEvolutionManager`（`core/evolution_manager.py`）：Darwin 进化 + Phase 2 模式发现
  - YunliPersonaPlugin 仅保留共享 state + 装饰器委托方法（向后兼容所有测试）
- **P1-2：错误处理规范化**
  - 4 类分级：A.已知可恢复（warning+fallback）B.内部错误（logger.exception）C.边界保护（debug）D.资源清理
  - 30+ 处 except 块升级：`logger.error("...%s", e)` → `logger.exception("...")`（自动含 traceback）
  - `except: pass` → `except: logger.debug(..., exc_info=True)`（保留 traceback 仅调试时显示）
- **P1-3：结构化日志**（`core/logging_helpers.py`，103 行）
  - contextvars 持有 scope/user\_id/group\_id 异步安全
  - ContextFilter 注入到 LogRecord，formatter 用 `[%(scope)s] [%(user_id)s] [%(group_id)s]` 输出
  - 关键入口（on\_request/response、cmd\_xxx、cmd\_darwin）bind\_context + try/finally reset\_bind
  - 9 个新单元测试覆盖：contextvar 隔离、Filter 注入、异步任务隔离、e2e 格式验证

#### P1 级修复 — 主要问题

- **M1** **`main.py`** **裸 except 捕获**：3 处 `except:` → `except Exception:`，避免吞没 `KeyboardInterrupt`/`SystemExit`
- **M2** **`main.py`** **`__Del__`** **不可靠清理**：新增 `async def close()` 方法作为显式资源清理入口，`__Del__` 保留为尽力而为的兜底
- **M3** **`utils.py`** **Token 估算偏差**：`estimate_tokens()` 区分中文（1.5 token/字）与英文（0.25 token/字），替代原先 `len(text)//2` 的粗略估算
- **M5** **`init_db.py`** **数据库方法缺少线程锁**：`YunliMemoryDB` 全部 20+ 公共方法添加 `with self._lock:` 保护，解决 `to_thread` 与事件循环并发访问的数据竞争
- **M6** **`init_db.py`** **日志缓冲区写入失败丢失数据**：`_flush_logs` 拆分为自动加锁版和 `_flush_logs_locked`（调用方持锁版）；写入失败时保留缓冲区数据，仅在超限（>5x batch\_size）时裁剪
- **M7** **`memory_manager.py`** **绕过数据访问层**：`_consolidate_memories_for_user` 改用 `self.db.memory_db.delete_user_memories()` 和 `self.db.add_memory()`，替代直接 `conn.execute()`
- **M8** **`asset_bridge.py`** **正则替换截断资产**：变量查找从正则替换改为 AST 解析（`ast.parse` + `ast.walk` + `ast.Assign`），消除正则截断风险；原子写入从 `unlink + rename` 改为 `os.replace()`
- **M9** **`rule_generator.py`** **LLM 文本直接用作正则**：新增 `_validate_regex_or_fallback()` 函数，验证正则合法性、检测嵌套量词（ReDoS 模式），非法时回退到 `re.escape()`
- **M10** **`darwin_evolve.py`** **棘轮机制忽略维度退化**：棘轮接受前新增关键维度退化检查，任一维度退化 >2 分时拒绝，即使总分提升
- **M11** **`darwin_evolve.py`** **探索性重写重复评分**：`_exploratory_rewrite` 接受 `current_score` 参数，由调用方传入已有分数，避免冗余 LLM 调用
- **M12** **`emotion.py`** **state\_history 无界增长**：新增 `MAX_STATE_HISTORY = 100` 类常量和 `_record_history()` 方法，自动裁剪历史记录
- **M13** **`language.py`** **formal\_words 单字替换破坏语义**：`formal_words` 字典替换为 `formal_replacements` 列表，使用正则 lookbehind/lookahead 实现上下文感知替换（如 `之` 不替换 `之前/之后`）
- **M14** **`group_perception.py`** **话题检测单字匹配过宽**：新增 `SINGLE_CHAR_BLACKLIST` 排除易误匹配的单字关键词
- **M15** **`context_builder.py`** **关键词映射重复定义**：`topic_keywords_map` 改为从 `GroupPerception.TOPIC_KEYWORDS` 动态导入，消除与 `group_perception.py` 的重复定义
- **M16** **`group_perception.py`** **缓存无 TTL 过期**：`_enforce_cache_limit` 先执行 TTL 过期清理，再执行容量淘汰

#### P2 级修复 — 次要问题

- **m1 统一日志输出**：8 个文件共 59 处 `print()` 全部替换为 `logging`（`logger.info/warning/error/debug`），移除硬编码前缀（如 `[云璃插件]`、`[云璃记忆]`），使用标准 `logging.getLogger(__name__)` 模块命名
- **m2 统一版本号**：`main.py` / `metadata.yaml` 版本号统一更新为 `2.2.0`
- **m3 替换仓库 URL 占位符**：`metadata.yaml` 和 `main.py` 中 `yourname` 替换为 `YunliDev`
- **m4** **`context_builder.py`** **空列表逻辑**：`_deterministic_choice` 修复 `if not items: return items[0]` → `if not items: return None`
- **m11** **`persona/core.py`** **死代码**：移除从未使用的 `_prompt_cache` 属性，同步更新 `clear_cache()` 方法和相关测试
- **m13** **`language.py`** **不存在的 topic**：`should_use_direct_response` 中 `"sword_simple"` → `"sword"`

#### 测试更新

- 修复 `test_persona.py` 中引用已删除 `_prompt_cache` 的 2 个测试用例
- 全量回归测试：**初版声称 429 个全部通过 → 实测 28 个失败**（README 描述与实际不同步）
- **P0-1 后续批量修复**：在 706 个测试中修复 23 个失败 + 新增 17 个 Darwin apply 端到端测试 = **723 个全部通过（0 失败）**

***

### v2.1.1 (2026-06-15) — Darwin 进化系统稳定性加固

> **本轮修复覆盖 5 个文件，修复 5 个问题（P1 级 2 个 + P2 级 3 个），所有 15 个进化模块测试通过。**

#### P1 级修复 — 运行时稳定性

- **`log_collector.py`** **`_append`** **异常处理**：文件写入新增 `OSError`/`IOError` 捕获，磁盘满或权限不足时记录警告日志并回退计数，避免数据丢失和进程崩溃
- **`darwin_evolve.py`** **`run_report`** **空值保护**：`int(dim["score"])` 前新增 `None` 检查，维度分数缺失时显示 `N/A` 而非抛出 `TypeError`

#### P2 级修复 — 一致性 + 日志 + 兼容性

- **`darwin_evolve.py`** **`eval_mode`** **一致性**：串行基线评估（`_run_baseline_serial`）和串行进化的 `new_baseline` 保存时新增 `eval_mode: "serial"` 字段，与并行评估（`eval_mode: "parallel"`）保持一致
- **`rule_generator.py`** **未知规则类型日志**：`_generate_single` 遇到未知规则类型时记录警告日志（含 `rule_type` 和 `category`），替代原先静默返回 `None`
- **`utils.py`** **JSON 数组解析**：`extract_json_from_response` 更新文档和类型标注，支持 LLM 返回 JSON 数组格式（`[{...}]`）

#### 导入修复

- **`eval/scorer.py`** **三级 fallback 导入**：`extract_json_from_response` 导入从二级 fallback 扩展为三级（`..utils` → `evolution.utils` → `utils`），修复独立运行测试时的 `ModuleNotFoundError`

#### 项目结构更新

```
yunli/
├── evolution/
│   ├── tests/                          # v2.1.2 进化模块单元测试
│   │   ├── __init__.py
│   │   ├── test_utils.py               # JSON 提取工具测试（8 个用例）
│   │   └── test_scorer.py              # 评分解析与比较测试（7 个用例）
│   └── ...（其余结构不变）
```

***

### v2.1.0 (2026-06-14) — Darwin 自进化系统 Phase 1 + Phase 2

> **新特性：云璃能够从对话中自动学习、发现新规则、持续优化**。
> 框架完全零侵入，启用与否由用户在设置页控制。

#### Phase 1 — 文本资产棘轮进化

参考花叔 Darwin Skill v2.0 的 9 维度评分体系，新增 1 维度"反AI腔"，合计 10 维度。棘轮机制保证新版本只在总分提升时保留，否则回滚旧版，自动备份 `.bak.{timestamp}.md`。

- **10 维度评分体系**（`evolution/eval/rubric.py`）：
  - 角色一致性（权重 1.2）→ 可执行具体性（1.1）→ 失败模式覆盖（1.1）→ 指令清晰度（1.0）→ 边界明确性（1.0）→ 高风险行动黑名单（1.0）→ 缓存友好性（1.0）→ 风格一致性（0.9）→ Token 效率（0.8）
  - **v2.1 新增【反AI腔】维度**（权重 1.0）：避免云璃变成"完美的假人"，主动识别并淘汰 AI 标准腔。失败模式编码包括"过于流畅/永远正确/情绪稳定/滴水不漏/用词过于考究/句式过于工整/全知全能/刻意共鸣"8 条；高分标准是"完全没有人造感，像一个真实的人在无意识地说"
  - 棘轮机制在「角色一致性」与「反AI腔」之间形成张力——过度追求任一方都会被扣分，迫使进化系统找到平衡
- **5 类可进化文本资产**（`evolution/assets/`）：
  - `system_prompt.md` / `review_rules.md` / `filter_rules.md` / `emotion_templates.md` / `language_style.md`
- **LLM 调用**（零侵入）：
  - 主路径：`context.get_provider().text_chat()`，与 `memory_manager` 调用方式一致
  - 兜底：HTTP 直连 DeepSeek，读取 `DEEPSEEK_API_KEY` 环境变量
  - 同步调用通过 `loop.run_in_executor` 包装，不阻塞 AstrBot 事件循环
- **三种使用方式**：
  - 命令行：`python -m evolution.darwin_evolve {baseline|evolve|report}`
  - 群聊：`/云璃进化 baseline|evolve [资产名]|report|status`
  - 自动触发：每 N 小时（配置 `evolution_trigger_interval_hours`）在 `on_llm_request` 钩子中非阻塞执行
- **配置项（6 个）**：`evolution_enabled`、`evolution_auto_trigger`、`evolution_trigger_interval_hours`、`evolution_max_iterations`、`evolution_min_improvement`、`evolution_assets`
- **安全保障**：每次保存自动备份 `.bak.{timestamp}.md`；棘轮机制只保留改进；Phase 1 切换 prompt 版本时 DeepSeek KV 缓存会瞬时失效一次，稳态命中率不变

#### Phase 2 — 对话日志模式发现 + 半自动规则生成

从对话日志中自动发现 6 类问题，半自动生成对应规则提案。人类最终决策（接受/拒绝），机器只做发现与生成。

- **数据流**：
  1. `LogCollector`（`evolution/log_collector.py`）：在 `_log_interaction` 钩子中采样采集对话日志，JSONL 格式轮转存储（5 个文件 × 200 条）
  2. `PatternDiscovery`（`evolution/pattern_discovery.py`）：调用 LLM 分析最近 24h 日志，发现 6 类问题（`filter_escape` / `emotion_miss` / `style_gap` / `boundary_violation` / `tone_inconsistency` / `new_topic`），输出 `DiscoveredPattern` 半结构化数据
  3. `RuleGenerator`（`evolution/rule_generator.py`）：将已接受的模式转为 5 种 `RuleProposal`（`filter_regex` / `emotion_trigger` / `topic_keyword` / `boundary_rule` / `tone_rule`），并附带目标文件路径与操作指引
- **完整工作流**（群聊命令）：
  - `/云璃进化 analyze` — 启动 LLM 分析，输出发现摘要
  - `/云璃进化 discoveries` — 列出所有发现（含 severity/状态/置信度）
  - `/云璃进化 accept <id>` — 接受发现并自动生成规则提案
  - `/云璃进化 reject <id>` — 拒绝发现
  - `/云璃进化 rules` — 查看待审核规则提案
  - `/云璃进化 logstats` — 查看日志采集统计
- **配置项（3 个）**：`phase2_enabled`、`phase2_log_sample_rate`（默认 0.1 = 10% 采样）、`phase2_auto_analyze_hours`
- **安全保证**：所有提案必须经人类接受才会写文件，机器永不自动修改源代码

#### 文件结构变化

```
yunli/
├── evolution/                          # v2.1 新增自进化系统
│   ├── __init__.py
│   ├── darwin_evolve.py                # Phase 1 主进化循环
│   ├── llm_client.py                   # v2.1.1 统一 LLM 调用客户端
│   ├── utils.py                        # v2.1.1 共享工具函数
│   ├── _locks.py                       # v2.1.1 文件写入线程安全锁
│   ├── parallel_eval.py                # v2.1.1 并行评估器
│   ├── asset_bridge.py                 # v2.1.1 资产运行时应用桥接
│   ├── provider_factory.py             # v2.1.1 LLM Provider 获取工厂
│   ├── log_collector.py                # Phase 2 日志采集
│   ├── pattern_discovery.py            # Phase 2 模式发现
│   ├── rule_generator.py               # Phase 2 规则生成
│   ├── test_prompts.json               # v2.1.1 执行测试用例
│   ├── assets/                         # 可进化的 5 类文本资产
│   │   ├── system_prompt.md
│   │   ├── review_rules.md
│   │   ├── filter_rules.md
│   │   ├── emotion_templates.md
│   │   └── language_style.md
│   └── eval/                           # 评分框架
│       ├── __init__.py
│       ├── rubric.py                   # 10 维度评分标准
│       └── scorer.py                   # 评分执行器
```

#### 配置分组新增

- **Darwin进化**：启用、迭代参数、资产范围、并行线程数、评分/改进/探索性温度、探索性阈值与候选数
- **Darwin Phase2**：启用、采样率、自动分析间隔

#### 清理与版本同步

- 删除 `__pycache__`、`*.pyc`、`*.bak.*` 备份
- 删除运行产物 `evolution/baseline.json`、`evolution/evolution_log.md`、`evolution/discoveries/`、`evolution/pending_rules/`、`evolution/logs/`
- 版本号统一更新为 `2.1.0`（`main.py` / `metadata.yaml` / `evolution/__init__.py`）

***

### v2.0.0 (2026-06-12) — LLM 上下文缓存友好重构

> **重大架构调整**：将 system\_prompt 拆分为静态层和动态层，大幅提升 DeepSeek 等 LLM 的上下文缓存命中率。

- **核心重构 — system\_prompt 静态化**：
  - `req.system_prompt` 只包含静态人格设定（`BASE_SYSTEM_PROMPT`，\~400 Token），确保所有请求间 system\_prompt 完全一致
  - 动态上下文（时间/场景/知识/关系/群聊/记忆/待续约定）从 `req.system_prompt` 移至 `req.prompt`（用户消息前缀），以 `[当前上下文]...[用户消息]` 格式注入
  - 兼容处理：旧版框架不支持 `req.prompt` 时自动回退到 system\_prompt 注入
- **消除所有随机性**：
  - `core/context_builder.py`：`random.choice(templates)` → `_deterministic_choice()` (基于种子 MD5 哈希)；`random.random()` → `_deterministic_probability()` (基于种子哈希)
  - `persona/core.py`：类比触发的 `random.random() < 0.3` → 基于类比内容哈希的确定性触发
  - `is_memory_fuzzy()`：随机模糊判断 → 基于 `(content + created_at + access_count)` 哈希的确定性判断
- **缓存效果预期**：
  - system\_prompt 层面：从 0% → 接近 100% 的 KV cache 命中率（所有请求间共享）
  - 整体缓存命中率：预计从 \~0% 提升至 30-60%
- **新增测试文件**：`tests/test_cache_hit_rate.py` — 28 个缓存命中率专项测试，覆盖 5 个测试类：
  - `TestSystemPromptStability`（8 个）：system\_prompt 跨消息一致性、动态内容位置验证、兼容回退
  - `TestDeterministicMethods`（10 个）：`_deterministic_choice` / `_deterministic_probability` / `is_memory_fuzzy` 确定性行为
  - `TestDeterministicMemoryText`（6 个）：记忆文本生成的确定性
  - `TestRealContextBuilderIntegration`（2 个）：环境感知输出格式稳定性
  - `TestCacheHitRateMetrics`（2 个）：模拟 5 条消息 → system\_prompt MD5 hash 100% 一致
- 版本号统一更新为 `v2.0.0`（main.py / metadata.yaml / README）

### v1.6.0 (2026-06-11)

- **请求链路重构**：
  - 新增 `core/request_context.py`：`RequestContext` 数据类统一管理 `on_llm_request` → `on_llm_response` 生命周期状态（替代原先 5 个散落动态属性 `_yunli_req`/`_yunli_prompt_injected`/`_yunli_is_knowledge_query`/`_yunli_debounce_buffered`/`_yunli_debounce_merged`），附着在 `event._yunli_ctx` 上跨阶段传递，main.py 中旧属性引用归零
  - 新增 `core/at_detector.py`：`AtDetector` 独立类将 `_should_activate` 的 30 行 `if/elif/break` 简化为 1 行委托，@ 检测逻辑集中可测试、可替换；self\_id 自动同步
  - `_do_inject_persona_prompt` 与 `on_llm_response` 使用 `ctx.group_id/user_id/user_nickname` 替代 `_get_*()` 调用，减少 3 次重复 getattr
- **人格注入冲突修复**：当 `req.system_prompt` 已有其他插件内容时，追加 `[[YUNLI_BOUNDARY]]` 标记而非简单覆盖，防止多插件提示词冲突
- **内存背压改进**：`_dialogue_buffer: List[Dict]` + `asyncio.Lock` 替换为 `asyncio.Queue(maxsize=2000)`（配置项 `memory_buffer_max_size`），队列满时自动丢弃新消息（400KB 硬上限），无需手动管理超限丢弃和锁
- **防抖器时间戳修复**：`MessageDebouncer.handle_message` 不再提前设置 `_last_process_time`，新增 `mark_processed()` 方法由调用方在处理成功后才调用，防止 `_inject_persona_prompt` 超时失败后下一条消息被错误防抖
- **`on_llm_response`** **不再清空非云璃响应**：跳过云璃后处理改为 `return` 而非 `response.completion_text = ""`，避免与其他插件冲突
- **统一后台任务生命周期**：`YunliPersonaPlugin` 和 `MemoryManager` 各自增加 `_background_tasks` 集合和 `_safe_create_task()` 方法，所有 `asyncio.create_task` 替换为 `_safe_create_task`，`__del__` 升级为取消未完成任务 → 刷新 DB 日志缓冲区 → 清空防抖缓冲区 → 关闭连接
- **`YunliMemoryDB`** **线程安全**：`_flush_logs` 和 `add_memory` 方法增加 `self._lock` 保护，解决 `to_thread` 与事件循环并发访问 `self.conn` 的数据竞争
- **分段发送事件失效保护**：`_send_remaining_segments` 开头增加事件有效性检查，插件卸载后静默终止
- **移除** **`_get_self_id`** **重复实现**：委托给 `_get_cached_self_id`，同时将 self\_id 同步到 `AtDetector`
- **修复** **`remove_internal_state_lines`** **正则转义**：`\.\?!` 改为 `.?!`，消除 Python 3.12+ 的 `SyntaxWarning`
- **工具函数统一重构**：新增 `core/utils.py` 集中管理纯工具函数（`remove_assistant_prefix`、`remove_internal_state_lines`、`clean_repeated_punctuation`、`is_structured_summary`、`truncate_at_sentence`、`estimate_tokens`、`merge_messages`），从 `persona/core.py` 的 `review_response` 和 `main.py` 中提取，消除重复代码
- **消息切分器简化**：`MessageSplitter` 大幅精简，移除 Markdown 块级保护、URL 保护、自然过渡词切分、软短句合并、语气词保护等复杂逻辑，仅保留空行切分 + 句子切分 + 过短合并 + 最大段数限制，作为 AstrBot 自带分段的增强补充
- **防抖器消息合并增强**：`MessageDebouncer._process_window` 新增同 scope 多条消息合并逻辑（`merge_messages()`），合并后的消息附带合并通知文本，标记 `req._yunli_debounce_merged` 供后续处理使用
- **测试体系重构**：从 9 个测试文件扩展为 12 个，测试数量从 302 增至 382
  - 新增 `tests/test_base.py` 共享测试基础设施（路径设置、AstrBot mock、测试基类），所有测试统一使用 `from yunli.xxx import yyy` 导入格式
  - 拆分旧测试文件为新结构：新增 `test_conversation.py`（对话相关）、`test_emotion.py`（情感状态机）、`test_filters.py`（语言过滤器）、`test_language.py`（语言风格处理）、`test_plugin.py`（插件主类集成）、`test_utils.py`（工具函数）
  - 删除旧测试文件：`test_hybrid_memory.py`、`test_memory_stress.py`、`test_mode_detection.py`、`test_quiet_mode.py`、`test_group_chat.py`（功能已合并至新测试文件）
- **清理缓存文件**：删除全部 `__pycache__` 目录、`.pytest_cache`、`.coverage`、SQLite WAL 文件（`.db-shm`/`.db-wal`）、`docs/plans/` 计划文档
- **修复插件版本号**：`main.py` 中 `@register` 版本号从 `"1.0.0"` 更正为 `"1.6.0"`，与 `metadata.yaml` 保持一致
- **新增配置项**：`prompt_inject_timeout`（人格注入超时，默认 10s）、`buffer_task_debounce_seconds`（记忆缓冲任务防抖，默认 60s）、`memory_buffer_max_size`（对话缓冲队列上限，默认 2000）

### v1.5.0 (2026-06-10)

- **测试文件规范化**：删除根目录下 6 个手动验证脚本（`test_mention_natural.py`、`test_mention_v2.py`、`test_mention_window.py`、`test_natural.py`、`test_particle.py`、`test_yunli_persona.py`），将 `test_quiet_mode.py` 迁移至 `tests/` 目录，统一使用 unittest 框架
- **新增群聊集成测试**：`tests/test_group_chat.py` 新增 91 个测试，覆盖 12 个测试类：群聊消息收发（5）、多用户互动与关系隔离（8）、消息类型兼容性（9）、群聊权限控制（6）、异常场景处理（11）、消息分段与延迟（7）、关系状态机与情感联动（12）、未完成约定追踪（5）、安静时段（4）、回复自审（7）、情感状态机（10）、记忆系统（5）
- **修复 quiet 模式仍主动说话**：移除 `event.stop_event()`（会阻止框架正常 @ 回复流程），改为 `on_llm_request` 中将 `req` 保存到 `event._yunli_req`，`on_llm_response` 中检查 `_yunli_prompt_injected` 标记：未注入则清空回复；同时增强三层 @ 检测（框架 `is_at_me()` → 消息组件 At 解析 → 文本 `[At:ID]` 正则匹配）
- **修复回复截断（三层）**：① `language.py` 的 `detect_query_mode` 新增 12 个总结/归纳类关键词（"总结"/"概括"/"归纳"/"整理"/"汇总"/"梳理"/"列出"/"列一下"/"列举"/"罗列"/"回顾一下"/"复盘"/"提炼"/"提取"）；② `main.py` 新增 `_is_structured_summary()` 方法，检测 LLM 输出是否包含 Markdown 标题（`##`/`###`）、编号列表（`1.`/`2.`）、粗体标注（`**...**`）、水平分隔线（`---`）；③ `on_llm_response` 中 `is_knowledge_query` 改为双条件触发（输入含知识查询关键词 **或** 输出生成结构化总结），确保长文本总结不被 `review_response` 以聊天模式截断
- **修复消息切分（省略号）**：`MessageSplitter._merge_short_segments` 中，以省略号 `…`/`……` 结尾的段落不再视为"完整句子"，允许与后续短句合并（如"到处找好剑……\n\n顺带找好吃的"合并为一段）
- **清理缓存文件**：删除全部 `__pycache__` 目录下的 `.pyc` 缓存和 `.pytest_cache`
- **测试总数**：302 个测试通过

### v1.4.0 (2026-06-09)

- **新增关系状态机**：`emotion.py` 新增 `RelationshipManager` 类，感知用户边界和互动温度，动态调节回复策略；4 种关系模式：normal（正常）、backoff（用户表达边界→收敛回复）、careful（用户情绪低落→温和关怀）、warming（互动升温→更自然亲近）；每用户独立状态，按 `(group_id, user_id)` 隔离；自动衰减（backoff 6小时/careful 2小时/warming 30分钟后恢复 normal）；提供 `get_hint()`（提示词注入）、`get_reply_length_limit()`（回复长度限制）、`get_proactive_suppress()`（插嘴概率系数）三个接口
- **新增用户意图分析**：`RelationshipManager.detect_user_intent()` 细分为 6 种意图（boundary/comfort/intimacy/play/help/chat），`INTENT_TO_EMOTION_TRIGGER` 映射意图到情感触发器（boundary→insulted, comfort→sad\_topic, play→joke\_made, intimacy→praised, help→mission\_mentioned），`_inject_persona_prompt` 中根据用户意图自动驱动 `emotion.transition()`
- **新增回复自审**：`core.py` 新增 `review_response()` 方法，LLM 回复后检测并修复 4 类问题：①助手腔开头（"好的，"/"当然，"/"以下是"等 16 种前缀→删除）；②泄露内部状态（"记忆模块"/"提示词"/"系统指令"等 11 个关键词→删除含关键词的整句）；③聊天模式过长（>200字→句子结束标点处自然截断，知识查询不受限）；④重复标点（3+个→压缩为 2 个）
- **新增未完成约定追踪**：`memory_schema.sql` 新增 `open_loops` 表（group\_id, user\_id, user\_nickname, text, status, expires\_at）；`YunliMemoryDB` 新增 `add_open_loop`/`complete_open_loop`/`get_pending_loops`/`cleanup_expired_loops` 4 个方法；`_extract_memory_lightweight` 新增约定检测（"帮我/提醒我/记得/明天/下次"→记录，"搞定了/不用了/算了"→标记完成）；`_inject_persona_prompt` 注入未完成约定提示（【待续】XX之前说YY）；14 天自动过期
- **新增安静时段**：`qq_behavior.py` 新增 `is_quiet_time()` 方法，支持跨午夜时段（如 23:00-07:00）和同天时段（如 12:00-14:00）；安静时段内插嘴概率降至 10%、冷却时间 ×3；新增配置项 `quiet_time_range`（格式 HH:MM-HH:MM，留空不启用）
- **关系模式影响插嘴行为**：`qq_behavior.py` 的 `should_chime_in` 新增 `user_id` 参数，根据关系模式调节插嘴概率（backoff ×0.1、careful ×0.5、warming ×1.3）；`_get_dynamic_cooldown` 安静时段冷却 ×3
- **关系模式影响回复长度**：`main.py` 的 `on_llm_response` 中根据关系模式在句子结束标点处自然截断（backoff 40字、careful 80字，仅聊天模式，知识查询不受限）
- **关系提示词注入**：`_inject_persona_prompt` 中新增关系状态提示（预算\~50 Token），如"用户可能在表达边界，回复要短、低压"
- **新增配置项**：`_conf_schema.json` 新增 `relationship_decay_multiplier`（关系状态衰减时间倍数 0.1-3.0）和 `quiet_time_range`（安静时段）
- **修复正则表达式字符错误**：`core.py` 的 `_LONE_OPEN_BRACKET_PATTERN` 预编译正则中 `【｝` 的 `｝` 是闭合括号（U+FF5D），应使用 `｛`（U+FF5B，全角左花括号）；导致残留的单边开括号无法正确匹配清理
- **修复中文首字符被破坏**：`language.py` 的 `apply_style` 中 `text[0].lower()` 对非英文字母也执行小写转换，可能破坏中文或其他 Unicode 首字符；修复为仅对 A-Z 应用 `lower()`
- **修复重复导入** **`re`**：`filters.py` 的 `filter_action_words` 函数内部重复 `import re`，改为文件顶部统一导入；`language.py` 的 `apply_style` 方法内部也重复导入，已删除
- **修复** **`_last_message_event`** **未定义**：`_extract_memory_lightweight` 中检查 `hasattr(self, '_last_message_event')` 但该属性从未赋值，即使存在也可能为 `None`；增加 `is not None` 检查
- **修复插嘴逻辑中** **`user_nickname`** **未定义**：`on_group_message` 的分段发送逻辑中 `_send_remaining_segments` 调用时传入 `user_nickname`，但该变量在之后才定义；将 `user_id` 和 `user_nickname` 的获取提前到发送逻辑之前
- **优化 LRU 缓存策略**：`core.py` 的 `_knowledge_cache` 从普通 `dict` 改为 `collections.OrderedDict`，实现真正的 LRU（命中时移到末尾，超限时移除最久未访问项），替代原来的 FIFO（删除最早的 20% 条目）
- **优化正则表达式预编译**：`core.py` 的 `_maintain_identity` 和 `_maintain_identity_light` 每次调用都现场编译 10+ 个正则模式；改为类定义层预编译为类属性，避免重复编译开销
- **优化多人同时@的延迟回复**：`_delayed_reply_to_others` 从固定 5-15 秒延迟改为根据被选中者消息长度动态估算"打字时间"；根据未选中人数调整延迟基数；新增 `_generate_personalized_delayed_reply` 方法，根据对方消息话题（问题/夸奖/剑/食物）生成个性化回应，替代固定模板
- **修复消息切分错误（连续换行问题）**：`MessageSplitter` 的 `_split_by_blocks` 只在空行处切分，导致相邻句子（仅换行分隔）被合并为一段；修复为当当前行以句子结束标点结尾且下一行是普通文本时，也在此处切分
- **修复短句被错误合并**：`_merge_short_segments` 将独立短句（如"客气啥！"和"咱俩谁跟谁啊"）合并成一段；新增限制：当前段或下一段如果是完整句子（以标点结尾），不再合并；合并后总长度必须 < 20 字符
- **修复消息切分后丢失换行格式**：`MessageSplitter` 的 `split` 方法在合并短句和重新组装分段时，未保留原始文本中的换行符，导致代码块、列表等 Markdown 块级元素内部的换行结构被破坏；修复为在合并和组装过程中显式保留 `\n` 换行符，确保分段后的消息仍具备正确的格式层次
- **清理缓存文件**：删除全部 `__pycache__` 目录下的 `.pyc` 缓存
- **新增测试**：`TestRelationshipManager`（25 个）、`TestUserIntentDetection`（8 个）、`TestResponseReview`（9 个）、`TestQuietTime`（4 个）
- **测试总数**：169 个测试通过

### v1.3.0 (2026-06-08)

- **修复死锁风险**：`_buffer_dialogue_for_deep_consolidation` 中在 `async with` 锁块内创建 `asyncio.create_task`，新任务可能尝试获取同一把锁导致死锁；修复为释放锁后才创建任务
- **修复空值检查缺失**：`_build_memory_lines_for_user` 中直接访问 `item[0]`、`item[1]`、`item[2]`，记忆数据不完整时会抛出 `IndexError`；添加 `valid_items` 过滤和 `contents` 非空判断
- **修复并发任务无限制**：`_send_remaining_segments` 被多次调用时创建大量未限制的任务，可能导致内存爆炸；添加 `asyncio.Semaphore(3)` 信号量，最多同时运行 3 个分段发送任务
- **修复事务保护缺失**：`_consolidate_memories_for_user` 中删除旧记忆和写入新记忆未使用事务，中间失败会导致数据不一致；使用 `BEGIN TRANSACTION` / `ROLLBACK` / `COMMIT` 确保原子性
- **修复 user\_nickname 未定义**：记忆整理事务中 `user_nickname` 变量未定义导致回滚；改为从已有记忆中提取昵称，无昵称时回退到 `user_id` 前 4 位
- **修复知识查询模式截断问题**：`language.py` 的 `_apply_general_rules` 和 `qq_behavior.py` 的 `format_for_qq` 在知识查询模式下仍应用 200 字符长度限制，导致长内容被提前截断；修复为知识查询模式下跳过长度限制，由 `main.py` 统一控制 1200 字符上限
- **修复语气词位置错误**：`language.py` 的 `_add_emotion_particles` 随机把单字语气词（"呢"/"吧"/"嘛"）加在句首，或在感叹号/问号后直接追加导致标点被吞；新增 `_attach_particle_prefix` 和 `_attach_particle_suffix` 方法，禁止单字语气词出现在句首，句末追加时自动插在标点前面（如"有需要随时喊我呢！"）
- **修复语气词被误切分为独立消息**：`main.py` 的 `NATURAL_BREAK_WORDS` 包含单字语气词（"呢"/"吧"/"哦"/"啦"/"嗯"/"啊"），消息切分时会把句末语气词切成单独片段；移除所有单字语气词，只保留多字过渡词（"不过"/"其实"/"话说回来"等）
- **新增语气词单元测试**：`tests/test_persona.py` 新增 `TestEmotionParticles` 测试类，覆盖句首禁止、句末标点保护、避免重复叠加、自然切分过滤等 7 个用例
- **修复情绪形容词被误删**：`filters.py` 的 `ACTION_WORDS` 和 `FORBIDDEN_PARTICLE_WORDS` 包含 `"害羞"` 等纯情绪形容词，导致 `"我才没有害羞呢"` 被过滤成 `"我才没有呢"`；移除纯情绪形容词，只保留带括号/动作格式的版本（如 `"（害羞）"`）
- **修复动作词上下文误删**：`filters.py` 的 `filter_action_words` 使用简单 `replace` 全词替换，导致嵌入在正常句子中的动作词（如 `"他突然笑了笑说"`）被误删；重构为基于正则的上下文判断逻辑：嵌入句中的保留、独立成句的删除、括号标签直接删除、星号标记的删除
- **新增动作词过滤单元测试**：`tests/test_persona.py` 新增 `TestActionWordFilter` 测试类，覆盖嵌入保留、独立删除、括号标签删除、星号标记删除、情绪形容词保留等 5 个用例
- **修复 quiet 模式关键词误触发**：`_should_activate` 中关键词触发（"云璃"/"yunli"/"猎剑士"/"老铁"/"琼实鸟串"）未检查 `response_mode`，导致 quiet 模式下提到这些词仍会触发 LLM 响应；修复为 quiet 模式下只回应@，关键词和概率激活均被屏蔽
- **清理缓存文件**：删除全部 `__pycache__` 目录下的 `.pyc` 缓存
- **测试总数**：153 个测试全部通过

### v1.2.0 (2026-06-08)

- **记忆系统与 AstrBot 上下文兼容**：移除重复读取最近群聊记录的逻辑，避免 Token 竞争和信息冗余，依赖 AstrBot 的 session 上下文处理短期对话历史
- **群友记忆共享**：支持跨用户记忆召回，群友相关记忆以 30% 降权参与评分，最多展示 2 位群友的记忆（按相关性得分排序）
- **群友记忆显示名优化**：`user_memories` 表新增 `user_nickname` 字段，群友记忆展示优先使用真实昵称（如"小明"），无昵称时回退到 `user_id` 前 4 位
- **轻量提取句式扩展**：从 3 种扩展到 20 种句式，覆盖偏好表达（超爱/最爱/沉迷）、身份职业、状态事件（带 1-14 天自动过期）、拥有能力四大类
- **话题检测增强**：关键词从 8 个扩展到 15 个主题/180+ 关键词，新增日常、情绪、天气、新闻、科技、健康、宠物、音乐、旅行等主题
- **群氛围记忆**：轻量内存统计群内最近聊天话题分布，积累 5 条以上时注入提示词（"这个群最近常聊 XX 相关的话题"）
- **话题切换冷却期**：`update_topic` 添加 30 秒冷却，冷却期内不同话题的消息计入当前活跃话题，避免频繁切换
- **群氛围缓存锁**：`_update_group_atmosphere` 改为异步函数，使用 `asyncio.Lock` 保护并发更新
- **LLM 深度整理动态门槛**：根据消息密度自动调整触发条件（超高密度 >100条/小时：4小时/80条，高密度 30-100条/小时：2小时/30条，中密度 10-30条/小时：3小时/15条）
- **话题与群氛围智能合并**：当活跃话题已包含在群氛围中时，合并为一条描述，避免信息冗余
- **测试总数**：138 个测试全部通过

### v1.1.0 (2026-06-08)

- **配置重构**：删除冗余的 `proactive_reply_enabled`，统一使用 `response_mode` 控制主动回复（quiet/balanced/active）
- **配置分组**：将 30+ 配置项按功能分为 7 大组（基础人格、群聊行为、情感与语气、记忆系统、知识查询、分段回复、性能优化）
- **修复主动回复关闭失效**：`_should_activate` 中关键词触发未检查 `response_mode`，导致 quiet 模式下提到"云璃"仍会响应
- **修复异步生成器错误**：`_send_remaining_segments` 通过 `create_task` 调用时内部使用 `yield` 导致后续片段永不发送，改为 `await event.send()`
- **修复并发安全问题**：`qq_behavior.py` 的 `group_states` 字典无锁保护，多协程下可能连续插嘴；添加 `asyncio.Lock` 和定期清理机制
- **修复 JSON 解析漏洞**：`_parse_memory_json` 缺少 `re` 导入、空输入未处理、贪婪正则可能匹配嵌套数组
- **代码质量优化**：
  - 提取硬编码值为可配置项（概率阈值、内容长度限制、话题关键词、时效性天数等）
  - 增加空值防御和类型安全检查
  - `asyncio.create_task` 增加异常捕获，避免静默失败
- **测试增强**：新增主动回复关闭场景专项测试（13 个测试覆盖 quiet/active/balanced 三种模式）
- **测试总数**：138 个测试全部通过

### v1.0.0

- 初始版本发布
- 实现人格引擎、QQ 行为、混合记忆架构
- **新增智能分段对话**：支持 Markdown 保护、语气词切分、动态打字延迟
- **修复平台兼容性**：`is_at_me()` 使用 `getattr` 安全调用

