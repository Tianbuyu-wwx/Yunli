import sqlite3
import json
import time
import threading
from collections import OrderedDict
from pathlib import Path
from typing import List, Dict, Optional


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
        self._knowledge_query_cache: OrderedDict = OrderedDict()
        self._knowledge_cache_max = 64

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
        """查询角色知识库（带LRU缓存）"""
        # 构建缓存 key
        cache_key = (keyword, limit)
        if cache_key in self._knowledge_query_cache:
            # 移到末尾标记为最近使用
            result = self._knowledge_query_cache.pop(cache_key)
            self._knowledge_query_cache[cache_key] = result
            return result

        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM character_knowledge WHERE entity_name LIKE ? OR description LIKE ? ORDER BY importance DESC LIMIT ?",
            (f"%{keyword}%", f"%{keyword}%", limit),
        )
        result = [dict(row) for row in cursor.fetchall()]

        # 写入缓存（超限时淘汰最久未访问条目）
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
            "UPDATE dialogues SET usage_count = usage_count + 1, last_used = ? WHERE id = ?",
            (int(time.time()), dialogue_id),
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
        "interaction_logs", "user_memories", "chat_topics",
        "chat_summaries", "open_loops",
    })

    def __init__(self, db_path: str = None):
        if db_path is None:
            self.db_path = Path(__file__).parent / "memory.db"
        else:
            self.db_path = Path(db_path)

        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL模式 + 生产级PRAGMA：读写不互斥，减少磁盘同步开销
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-8000")
        self.conn.execute("PRAGMA busy_timeout=5000")
        # 批量提交缓冲区
        self._log_buffer = []
        self._log_batch_size = 10
        self._init_tables()

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
        self._log_buffer.append((
            group_id, user_id, user_nickname,
            message, response, trigger_type, emotion_state,
        ))
        if len(self._log_buffer) >= self._log_batch_size:
            self._flush_logs()

    def _flush_logs(self):
        """批量写入缓冲区中的日志并提交（线程安全）"""
        if not self._log_buffer:
            return
        with self._lock:
            try:
                self.conn.executemany(
                    "INSERT INTO interaction_logs (group_id, user_id, user_nickname, message, response, trigger_type, emotion_state) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    self._log_buffer,
                )
                self.conn.commit()
                self._log_buffer.clear()
            except Exception as e:
                print(f"[云璃数据库] 批量写入互动日志失败: {e}")
                # 回滚并清空缓冲区，避免内存泄漏
                self.conn.rollback()
                self._log_buffer.clear()

    def flush_logs(self):
        """显式刷新日志缓冲区（供外部调用，如关闭数据库前）"""
        self._flush_logs()

    def get_user_stats(self, group_id: str, user_id: str) -> Dict:
        """获取用户在群里的互动统计"""
        self._flush_logs()
        cursor = self.conn.execute(
            "SELECT COUNT(*) as total, MAX(created_at) as last_time FROM interaction_logs WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        return dict(cursor.fetchone())

    def get_user_recent_messages(
        self, group_id: str, user_id: str, limit: int = 5
    ) -> List[Dict]:
        """获取用户最近的消息记录"""
        self._flush_logs()
        cursor = self.conn.execute(
            "SELECT message, response, emotion_state, created_at FROM interaction_logs WHERE group_id = ? AND user_id = ? ORDER BY created_at DESC LIMIT ?",
            (group_id, user_id, limit),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_user_emotion_trend(self, group_id: str, user_id: str) -> str:
        """获取用户最近的情绪趋势"""
        cursor = self.conn.execute(
            "SELECT emotion_state, COUNT(*) as count FROM interaction_logs WHERE group_id = ? AND user_id = ? AND created_at > datetime('now', '-1 day') GROUP BY emotion_state ORDER BY count DESC LIMIT 1",
            (group_id, user_id),
        )
        row = cursor.fetchone()
        return row["emotion_state"] if row else "neutral"

    def get_recent_logs(self, group_id: str, limit: int = 5) -> List[Dict]:
        """获取最近群聊记录"""
        self._flush_logs()
        cursor = self.conn.execute(
            "SELECT * FROM interaction_logs WHERE group_id = ? ORDER BY created_at DESC LIMIT ?",
            (group_id, limit),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_group_stats(self, group_id: str) -> Dict:
        """获取群的互动统计"""
        self._flush_logs()
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
        now = int(time.time())

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
                    "UPDATE topic_history SET last_active = ?, message_count = message_count + 1, participants = ? WHERE id = ?",
                    (now, json.dumps(participants), active_row["id"]),
                )
                self.conn.commit()
                return

            # 不同话题：检查冷却时间
            last_active = active_row["last_active"] or 0
            time_since_last = now - last_active

            if time_since_last < cooldown_seconds:
                # 冷却期内，不切换话题，将消息计入当前活跃话题
                participants = json.loads(active_row["participants"] or "[]")
                if user_id not in participants:
                    participants.append(user_id)

                self.conn.execute(
                    "UPDATE topic_history SET last_active = ?, message_count = message_count + 1, participants = ? WHERE id = ?",
                    (now, json.dumps(participants), active_row["id"]),
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
                "UPDATE topic_history SET is_active = 1, last_active = ?, message_count = message_count + 1, participants = ? WHERE id = ?",
                (now, json.dumps(participants), existing_row["id"]),
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
        cursor = self.conn.execute(
            "SELECT * FROM topic_history WHERE group_id = ? AND is_active = 1 ORDER BY last_active DESC LIMIT 1",
            (group_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_recent_topics(self, group_id: str, limit: int = 5) -> List[Dict]:
        """获取最近的话题历史"""
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
                    new_confidence = min(mem["confidence"] + 1, 10)
                    if user_nickname:
                        self.conn.execute(
                            "UPDATE user_memories SET confidence = ?, access_count = access_count + 1, last_accessed = ?, user_nickname = ? WHERE id = ?",
                            (new_confidence, int(time.time()), user_nickname, mem["id"]),
                        )
                    else:
                        self.conn.execute(
                            "UPDATE user_memories SET confidence = ?, access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                            (new_confidence, int(time.time()), mem["id"]),
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
                                "UPDATE user_memories SET content = ?, confidence = ?, last_accessed = ?, user_nickname = ? WHERE id = ?",
                                (content, min(confidence + 1, 10), int(time.time()), user_nickname, mem["id"]),
                            )
                        else:
                            self.conn.execute(
                                "UPDATE user_memories SET content = ?, confidence = ?, last_accessed = ? WHERE id = ?",
                                (content, min(confidence + 1, 10), int(time.time()), mem["id"]),
                            )
                    else:
                        new_confidence = min(mem["confidence"] + 1, 10)
                        if user_nickname:
                            self.conn.execute(
                                "UPDATE user_memories SET confidence = ?, access_count = access_count + 1, last_accessed = ?, user_nickname = ? WHERE id = ?",
                                (new_confidence, int(time.time()), user_nickname, mem["id"]),
                            )
                        else:
                            self.conn.execute(
                                "UPDATE user_memories SET confidence = ?, access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                                (new_confidence, int(time.time()), mem["id"]),
                            )
                    self.conn.commit()
                    return {"action": "updated", "needs_consolidation": False}

            # 检查是否有冲突记忆（反义词检测）
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

            has_conflict = False
            for mem in existing_memories:
                existing_content = mem["content"]
                for pos, neg in conflict_keywords.items():
                    if (pos in existing_content and neg in content) or (
                        neg in existing_content and pos in content
                    ):
                        self.conn.execute(
                            "UPDATE user_memories SET status = 'conflicted' WHERE id = ?",
                            (mem["id"],),
                        )
                        has_conflict = True
                        break
                if has_conflict:
                    break

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

    def get_memories(
        self,
        group_id: str,
        user_id: str,
        memory_type: str = None,
        limit: int = 10,
        include_outdated: bool = False,
    ) -> List[Dict]:
        """获取用户记忆（自动过滤过期记忆）"""
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
        cursor = self.conn.execute(
            "SELECT * FROM user_memories WHERE group_id = ? AND user_id = ? AND confidence >= ? AND status = 'active' AND (expires_at IS NULL OR expires_at > datetime('now')) ORDER BY confidence DESC, last_accessed DESC LIMIT ?",
            (group_id, user_id, min_confidence, limit),
        )
        return [dict(row) for row in cursor.fetchall()]

    def update_memory_status(self, memory_id: int, status: str):
        """更新记忆状态（active/outdated/conflicted）"""
        self.conn.execute(
            "UPDATE user_memories SET status = ? WHERE id = ?", (status, memory_id)
        )
        self.conn.commit()

    def access_memory(self, memory_id: int):
        """访问记忆，更新访问次数"""
        self.conn.execute(
            "UPDATE user_memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
            (int(time.time()), memory_id),
        )
        self.conn.commit()

    def cleanup_expired_memories(self, group_id: str = None):
        """清理过期记忆"""
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
        cursor = self.conn.execute(
            "SELECT * FROM chat_summaries WHERE group_id = ? ORDER BY end_time DESC LIMIT 1",
            (group_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_db_version(self) -> int:
        """获取数据库版本"""
        cursor = self.conn.execute("SELECT version FROM db_version LIMIT 1")
        row = cursor.fetchone()
        return row["version"] if row else 0

    # ========== 未完成约定追踪 ==========

    def add_open_loop(self, group_id: str, user_id: str, text: str, user_nickname: str = "", expires_days: int = 14):
        """添加未完成约定"""
        from datetime import datetime, timedelta
        expires_at = (datetime.now() + timedelta(days=expires_days)).isoformat()
        self.conn.execute(
            "INSERT INTO open_loops (group_id, user_id, user_nickname, text, status, expires_at) VALUES (?, ?, ?, ?, 'pending', ?)",
            (group_id, user_id, user_nickname, text, expires_at),
        )
        self.conn.commit()

    def complete_open_loop(self, group_id: str, user_id: str, keyword: str = "") -> bool:
        """标记约定为已完成（匹配关键词）"""
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
        from datetime import datetime
        now = datetime.now().isoformat()
        self.conn.execute(
            "DELETE FROM open_loops WHERE status IN ('completed', 'expired', 'cancelled') OR (status = 'pending' AND expires_at IS NOT NULL AND expires_at < ?)",
            (now,),
        )
        self.conn.commit()

    def close(self):
        """关闭数据库连接（先刷新日志缓冲区）"""
        self._flush_logs()
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
        """重置记忆库（删除所有动态数据，保留表结构）"""
        try:
            # 清空所有记忆相关表（使用白名单校验防止注入）
            tables = [
                "interaction_logs",
                "user_memories",
                "chat_topics",
                "chat_summaries",
                "open_loops",
            ]
            for table in tables:
                if table not in self.memory_db._ALLOWED_TABLES:
                    print(f"[云璃数据库] 跳过非白名单表: {table}")
                    continue
                try:
                    self.memory_db.conn.execute(f"DELETE FROM \"{table}\"")
                except sqlite3.OperationalError:
                    pass  # 表可能不存在
            self.memory_db.conn.commit()
            return True
        except Exception as e:
            print(f"[云璃数据库] 重置记忆库失败: {e}")
            return False

    def close(self):
        """关闭所有数据库连接"""
        self.knowledge_db.close()
        self.memory_db.close()
