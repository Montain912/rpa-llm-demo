"""查询专用知识检索、组合摘要与缓存版本测试。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


# test_input_method 会为隔离 rpa_agent 依赖临时注册同名模块桩；这里按源文件
# 独立加载待测模块，避免 unittest discovery 的模块导入顺序影响测试结果。
MODULE_PATH = Path(__file__).resolve().parents[1] / "knowledge_loader.py"
MODULE_SPEC = importlib.util.spec_from_file_location(
    "_knowledge_loader_under_test", MODULE_PATH
)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
knowledge_loader = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = knowledge_loader
MODULE_SPEC.loader.exec_module(knowledge_loader)


FULL_QUERY_TASK = (
    "打开浏览器登录http://172.19.133.168:7010/web/views/login.html，"
    "进入体验中心的智能体能力页面，选择智能问数与新SQL生成作为测试类型搜索，"
    "搜索完成后点击右上角退出登录"
)


class KnowledgeMatchingTests(unittest.TestCase):
    def test_phase_filter_keeps_business_and_restores_input_guidance(self):
        entries = [
            {"filePath": "input", "always": True, "promptPhases": ["input", "recovery"]},
            {"filePath": "business", "always": True},
        ]
        with patch.object(knowledge_loader, "load_index", return_value=entries), \
             patch.object(knowledge_loader, "match_entries_by_task", return_value=[]), \
             patch.object(knowledge_loader, "_get_entry_summary", side_effect=lambda entry: entry["filePath"]):
            self.assertEqual(knowledge_loader.get_summary("task", phase="business"), "business")
            self.assertIn("input", knowledge_loader.get_summary("task", phase="recovery"))
            self.assertIn("input", knowledge_loader.get_summary("task"))

    def tearDown(self):
        # 只清理进程内缓存；测试不得删除或改写仓库中的生成缓存文件。
        knowledge_loader._memory_cache.clear()

    def test_complete_query_task_matches_query_knowledge_not_add_scenario(self):
        matches = knowledge_loader.match_entries_by_task(FULL_QUERY_TASK)
        paths = [entry["filePath"] for entry in matches]

        self.assertIn("./browser_navigation.json", paths)
        self.assertIn("./experience_center_agent_capability.json", paths)
        self.assertNotIn("./添加助手场景.json", paths)
        self.assertEqual(
            knowledge_loader.match_by_task(FULL_QUERY_TASK)["filePath"],
            "./experience_center_agent_capability.json",
        )

    def test_name_is_never_reassembled_from_unrelated_single_char_hits(self):
        entries = [
            {
                "name": "login页面",
                "description": "登陆页面",
                "filePath": "./login.json",
                "parentPage": "",
            }
        ]
        task = "打开 http://demo/path 后由 zhouhao 查看 SQL 页面"

        with patch.object(knowledge_loader, "load_index", return_value=entries):
            self.assertIsNone(knowledge_loader.match_by_task(task))
            self.assertEqual(knowledge_loader.match_entries_by_task(task), [])

    def test_matching_child_prunes_its_also_matched_parent(self):
        entries = [
            {
                "name": "父页面",
                "description": "父页面入口",
                "filePath": "./parent.json",
                "parentPage": "",
            },
            {
                "name": "子查询",
                "description": "子页面查询",
                "filePath": "./child.json",
                "parentPage": "父页面",
                "keywords": ["目标筛选"],
            },
        ]

        with patch.object(knowledge_loader, "load_index", return_value=entries):
            matches = knowledge_loader.match_entries_by_task("在父页面执行目标筛选")

        self.assertEqual([entry["filePath"] for entry in matches], ["./child.json"])

    def test_summary_combines_always_browser_and_specific_query_without_writing_cache(self):
        cache_path = knowledge_loader.CACHE_FILE
        before = cache_path.read_bytes() if cache_path.exists() else None

        with patch.object(knowledge_loader, "_persist_cache"):
            summary = knowledge_loader.get_summary(FULL_QUERY_TASK)

        after = cache_path.read_bytes() if cache_path.exists() else None
        self.assertEqual(before, after)
        self.assertIn("【页面结构参考：通用文本输入与输入法操作】", summary)
        self.assertIn("【页面结构参考：浏览器导航与网址输入】", summary)
        self.assertIn("【操作指南：体验中心 - 智能体能力只读查询】", summary)
        self.assertIn(
            "http://172.19.133.168:7010/web/views/login.html",
            summary,
        )
        self.assertNotIn("main2.html", summary)
        self.assertIn("严禁执行的操作: 新增, 添加, 编辑, 删除, 保存, 发布", summary)
        self.assertIn("绿色顶部栏最右侧", summary)
        self.assertIn("开口门框与向右箭头", summary)
        self.assertIn("无可见文字", summary)
        self.assertIn("无需展开用户菜单", summary)

    def test_scene_name_search_uses_query_knowledge(self):
        task = "在智能体能力页面通过输入测试场景名称进行搜索，然后重置并退出登录"
        match = knowledge_loader.match_by_task(task)
        self.assertIsNotNone(match)
        self.assertEqual(match["filePath"], "./experience_center_agent_capability.json")

    def test_query_knowledge_declares_exact_login_url_and_query_only_scope(self):
        path = Path(__file__).resolve().parents[1] / "knowledge" / (
            "experience_center_agent_capability.json"
        )
        data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(
            data["pageLocation"]["loginUrl"],
            "http://172.19.133.168:7010/web/views/login.html",
        )
        self.assertEqual(data["scopeGuard"]["mode"], "query-only")
        self.assertEqual(
            data["fieldDetails"]["testType"]["options"],
            ["智能问数", "新SQL生成"],
        )
        logout = data["fieldDetails"]["logout"]
        self.assertEqual(logout["type"], "icon-button")
        self.assertIs(logout["hasVisibleText"], False)
        self.assertEqual(logout["container"], "绿色顶部栏")
        self.assertIn("周昊", logout["position"])
        self.assertIn("开口门框与向右箭头", logout["visualFeature"])
        self.assertEqual(logout["stableTargetName"], "退出登录图标")
        self.assertIs(logout["requiresUserMenu"], False)
        self.assertEqual(logout["clickCount"], 1)
        forbidden = set(data["scopeGuard"]["forbiddenOperations"])
        self.assertTrue({"新增", "编辑", "删除", "保存", "发布"} <= forbidden)

    def test_cache_record_requires_current_summarizer_version(self):
        legacy = {"mtime": 10.0, "summary": "旧摘要"}
        current = {
            "mtime": 10.0,
            "version": knowledge_loader.SUMMARIZER_VERSION,
            "summary": "新摘要",
        }

        self.assertFalse(knowledge_loader._cache_record_is_valid(legacy, 10.0))
        self.assertFalse(knowledge_loader._cache_record_is_valid(current, 11.0))
        self.assertTrue(knowledge_loader._cache_record_is_valid(current, 10.0))


if __name__ == "__main__":
    unittest.main()
