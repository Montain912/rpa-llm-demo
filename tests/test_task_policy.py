"""智能体能力只读查询策略的独立单元测试。"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from task_policy import AGENT_CAPABILITY_LOGIN_URL, QueryOnlyPolicy


QUERY_TASK = "在体验中心的智能体能力页面查询智能问数和新SQL生成"


class QueryOnlyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = QueryOnlyPolicy.for_task(QUERY_TASK)
        self.assertTrue(self.policy.active)

    def test_policy_and_knowledge_use_the_exact_login_url(self):
        knowledge_path = (
            Path(__file__).resolve().parents[1]
            / "knowledge"
            / "experience_center_agent_capability.json"
        )
        knowledge = json.loads(knowledge_path.read_text(encoding="utf-8"))

        self.assertEqual(
            AGENT_CAPABILITY_LOGIN_URL,
            "http://172.19.133.168:7010/web/views/login.html",
        )
        self.assertEqual(
            knowledge["pageLocation"]["loginUrl"],
            AGENT_CAPABILITY_LOGIN_URL,
        )
        allowed, reason = self.policy.validate_action(
            "skill_open_url", {"url": AGENT_CAPABILITY_LOGIN_URL}
        )
        self.assertTrue(allowed, reason)

    def test_policy_identifies_visible_test_type_popup_anchors(self):
        prompt = self.policy.prompt
        self.assertIn('skill_open_app(application="edge")', prompt)
        self.assertIn("Microsoft Edge 桌面图标", prompt)
        self.assertIn("Microsoft Edge 任务栏图标", prompt)
        self.assertIn("浏览器启动属于进入指定查询页前的准备动作", prompt)
        self.assertIn("文本结构化增强", prompt)
        self.assertIn("说明测试类型弹层已经正确打开", prompt)
        self.assertIn("不得重点击下拉框头部", prompt)
        self.assertIn("弹层内部向下滚动", prompt)

    def test_all_other_url_forms_are_rejected(self):
        other_urls = (
            "http://172.19.133.168:7010/web/views/main2.html",
            "http://172.19.133.168:7010/web/views/login.htm",
            "http://172.19.133.168:7010/web/views/login.html/",
            "https://172.19.133.168:7010/web/views/login.html",
            "http://172.19.133.168:7011/web/views/login.html",
            "http://172.19.133.168:7010/web/views/login.html?redirect=/admin",
        )

        for url in other_urls:
            with self.subTest(url=url):
                allowed, reason = self.policy.validate_action(
                    "skill_open_url", {"url": url}
                )
                self.assertFalse(allowed)
                self.assertIn(AGENT_CAPABILITY_LOGIN_URL, reason)

    def test_query_action_whitelist_allows_only_expected_interactions(self):
        allowed_actions = (
            ("skill_open_app", {"application": "Microsoft Edge"}),
            ("click", {"target": "Microsoft Edge 桌面图标"}),
            ("double_click", {"target": "Microsoft Edge 桌面图标"}),
            ("click", {"target": "Microsoft Edge 图标"}),
            ("click", {"target": "Microsoft Edge 任务栏图标"}),
            ("skill_open_webpage", {"stage": "username"}),
            ("click", {"target": "体验中心"}),
            ("click", {"target": "智能体能力"}),
            ("click", {"target": "智能问数选项"}),
            ("click", {"target": "新SQL生成选项"}),
            ("click", {"target": "搜索按钮"}),
            ("click", {"target": "退出登录"}),
            ("scroll", {"target": "测试类型下拉列表"}),
            ("press", {"key": "escape"}),
            ("verify_text_input", {"observed_text": "回归场景"}),
            ("wait", {"seconds": 1}),
        )

        for action, params in allowed_actions:
            with self.subTest(action=action, params=params):
                allowed, reason = self.policy.validate_action(action, params)
                self.assertTrue(allowed, reason)

        denied_actions = (
            ("skill_open_app", {"application": "notepad"}),
            ("skill_open_webpage", {"stage": "all-at-once"}),
            ("click", {}),
            ("scroll", {"target": "页面主体"}),
            ("press", {"key": "f5"}),
            ("discover_options", {"options": ["智能问数", "其他类型"]}),
            ("discover_options", {"options": ["智能问数", "新SQL生成"]}),
            ("double_click", {"target": "搜索按钮"}),
            ("double_click", {"target": "浏览器图标"}),
            ("right_click", {"target": "搜索按钮"}),
            ("drag", {"target": "测试类型"}),
            ("screenshot_and_click", {"target": "搜索按钮"}),
            ("click", {"target": "重置按钮"}),
            ("click", {"target": "测试场景名称输入框"}),
            ("input_text", {"field_name": "测试场景名称", "text": "回归场景"}),
            ("input_text", {"field_type": "password", "text": "伪装写入"}),
            ("input_text", {"field_type": "url", "text": "http://example.com/"}),
        )
        for action, params in denied_actions:
            with self.subTest(action=action, params=params):
                allowed, _ = self.policy.validate_action(action, params)
                self.assertFalse(allowed)

        for field_type in ("username", "password"):
            with self.subTest(controlled_login_field=field_type):
                allowed, reason = self.policy.validate_action(
                    "input_text",
                    {"field_type": field_type, "text": "value"},
                    internal_skill="skill_open_webpage",
                )
                self.assertTrue(allowed, reason)
        allowed, reason = self.policy.validate_action(
            "input_text",
            {"field_type": "url", "text": AGENT_CAPABILITY_LOGIN_URL},
            internal_skill="skill_open_url",
        )
        self.assertTrue(allowed, reason)

    def test_write_targets_and_write_plans_are_rejected(self):
        mutating_words = (
            "新增", "添加", "编辑", "删除", "保存", "发布", "启用", "停用", "导入", "导出"
        )

        for word in mutating_words:
            with self.subTest(kind="pointer", word=word):
                allowed, _ = self.policy.validate_action(
                    "click", {"target": f"{word}按钮"}
                )
                self.assertFalse(allowed)
            with self.subTest(kind="plan", word=word):
                allowed, _ = self.policy.validate_plan([
                    {"content": f"执行{word}操作", "expected_result": "操作完成"}
                ])
                self.assertFalse(allowed)

    def test_default_plan_is_fixed_query_sequence(self):
        first = self.policy.build_plan("查询智能问数和新SQL生成")
        second = self.policy.build_plan("查看两个指定测试类型的结果")

        self.assertEqual(first, second)
        self.assertEqual(
            [item["content"] for item in first],
            [
                "确认已从指定登录页登录，并进入体验中心的智能体能力页面",
                "在测试类型下拉列表内定位并选择智能问数，先核对其已选状态，不执行搜索",
                "重新打开测试类型下拉列表，必要时仅在弹层内部滚动，选择新SQL生成",
                "两个测试类型均验收后，只点击一次搜索并等待结果稳定",
                "点击绿色顶部栏最右侧的无文字退出登录图标，并验证重新出现指定登录页",
            ],
        )
        self.assertNotIn("用户菜单", first[-1]["content"])
        self.assertIn("退出登录图标", first[-1]["content"])
        self.assertTrue(all(item["completed"] is False for item in first))
        joined = " ".join(item["content"] for item in first)
        for word in ("新增", "添加", "编辑", "删除", "保存", "发布"):
            self.assertNotIn(word, joined)

    def test_scene_name_query_is_added_only_when_a_value_is_explicit(self):
        def has_scene_name_step(task: str) -> bool:
            return any(
                "按用户明确给出的测试场景名称" in item["content"]
                for item in self.policy.build_plan(task)
            )

        self.assertTrue(has_scene_name_step("测试场景名称为：订单查询回归01"))
        self.assertTrue(has_scene_name_step("测试场景名称=订单查询回归01"))
        self.assertFalse(has_scene_name_step("通过测试场景名称进行搜索"))
        self.assertFalse(has_scene_name_step("测试场景名称为"))
        self.assertFalse(has_scene_name_step("测试场景名称：   "))
        self.assertFalse(has_scene_name_step(
            "未提供测试场景名称：不要操作或输入场景名称，不要点击重置"
        ))
        denied = QueryOnlyPolicy.for_task(
            QUERY_TASK
            + "；未提供测试场景名称：不要操作或输入场景名称，不要点击重置"
        )
        self.assertEqual(denied.scene_name, "")
        self.assertEqual(len(denied.build_plan(QUERY_TASK)), 5)

    def test_optional_query_controls_require_explicit_user_authority(self):
        denied = QueryOnlyPolicy.for_task(
            QUERY_TASK + "；未提供测试场景名称，不要点击重置"
        )
        self.assertFalse(denied.allow_reset)
        self.assertEqual(denied.scene_name, "")
        self.assertEqual(denied.max_search_clicks, 1)
        for action, params in (
            ("click", {"target": "重置按钮"}),
            ("click", {"target": "测试场景名称输入框"}),
            ("input_text", {"field_name": "测试场景名称", "text": "臆造名称"}),
        ):
            with self.subTest(action=action):
                allowed, _ = denied.validate_action(action, params)
                self.assertFalse(allowed)

        allowed_policy = QueryOnlyPolicy.for_task(
            QUERY_TASK + "；测试场景名称为：订单回归01，并先点击重置"
        )
        self.assertTrue(allowed_policy.allow_reset)
        self.assertEqual(allowed_policy.scene_name, "订单回归01")
        self.assertEqual(allowed_policy.max_search_clicks, 2)
        for action, params in (
            ("click", {"target": "重置按钮"}),
            ("click", {"target": "测试场景名称输入框"}),
            ("input_text", {"field_name": "测试场景名称", "text": "订单回归01"}),
        ):
            with self.subTest(action=action):
                allowed, reason = allowed_policy.validate_action(action, params)
                self.assertTrue(allowed, reason)
        allowed, _ = allowed_policy.validate_action(
            "input_text", {"field_name": "测试场景名称", "text": "别的名称"}
        )
        self.assertFalse(allowed)

    def test_runtime_phase_gate_binds_search_and_logout_to_fixed_plan(self):
        plans = self.policy.build_plan(QUERY_TASK)

        allowed, _ = self.policy.validate_runtime_action(
            "click", {"target": "搜索按钮"}, task_plan=plans, current_plan_idx=0
        )
        self.assertFalse(allowed)
        allowed, _ = self.policy.validate_runtime_action(
            "click", {"target": "搜索按钮"}, task_plan=plans, current_plan_idx=1
        )
        self.assertFalse(allowed)  # “不执行搜索”不是搜索步骤
        for item in plans[:3]:
            item["completed"] = True
            item["passed"] = True
        allowed, reason = self.policy.validate_runtime_action(
            "click", {"target": "搜索按钮"}, task_plan=plans,
            current_plan_idx=3, search_clicks=0,
        )
        self.assertTrue(allowed, reason)
        allowed, _ = self.policy.validate_runtime_action(
            "click", {"target": "搜索按钮"}, task_plan=plans,
            current_plan_idx=3, search_clicks=1,
        )
        self.assertFalse(allowed)

    def test_filter_steps_allow_only_the_current_exact_option(self):
        plans = self.policy.build_plan(QUERY_TASK)

        for action, target in (
            ("click", "智能问数选项"),
            ("click", "请选择测试类型"),
            ("scroll", "测试类型下拉弹层"),
            ("click", "测试类型下拉弹层外空白区域"),
        ):
            with self.subTest(step="智能问数", action=action, target=target):
                allowed, reason = self.policy.validate_runtime_action(
                    action, {"target": target}, task_plan=plans, current_plan_idx=1
                )
                self.assertTrue(allowed, reason)

        for target in (
            "新SQL生成选项",
            "测试类型选项：摘要提取",
            "测试类型下拉列表内的问答提取",
        ):
            with self.subTest(step="智能问数", denied_target=target):
                allowed, _ = self.policy.validate_runtime_action(
                    "click", {"target": target}, task_plan=plans, current_plan_idx=1
                )
                self.assertFalse(allowed)

        allowed, reason = self.policy.validate_runtime_action(
            "click", {"target": "新SQL生成选项"},
            task_plan=plans, current_plan_idx=2,
        )
        self.assertTrue(allowed, reason)
        allowed, _ = self.policy.validate_runtime_action(
            "click", {"target": "智能问数选项"},
            task_plan=plans, current_plan_idx=2,
        )
        self.assertFalse(allowed)

    def test_search_freezes_business_actions_until_acceptance(self):
        plans = self.policy.build_plan(QUERY_TASK)
        for item in plans[:3]:
            item["completed"] = True
            item["passed"] = True

        for action, params in (
            ("wait", {"seconds": 1}),
            ("subtask_done", {"passed": True}),
        ):
            with self.subTest(allowed_action=action):
                allowed, reason = self.policy.validate_runtime_action(
                    action, params, task_plan=plans,
                    current_plan_idx=3, search_clicks=1,
                )
                self.assertTrue(allowed, reason)

        for action, params in (
            ("click", {"target": "请选择测试类型"}),
            ("click", {"target": "智能问数选项"}),
            ("press", {"key": "escape"}),
            ("maximize_window", {}),
        ):
            with self.subTest(blocked_action=action, params=params):
                allowed, reason = self.policy.validate_runtime_action(
                    action, params, task_plan=plans,
                    current_plan_idx=3, search_clicks=1,
                )
                self.assertFalse(allowed)
                self.assertIn("搜索已执行", reason)

    def test_generic_close_and_blank_targets_are_never_allowed(self):
        plans = self.policy.build_plan(QUERY_TASK)
        for target in ("关闭按钮", "页面空白区域", "弹窗右上角关闭图标"):
            with self.subTest(target=target):
                allowed, _ = self.policy.validate_action("click", {"target": target})
                self.assertFalse(allowed)
                allowed, _ = self.policy.validate_runtime_action(
                    "click", {"target": target},
                    task_plan=plans, current_plan_idx=1,
                )
                self.assertFalse(allowed)

        allowed, _ = self.policy.validate_runtime_action(
            "click", {"target": "用户头像"}, task_plan=plans,
            current_plan_idx=4, search_clicks=1,
        )
        self.assertFalse(allowed)
        for item in plans[:4]:
            item["completed"] = True
            item["passed"] = True
        allowed, reason = self.policy.validate_runtime_action(
            "click", {"target": "用户头像"}, task_plan=plans,
            current_plan_idx=4, search_clicks=1,
        )
        self.assertFalse(allowed)
        self.assertIn("无需展开用户菜单", reason)
        allowed, reason = self.policy.validate_runtime_action(
            "click", {"target": "退出登录图标"}, task_plan=plans,
            current_plan_idx=4, search_clicks=1,
        )
        self.assertTrue(allowed, reason)

        allowed, _ = self.policy.validate_runtime_action(
            "discover_options", {"options": ["智能问数", "新SQL生成"]},
            task_plan=plans, current_plan_idx=1,
        )
        self.assertTrue(allowed)  # static validate_action is the fail-closed layer
        allowed, _ = self.policy.validate_action(
            "discover_options", {"options": ["智能问数", "新SQL生成"]}
        )
        self.assertFalse(allowed)

    def test_do_not_search_filter_step_never_authorizes_search(self):
        plans = self.policy.build_plan(QUERY_TASK)
        plans[0]["completed"] = True
        plans[0]["passed"] = True

        allowed, reason = self.policy.validate_runtime_action(
            "click", {"target": "搜索按钮"}, task_plan=plans,
            current_plan_idx=1, search_clicks=0,
        )

        self.assertFalse(allowed)
        self.assertIn("搜索步骤", reason)

    def test_each_filter_step_allows_only_its_exact_option_and_popup(self):
        plans = self.policy.build_plan(QUERY_TASK)
        for target, expected in (
            ("请选择测试类型", True),
            ("智能问数选项", True),
            ("新SQL生成选项", False),
            ("测试类型选项：摘要提取", False),
        ):
            with self.subTest(step="智能问数", target=target):
                allowed, _ = self.policy.validate_runtime_action(
                    "click", {"target": target}, task_plan=plans,
                    current_plan_idx=1,
                )
                self.assertEqual(allowed, expected)
        allowed, reason = self.policy.validate_runtime_action(
            "scroll", {"target": "测试类型下拉弹层内容区"},
            task_plan=plans, current_plan_idx=1,
        )
        self.assertTrue(allowed, reason)

        for target, expected in (
            ("请选择测试类型", True),
            ("智能问数选项", False),
            ("新SQL生成选项", True),
            ("测试类型选项：追问", False),
        ):
            with self.subTest(step="新SQL生成", target=target):
                allowed, _ = self.policy.validate_runtime_action(
                    "click", {"target": target}, task_plan=plans,
                    current_plan_idx=2,
                )
                self.assertEqual(allowed, expected)

    def test_search_freezes_business_controls_until_acceptance(self):
        plans = self.policy.build_plan(QUERY_TASK)
        for item in plans[:3]:
            item["completed"] = True
            item["passed"] = True

        for action, params, expected in (
            ("wait", {"seconds": 1}, True),
            ("subtask_done", {"passed": True}, True),
            ("click", {"target": "搜索按钮"}, False),
            ("click", {"target": "请选择测试类型"}, False),
            ("press", {"key": "escape"}, False),
            ("maximize_window", {}, False),
        ):
            with self.subTest(action=action, params=params):
                allowed, _ = self.policy.validate_runtime_action(
                    action, params, task_plan=plans,
                    current_plan_idx=3, search_clicks=1,
                )
                self.assertEqual(allowed, expected)

    def test_generic_blank_or_close_targets_are_not_runtime_escape_hatches(self):
        plans = self.policy.build_plan(QUERY_TASK)
        for target in ("空白区域", "关闭按钮", "页面列表"):
            with self.subTest(target=target):
                allowed, _ = self.policy.validate_action(
                    "click", {"target": target}
                )
                self.assertFalse(allowed)
        allowed, reason = self.policy.validate_runtime_action(
            "click", {"target": "测试类型弹层关闭按钮"},
            task_plan=plans, current_plan_idx=1,
        )
        self.assertTrue(allowed, reason)

    def test_runtime_phase_gate_blocks_keyboard_focus_bypass(self):
        plans = self.policy.build_plan(QUERY_TASK)
        for key in ("enter", "tab", "shift+tab", "down"):
            with self.subTest(key=key):
                allowed, _ = self.policy.validate_runtime_action(
                    "press", {"key": key}, task_plan=plans, current_plan_idx=3
                )
                self.assertFalse(allowed)

        allowed, reason = self.policy.validate_runtime_action(
            "press", {"key": "escape"}, task_plan=plans, current_plan_idx=1
        )
        self.assertTrue(allowed, reason)
        allowed, reason = self.policy.validate_runtime_action(
            "press", {"key": "enter"}, pending_input={
                "verified": True, "field_type": "url",
            }
        )
        self.assertTrue(allowed, reason)

    def test_only_browser_credential_prompt_close_is_allowed(self):
        for target in (
            "保存密码弹窗关闭按钮",
            "保存的信息提示右上角关闭按钮",
        ):
            with self.subTest(target=target):
                allowed, reason = self.policy.validate_action(
                    "click", {"target": target}
                )
                self.assertTrue(allowed, reason)
                allowed, reason = self.policy.validate_runtime_action(
                    "click", {"target": target}
                )
                self.assertTrue(allowed, reason)

        for target in ("保存密码按钮", "保存密码弹窗确认按钮", "业务保存按钮"):
            with self.subTest(target=target):
                allowed, _ = self.policy.validate_action("click", {"target": target})
                self.assertFalse(allowed)

        plans = self.policy.build_plan(QUERY_TASK)
        allowed, _ = self.policy.validate_runtime_action(
            "click", {"target": "保存密码弹窗关闭按钮"},
            task_plan=plans, current_plan_idx=0,
        )
        self.assertFalse(allowed)

    def test_targets_for_other_business_pages_are_rejected(self):
        targets = (
            "工作流AGENT",
            "模型管理",
            "数据中心",
            "体验中心 → 工作流AGENT",
            "模型管理页面的搜索按钮",
        )

        for target in targets:
            with self.subTest(target=target):
                allowed, reason = self.policy.validate_action(
                    "click", {"target": target}
                )
                self.assertFalse(allowed, reason)

    def test_browser_best_match_requires_exact_internal_one_shot_capability(self):
        exact = {"target": "Microsoft Edge 最佳匹配"}
        allowed, _ = self.policy.validate_action("click", exact)
        self.assertFalse(allowed)

        allowed, reason = self.policy.validate_action(
            "click", exact, internal_skill="skill_open_app_recovery"
        )
        self.assertTrue(allowed, reason)
        allowed, reason = self.policy.validate_runtime_action(
            "click", exact, internal_skill="skill_open_app_recovery"
        )
        self.assertTrue(allowed, reason)

        for action, target in (
            ("move", "Microsoft Edge 最佳匹配"),
            ("scroll", "Microsoft Edge 最佳匹配"),
            ("double_click", "Microsoft Edge 最佳匹配"),
            ("click", "最佳匹配"),
            ("click", "Microsoft Edge 应用"),
            ("click", "Microsoft Edge 任务栏图标"),
            ("click", "Microsoft Edge 最佳匹配中的打开文件位置"),
        ):
            with self.subTest(action=action, target=target):
                allowed, _ = self.policy.validate_action(
                    action,
                    {"target": target},
                    internal_skill="skill_open_app_recovery",
                )
                self.assertFalse(allowed)

        allowed, _ = self.policy.validate_runtime_action(
            "move",
            exact,
            internal_skill="skill_open_app_recovery",
        )
        self.assertFalse(allowed)
        allowed, _ = self.policy.validate_runtime_action(
            "click",
            {"target": "Microsoft Edge 任务栏图标"},
            internal_skill="skill_open_app_recovery",
        )
        self.assertFalse(allowed)

        plans = self.policy.build_plan(QUERY_TASK)
        allowed, _ = self.policy.validate_runtime_action(
            "click",
            exact,
            internal_skill="skill_open_app_recovery",
            task_plan=plans,
            current_plan_idx=0,
        )
        self.assertFalse(allowed)


if __name__ == "__main__":
    unittest.main()
