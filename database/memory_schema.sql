-- 云璃人格插件 - 记忆库Schema（动态数据，按群隔离）

-- 群聊互动记录
CREATE TABLE IF NOT EXISTS interaction_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user_nickname TEXT,
    message TEXT,
    response TEXT,
    trigger_type TEXT,
    emotion_state TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_logs_group ON interaction_logs(group_id);
CREATE INDEX IF NOT EXISTS idx_logs_user ON interaction_logs(user_id, group_id);
CREATE INDEX IF NOT EXISTS idx_logs_time ON interaction_logs(created_at);

-- 话题追踪表
CREATE TABLE IF NOT EXISTS topic_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    message_count INTEGER DEFAULT 1,
    participants TEXT,  -- JSON数组
    is_active INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_topic_group ON topic_history(group_id);
CREATE INDEX IF NOT EXISTS idx_topic_active ON topic_history(group_id, is_active);

-- 用户记忆表（长期记忆）
CREATE TABLE IF NOT EXISTS user_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user_nickname TEXT,  -- 用户昵称（用于群友记忆共享时显示）
    memory_type TEXT NOT NULL,  -- 'fact', 'preference', 'event', 'relationship'
    content TEXT NOT NULL,
    confidence INTEGER DEFAULT 5,  -- 1-10，记忆可信度
    status TEXT DEFAULT 'active',  -- 'active', 'outdated', 'conflicted'
    expires_at TIMESTAMP,  -- 过期时间（NULL表示永久）
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    access_count INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_memories_user ON user_memories(group_id, user_id);
CREATE INDEX IF NOT EXISTS idx_memories_type ON user_memories(memory_type);

-- 群聊摘要表（周期性总结）
CREATE TABLE IF NOT EXISTS chat_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    key_topics TEXT,  -- JSON数组
    active_users TEXT,  -- JSON数组
    message_count INTEGER,
    start_time TIMESTAMP,
    end_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_summaries_group ON chat_summaries(group_id);

-- 数据库版本表
CREATE TABLE IF NOT EXISTS db_version (
    version INTEGER PRIMARY KEY,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 未完成约定追踪表
CREATE TABLE IF NOT EXISTS open_loops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user_nickname TEXT,
    text TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_open_loops_user ON open_loops(group_id, user_id);
CREATE INDEX IF NOT EXISTS idx_open_loops_status ON open_loops(status);

INSERT OR IGNORE INTO db_version (version) VALUES (4);
