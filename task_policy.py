"""Task-scoped safety policies for RPA execution.

The visual model is useful for locating controls, but it must not be allowed to
silently broaden a read-only query task into CRUD or other feature testing.
This module keeps the policy deterministic and independently testable.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlsplit, urlunsplit


AGENT_CAPABILITY_LOGIN_URL = (
    "http://172.19.133.168:7010/web/views/login.html"
)

_QUERY_TASK_MARKERS = (
    "智能体能力",
    "智能问数",
    "新sql生成",
)

_MUTATING_WORDS = (
    "新增", "新建", "创建", "添加", "编辑", "修改", "删除", "移除",
    "保存", "发布", "复制", "导入", "导出", "上传", "下载", "启用", "停用", "配置",
    "create", "add", "edit", "delete", "remove", "save", "publish",
    "upload", "enable", "disable",
)

_ALLOWED_POINTER_TARGETS = (
    "重新登录", "登录", "用户名", "密码",
    "体验中心", "智能体能力",
    "测试类型", "智能问数", "新sql生成",
    "测试场景名称", "场景名称", "搜索", "查询", "重置",
    "用户", "头像", "退出", "注销", "确认退出", "退出确认",
)

_TEST_TYPE_HEADER_TARGETS = {
    "测试类型", "测试类型下拉", "测试类型下拉框", "测试类型控件",
    "测试类型选择框", "测试类型筛选框", "请选择测试类型",
}

_TEST_TYPE_POPUP_TARGETS = {
    "测试类型弹层", "测试类型下拉弹层", "测试类型列表", "测试类型下拉列表",
    "测试类型弹层内容区", "测试类型下拉弹层内容区",
    "测试类型列表内容区", "测试类型下拉列表内容区",
    "测试类型弹层滚动区", "测试类型下拉弹层滚动区",
}

_TEST_TYPE_POPUP_CLOSE_TARGETS = {
    "关闭测试类型弹层", "关闭测试类型下拉弹层",
    "测试类型弹层关闭按钮", "测试类型下拉弹层关闭按钮",
    "测试类型弹层外空白区域", "测试类型下拉弹层外空白区域",
}

_SCENE_NAME_PATTERN = re.compile(
    r"测试场景名称\s*(?:(?:为|是)\s*(?:=|：|:)?|(?:=|：|:))"
    r"\s*([^\s,，;；]+)"
)

_OTHER_PAGE_WORDS = (
    "工作流agent", "工作流智能体", "模型管理", "系统管理", "用户管理",
    "权限管理", "知识库", "助手场景", "应用管理", "数据源", "运营中心",
)

_ALLOWED_QUERY_KEYS = {
    "enter", "return", "escape", "esc", "tab", "shift+tab",
    "up", "down", "left", "right", "home", "end", "pageup", "pgup",
    "pagedown", "pgdn", "ctrl+l", "ctrl+a", "cmd+l", "cmd+a",
}

_BROWSER_LAUNCH_RESULT_TARGETS = {
    "microsoftedge最佳匹配",
    "microsoftedge最佳匹配项",
    "microsoftedge应用最佳匹配",
    "microsoftedge最佳匹配应用",
    "googlechrome最佳匹配",
    "googlechrome最佳匹配项",
    "googlechrome应用最佳匹配",
    "googlechrome最佳匹配应用",
}

_BROWSER_ICON_TARGETS = {
    "edge图标",
    "microsoftedge图标",
    "edge桌面图标",
    "microsoftedge桌面图标",
    "桌面edge图标",
    "桌面microsoftedge图标",
    "edge任务栏图标",
    "microsoftedge任务栏图标",
    "任务栏edge图标",
    "任务栏microsoftedge图标",
    "chrome图标",
    "googlechrome图标",
    "chrome桌面图标",
    "googlechrome桌面图标",
    "桌面chrome图标",
    "桌面googlechrome图标",
    "chrome任务栏图标",
    "googlechrome任务栏图标",
    "任务栏chrome图标",
    "任务栏googlechrome图标",
}


def _plain(value) -> str:
    return str(value or "").strip().lower()


def _compact_target(value) -> str:
    """Normalize a declared visible target for exact query-control matching."""
    return re.sub(r"[\s:：,，;；_\-/\\>→（）()\[\]【】'\"]+", "", _plain(value))


def _is_exact_browser_launch_result(target: str) -> bool:
    """Match only the visible Start-menu result authorized by a code-side token."""
    return _compact_target(target) in _BROWSER_LAUNCH_RESULT_TARGETS


def _is_exact_browser_icon(target: str) -> bool:
    """Allow only an explicitly named Edge/Chrome launch icon."""
    return _compact_target(target) in _BROWSER_ICON_TARGETS


def _is_exact_test_type_option(target: str, option: str) -> bool:
    candidate = _compact_target(target)
    option = _compact_target(option)
    prefixes = ("", "测试类型", "测试类型选项", "测试类型下拉选项")
    suffixes = ("", "选项", "复选框", "勾选框")
    return candidate in {
        f"{prefix}{option}{suffix}"
        for prefix in prefixes
        for suffix in suffixes
    }


def _is_test_type_header(target: str) -> bool:
    return _compact_target(target) in _TEST_TYPE_HEADER_TARGETS


def _is_test_type_popup(target: str) -> bool:
    return _compact_target(target) in _TEST_TYPE_POPUP_TARGETS


def _is_test_type_popup_close(target: str) -> bool:
    return _compact_target(target) in _TEST_TYPE_POPUP_CLOSE_TARGETS


def _canonical_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    path = parsed.path or "/"
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, parsed.fragment)
    )


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    normalized = _plain(text)
    return any(word in normalized for word in words)


def _is_browser_credential_prompt_dismiss(target: str) -> bool:
    """Recognize only the close control of browser credential/autofill UI."""
    normalized = _plain(target)
    has_credential_prompt = _contains_any(
        normalized,
        (
            "保存密码弹窗", "保存密码提示", "密码保存弹窗", "密码保存提示",
            "保存的信息弹窗", "保存的信息提示",
        ),
    )
    return has_credential_prompt and _contains_any(
        normalized, ("关闭按钮", "关闭图标", "关闭叉号", "关闭", "dismiss")
    )


def _extract_scene_name(task: str) -> str:
    text = str(task or "")
    for match in _SCENE_NAME_PATTERN.finditer(text):
        # A prohibition such as ``未提供测试场景名称：不要操作`` contains the
        # same punctuation as a positive assignment.  Bind authority to this
        # particular mention instead of treating every colon as a value.
        prefix = text[max(0, match.start() - 20):match.start()]
        if re.search(
            r"(?:未|没有|无|不)(?:明确)?(?:提供|给出|指定|填写|使用|操作)?\s*$",
            prefix,
        ):
            continue
        value = match.group(1).strip()
        if re.match(
            r"^(?:不要|不得|禁止|无需|不需要|不可|未提供|未给出|未指定|"
            r"没有|无|为空|空值|不操作)",
            value,
        ):
            continue
        return value
    return ""


def _explicitly_requests_reset(task: str) -> bool:
    """Only grant reset when the task positively asks for it.

    Merely mentioning reset in a prohibition (for example ``不要点击重置``)
    must not widen the executable action set.
    """
    normalized = _plain(task)
    if not normalized or re.search(
        r"(?:不要|不得|禁止|无需|不需要|未要求|不可).{0,8}重置",
        normalized,
    ):
        return False
    return bool(re.search(r"(?:点击|执行|使用|需要|先)重置|重置筛选", normalized))


@dataclass(frozen=True)
class QueryOnlyPolicy:
    """Policy for the Experience Center / Agent Capability query scenario."""

    active: bool
    login_url: str = AGENT_CAPABILITY_LOGIN_URL
    scene_name: str = ""
    allow_reset: bool = False

    @classmethod
    def for_task(cls, task: str) -> "QueryOnlyPolicy":
        normalized = _plain(task)
        active = any(marker in normalized for marker in _QUERY_TASK_MARKERS)
        return cls(
            active=active,
            scene_name=_extract_scene_name(task),
            allow_reset=_explicitly_requests_reset(task),
        )

    @property
    def max_search_clicks(self) -> int:
        """Bound searches to the deterministic plan, rather than model intent."""
        return 1 + int(bool(self.scene_name))

    @property
    def prompt(self) -> str:
        if not self.active:
            return ""
        return f"""
