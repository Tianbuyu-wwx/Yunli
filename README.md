# 云璃QQ群聊人格插件 - 开发报告

## 1. 项目概述

基于 AstrBot 框架的 QQ 群聊人格插件，让 AI 化身《崩坏：星穹铁道》角色"云璃"，以朱明仙舟猎剑士的身份融入群聊。

核心设计目标：

- **人格一致性**：基于官方台词、语音、剧情数据构建系统提示词，Token 预算控制在 ~1150
- **拟真交互**：智能分段对话 + 动态打字延迟 + 思考停顿，模拟真人聊天节奏
- **群聊感知**：根据消息密度自动调节活跃度
- **长期记忆**：双层记忆架构，零Token轻量提取 + LLM深度整理，记住群友喜好和群氛围

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

### 2.2 QQ 群聊行为 (persona/qq_behavior.py)

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

### 2.4 智能分段对话 (persona/message_splitter.py)

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
- **线程安全**：知识库使用线程本地存储，记忆库使用 `check_same_thread=False`
- **记忆重置**：支持 `reset_memory()` 清空动态数据（保留表结构）

### 2.6 请求链路管理 (core/)

**请求上下文 (`RequestContext`)**

- 在 `on_llm_request` 中创建统一上下文，附着在 `event._yunli_ctx` 上跨阶段传递至 `on_llm_response`
- 封装 `group_id`/`user_id`/`user_nickname`/`scope` 来源信息和 `is_prompt_injected`/`is_debounce_buffered`/`is_debounce_merged`/`is_knowledge_query` 生命周期标记
- 替代原先 5 个散落动态属性，确保两个阶段状态一致且可追溯

**@检测器 (`AtDetector`)**

- 独立类封装三层 @ 检测逻辑：框架 `is_at_me()` → 消息组件 At 解析 → 文本 `[At:ID]` 匹配
- self_id 带缓存，首次获取后注入检测器，避免每次检测重复查找
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

- 对话缓冲队列使用 `asyncio.Queue(maxsize=2000)` 替代 `List[Dict]` + `asyncio.Lock`，队列满时自动丢弃新消息，硬上限 ~400KB
- `YunliMemoryDB._flush_logs` 和 `add_memory` 方法使用 `self._lock` 保护，解决 `to_thread` 与事件循环并发访问的数据竞争
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

## 4. 测试覆盖

- **测试文件**：`tests/test_*.py`（共 12 个测试文件）
- **测试数量**：381+ 个
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
  - 数据库线程安全（锁保护、并发读写）
  - 后台任务生命周期管理
  - 消息防抖合并
  - 工具函数（助手腔去除、内部状态行清理、重复标点清理、结构化总结检测、消息合并）

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
├── main.py                    # 插件主入口（含 MessageSplitter 分段器）
├── metadata.yaml              # 插件元数据
├── README.md                  # 用户说明文档
├── requirements.txt           # 依赖声明
├── _conf_schema.json          # 配置 Schema
├── core/
│   ├── __init__.py            # 模块导出（AtDetector、RequestContext 等）
│   ├── request_context.py     # 请求上下文（on_llm_request → on_llm_response 状态传递）
│   ├── at_detector.py         # @检测器（三层检测 + self_id 缓存）
│   ├── context_builder.py     # LLM 提示词上下文构建
│   ├── debouncer.py           # 消息防抖（窗口期内合并多条消息）
│   ├── group_perception.py    # 群聊感知（话题检测、氛围分析）
│   ├── memory_manager.py      # 记忆管理器（轻量提取 + LLM 深度整理）
│   └── utils.py               # 通用工具函数（回复自审、Token估算、截断、消息合并等）
├── database/
│   ├── init_db.py             # 数据库初始化
│   ├── schema.sql             # 主表结构
│   └── data/
│       └── initial_data.json  # 初始数据（台词、知识等）
├── persona/
│   ├── core.py                # 人格引擎核心
│   ├── emotion.py             # 情感状态机 + 关系状态机
│   ├── filters.py             # 语言过滤器
│   ├── language.py            # 语言风格处理 + 查询模式检测
│   └── qq_behavior.py         # QQ 群聊行为管理
└── tests/                     # 单元测试与集成测试
    ├── __init__.py
    ├── test_base.py            # 共享测试基础设施（路径设置、AstrBot mock、基类）
    ├── test_conversation.py    # 对话相关测试
    ├── test_database.py        # 数据库操作测试
    ├── test_emotion.py         # 情感状态机测试
    ├── test_filters.py         # 语言过滤器测试
    ├── test_language.py        # 语言风格处理测试
    ├── test_message_splitter.py # 消息切分器测试
    ├── test_persona.py         # 人格引擎测试
    ├── test_plugin.py          # 插件主类集成测试
    ├── test_qq_behavior.py     # QQ群聊行为测试
    └── test_utils.py           # 工具函数测试
