import sqlite3
import json
import time
import threading
import logging
from collections import OrderedDict
from pathlib import Path
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class YunliKnowledgeDB:
    """云璃知识库 - 存储静态数据（台词、语音、剧情、知识、类比、情感模板）

    使用线程本地存储确保线程安全，适配AstrBot的多线程/异步架构。
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            self.db_path = Path(__file__).parent / "knowledge.db"
        else:
            self.db_path = Path(db_path)

        # 线程本地存储，每个线程独立的数据库连接
        self._local = threading.local()
        self._init_tables()

        # query_knowledge LRU 缓存（线程安全，缓存最近 64 次查询结果）
        # 注意：连接是线程本地的，但缓存是实例级共享的，必须加锁保护
        # OrderedDict 的 pop/popitem/写入是复合操作，并发下会损坏
        self._knowledge_query_cache: OrderedDict = OrderedDict()
        self._knowledge_cache_max = 64
        self._cache_lock = threading.Lock()

    def _get_conn(self):
        """获取当前线程的数据库连接（线程安全）"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self.db_path))
            self._local.conn.row_factory = sqlite3.Row
            # WAL模式 + 生产级PRAGMA：读写不互斥，减少磁盘同步开销
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.execute("PRAGMA cache_size=-8000")
            self._local.conn.execute("PRAGMA busy_timeout=5000")
        return self._local.conn

    def _init_tables(self):
        """初始化知识库表结构"""
        schema_path = Path(__file__).parent / "knowledge_schema.sql"
        if schema_path.exists():
            with open(schema_path, "r", encoding="utf-8") as f:
                self._get_conn().executescript(f.read())
            self._get_conn().commit()

    def import_from_json(self, json_path: str):
        """从JSON批量导入知识数据"""
        path = Path(json_path)
        if not path.exists():
            return False

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        conn = self._get_conn()

        # 导入台词
        for dialogue in data.get("dialogues", []):
            conn.execute(
                "INSERT INTO dialogues (scene_type, content, mood, weight) VALUES (?, ?, ?, ?)",
                (
                    dialogue["scene_type"],
                    dialogue["content"],
                    dialogue.get("mood", "neutral"),
                    dialogue.get("weight", 1),
                ),
            )

        # 导入语音台词
        for line in data.get("voice_lines", []):
            conn.execute(
                "INSERT INTO voice_lines (line_type, content, context, translation, weight) VALUES (?, ?, ?, ?, ?)",
                (
                    line["line_type"],
                    line["content"],
                    line.get("context"),
                    line.get("translation"),
                    line.get("weight", 1),
                ),
            )

        # 导入剧情
        for chapter in data.get("story_chapters", []):
            conn.execute(
                "INSERT INTO story_chapters (chapter_name, summary, full_text, characters, location, importance) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chapter["chapter_name"],
                    chapter["summary"],
                    chapter.get("full_text"),
                    json.dumps(chapter.get("characters", [])),
                    chapter.get("location"),
                    chapter.get("importance", 1),
                ),
            )

        # 导入知识
        for knowledge in data.get("knowledge", []):
            conn.execute(
                "INSERT INTO character_knowledge (category, entity_name, description, related_entities, importance) VALUES (?, ?, ?, ?, ?)",
                (
                    knowledge["category"],
                    knowledge["entity_name"],
                    knowledge["description"],
                    json.dumps(knowledge.get("related_entities", [])),
                    knowledge.get("importance", 1),
                ),
            )

        # 导入类比
        for analogy in data.get("analogies", []):
            conn.execute(
                "INSERT INTO modern_analogies (modern_term, yunli_analogy, category) VALUES (?, ?, ?)",
                (
                    analogy["modern_term"],
                    analogy["yunli_analogy"],
                    analogy.get("category"),
                ),
            )

        # 导入情感模板
        for template in data.get("emotion_templates", []):
            conn.execute(
                "INSERT INTO emotion_templates (emotion, template_type, content, weight) VALUES (?, ?, ?, ?)",
                (
                    template["emotion"],
                    template["template_type"],
                    template["content"],
                    template.get("weight", 1),
                ),
            )

        conn.commit()
        return True

    def query_dialogues(
        self, scene_type: str, mood: str = None, limit: int = 5
    ) -> List[Dict]:
        """按场景和情绪查询台词"""
        conn = self._get_conn()
        if mood:
            cursor = conn.execute(
                "SELECT * FROM dialogues WHERE scene_type = ? AND mood = ? ORDER BY weight DESC, RANDOM() LIMIT ?",
                (scene_type, mood, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM dialogues WHERE scene_type = ? ORDER BY weight DESC, RANDOM() LIMIT ?",
                (scene_type, limit),
            )
        return [dict(row) for row in cursor.fetchall()]

    def query_voice_lines(self, line_type: str = None, limit: int = 5) -> List[Dict]:
        """查询语音台词"""
        conn = self._get_conn()
        if line_type:
            cursor = conn.execute(
                "SELECT * FROM voice_lines WHERE line_type = ? ORDER BY weight DESC, RANDOM() LIMIT ?",
                (line_type, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM voice_lines ORDER BY RANDOM() LIMIT ?", (limit,)
            )
        return [dict(row) for row in cursor.fetchall()]

    def query_knowledge(self, keyword: str, limit: int = 3) -> List[Dict]:
        """查询角色知识库（带LRU缓存，线程安全）

        缓存读/写均受 _cache_lock 保护，避免多线程并发下
        OrderedDict 的 pop+重新插入复合操作导致数据损坏。
        数据库查询在锁外执行，不影响并发性能。
        """
        cache_key = (keyword, limit)

        # 缓存读：加锁保护 OrderedDict 的 pop + 重新插入（LRU 命中提升）
        with self._cache_lock:
            if cache_key in self._knowledge_query_cache:
                result = self._knowledge_query_cache.pop(cache_key)
                self._knowledge_query_cache[cache_key] = result
                return result

        # 缓存未命中：查询数据库（使用线程本地连接，无需加锁）
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM character_knowledge WHERE entity_name LIKE ? OR description LIKE ? ORDER BY importance DESC LIMIT ?",
            (f"%{keyword}%", f"%{keyword}%", limit),
        )
        result = [dict(row) for row in cursor.fetchall()]

        # 缓存写：加锁保护淘汰 + 写入
        with self._cache_lock:
            while len(self._knowledge_query_cache) >= self._knowledge_cache_max:
                self._knowledge_query_cache.popitem(last=False)
            self._knowledge_query_cache[cache_key] = result
        return result

    def query_knowledge_by_category(self, category: str, limit: int = 10) -> List[Dict]:
        """按分类查询知识"""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM character_knowledge WHERE category = ? ORDER BY importance DESC LIMIT ?",
            (category, limit),
        )
        return [dict(row) for row in cursor.fetchall()]

    def query_analogy(self, term: str) -> Optional[Dict]:
        """查询现代概念类比"""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM modern_analogies WHERE modern_term = ? OR modern_term LIKE ? LIMIT 1",
            (term, f"%{term}%"),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def query_analogies_by_category(self, category: str, limit: int = 10) -> List[Dict]:
        """按分类查询类比"""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM modern_analogies WHERE category = ? LIMIT ?",
            (category, limit),
        )
        return [dict(row) for row in cursor.fetchall()]

    def query_story(self, keyword: str, limit: int = 3) -> List[Dict]:
        """查询剧情章节"""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM story_chapters WHERE chapter_name LIKE ? OR summary LIKE ? OR characters LIKE ? ORDER BY importance DESC LIMIT ?",
            (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%", limit),
        )
        return [dict(row) for row in cursor.fetchall()]

    def query_emotion_templates(
        self, emotion: str, template_type: str = None, limit: int = 5
    ) -> List[Dict]:
        """查询情感模板"""
        conn = self._get_conn()
        if template_type:
            cursor = conn.execute(
                "SELECT * FROM emotion_templates WHERE emotion = ? AND template_type = ? ORDER BY weight DESC, RANDOM() LIMIT ?",
                (emotion, template_type, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM emotion_templates WHERE emotion = ? ORDER BY weight DESC, RANDOM() LIMIT ?",
                (emotion, limit),
            )
        return [dict(row) for row in cursor.fetchall()]

    def update_dialogue_usage(self, dialogue_id: int):
        """更新台词使用次数"""
        conn = self._get_conn()
        conn.execute(
            "UPDATE dialogues SET usage_count = usage_count + 1, last_used = datetime('now') WHERE id = ?",
            (dialogue_id,),
        )
        conn.commit()

    def add_knowledge(
        self,
        category: str,
        entity_name: str,
        description: str,
        related_entities: List[str] = None,
        importance: int = 1,
    ):
        """动态添加知识（支持运行时扩展）"""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO character_knowledge (category, entity_name, description, related_entities, importance) VALUES (?, ?, ?, ?, ?)",
            (
                category,
                entity_name,
                description,
                json.dumps(related_entities or []),
                importance,
            ),
        )
        conn.commit()

    def add_analogy(self, modern_term: str, yunli_analogy: str, category: str = None):
        """动态添加现代概念类比"""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO modern_analogies (modern_term, yunli_analogy, category) VALUES (?, ?, ?)",
            (modern_term, yunli_analogy, category),
        )
        conn.commit()

    def add_dialogue(
        self, scene_type: str, content: str, mood: str = "neutral", weight: int = 1
    ):
        """动态添加台词"""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO dialogues (scene_type, content, mood, weight) VALUES (?, ?, ?, ?)",
            (scene_type, content, mood, weight),
        )
        conn.commit()

    def add_voice_line(
        self,
        line_type: str,
        content: str,
        context: str = None,
        translation: str = None,
        weight: int = 1,
    ):
        """动态添加语音/经典台词"""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO voice_lines (line_type, content, context, translation, weight) VALUES (?, ?, ?, ?, ?)",
            (line_type, content, context, translation, weight),
        )
        conn.commit()

    def add_story_chapter(
        self,
        chapter_name: str,
        summary: str,
        full_text: str = None,
        characters: List[str] = None,
        location: str = None,
        importance: int = 1,
    ):
        """动态添加剧情章节"""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO story_chapters (chapter_name, summary, full_text, characters, location, importance) VALUES (?, ?, ?, ?, ?, ?)",
            (
                chapter_name,
                summary,
                full_text,
                json.dumps(characters or []),
                location,
                importance,
            ),
        )
        conn.commit()

    def add_emotion_template(
        self, emotion: str, template_type: str, content: str, weight: int = 1
    ):
        """动态添加情感表达模板"""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO emotion_templates (emotion, template_type, content, weight) VALUES (?, ?, ?, ?)",
            (emotion, template_type, content, weight),
        )
        conn.commit()

    def get_db_version(self) -> int:
        """获取数据库版本"""
        conn = self._get_conn()
        cursor = conn.execute("SELECT version FROM db_version LIMIT 1")
        row = cursor.fetchone()
        return row["version"] if row else 0

    def close(self):
        """关闭数据库连接"""
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


