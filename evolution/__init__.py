"""Darwin 进化系统 — 云璃插件文本资产自进化框架 v2.1.1

基于花叔 Darwin Skill v2.0 的 10 维度评分 + 棘轮机制，含 Phase 2 模式发现与半自动规则生成。

目录结构:
    evolution/
    ├── darwin_evolve.py        # Phase 1 主进化循环（命令行 + AstrBot 集成）
    ├── log_collector.py        # Phase 2 对话日志结构化采集
    ├── pattern_discovery.py    # Phase 2 LLM 驱动 6 类模式发现
    ├── rule_generator.py       # Phase 2 半自动规则提案生成
    ├── test_prompts.json       # 20 条测试 prompt
    ├── assets/                 # 可进化的文本资产
    │   ├── system_prompt.md
    │   ├── review_rules.md
    │   ├── filter_rules.md
    │   ├── emotion_templates.md
    │   └── language_style.md
    └── eval/                   # 评分框架
        ├── rubric.py           # 10 维度评分标准（含 v2.1 反AI腔维度）
        └── scorer.py           # 评分执行器 + 比较器

使用方式:
    CLI:  python -m evolution.darwin_evolve {baseline|evolve|report|benchmark}
    群聊: /云璃进化 {baseline|evolve|analyze|discoveries|accept|reject|rules|logstats}
"""