【强制只读查询范围】
- 只能从 {self.login_url} 登录。
- 浏览器未打开时可使用 skill_open_app(application="edge")；也可在独立落点验收确认后，双击明确的 Microsoft Edge 桌面图标，或单击明确的 Microsoft Edge 任务栏图标。浏览器启动属于进入指定查询页前的准备动作，不受业务控件白名单限制；名称不明的图标仍禁止操作。
- 登录后只能进入“体验中心 → 智能体能力”页面。
- 只允许选择“测试类型”、点击计划内的搜索/查询，以及最后退出登录。
- 当前页面使用绿色顶部栏最右侧、用户姓名“周昊”右边的无文字白色门框/向右箭头图标直接退出；不要反复点击用户姓名或等待下拉菜单。
- “测试类型”弹层上部已知会显示“文本结构化增强、图像文字提取、图文理解、关键词提取、问答提取、摘要提取、追问”等选项；看到这些文字说明测试类型弹层已经正确打开，并非渠道弹层。此时不得重点击下拉框头部，应只在该弹层内部向下滚动寻找“智能问数”和“新SQL生成”。
- 若浏览器自身出现保存密码提示，只允许关闭该提示或按 Escape；不得点击提示中的保存/确认选项。
- 只有用户明确给出测试场景名称时才允许输入该名称；只有用户明确正向要求重置时才允许点击重置。
- 严禁测试或触发新增、新建、创建、添加、编辑、修改、删除、保存、发布、导入、上传、启用、停用等写操作。
- 原始 click/move/scroll 必须在 params.target 中填写截图上实际可见的目标名称；系统会在发送输入事件前校验。
- 不得进入其他业务页面。若目标查询无法完成，应明确失败，不得改做其他功能测试。
""".strip()

    def validate_action(
        self,
        action: str,
        params: dict | None,
        *,
        internal_skill: str | None = None,
    ) -> tuple[bool, str]:
        """Return whether an action stays inside the query-only boundary."""
        if not self.active:
            return True, ""

        action = _plain(action)
        params = params if isinstance(params, dict) else {}
        internal_skill = _plain(internal_skill)

        if action == "skill_open_url":
            actual = _canonical_url(params.get("url", ""))
            expected = _canonical_url(self.login_url)
            if actual != expected:
                return False, f"查询任务只能访问指定登录页 {self.login_url}"
            return True, ""

        if action == "skill_open_app":
            application = _plain(params.get("application"))
            if application not in {
                "edge", "msedge", "microsoft edge", "chrome", "google chrome"
            }:
                return False, "查询任务只允许打开浏览器"
            return True, ""

        if action == "skill_open_webpage":
            stage = _plain(params.get("stage", "username"))
            if stage not in {"username", "password", "submit"}:
                return False, "登录技能只允许 username/password/submit 阶段"
            return True, ""

        if (
            not internal_skill
            and action in {"click", "double_click"}
            and _is_exact_browser_icon(params.get("target", ""))
        ):
            return True, ""

        if internal_skill == "skill_open_app_recovery":
            if action == "click" and _is_exact_browser_launch_result(
                params.get("target", "")
            ):
                return True, ""
            return False, "应用启动恢复只允许点击一次精确的浏览器最佳匹配结果"

        if action in {"double_click", "right_click", "drag"}:
            return False, f"查询任务不需要 {action}，已阻止可能的额外操作"

        if action in {"click", "move", "scroll"}:
            # Login macro coordinates are generated by the approved staged skill.
            if internal_skill == "skill_open_webpage":
                return True, ""
            target = _plain(params.get("target"))
            if not target:
                return False, "查询任务的指针动作必须声明 params.target"
            if action == "click" and _is_browser_credential_prompt_dismiss(target):
                return True, ""
            if _contains_any(target, ("重置",)) and not self.allow_reset:
                return False, "用户未要求重置筛选，已阻止额外查询操作"
            if _contains_any(target, ("测试场景名称", "场景名称")) and not self.scene_name:
                return False, "用户未提供测试场景名称，已阻止操作该字段"
            if _contains_any(target, _MUTATING_WORDS):
                return False, f"目标 {target!r} 属于写操作，已阻止"
            if _contains_any(target, _OTHER_PAGE_WORDS):
                return False, f"目标 {target!r} 属于其他业务页面，已阻止"
            if not _contains_any(target, _ALLOWED_POINTER_TARGETS):
                return False, f"目标 {target!r} 不在智能体能力查询白名单内"
            if action == "scroll" and not _contains_any(
                target, ("测试类型", "下拉", "弹层", "列表")
            ):
                return False, "查询任务只允许在测试类型下拉列表内滚动"
            return True, ""

        if action == "press":
            if internal_skill in {"skill_open_app", "skill_open_url"}:
                return True, ""
            key = _plain(params.get("key"))
            if key not in _ALLOWED_QUERY_KEYS:
                return False, f"按键 {key!r} 不在查询流程白名单内"
            return True, ""

        if action in {"type", "input_text", "retry_text_input", "skill_input_text"}:
            field_type = _plain(params.get("field_type", "auto"))
            field_name = _plain(params.get("field_name") or params.get("target"))
            if field_type == "url":
                if _canonical_url(params.get("text", "")) != _canonical_url(self.login_url):
                    return False, f"地址栏只能输入指定登录页 {self.login_url}"
                return True, ""
            if field_type in {"username", "password"}:
                if internal_skill != "skill_open_webpage":
                    return False, "登录凭据只能由受控分阶段登录技能输入"
                return True, ""
            if internal_skill in {"skill_open_app", "skill_open_url", "skill_open_webpage"}:
                return True, ""
            is_scene_field = field_type in {"search", "query"} or _contains_any(
                field_name, ("测试场景名称", "场景名称")
            )
            if is_scene_field:
                if not self.scene_name:
                    return False, "用户未提供测试场景名称，禁止输入额外查询条件"
                if str(params.get("text", "")) != self.scene_name:
                    return False, "测试场景名称输入值与用户明确给出的值不一致"
                return True, ""
            return False, "查询任务只允许向登录、地址栏或查询条件字段输入"

        if action == "discover_options":
            return False, "查询任务使用不可变固定计划，禁止动态替换剩余步骤"

        # Control, verification and IME actions do not broaden page scope.
        if action in {
            "wait", "planning", "subtask_done", "done", "abort_input",
            "verify_text_input", "ime_operation", "skill_input_method", "skill_ime",
            "maximize_window",
        }:
            return True, ""

        return False, f"动作 {action!r} 不在智能体能力查询策略内"

    def validate_runtime_action(
        self,
        action: str,
        params: dict | None,
        *,
        internal_skill: str | None = None,
        task_plan: list[dict] | None = None,
        current_plan_idx: int = -1,
        search_clicks: int = 0,
        pending_input: dict | None = None,
    ) -> tuple[bool, str]:
        """Enforce where an otherwise-safe action may occur in the fixed plan."""
        if not self.active:
            return True, ""

        action = _plain(action)
        params = params if isinstance(params, dict) else {}
        internal_skill = _plain(internal_skill)
        plans = task_plan if isinstance(task_plan, list) else []
        current = (
            plans[current_plan_idx]
            if 0 <= current_plan_idx < len(plans)
            else None
        )
        current_text = " ".join(
            str(current.get(key, "")) for key in ("content", "expected_result")
        ) if isinstance(current, dict) else ""
        current_content = str(current.get("content", "")) if isinstance(current, dict) else ""
        current_is_search = self._is_search_plan_item(current)
        searches_through_current = sum(
            1
            for item in plans[:current_plan_idx + 1]
            if self._is_search_plan_item(item)
        )

        # Once this plan step's Search click has been sent, freeze all business
        # controls. Only passive waiting and the code-owned acceptance gate may
        # follow, keeping the accepted result bound to that query.
        if (
            current_is_search
            and searches_through_current > 0
            and search_clicks >= searches_through_current
            and action not in {"wait", "subtask_done"}
        ):
            return False, "本次搜索已执行；验收前只允许等待或提交当前搜索步骤验收"

        # Login/application macros are phase-1 operations. Their recursively
        # expanded clicks and keystrokes are already constrained by validate_action.
        if internal_skill == "skill_open_app_recovery":
            if plans:
                return False, "已进入查询计划，应用启动恢复权限已经失效"
            if action != "click" or not _is_exact_browser_launch_result(
                params.get("target", "")
            ):
                return False, "应用启动恢复只允许精确点击浏览器最佳匹配结果"
            return True, ""

        if internal_skill in {"skill_open_app", "skill_open_url", "skill_open_webpage"}:
            if plans:
                return False, "已进入查询计划，禁止重新执行登录或地址栏宏"
            return True, ""

        if action in {"skill_open_app", "skill_open_url", "skill_open_webpage"}:
            if plans:
                return False, "已进入查询计划，禁止重新打开应用、网址或登录"
            return True, ""

        if action in {"click", "double_click"} and _is_exact_browser_icon(
            params.get("target", "")
        ):
            if plans:
                return False, "进入查询计划后不再允许操作桌面浏览器图标"
            return True, ""

        if action == "press":
            key = _plain(params.get("key"))
            pending = pending_input if isinstance(pending_input, dict) else {}
            launch_or_navigate = bool(
                key in {"enter", "return"}
                and pending.get("verified") is True
                and _plain(pending.get("field_type")) in {"url", "app_search"}
                and not plans
            )
            if launch_or_navigate or key in {"escape", "esc"}:
                return True, ""
            return False, "查询模式禁止用通用键盘导航激活未知焦点控件"

        if action in {"type", "input_text", "retry_text_input", "skill_input_text"}:
            field_name = _plain(params.get("field_name") or params.get("target"))
            field_type = _plain(params.get("field_type", "auto"))
            is_scene_input = field_type in {"search", "query"} or _contains_any(
                field_name, ("测试场景名称", "场景名称")
            )
            if is_scene_input and "测试场景名称" not in current_text:
                return False, "当前固定计划步骤不是测试场景名称查询"
            return True, ""

        if action not in {"click", "move", "scroll"}:
            return True, ""

        target = _plain(params.get("target"))
        if _is_browser_credential_prompt_dismiss(target):
            if plans:
                return False, "浏览器密码提示只能在生成查询计划之前关闭"
            return True, ""
        if (
            _contains_any(target, ("登录", "用户名", "密码", "重新登录"))
            and not _contains_any(target, ("退出登录", "注销"))
        ):
            if plans:
                return False, "登录控件只能在生成查询计划之前操作"
            return True, ""

        if _contains_any(target, ("搜索", "查询")):
            if current is None or not current_is_search:
                return False, "搜索只能在固定计划的搜索步骤执行"
            if not all(
                item.get("completed") and item.get("passed") is True
                for item in plans[:current_plan_idx]
            ):
                return False, "前置筛选步骤尚未全部通过，禁止搜索"
            expected_prior_searches = sum(
                1
                for item in plans[:current_plan_idx]
                if self._is_search_plan_item(item)
            )
            if search_clicks != expected_prior_searches:
                return False, "当前搜索步骤的已执行次数与固定计划不一致"
            if search_clicks >= self.max_search_clicks:
                return False, f"搜索最多执行 {self.max_search_clicks} 次"
            return True, ""

        if _contains_any(target, ("退出", "注销")):
            is_logout_step = current is not None and "退出登录" in current_text
            prior_passed = bool(plans) and all(
                item.get("completed") and item.get("passed") is True
                for item in plans[:current_plan_idx]
            )
            if not is_logout_step or not prior_passed:
                return False, "只有全部查询步骤通过后才能操作用户菜单并退出"
            if search_clicks != self.max_search_clicks:
                return False, "计划内搜索尚未按要求执行，禁止提前退出"
            return True, ""

        if _contains_any(target, ("用户", "头像", "周昊")):
            return False, (
                "当前页面无需展开用户菜单；请只定位绿色顶部栏最右侧、周昊右边的"
                "无文字白色门框/向右箭头，并声明 target='退出登录图标'"
            )

        if _contains_any(target, ("体验中心", "智能体能力")):
            if not plans:
                return False, "尚未生成固定查询计划；请先调用 planning 并通过登录后页面验收"
            if current is None or "进入体验中心" not in current_text:
                return False, "业务导航只能在固定计划的智能体能力页面步骤执行"
            return True, ""

        if _contains_any(
            target,
            ("测试类型", "智能问数", "新sql生成", "下拉", "弹层", "列表"),
        ):
            expected_option = ""
            if "选择智能问数" in current_content:
                expected_option = "智能问数"
            elif "选择新sql生成" in _plain(current_content):
                expected_option = "新sql生成"
            if not expected_option:
                return False, "测试类型控件只能在固定计划的筛选步骤执行"
            exact_option = _is_exact_test_type_option(target, expected_option)
            if action == "scroll":
                if _is_test_type_popup(target):
                    return True, ""
                return False, "筛选步骤只允许在明确的测试类型弹层内容区滚动"
            if action == "click":
                if (
                    exact_option
                    or _is_test_type_header(target)
                    or _is_test_type_popup_close(target)
                ):
                    return True, ""
                return False, f"当前筛选步骤只允许选择 {expected_option}，禁止点击其他具体选项"
            if action == "move" and (
                exact_option
                or _is_test_type_header(target)
                or _is_test_type_popup(target)
            ):
                return True, ""
            return False, "当前筛选步骤的指针目标不在精确测试类型白名单内"

        if _contains_any(target, ("测试场景名称", "场景名称", "重置")):
            if current is None or "测试场景名称" not in current_text:
                return False, "可选查询控件不属于当前固定计划步骤"
            return True, ""
        return False, f"目标 {target!r} 不属于当前固定计划步骤"

    @staticmethod
    def _is_search_plan_item(item: dict | None) -> bool:
        if not isinstance(item, dict):
            return False
        content = str(item.get("content", "") or "")
        return "搜索" in content and "不执行搜索" not in content

    def validate_plan(self, plan: list[dict]) -> tuple[bool, str]:
        if not self.active:
            return True, ""
        if not plan:
            return False, "查询计划为空"
        for index, item in enumerate(plan, 1):
            text = " ".join(
                str(item.get(key, "")) for key in ("content", "expected_result")
            )
            if _contains_any(text, _MUTATING_WORDS):
                return False, f"第 {index} 个子任务包含写操作"
        return True, ""

    def build_plan(self, task: str) -> list[dict]:
        """Build a deterministic plan so 'test' cannot expand into CRUD testing."""
        if not self.active:
            return []
        plan = [
            {
                "content": "确认已从指定登录页登录，并进入体验中心的智能体能力页面",
                "expected_result": "页面标题或导航锚点明确显示体验中心和智能体能力",
                "completed": False,
            },
            {
                "content": "在测试类型下拉列表内定位并选择智能问数，先核对其已选状态，不执行搜索",
                "expected_result": "测试类型控件明确保留智能问数的勾选或已选标签",
                "completed": False,
            },
            {
                "content": "重新打开测试类型下拉列表，必要时仅在弹层内部滚动，选择新SQL生成",
                "expected_result": "测试类型控件同时保留智能问数和新SQL生成两个已选值",
                "completed": False,
            },
            {
                "content": "两个测试类型均验收后，只点击一次搜索并等待结果稳定",
                "expected_result": "两个筛选值仍保留，且结果表格、分页或明确空结果状态完成加载",
                "completed": False,
            },
        ]
        if _extract_scene_name(task):
            plan.append({
                "content": "按用户明确给出的测试场景名称填写查询条件并搜索",
                "expected_result": "输入值与用户给定名称逐字符一致，并出现查询结果或明确空结果状态",
                "completed": False,
            })
        plan.append({
            "content": "点击绿色顶部栏最右侧的无文字退出登录图标，并验证重新出现指定登录页",
            "expected_result": "页面重新显示登录表单，且地址属于指定 login.html 入口",
            "completed": False,
        })
        return plan