class YunliMemoryDB:
    """云璃记忆库 - 存储动态数据（互动记录、话题、用户记忆、群摘要）

    使用 threading.Lock 保护共享连接，替代 check_same_thread=False 的不安全方案。
    所有 public 方法在访问 self.conn 前必须获取 self._lock。
    """

    # 安全表名白名单（用于 reset_memory 防止注入）
    _ALLOWED_TABLES = frozenset({
        "interaction_logs", "user_memories", "topic_history",
        "chat_summaries", "open_loops",
    })

    def __init__(self, db_path: str = None):
        if db_path is None:
            self.db_path = Path(__file__).parent / "memory.db"
        else:
            self.db_path = Path(db_path)

        self._lock = threading.Lock()
        # P1-5 修复：记录连接参数，支持自动重连
        self._connect()
        # 批量提交缓冲区
        self._log_buffer = []
        self._log_batch_size = 10
        self._init_tables()

    def _connect(self):
        """建立数据库连接并配置 PRAGMA

        P1-5 修复：抽出连接逻辑，支持连接断开后自动重连。
        """
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL模式 + 生产级PRAGMA：读写不互斥，减少磁盘同步开销
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-8000")
        self.conn.execute("PRAGMA busy_timeout=5000")

    def health_check(self) -> bool:
        """P1-5 修复：数据库连接健康检查

        通过执行 `SELECT 1` 检测连接是否有效。
        若连接失效，尝试自动重连。

        Returns:
            True 表示连接有效（含重连成功），False 表示连接不可恢复
        """
        with self._lock:
            try:
                self.conn.execute("SELECT 1").fetchone()
                return True
            except sqlite3.Error as e:
                logger.warning("数据库连接失效，尝试重连: %s", e)
                try:
                    # 关闭旧连接（可能已失效）
                    try:
                        self.conn.close()
                    except Exception:
                        pass
                    # 重新建立连接
                    self._connect()
                    # 验证重连是否成功
                    self.conn.execute("SELECT 1").fetchone()
                    logger.info("数据库重连成功")
                    return True
                except sqlite3.Error as reconnect_err:
                    logger.error(
                        "数据库重连失败，连接不可恢复: %s",
                        reconnect_err, exc_info=True,
                    )
                    return False

    def _init_tables(self):
        """初始化记忆库表结构"""
        schema_path = Path(__file__).parent / "memory_schema.sql"
        if schema_path.exists():
            with open(schema_path, "r", encoding="utf-8") as f:
                self.conn.executescript(f.read())
            self.conn.commit()

    def log_interaction(
        self,
        group_id: str,
        user_id: str,
        user_nickname: str,
        message: str,
        response: str,
        trigger_type: str,
        emotion_state: str,
    ):
        """记录互动日志（批量提交，减少磁盘同步开销）"""
        with self._lock:
            self._log_buffer.append((
                group_id, user_id, user_nickname,
                message, response, trigger_type, emotion_state,
            ))
            if len(self._log_buffer) >= self._log_batch_size:
                self._flush_logs_locked()

    def _flush_logs(self):
        """批量写入缓冲区中的日志并提交（自动加锁）"""
        with self._lock:
            self._flush_logs_locked()

    def _flush_logs_locked(self):
        """批量写入缓冲区中的日志并提交（调用方必须已持有 self._lock）"""
        if not self._log_buffer:
            return
        try:
            self.conn.executemany(
                "INSERT INTO interaction_logs (group_id, user_id, user_nickname, message, response, trigger_type, emotion_state) VALUES (?, ?, ?, ?, ?, ?, ?)",
                self._log_buffer,
            )
            self.conn.commit()
            self._log_buffer.clear()
        except Exception as e:
            logger.error("批量写入互动日志失败: %s", e)
            self.conn.rollback()
            # 保留缓冲区数据以便重试，仅清除已超限的部分避免内存泄漏
            if len(self._log_buffer) > self._log_batch_size * 5:
                self._log_buffer = self._log_buffer[-self._log_batch_size:]

    def flush_logs(self):
        """显式刷新日志缓冲区（供外部调用，如关闭数据库前）"""
        self._flush_logs()

    def get_user_stats(self, group_id: str, user_id: str) -> Dict:
        """获取用户在群里的互动统计"""
        with self._lock:
            self._flush_logs_locked()
            cursor = self.conn.execute(
                "SELECT COUNT(*) as total, MAX(created_at) as last_time FROM interaction_logs WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            )
            return dict(cursor.fetchone())

    def get_user_recent_messages(
        self, group_id: str, user_id: str, limit: int = 5
    ) -> List[Dict]:
        """获取用户最近的消息记录"""
        with self._lock:
            self._flush_logs_locked()
            cursor = self.conn.execute(
                "SELECT message, response, emotion_state, created_at FROM interaction_logs WHERE group_id = ? AND user_id = ? ORDER BY created_at DESC LIMIT ?",
                (group_id, user_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_user_emotion_trend(self, group_id: str, user_id: str) -> str:
        """获取用户最近的情绪趋势"""
        with self._lock:
            self._flush_logs_locked()  # 修复：查询前刷新日志缓冲区，与同类查询方法保持一致
            cursor = self.conn.execute(
                "SELECT emotion_state, COUNT(*) as count FROM interaction_logs WHERE group_id = ? AND user_id = ? AND created_at > datetime('now', '-1 day') GROUP BY emotion_state ORDER BY count DESC LIMIT 1",
                (group_id, user_id),
            )
            row = cursor.fetchone()
            return row["emotion_state"] if row else "neutral"

    def get_recent_logs(self, group_id: str, limit: int = 5) -> List[Dict]:
        """获取最近群聊记录"""
        with self._lock:
            self._flush_logs_locked()
            cursor = self.conn.execute(
                "SELECT * FROM interaction_logs WHERE group_id = ? ORDER BY created_at DESC LIMIT ?",
                (group_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_group_stats(self, group_id: str) -> Dict:
        """获取群的互动统计"""
        with self._lock:
            self._flush_logs_locked()
            cursor = self.conn.execute(
                "SELECT COUNT(*) as total, COUNT(DISTINCT user_id) as unique_users FROM interaction_logs WHERE group_id = ?",
                (group_id,),
            )
            return dict(cursor.fetchone())

    # ========== 话题追踪 ==========

    def update_topic(self, group_id: str, topic: str, user_id: str, cooldown_seconds: int = 30):
        """更新或创建话题

        Args:
            group_id: 群ID
            topic: 话题名称
            user_id: 用户ID
            cooldown_seconds: 话题切换冷却时间（秒），默认30秒
                - 避免话题过于频繁切换（如群友A说食物，群友B立刻说游戏）
                - 同一话题内不受冷却限制，正常累加计数
        """
        with self._lock:
            # P2-1 修复：统一时间戳类型为 TEXT（datetime 格式）
            # 原 last_active 用 int(time.time())（INTEGER），与 schema 声明的 TIMESTAMP（TEXT）不一致
            # 冷却时间计算改用 strftime 提取 epoch 秒数

            # 检查当前活跃话题
            cursor = self.conn.execute(
                "SELECT id, topic, message_count, participants, last_active FROM topic_history WHERE group_id = ? AND is_active = 1",
                (group_id,),
            )
            active_row = cursor.fetchone()

            if active_row:
                active_topic_name = active_row["topic"]

                # 同一话题：正常累加，不受冷却限制
                if active_topic_name == topic:
                    participants = json.loads(active_row["participants"] or "[]")
                    if user_id not in participants:
                        participants.append(user_id)

                    self.conn.execute(
                        "UPDATE topic_history SET last_active = datetime('now'), message_count = message_count + 1, participants = ? WHERE id = ?",
                        (json.dumps(participants), active_row["id"]),
                    )
                    self.conn.commit()
                    return

                # 不同话题：检查冷却时间
                # P2-1 修复：last_active 现在是 TEXT 格式，用 strftime 转换为 epoch 秒数
                last_active_str = active_row["last_active"] or ""
                if last_active_str and last_active_str != "0":
                    try:
                        last_active_epoch = self.conn.execute(
                            "SELECT strftime('%s', ?)", (last_active_str,)
                        ).fetchone()[0]
                        now_epoch = self.conn.execute(
                            "SELECT strftime('%s', 'now')"
                        ).fetchone()[0]
                        time_since_last = int(now_epoch) - int(last_active_epoch)
                    except (ValueError, TypeError):
                        time_since_last = cooldown_seconds + 1  # 解析失败，跳过冷却
                else:
                    time_since_last = cooldown_seconds + 1  # 无上次活跃时间，跳过冷却

                if time_since_last < cooldown_seconds:
                    # 冷却期内，不切换话题，将消息计入当前活跃话题
                    participants = json.loads(active_row["participants"] or "[]")
                    if user_id not in participants:
                        participants.append(user_id)

                    self.conn.execute(
                        "UPDATE topic_history SET last_active = datetime('now'), message_count = message_count + 1, participants = ? WHERE id = ?",
                        (json.dumps(participants), active_row["id"]),
                    )
                    self.conn.commit()
                    return

            # 新话题或冷却期已过：切换话题
            # 先关闭当前活跃话题
            self.conn.execute(
                "UPDATE topic_history SET is_active = 0 WHERE group_id = ? AND is_active = 1",
                (group_id,),
            )

            # 检查是否已存在该话题的历史记录（非活跃状态）
            cursor = self.conn.execute(
                "SELECT id, message_count, participants FROM topic_history WHERE group_id = ? AND topic = ? AND is_active = 0 ORDER BY last_active DESC LIMIT 1",
                (group_id, topic),
            )
            existing_row = cursor.fetchone()

            if existing_row:
                # 重新激活已有话题
                participants = json.loads(existing_row["participants"] or "[]")
                if user_id not in participants:
                    participants.append(user_id)

                self.conn.execute(
                    "UPDATE topic_history SET is_active = 1, last_active = datetime('now'), message_count = message_count + 1, participants = ? WHERE id = ?",
                    (json.dumps(participants), existing_row["id"]),
                )
            else:
                # 创建新话题
                self.conn.execute(
                    "INSERT INTO topic_history (group_id, topic, participants) VALUES (?, ?, ?)",
                    (group_id, topic, json.dumps([user_id])),
                )

            self.conn.commit()

    def get_active_topic(self, group_id: str) -> Optional[Dict]:
        """获取当前活跃话题"""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM topic_history WHERE group_id = ? AND is_active = 1 ORDER BY last_active DESC LIMIT 1",
                (group_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_recent_topics(self, group_id: str, limit: int = 5) -> List[Dict]:
        """获取最近的话题历史"""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM topic_history WHERE group_id = ? ORDER BY last_active DESC LIMIT ?",
                (group_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    # ========== 用户记忆 ==========

    def add_memory(
        self,
        group_id: str,
        user_id: str,
        memory_type: str,
        content: str,
        confidence: int = 5,
        expires_at: str = None,
        max_memories_per_user: int = 50,
        user_nickname: str = None,
    ):
        """添加用户记忆（增强版，支持相似度检测、冲突处理和容量控制）

        Args:
            max_memories_per_user: 单用户记忆上限，超过时返回需要整理的信号
            user_nickname: 用户昵称（可选，用于群友记忆共享时显示）

        线程安全：全程持有 self._lock，防止并发写入破坏数据一致性。
        """
        with self._lock:
            cursor = self.conn.execute(
                "SELECT id, content, confidence, status FROM user_memories WHERE group_id = ? AND user_id = ? AND memory_type = ? AND status = 'active'",
                (group_id, user_id, memory_type),
            )
            existing_memories = [dict(row) for row in cursor.fetchall()]

            # 检查是否有完全相同的记忆
            for mem in existing_memories:
                if mem["content"] == content:
                    # P2-1 修复：SQLite 可能返回字符串类型的 confidence，需强制转换
                    mem_confidence = int(mem["confidence"]) if mem["confidence"] else 0
                    new_confidence = min(mem_confidence + 1, 10)
                    if user_nickname:
                        self.conn.execute(
                            "UPDATE user_memories SET confidence = ?, access_count = access_count + 1, last_accessed = datetime('now'), user_nickname = ? WHERE id = ?",
                            (new_confidence, user_nickname, mem["id"]),
                        )
                    else:
                        self.conn.execute(
                            "UPDATE user_memories SET confidence = ?, access_count = access_count + 1, last_accessed = datetime('now') WHERE id = ?",
                            (new_confidence, mem["id"]),
                        )
                    self.conn.commit()
                    return {"action": "updated", "needs_consolidation": False}

            # 检查是否有相似记忆
            for mem in existing_memories:
                existing_content = mem["content"]
                if content in existing_content or existing_content in content:
                    if len(content) > len(existing_content):
                        if user_nickname:
                            self.conn.execute(
                                "UPDATE user_memories SET content = ?, confidence = ?, last_accessed = datetime('now'), user_nickname = ? WHERE id = ?",
                                (content, min(confidence + 1, 10), user_nickname, mem["id"]),
                            )
                        else:
                            self.conn.execute(
                                "UPDATE user_memories SET content = ?, confidence = ?, last_accessed = datetime('now') WHERE id = ?",
                                (content, min(confidence + 1, 10), mem["id"]),
                            )
                    else:
                        mem_confidence = int(mem["confidence"]) if mem["confidence"] else 0
                        new_confidence = min(mem_confidence + 1, 10)
                        if user_nickname:
                            self.conn.execute(
                                "UPDATE user_memories SET confidence = ?, access_count = access_count + 1, last_accessed = datetime('now'), user_nickname = ? WHERE id = ?",
                                (new_confidence, user_nickname, mem["id"]),
                            )
                        else:
                            self.conn.execute(
                                "UPDATE user_memories SET confidence = ?, access_count = access_count + 1, last_accessed = datetime('now') WHERE id = ?",
                                (new_confidence, mem["id"]),
                            )
                    self.conn.commit()
                    return {"action": "updated", "needs_consolidation": False}

            # 检查是否有冲突记忆（反义词检测 + 语义冲突检测）
            conflict_keywords = {
                "喜欢": "讨厌",
                "爱": "恨",
                "开心": "难过",
                "好": "坏",
                "大": "小",
                "多": "少",
                "吃": "不吃",
                "去": "不去",
                "要": "不要",
            }

            # 语义冲突模式：检测隐含的身份/状态矛盾
            # 注意：每对词都附带"上下文要求"，避免误判
            # - ("学生", "工作", "身份")：仅在"是学生"/"在工作"等身份表述时才判冲突
            # - ("男", "女", "性别")：仅在"是男"/"是女"等性别表述时才判冲突
            semantic_conflicts = [
                ("学生", "工作", "身份"),
                ("上学", "上班", "身份"),
                ("读书", "工作", "身份"),
                ("单身", "恋爱", "状态"),
                ("男", "女", "性别"),
            ]

            # 身份/性别表述的上下文模板：仅在这些句式中才判定冲突
            # 避免从"我喜欢男的"和"我是女的"误判性别冲突
            identity_patterns = {
                "身份": ["我是", "现在是", "毕业后是", "一直是个"],
                "性别": ["我是", "性别是", "我是男", "我是女"],
                "状态": ["我是", "现在是", "目前", "单身", "恋爱中"],
            }

            def _is_identity_statement(content: str, word: str, ctx_type: str) -> bool:
                """判断 content 中 word 是否出现在身份/性别表述的上下文中

                如"我是学生"中"学生"是身份表述，但"我喜欢学生"中"学生"不是身份表述。

                P0-5 修复：原逻辑仅检查 word 前 5 个字符，存在缺陷：
                  - "我其实是一个学生" → "学生"前 5 字符是"其实是一个"，
                    不包含"我是"，不触发身份校验，导致冲突检测失效
                修复方案：
                  - 扩大检查范围到 word 前 10 字符（覆盖"我其实是一个"等口语前缀）
                  - 增加全文匹配模式（如"我是XX""现在是XX""毕业后是XX"）
                """
                patterns = identity_patterns.get(ctx_type, [])
                for pattern in patterns:
                    # 检查 word 是否紧跟在身份表述之后（允许中间有"一个""一名"等量词）
                    idx = content.find(word)
                    if idx < 0:
                        continue
                    before = content[:idx]
                    # P0-5 修复：扩大检查范围到前 10 字符（覆盖"我其实是一个"等口语前缀）
                    if any(p in before[-10:] for p in patterns):
                        return True
                    # 全文匹配：检查 content 是否以"我是/现在是 + word"结尾
                    # 如"我其实是一个学生"中"学生"前有"是一个"，
                    # 虽然"我是"不在最后 10 字符的结尾，但"是"在
                    if any(p in before for p in patterns):
                        return True
                return False

            has_conflict = False
            for mem in existing_memories:
                existing_content = mem["content"]

                # 1. 反义词冲突检测
                # P1-4 修复：原逻辑对"大城市 vs 小城市"等同一修饰对象的不同偏好误判为冲突
                # 修复方案：
                #   - 提取冲突词所在的完整修饰对象（冲突词+后续字符）
                #   - 比较两个修饰对象是否相同：相同则不是冲突（如"大城市"vs"小城市"）
                #   - 不同则是真正的冲突（如"喜欢猫"vs"讨厌狗"）
                for pos, neg in conflict_keywords.items():
                    pos_in_existing = pos in existing_content
                    neg_in_existing = neg in existing_content
                    pos_in_new = pos in content
                    neg_in_new = neg in content

                    if (pos_in_existing and neg_in_new) or (neg_in_existing and pos_in_new):
                        # 长度保护：冲突词对不应占内容主体
                        shorter = min(len(existing_content), len(content))
                        if shorter <= 3:
                            has_conflict = True
                            break

                        # P1-4 修复：提取并比较冲突词所在的修饰对象
                        # 判断是否是"大X vs 小X"这类同一对象的不同偏好
                        def _extract_modifier(text: str, word: str) -> str:
                            """提取冲突词及其后的修饰对象（如"大城市"中的"大城市"）"""
                            idx = text.find(word)
                            if idx < 0:
                                return word
                            # 取冲突词及其后最多 4 个字符作为修饰对象
                            return text[idx:idx + len(word) + 4]

                        if pos_in_existing and neg_in_new:
                            existing_mod = _extract_modifier(existing_content, pos)
                            new_mod = _extract_modifier(content, neg)
                        else:
                            existing_mod = _extract_modifier(existing_content, neg)
                            new_mod = _extract_modifier(content, pos)

                        # 检查两个修饰对象是否有公共后缀（如"城市"）
                        # 如果冲突词后跟相同的字符，说明是同一对象的不同偏好
                        is_same_object = False
                        for suffix_len in range(1, min(len(existing_mod), len(new_mod)) + 1):
                            if existing_mod[-suffix_len:] == new_mod[-suffix_len:]:
                                # 检查公共后缀是否在冲突词之后（排除冲突词本身相同的情况）
                                existing_suffix = existing_mod[len(pos):][-suffix_len:] if pos_in_existing else existing_mod[len(neg):][-suffix_len:]
                                new_suffix = new_mod[len(neg):][-suffix_len:] if pos_in_existing and neg_in_new else new_mod[len(pos):][-suffix_len:]
                                if existing_suffix and new_suffix and existing_suffix == new_suffix:
                                    is_same_object = True
                                    break

                        if not is_same_object:
                            has_conflict = True
                            break

                # 2. 语义冲突检测（身份/状态矛盾，带上下文校验）
                #    仅当双方都是身份/性别表述时才判定冲突
                #    避免"我是学生干部"和"我在工作"误判
                #    避免"我喜欢男的"和"我是女的"误判
                if not has_conflict:
                    for word_a, word_b, ctx_type in semantic_conflicts:
                        a_in_existing = word_a in existing_content
                        b_in_existing = word_b in existing_content
                        a_in_new = word_a in content
                        b_in_new = word_b in content

                        # 一方有 word_a，另一方有 word_b
                        if (a_in_existing and b_in_new) or (b_in_existing and a_in_new):
                            # 上下文校验：双方都必须是身份/性别表述
                            if (a_in_existing and b_in_new):
                                is_existing_identity = _is_identity_statement(existing_content, word_a, ctx_type)
                                is_new_identity = _is_identity_statement(content, word_b, ctx_type)
                            else:
                                is_existing_identity = _is_identity_statement(existing_content, word_b, ctx_type)
                                is_new_identity = _is_identity_statement(content, word_a, ctx_type)

                            if is_existing_identity and is_new_identity:
                                has_conflict = True
                                break

                # 标记冲突的旧记忆
                if has_conflict:
                    self.conn.execute(
                        "UPDATE user_memories SET status = 'conflicted' WHERE id = ?",
                        (mem["id"],),
                    )
                    self.conn.commit()
                    break  # 标记后跳出外层 for

            # 检查单用户记忆总数
            total_count = self.conn.execute(
                "SELECT COUNT(*) FROM user_memories WHERE group_id = ? AND user_id = ? AND status = 'active'",
                (group_id, user_id),
            ).fetchone()[0]

            needs_consolidation = total_count + 1 >= max_memories_per_user

            if total_count >= max_memories_per_user:
                self.conn.execute(
                    """DELETE FROM user_memories WHERE id = (
                        SELECT id FROM user_memories
                        WHERE group_id = ? AND user_id = ? AND status = 'active'
                        ORDER BY confidence ASC, access_count ASC, last_accessed ASC LIMIT 1
                    )""",
                    (group_id, user_id),
                )

            self.conn.execute(
                "INSERT INTO user_memories (group_id, user_id, user_nickname, memory_type, content, confidence, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (group_id, user_id, user_nickname, memory_type, content, confidence, expires_at),
            )
            self.conn.commit()

            return {"action": "inserted", "needs_consolidation": needs_consolidation}

    def delete_user_memories(self, group_id: str, user_id: str):
        """删除指定用户的所有记忆（用于深度整理前清空旧记忆）"""
        with self._lock:
            self.conn.execute(
                "DELETE FROM user_memories WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            )
            self.conn.commit()

    def _safe_rollback(self):
        """安全回滚事务，记录失败日志并尝试恢复连接状态

        P0-2 修复：ROLLBACK 失败不再静默吞掉（原代码 `except: pass`），
        而是记录错误日志，便于排查"连接处于脏状态"问题。

        ROLLBACK 失败的常见原因：
        - 连接已断开（磁盘错误、数据库文件被删）
        - 事务嵌套（显式 BEGIN 与 sqlite3 隐式事务冲突）
        - 连接被其他线程锁定

        失败后连接可能处于"cannot start a transaction within a transaction"
        状态，后续所有操作都会失败。此处记录日志以便运维介入。
        """
        try:
            self.conn.execute("ROLLBACK")
        except Exception as rollback_err:
            # ROLLBACK 失败：连接可能处于脏状态，后续操作会失败
            logger.error(
                "ROLLBACK 失败，连接可能处于脏状态: %s",
                rollback_err,
                exc_info=True,
            )
            # 尝试恢复：再次执行 ROLLBACK（处理可能的嵌套事务）
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                # 仍然失败：连接已不可恢复，记录严重警告
                logger.error(
                    "ROLLBACK 二次尝试仍失败，连接已不可恢复，"
                    "建议重启插件或检查数据库完整性"
                )

    def replace_user_memories(
        self,
        group_id: str,
        user_id: str,
        new_memories: List[Dict],
        user_nickname: str = "",
    ) -> int:
        """原子性替换用户记忆（用于 LLM 深度整理）

        事务保护流程：
        1. BEGIN TRANSACTION
        2. 删除旧记忆
        3. 写入新记忆
        4. 检查 written_count：
           - == 0 → ROLLBACK（保留旧记忆，避免空结果数据丢失）
           - > 0  → COMMIT
        5. 任何步骤失败 → ROLLBACK（旧记忆不变）

        P0-1 修复：当 LLM 返回的内容全部被长度校验过滤掉时，
        written_count 为 0，此时不再 COMMIT（会导致旧记忆被清空），
        而是 ROLLBACK 保留旧记忆，避免数据丢失。

        Args:
            new_memories: [{"type": "fact", "content": "...", "confidence": 8}, ...]

        Returns:
            成功写入的记忆条数（0 表示未写入，旧记忆已保留）；
            失败时抛出异常，旧记忆保持不变

        注意：此方法跳过 add_memory 的冲突检测和相似度检测，
        因为整理后的记忆已经是 LLM 去重和解决冲突后的结果。
        """
        with self._lock:
            try:
                # 开启事务
                self.conn.execute("BEGIN TRANSACTION")

                # P1-2 修复：删除旧记忆前，按 memory_type 缓存原 expires_at
                # 避免整理后临时事件（如"今天去爬山"）变为永久记忆
                # 策略：同类型记忆共享原 expires_at（取最近过期时间）
                cursor = self.conn.execute(
                    "SELECT memory_type, expires_at FROM user_memories "
                    "WHERE group_id = ? AND user_id = ? AND expires_at IS NOT NULL",
                    (group_id, user_id),
                )
                type_expires_map = {}
                for row in cursor.fetchall():
                    mem_type = row["memory_type"]
                    expires_at = row["expires_at"]
                    # 同类型取最近的过期时间（避免延长有效期）
                    if mem_type not in type_expires_map or expires_at < type_expires_map[mem_type]:
                        type_expires_map[mem_type] = expires_at

                # P2-7 修复：缓存旧记忆的最高 access_count，整理后继承
                # 避免高频访问的记忆整理后 access_count 重置为 1，丢失访问频次信息
                cursor = self.conn.execute(
                    "SELECT memory_type, MAX(access_count) as max_count FROM user_memories "
                    "WHERE group_id = ? AND user_id = ? GROUP BY memory_type",
                    (group_id, user_id),
                )
                type_max_access = {}
                for row in cursor.fetchall():
                    type_max_access[row["memory_type"]] = row["max_count"]

                # 删除旧记忆
                self.conn.execute(
                    "DELETE FROM user_memories WHERE group_id = ? AND user_id = ?",
                    (group_id, user_id),
                )

                # 写入新记忆
                written_count = 0
                for mem in new_memories:
                    mem_type = mem.get("type", "fact")
                    content = mem.get("content", "").strip()
                    confidence = min(max(mem.get("confidence", 5), 1), 10)

                    # 内容校验
                    if not content or len(content) < 2 or len(content) > 50:
                        continue

                    # P1-2 修复：继承同类型旧记忆的 expires_at
                    # 如果新记忆显式指定了 expires_at，优先使用新值
                    expires_at = mem.get("expires_at")
                    if expires_at is None and mem_type in type_expires_map:
                        expires_at = type_expires_map[mem_type]

                    # P2-7 修复：继承同类型旧记忆的最高 access_count
                    # 避免高频访问的记忆整理后 access_count 重置为 1
                    inherited_access_count = type_max_access.get(mem_type, 1)

                    self.conn.execute(
                        "INSERT INTO user_memories (group_id, user_id, user_nickname, memory_type, content, confidence, status, expires_at, access_count) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                        (group_id, user_id, user_nickname, mem_type, content, confidence, expires_at, inherited_access_count),
                    )
                    written_count += 1

                # P0-1 修复：空结果保护
                # 如果写入 0 条新记忆，ROLLBACK 保留旧记忆，避免数据丢失
                # 场景：LLM 返回的内容全部被长度校验过滤掉，或返回空列表
                if written_count == 0:
                    logger.warning(
                        "replace_user_memories: 整理后 0 条有效记忆，"
                        "ROLLBACK 保留旧记忆 (group_id=%s, user_id=%s, 输入 %d 条)",
                        group_id, user_id, len(new_memories),
                    )
                    self._safe_rollback()
                    return 0

                # 提交事务
                self.conn.execute("COMMIT")
                return written_count

            except Exception:
                # 回滚事务，旧记忆保持不变
                self._safe_rollback()
                raise

    def get_memories(
        self,
        group_id: str,
        user_id: str,
        memory_type: str = None,
        limit: int = 10,
        include_outdated: bool = False,
    ) -> List[Dict]:
        """获取用户记忆（自动过滤过期记忆）"""
        with self._lock:
            if include_outdated:
                if memory_type:
                    cursor = self.conn.execute(
                        "SELECT * FROM user_memories WHERE group_id = ? AND user_id = ? AND memory_type = ? ORDER BY confidence DESC, last_accessed DESC LIMIT ?",
                        (group_id, user_id, memory_type, limit),
                    )
                else:
                    cursor = self.conn.execute(
                        "SELECT * FROM user_memories WHERE group_id = ? AND user_id = ? ORDER BY confidence DESC, last_accessed DESC LIMIT ?",
                        (group_id, user_id, limit),
                    )
            else:
                if memory_type:
                    cursor = self.conn.execute(
                        "SELECT * FROM user_memories WHERE group_id = ? AND user_id = ? AND memory_type = ? AND status = 'active' AND (expires_at IS NULL OR expires_at > datetime('now')) ORDER BY confidence DESC, last_accessed DESC LIMIT ?",
                        (group_id, user_id, memory_type, limit),
                    )
                else:
                    cursor = self.conn.execute(
                        "SELECT * FROM user_memories WHERE group_id = ? AND user_id = ? AND status = 'active' AND (expires_at IS NULL OR expires_at > datetime('now')) ORDER BY confidence DESC, last_accessed DESC LIMIT ?",
                        (group_id, user_id, limit),
                    )
            return [dict(row) for row in cursor.fetchall()]

    def get_important_memories(
        self,
        group_id: str,
        user_id: str,
        min_confidence: int = 7,
        limit: int = 5,
        prefer_short: bool = True,
    ) -> List[Dict]:
        """获取重要记忆（高可信度，自动过滤过期，可选优先短记忆）"""
        with self._lock:
            if prefer_short:
                cursor = self.conn.execute(
                    "SELECT * FROM user_memories WHERE group_id = ? AND user_id = ? AND confidence >= ? AND status = 'active' AND (expires_at IS NULL OR expires_at > datetime('now')) ORDER BY LENGTH(content) ASC, confidence DESC, access_count DESC LIMIT ?",
                    (group_id, user_id, min_confidence, limit * 2),
                )
                memories = [dict(row) for row in cursor.fetchall()]
                seen_types = set()
                filtered = []
                for mem in memories:
                    mem_type = mem.get("memory_type", "")
                    if mem_type not in seen_types:
                        seen_types.add(mem_type)
                        filtered.append(mem)
                        if len(filtered) >= limit:
                            break
                return filtered
            else:
                cursor = self.conn.execute(
                    "SELECT * FROM user_memories WHERE group_id = ? AND user_id = ? AND confidence >= ? AND status = 'active' AND (expires_at IS NULL OR expires_at > datetime('now')) ORDER BY confidence DESC, access_count DESC LIMIT ?",
                    (group_id, user_id, min_confidence, limit),
                )
                return [dict(row) for row in cursor.fetchall()]

    def get_group_memories(
        self,
        group_id: str,
        min_confidence: int = 5,
        limit: int = 10,
        exclude_user_id: str = None,
    ) -> List[Dict]:
        """获取群内所有用户的记忆（用于群友记忆共享）

        Args:
            group_id: 群ID
            min_confidence: 最小置信度
            limit: 返回数量限制
            exclude_user_id: 排除特定用户（通常是当前发言者）
        """
        with self._lock:
            if exclude_user_id:
                cursor = self.conn.execute(
                    "SELECT * FROM user_memories WHERE group_id = ? AND user_id != ? AND confidence >= ? AND status = 'active' AND (expires_at IS NULL OR expires_at > datetime('now')) ORDER BY confidence DESC, last_accessed DESC LIMIT ?",
                    (group_id, exclude_user_id, min_confidence, limit),
                )
            else:
                cursor = self.conn.execute(
                    "SELECT * FROM user_memories WHERE group_id = ? AND confidence >= ? AND status = 'active' AND (expires_at IS NULL OR expires_at > datetime('now')) ORDER BY confidence DESC, last_accessed DESC LIMIT ?",
                    (group_id, min_confidence, limit),
                )
            return [dict(row) for row in cursor.fetchall()]

    def get_memories_by_confidence(
        self, group_id: str, user_id: str, min_confidence: int = 1, limit: int = 10
    ) -> List[Dict]:
        """按可信度获取记忆（不过滤类型，用于测试）"""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM user_memories WHERE group_id = ? AND user_id = ? AND confidence >= ? AND status = 'active' AND (expires_at IS NULL OR expires_at > datetime('now')) ORDER BY confidence DESC, last_accessed DESC LIMIT ?",
                (group_id, user_id, min_confidence, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def update_memory_status(self, memory_id: int, status: str):
        """更新记忆状态（active/outdated/conflicted）"""
        with self._lock:
            self.conn.execute(
                "UPDATE user_memories SET status = ? WHERE id = ?", (status, memory_id)
            )
            self.conn.commit()

    def access_memory(self, memory_id: int):
        """访问记忆，更新访问次数"""
        with self._lock:
            self.conn.execute(
                "UPDATE user_memories SET access_count = access_count + 1, last_accessed = datetime('now') WHERE id = ?",
                (memory_id,),
            )
            self.conn.commit()

    def access_memories_batch(self, memory_ids: list):
        """P1-6 修复：批量访问记忆，更新访问次数

        替代循环调用 access_memory 的 N+1 查询模式。
        一次 UPDATE + COMMIT 完成批量更新，减少锁争用和磁盘 I/O。

        Args:
            memory_ids: 记忆 ID 列表
        """
        if not memory_ids:
            return
        with self._lock:
            placeholders = ",".join("?" * len(memory_ids))
            self.conn.execute(
                f"UPDATE user_memories SET access_count = access_count + 1, last_accessed = datetime('now') "
                f"WHERE id IN ({placeholders})",
                (*memory_ids,),
            )
            self.conn.commit()

    def cleanup_expired_memories(self, group_id: str = None):
        """清理过期记忆"""
        with self._lock:
            if group_id:
                self.conn.execute(
                    "UPDATE user_memories SET status = 'outdated' WHERE group_id = ? AND expires_at IS NOT NULL AND expires_at <= datetime('now')",
                    (group_id,),
                )
            else:
                self.conn.execute(
                    "UPDATE user_memories SET status = 'outdated' WHERE expires_at IS NOT NULL AND expires_at <= datetime('now')"
                )
            self.conn.commit()

    def decay_memory_confidence(self, decay_factor: float = 0.95, min_confidence: int = 5):
        """置信度衰减：定期对所有活跃记忆的 confidence 衰减

        解决"富者愈富"反馈循环：常被召回的记忆越来越强，新记忆永远召回不出来。
        衰减后让新记忆有机会浮现。

        P1-1 修复：min_confidence 默认从 3 提升到 5
          - 原下限 3 低于 get_important_memories / get_group_memories 的默认
            min_confidence=5，导致衰减触底的记忆无法被检索，形成"软性遗忘"
          - 提升到 5 后，衰减下限与检索阈值一致，避免记忆被"软删除"

        P1-7 修复：按 memory_type 差异化衰减
          - fact（身份/职业）：稳定，衰减系数 0.98（衰减慢）
          - preference（偏好）：较稳定，衰减系数 0.95（默认）
          - event（事件）：易变，衰减系数 0.90（衰减快）
          - relationship（关系）：较稳定，衰减系数 0.95（默认）
          避免身份事实与短期事件同等对待导致重要身份记忆丢失

        P1 新记忆保护期：跳过最近 1 小时内创建的记忆
          - 避免刚提取的新记忆立即参与衰减

        Args:
            decay_factor: 衰减系数（0.95 表示每次衰减 5%），用于 preference/relationship
            min_confidence: 最低置信度下限，低于此值不衰减（避免低置信度记忆被清零）
        """
        with self._lock:
            # P1-7 修复：按 memory_type 差异化衰减
            # fact（身份/职业）衰减最慢，event（事件）衰减最快
            type_decay_map = {
                "fact": min(decay_factor + 0.03, 0.99),  # 0.98，身份事实稳定
                "preference": decay_factor,               # 0.95，偏好较稳定
                "relationship": decay_factor,             # 0.95，关系较稳定
                "event": max(decay_factor - 0.05, 0.80),  # 0.90，事件易变
            }

            for mem_type, factor in type_decay_map.items():
                self.conn.execute(
                    """UPDATE user_memories
                       SET confidence = MAX(?, CAST(confidence * ? AS INTEGER))
                       WHERE status = 'active'
                         AND confidence > ?
                         AND memory_type = ?
                         AND created_at < datetime('now', '-1 hour')""",
                    (min_confidence, factor, min_confidence, mem_type),
                )
            self.conn.commit()

    # ========== 群聊摘要 ==========

    def add_summary(
        self,
        group_id: str,
        summary: str,
        key_topics: List[str],
        active_users: List[str],
        message_count: int,
    ):
        """添加群聊摘要"""
        with self._lock:
            self.conn.execute(
                "INSERT INTO chat_summaries (group_id, summary, key_topics, active_users, message_count, start_time) VALUES (?, ?, ?, ?, ?, datetime('now', '-1 hour'))",
                (
                    group_id,
                    summary,
                    json.dumps(key_topics),
                    json.dumps(active_users),
                    message_count,
                ),
            )
            self.conn.commit()

    def get_latest_summary(self, group_id: str) -> Optional[Dict]:
        """获取最新的群聊摘要"""
        with self._lock:
            cursor = self.conn.execute(
                "SELECT * FROM chat_summaries WHERE group_id = ? ORDER BY end_time DESC LIMIT 1",
                (group_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_recent_interactions(
        self, group_id: str, hours: int = 1, limit: int = 50
    ) -> List[Dict]:
        """获取指定群最近 N 小时的互动日志（用于群聊摘要生成）

        Args:
            group_id: 群号
            hours: 查询时间范围（小时）
            limit: 最大返回条数
        """
        with self._lock:
            cursor = self.conn.execute(
                """SELECT * FROM interaction_logs
                   WHERE group_id = ? AND created_at >= datetime('now', ?)
                   ORDER BY created_at DESC LIMIT ?""",
                (group_id, f"-{hours} hours", limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_active_groups(self) -> List[str]:
        """获取最近有互动的群列表（用于群聊摘要生成）"""
        with self._lock:
            cursor = self.conn.execute(
                """SELECT DISTINCT group_id FROM interaction_logs
                   WHERE created_at >= datetime('now', '-2 hours')""",
            )
            return [row["group_id"] for row in cursor.fetchall()]

    def get_db_version(self) -> int:
        """获取数据库版本"""
        with self._lock:
            cursor = self.conn.execute("SELECT version FROM db_version LIMIT 1")
            row = cursor.fetchone()
            return row["version"] if row else 0

    # ========== 未完成约定追踪 ==========

    def add_open_loop(self, group_id: str, user_id: str, text: str, user_nickname: str = "", expires_days: int = 14):
        """添加未完成约定"""
        with self._lock:
            from datetime import datetime, timedelta
            expires_at = (datetime.now() + timedelta(days=expires_days)).isoformat()
            self.conn.execute(
                "INSERT INTO open_loops (group_id, user_id, user_nickname, text, status, expires_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                (group_id, user_id, user_nickname, text, expires_at),
            )
            self.conn.commit()

    def complete_open_loop(self, group_id: str, user_id: str, keyword: str = "") -> bool:
        """标记约定为已完成（匹配关键词）"""
        with self._lock:
            if keyword:
                cursor = self.conn.execute(
                    "UPDATE open_loops SET status = 'completed' WHERE group_id = ? AND user_id = ? AND status = 'pending' AND text LIKE ?",
                    (group_id, user_id, f"%{keyword}%"),
                )
            else:
                cursor = self.conn.execute(
                    "UPDATE open_loops SET status = 'completed' WHERE group_id = ? AND user_id = ? AND status = 'pending'",
                    (group_id, user_id),
                )
            self.conn.commit()
            return cursor.rowcount > 0

    def get_pending_loops(self, group_id: str, user_id: str = "", limit: int = 3) -> List[Dict]:
        """获取未完成的约定"""
        with self._lock:
            from datetime import datetime
            # 先清理过期的约定
            now = datetime.now().isoformat()
            self.conn.execute(
                "UPDATE open_loops SET status = 'expired' WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            self.conn.commit()

            if user_id:
                cursor = self.conn.execute(
                    "SELECT * FROM open_loops WHERE group_id = ? AND user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT ?",
                    (group_id, user_id, limit),
                )
            else:
                cursor = self.conn.execute(
                    "SELECT * FROM open_loops WHERE group_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT ?",
                    (group_id, limit),
                )
            return [dict(row) for row in cursor.fetchall()]

    def cleanup_expired_loops(self):
        """清理所有过期的约定"""
        with self._lock:
            from datetime import datetime
            now = datetime.now().isoformat()
            # 注意：'cancelled' 状态无写入路径（未实现），已从清理条件中移除
            self.conn.execute(
                "DELETE FROM open_loops WHERE status IN ('completed', 'expired') OR (status = 'pending' AND expires_at IS NOT NULL AND expires_at < ?)",
                (now,),
            )
            self.conn.commit()

    def cleanup_old_logs(self, retention_days: int = 7):
        """P1-3 修复：清理过期的互动日志

        interaction_logs 表无 TTL 机制，长期运行后会无限膨胀
        （每条消息写入一条记录，高活跃群每天可达数万条）。

        定期清理保留最近 N 天的数据，避免：
        - 磁盘空间持续增长
        - get_recent_interactions / get_active_groups 查询性能下降
        - 数据库文件过大影响备份和恢复

        Args:
            retention_days: 保留天数，默认 7 天
        """
        with self._lock:
            deleted = self.conn.execute(
                "DELETE FROM interaction_logs WHERE created_at < datetime('now', ?)",
                (f'-{retention_days} days',),
            ).rowcount
            self.conn.commit()
            if deleted > 0:
                logger.info(
                    "清理过期互动日志: 删除 %d 条（保留 %d 天）",
                    deleted, retention_days,
                )

    def cleanup_old_summaries(self, keep_count: int = 48):
        """P1-3 修复：清理过期的群聊摘要

        chat_summaries 表无 TTL 机制，每小时生成 1 条摘要，
        长期运行后会无限增长（一年 8760 条/群）。

        仅保留每群最近 N 条摘要，避免：
        - 磁盘空间持续增长
        - 历史摘要永不检索但仍占用存储

        Args:
            keep_count: 每群保留的摘要条数，默认 48 条（约 48 小时）
        """
        with self._lock:
            # 删除每群超出 keep_count 的旧摘要
            # 使用子查询找出每群要保留的 id
            deleted = self.conn.execute(
                """DELETE FROM chat_summaries
                   WHERE id NOT IN (
                       SELECT id FROM (
                           SELECT id, ROW_NUMBER() OVER (
                               PARTITION BY group_id ORDER BY end_time DESC
                           ) as rn
                           FROM chat_summaries
                       ) WHERE rn <= ?
                   )""",
                (keep_count,),
            ).rowcount
            self.conn.commit()
            if deleted > 0:
                logger.info(
                    "清理过期群聊摘要: 删除 %d 条（每群保留 %d 条）",
                    deleted, keep_count,
                )

    def close(self):
        """关闭数据库连接（先刷新日志缓冲区）"""
        self._flush_logs()
        with self._lock:
            self.conn.close()


class YunliDatabase:
    """云璃数据库统一接口（兼容旧版，内部自动分离知识库和记忆库）

    通过 __getattr__ 动态代理减少 50+ 行显式委托方法。
    仅保留有特殊逻辑的方法（import_from_json、get_db_version、reset_memory、close）。
    """

    def __init__(self, db_path: str = None):
        """初始化数据库

        Args:
            db_path: 兼容参数，实际会分离为知识库和记忆库
        """
        base_dir = Path(db_path).parent if db_path else Path(__file__).parent

        # 分离为两个数据库
        self.knowledge_db = YunliKnowledgeDB(str(base_dir / "knowledge.db"))
        self.memory_db = YunliMemoryDB(str(base_dir / "memory.db"))

    def __getattr__(self, name):
        """动态代理到子数据库（知识库优先，记忆库兜底）"""
        if hasattr(self.knowledge_db, name):
            return getattr(self.knowledge_db, name)
        if hasattr(self.memory_db, name):
            return getattr(self.memory_db, name)
        raise AttributeError(
            f"YunliDatabase 及其子数据库均无属性 '{name}'"
        )

    def import_from_json(self, json_path: str):
        """从JSON导入数据到知识库"""
        return self.knowledge_db.import_from_json(json_path)

    def get_db_version(self) -> int:
        """返回记忆库版本号（知识库和记忆库各有一个 get_db_version）"""
        return self.memory_db.get_db_version()

    def reset_memory(self):
        """重置记忆库（删除所有动态数据，保留表结构）

        必须通过 memory_db._lock 保护，遵守 YunliMemoryDB 的锁契约：
        所有访问 self.conn 的操作都必须在 _lock 保护下进行。
        """
        try:
            # 清空所有记忆相关表（使用白名单校验防止注入）
            tables = [
                "interaction_logs",
                "user_memories",
                "topic_history",
                "chat_summaries",
                "open_loops",
            ]
            memory_db = self.memory_db
            with memory_db._lock:
                for table in tables:
                    if table not in memory_db._ALLOWED_TABLES:
                        logger.warning("跳过非白名单表: %s", table)
                        continue
                    try:
                        memory_db.conn.execute(f"DELETE FROM \"{table}\"")
                    except sqlite3.OperationalError:
                        pass  # 表可能不存在
                memory_db.conn.commit()
            return True
        except Exception as e:
            logger.error("重置记忆库失败: %s", e)
            return False

    def close(self):
        """关闭所有数据库连接"""
        self.knowledge_db.close()
        self.memory_db.close()