```

## 7. 技术栈

- Python 3.12+
- AstrBot 插件框架（v4.25.2+）
- SQLite（知识库 + 记忆库）

## 8. 更新日志

### v1.6.0 (2026-06-11)

- **请求链路重构**：
  - 新增 `core/request_context.py`：`RequestContext` 数据类统一管理 `on_llm_request` → `on_llm_response` 生命周期状态（替代原先 5 个散落动态属性 `_yunli_req`/`_yunli_prompt_injected`/`_yunli_is_knowledge_query`/`_yunli_debounce_buffered`/`_yunli_debounce_merged`），附着在 `event._yunli_ctx` 上跨阶段传递，main.py 中旧属性引用归零
  - 新增 `core/at_detector.py`：`AtDetector` 独立类将 `_should_activate` 的 30 行 `if/elif/break` 简化为 1 行委托，@ 检测逻辑集中可测试、可替换；self_id 自动同步
  - `_do_inject_persona_prompt` 与 `on_llm_response` 使用 `ctx.group_id/user_id/user_nickname` 替代 `_get_*()` 调用，减少 3 次重复 getattr
- **人格注入冲突修复**：当 `req.system_prompt` 已有其他插件内容时，追加 `[[YUNLI_BOUNDARY]]` 标记而非简单覆盖，防止多插件提示词冲突
- **内存背压改进**：`_dialogue_buffer: List[Dict]` + `asyncio.Lock` 替换为 `asyncio.Queue(maxsize=2000)`（配置项 `memory_buffer_max_size`），队列满时自动丢弃新消息（400KB 硬上限），无需手动管理超限丢弃和锁
- **防抖器时间戳修复**：`MessageDebouncer.handle_message` 不再提前设置 `_last_process_time`，新增 `mark_processed()` 方法由调用方在处理成功后才调用，防止 `_inject_persona_prompt` 超时失败后下一条消息被错误防抖
- **`on_llm_response` 不再清空非云璃响应**：跳过云璃后处理改为 `return` 而非 `response.completion_text = ""`，避免与其他插件冲突
- **统一后台任务生命周期**：`YunliPersonaPlugin` 和 `MemoryManager` 各自增加 `_background_tasks` 集合和 `_safe_create_task()` 方法，所有 `asyncio.create_task` 替换为 `_safe_create_task`，`__del__` 升级为取消未完成任务 → 刷新 DB 日志缓冲区 → 清空防抖缓冲区 → 关闭连接
- **`YunliMemoryDB` 线程安全**：`_flush_logs` 和 `add_memory` 方法增加 `self._lock` 保护，解决 `to_thread` 与事件循环并发访问 `self.conn` 的数据竞争
- **分段发送事件失效保护**：`_send_remaining_segments` 开头增加事件有效性检查，插件卸载后静默终止
- **移除 `_get_self_id` 重复实现**：委托给 `_get_cached_self_id`，同时将 self_id 同步到 `AtDetector`
- **修复 `remove_internal_state_lines` 正则转义**：`\.\?!` 改为 `.?!`，消除 Python 3.12+ 的 `SyntaxWarning`
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
- **关系提示词注入**：`_inject_persona_prompt` 中新增关系状态提示（预算~50 Token），如"用户可能在表达边界，回复要短、低压"
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