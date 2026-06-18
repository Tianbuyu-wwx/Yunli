-- 云璃人格插件 - 知识库Schema（静态数据）

-- 台词库
CREATE TABLE IF NOT EXISTS dialogues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_type TEXT NOT NULL,
    content TEXT NOT NULL,
    mood TEXT DEFAULT 'neutral',
    weight INTEGER DEFAULT 1,
    usage_count INTEGER DEFAULT 0,
    last_used TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_dialogues_scene ON dialogues(scene_type);
CREATE INDEX IF NOT EXISTS idx_dialogues_mood ON dialogues(mood);

-- 语音/经典台词
CREATE TABLE IF NOT EXISTS voice_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_type TEXT NOT NULL,
    content TEXT NOT NULL,
    context TEXT,
    translation TEXT,
    weight INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_voice_lines_type ON voice_lines(line_type);

-- 剧情章节
CREATE TABLE IF NOT EXISTS story_chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_name TEXT NOT NULL,
    summary TEXT NOT NULL,
    full_text TEXT,
    characters TEXT,
    location TEXT,
    importance INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_story_chapters_name ON story_chapters(chapter_name);

-- 角色知识库
CREATE TABLE IF NOT EXISTS character_knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    entity_name TEXT NOT NULL,
    description TEXT NOT NULL,
    related_entities TEXT,
    importance INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_knowledge_category ON character_knowledge(category);
CREATE INDEX IF NOT EXISTS idx_knowledge_entity ON character_knowledge(entity_name);

-- 现代概念类比库
CREATE TABLE IF NOT EXISTS modern_analogies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    modern_term TEXT NOT NULL,
    yunli_analogy TEXT NOT NULL,
    category TEXT,
    -- deprecated: usage_count 字段无更新机制，永远为 0，保留仅为向后兼容
    usage_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_analogies_term ON modern_analogies(modern_term);
CREATE INDEX IF NOT EXISTS idx_analogies_category ON modern_analogies(category);

-- 情感表达模板
CREATE TABLE IF NOT EXISTS emotion_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    emotion TEXT NOT NULL,
    template_type TEXT NOT NULL,
    content TEXT NOT NULL,
    weight INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_emotion_emotion ON emotion_templates(emotion);

-- 数据库版本表
CREATE TABLE IF NOT EXISTS db_version (
    version INTEGER PRIMARY KEY,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO db_version (version) VALUES (3);
