import os
import sys
import unittest
import tempfile
import shutil

from tests.test_base import YunliTestCase, setup_test_path

setup_test_path()

from yunli.database import YunliDatabase, YunliKnowledgeDB


class TestYunliDatabase(YunliTestCase):
    """数据库模块测试"""

    def setUp(self):
        """测试前准备"""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.db = YunliDatabase(self.db_path)

        # 导入测试数据
        test_data_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "database",
            "data",
            "initial_data.json",
        )
        if os.path.exists(test_data_path):
            self.db.import_from_json(test_data_path)

    def tearDown(self):
        """测试后清理"""
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def test_query_dialogues(self):
        """测试台词查询"""
        dialogues = self.db.query_dialogues("greeting", limit=2)
        self.assertTrue(len(dialogues) > 0)
        self.assertTrue(all(d["scene_type"] == "greeting" for d in dialogues))

    def test_query_dialogues_with_mood(self):
        """测试按情绪查询台词"""
        dialogues = self.db.query_dialogues("greeting", mood="tsundere", limit=2)
        self.assertTrue(len(dialogues) > 0)
        self.assertTrue(all(d["mood"] == "tsundere" for d in dialogues))

    def test_query_knowledge(self):
        """测试知识查询"""
        knowledge = self.db.query_knowledge("云璃", limit=3)
        self.assertTrue(len(knowledge) > 0)

    def test_query_analogy(self):
        """测试类比查询"""
        analogy = self.db.query_analogy("手机")
        self.assertIsNotNone(analogy)
        # 验证返回了正确的类比数据
        self.assertEqual(analogy["modern_term"], "手机")
        self.assertIsNotNone(analogy["yunli_analogy"])
        self.assertGreater(len(analogy["yunli_analogy"]), 0)

    def test_query_analogy_not_found(self):
        """测试查询不存在的类比"""
        analogy = self.db.query_analogy("不存在的概念")
        self.assertIsNone(analogy)

    def test_log_interaction(self):
        """测试互动记录"""
        self.db.log_interaction(
            group_id="12345",
            user_id="67890",
            user_nickname="测试用户",
            message="你好",
            response="哼，你好。",
            trigger_type="at",
            emotion_state="tsundere",
        )

        stats = self.db.get_user_stats("12345", "67890")
        self.assertTrue(stats["total"] > 0)

    def test_get_recent_logs(self):
        """测试获取最近记录"""
        # 先添加一些记录
        for i in range(5):
            self.db.log_interaction(
                "12345", str(i), f"用户{i}", f"消息{i}", f"回复{i}", "test", "neutral"
            )

        logs = self.db.get_recent_logs("12345", limit=3)
        self.assertEqual(len(logs), 3)

    def test_update_dialogue_usage(self):
        """测试更新台词使用次数"""
        dialogues = self.db.query_dialogues("greeting", limit=1)
        if dialogues:
            dialogue_id = dialogues[0]["id"]
            self.db.update_dialogue_usage(dialogue_id)

            # 验证使用次数增加（通过知识库直接查询）
            self.assertTrue(isinstance(self.db.knowledge_db, YunliKnowledgeDB))

    def test_query_voice_lines(self):
        """测试语音台词查询"""
        lines = self.db.query_voice_lines("skill", limit=2)
        self.assertTrue(len(lines) > 0)
        self.assertTrue(all(l["line_type"] == "skill" for l in lines))

    def test_query_story(self):
        """测试剧情查询"""
        stories = self.db.query_story("云璃", limit=3)
        self.assertTrue(len(stories) > 0)

    def test_query_emotion_templates(self):
        """测试情感模板查询"""
        templates = self.db.query_emotion_templates("excited", limit=3)
        self.assertTrue(len(templates) > 0)

    def test_get_db_version(self):
        """测试数据库版本"""
        version = self.db.get_db_version()
        self.assertGreaterEqual(version, 3)

    def test_topic_tracking(self):
        """测试话题追踪"""
        # 更新话题
        self.db.update_topic("12345", "剑", "user1")
        self.db.update_topic("12345", "剑", "user2")

        # 获取活跃话题
        active_topic = self.db.get_active_topic("12345")
        self.assertIsNotNone(active_topic)
        self.assertEqual(active_topic["topic"], "剑")
        self.assertEqual(active_topic["message_count"], 2)

    def test_user_memory(self):
        """测试用户记忆"""
        # 添加记忆
        self.db.add_memory("12345", "user1", "preference", "吃辣", confidence=6)
        self.db.add_memory("12345", "user1", "fact", "程序员", confidence=5)

        # 获取记忆
        memories = self.db.get_memories("12345", "user1")
        self.assertEqual(len(memories), 2)

        # 获取重要记忆
        important = self.db.get_important_memories("12345", "user1", min_confidence=6)
        self.assertEqual(len(important), 1)
        self.assertEqual(important[0]["content"], "吃辣")

    def test_memory_similarity(self):
        """测试记忆相似度检测"""
        # 添加第一条记忆
        self.db.add_memory("12345", "user1", "preference", "吃火锅", confidence=5)

        # 添加相似记忆（应该合并）
        self.db.add_memory("12345", "user1", "preference", "吃火锅的", confidence=5)

        memories = self.db.get_memories("12345", "user1")
        # 应该只有一条（被合并了）
        self.assertEqual(len(memories), 1)

    def test_memory_conflict(self):
        """测试记忆冲突处理

        v2.2.0 行为：add_memory 检测到反义词冲突时，标记旧记忆 status='conflicted'。
        由于 get_memories 默认只返回 status='active' 的记忆，
        验证冲突需要用 include_outdated=True 看到被标记的旧记忆。
        """
        # 添加正面记忆
        self.db.add_memory("12345", "user1", "preference", "喜欢猫", confidence=6)

        # 添加冲突记忆
        self.db.add_memory("12345", "user1", "preference", "讨厌猫", confidence=6)

        # 获取活跃记忆（默认不过滤 outdated/conflicted）
        active = self.db.get_memories("12345", "user1")
        # 应只有 1 条活跃记忆（旧记忆被标记为 conflicted，新记忆为 active）
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["content"], "讨厌猫")

        # 验证旧记忆确实被标记为 conflicted（需要 include_outdated=True 看到）
        all_memories = self.db.get_memories("12345", "user1", include_outdated=True)
        self.assertEqual(len(all_memories), 2)
        statuses = {m["status"] for m in all_memories}
        self.assertIn("conflicted", statuses)
        self.assertIn("active", statuses)

    def test_memory_expiration(self):
        """测试记忆过期"""
        from datetime import datetime, timedelta

        # 添加过期记忆
        expired_time = (datetime.now() - timedelta(days=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self.db.add_memory(
            "12345", "user1", "fact", "今天加班", confidence=5, expires_at=expired_time
        )

        # 获取记忆（不过滤）
        all_memories = self.db.get_memories("12345", "user1", include_outdated=True)
        self.assertEqual(len(all_memories), 1)

        # 获取记忆（过滤过期）
        active_memories = self.db.get_memories("12345", "user1")
        self.assertEqual(len(active_memories), 0)  # 已过期，应该被过滤

    def test_chat_summary(self):
        """测试群聊摘要"""
        self.db.add_summary(
            "12345", "大家聊了很多关于剑的话题", ["剑", "武器"], ["user1", "user2"], 10
        )

        summary = self.db.get_latest_summary("12345")
        self.assertIsNotNone(summary)
        self.assertIn("剑", summary["summary"])

    def test_add_new_voice_line(self):
        """测试动态添加新角色语音"""
        # 模拟版本更新：添加新角色语音
        self.db.add_voice_line(
            line_type="skill",
            content="新技能：剑气纵横！",
            context="新角色技能释放",
            translation="New skill: Sword Qi Rampage!",
            weight=5,
        )

        # 验证新语音已添加
        lines = self.db.query_voice_lines("skill", limit=10)
        self.assertTrue(len(lines) > 4)  # 原有4条skill语音 + 新添加的

        # 验证能查找到新添加的语音
        new_line = [l for l in lines if l["content"] == "新技能：剑气纵横！"]
        self.assertEqual(len(new_line), 1)
        self.assertEqual(new_line[0]["context"], "新角色技能释放")

    def test_add_new_story_chapter(self):
        """测试动态添加新剧情章节"""
        # 模拟版本更新：添加新剧情
        self.db.add_story_chapter(
            chapter_name="云璃的新冒险",
            summary="云璃在曜青仙舟遇到了新的挑战...",
            full_text="详细剧情内容...",
            characters=["云璃", "新角色", "曜青将军"],
            location="曜青仙舟",
            importance=5,
        )

        # 验证新剧情已添加
        stories = self.db.query_story("曜青", limit=5)
        self.assertTrue(len(stories) > 0)

        # 验证剧情内容正确
        new_story = [s for s in stories if s["chapter_name"] == "云璃的新冒险"]
        self.assertEqual(len(new_story), 1)
        self.assertEqual(new_story[0]["location"], "曜青仙舟")
        self.assertEqual(new_story[0]["importance"], 5)

    def test_add_new_dialogue(self):
        """测试动态添加新台词"""
        # 模拟活动限定台词添加
        self.db.add_dialogue(
            scene_type="event",
            content="这是活动限定的特殊台词！",
            mood="excited",
            weight=10,
        )

        # 验证新台词已添加
        dialogues = self.db.query_dialogues("event", limit=5)
        self.assertTrue(len(dialogues) > 0)
        self.assertEqual(dialogues[0]["content"], "这是活动限定的特殊台词！")
        self.assertEqual(dialogues[0]["mood"], "excited")

    def test_add_new_emotion_template(self):
        """测试动态添加新情感模板"""
        # 添加新情感模板
        self.db.add_emotion_template(
            emotion="surprised", template_type="prefix", content="什么？！", weight=5
        )

        # 验证新模板已添加
        templates = self.db.query_emotion_templates("surprised", limit=5)
        self.assertTrue(len(templates) > 0)
        self.assertEqual(templates[0]["content"], "什么？！")

    def test_dynamic_content_integration(self):
        """测试动态内容集成效果：模拟版本更新后查询"""
        # 1. 添加新角色
        self.db.add_knowledge(
            category="character",
            entity_name="新伙伴",
            description="来自曜青仙舟的新伙伴，擅长使用双剑。",
            related_entities=["云璃", "曜青"],
            importance=4,
        )

        # 2. 添加新剧情
        self.db.add_story_chapter(
            chapter_name="曜青之行",
            summary="云璃前往曜青仙舟，结识了新伙伴...",
            characters=["云璃", "新伙伴"],
            location="曜青仙舟",
            importance=5,
        )

        # 3. 添加新语音
        self.db.add_voice_line(
            line_type="greeting",
            content="曜青的风，和朱明不一样呢。",
            context="曜青场景问候",
            weight=4,
        )

        # 4. 验证集成查询效果
        # 查询新角色知识
        knowledge = self.db.query_knowledge("新伙伴", limit=3)
        self.assertTrue(len(knowledge) > 0)
        self.assertEqual(knowledge[0]["entity_name"], "新伙伴")

        # 查询新剧情
        stories = self.db.query_story("曜青之行", limit=3)
        self.assertTrue(len(stories) > 0)

        # 查询新语音
        voices = self.db.query_voice_lines("greeting", limit=10)
        new_voice = [v for v in voices if "曜青" in v["content"]]
        self.assertEqual(len(new_voice), 1)

        print(f"\n[动态内容测试] 版本更新后集成查询正常：")
        print(f"  - 新角色知识: {len(knowledge)}条")
        print(f"  - 新剧情: {len(stories)}条")
        print(f"  - 新语音: {len(new_voice)}条")


if __name__ == "__main__":
    unittest.main()
