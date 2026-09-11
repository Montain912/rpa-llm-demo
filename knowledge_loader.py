"""
知识库加载与摘要模块
- 加载 ./knowledge/index.json 作为导航
- 按任务文本匹配页面知识条目
- 将 Schema 结构转换为 LLM 可读的文本摘要
- 带内存缓存 + 文件缓存（以文件 mtime 和摘要器版本失效）
"""
from __future__ import annotations

import json
import os
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
CACHE_FILE = KNOWLEDGE_DIR / ".cache" / "summaries.json"
SUMMARIZER_VERSION = "2026-09-query-v2"

# 内存缓存：file_path -> {mtime, version, summary}
_memory_cache: dict[str, dict] = {}


def load_index() -> list[dict]:
    """加载知识导航 index.json"""
    idx_path = KNOWLEDGE_DIR / "index.json"
    if not idx_path.exists():
        return []
    with open(idx_path, encoding="utf-8") as f:
        return json.load(f)


def _resolve_path(file_path: str) -> Path:
    """index.json 里的 filePath 是相对 knowledge 目录的路径"""
    p = Path(file_path)
    if not p.is_absolute():
        p = KNOWLEDGE_DIR / p
    return p


def _entry_depth(entry: dict, entries_by_name: dict[str, dict]) -> int:
    """返回条目在索引父子关系中的深度，循环或缺失父节点时安全停止。"""
    depth = 0
    current = entry
    visited: set[str] = set()
    while True:
        parent = str(current.get("parentPage", "")).strip().lower()
        if not parent or parent in visited or parent not in entries_by_name:
            return depth
        visited.add(parent)
        depth += 1
        current = entries_by_name[parent]


def _entry_match_rank(
    entry: dict,
    task_lower: str,
    entries_by_name: dict[str, dict],
    index: int,
) -> tuple[int, int, int, int] | None:
    """计算显式匹配排名；不再把名称拆成单字符进行模糊拼接。"""
    name = str(entry.get("name", "")).strip()
    description = str(entry.get("description", "")).strip()
    parent = str(entry.get("parentPage", "")).strip()
    keywords = entry.get("keywords", [])
    if isinstance(keywords, str):
        keywords = [keywords]
    elif not isinstance(keywords, (list, tuple, set)):
        keywords = []
    matched_keywords = [
        str(keyword).strip()
        for keyword in keywords
        if str(keyword).strip() and str(keyword).strip().lower() in task_lower
    ]

    match_class = 0
    evidence_length = 0
    evidence_count = 0
    if name and name.lower() in task_lower:
        match_class = 4
        evidence_length = len(name)
        evidence_count = 1
    if matched_keywords:
        keyword_length = sum(len(keyword) for keyword in set(matched_keywords))
        keyword_count = len(set(matched_keywords))
        if match_class < 3:
            match_class = 3
            evidence_length = keyword_length
            evidence_count = keyword_count
        else:
            evidence_length += keyword_length
            evidence_count += keyword_count
    if description and description.lower() in task_lower and match_class < 2:
        match_class = 2
        evidence_length = len(description)
        evidence_count = 1
    if parent and parent.lower() in task_lower and match_class < 1:
        match_class = 1
        evidence_length = len(parent)
        evidence_count = 1
    if not match_class:
        return None

    return (
        match_class,
        _entry_depth(entry, entries_by_name),
        evidence_count * 1000 + evidence_length,
        -index,
    )


def _is_ancestor(
    possible_ancestor: dict,
    descendant: dict,
    entries_by_name: dict[str, dict],
) -> bool:
    ancestor_name = str(possible_ancestor.get("name", "")).strip().lower()
    if not ancestor_name:
        return False
    current = descendant
    visited: set[str] = set()
    while True:
        parent = str(current.get("parentPage", "")).strip().lower()
        if not parent or parent in visited:
            return False
        if parent == ancestor_name:
            return True
        visited.add(parent)
        current = entries_by_name.get(parent)
        if current is None:
            return False


def match_entries_by_task(task: str) -> list[dict]:
    """返回所有相关的非 always 条目，并剔除已命中子页面的祖先条目。"""
    task_lower = str(task or "").lower()
    entries = load_index()
    entries_by_name = {
        str(entry.get("name", "")).strip().lower(): entry
        for entry in entries
        if str(entry.get("name", "")).strip()
    }
    ranked: list[tuple[tuple[int, int, int, int], int, dict]] = []
    for index, entry in enumerate(entries):
        if entry.get("always") is True:
            continue
        rank = _entry_match_rank(entry, task_lower, entries_by_name, index)
        if rank is not None:
            ranked.append((rank, index, entry))

    matched = [item[2] for item in ranked]
    most_specific = [
        entry
        for entry in matched
        if not any(
            entry is not other and _is_ancestor(entry, other, entries_by_name)
            for other in matched
        )
    ]
    # 摘要按索引顺序输出，使通用浏览器规则自然排在具体页面规则之前。
    selected_ids = {id(entry) for entry in most_specific}
    return [entry for entry in entries if id(entry) in selected_ids]


def match_by_task(task: str) -> dict | None:
    """兼容旧调用：返回相关非 always 条目中排名最高、最具体的一条。"""
    matches = match_entries_by_task(task)
    if not matches:
        return None
    task_lower = str(task or "").lower()
    entries = load_index()
    entries_by_name = {
        str(entry.get("name", "")).strip().lower(): entry
        for entry in entries
        if str(entry.get("name", "")).strip()
    }
    index_by_key = {
        (str(entry.get("name", "")), str(entry.get("filePath", ""))): index
        for index, entry in enumerate(entries)
    }
    return max(
        matches,
        key=lambda entry: _entry_match_rank(
            entry,
            task_lower,
            entries_by_name,
            index_by_key.get(
                (str(entry.get("name", "")), str(entry.get("filePath", ""))),
                len(entries),
            ),
        ) or (0, 0, 0, 0),
    )


def load_knowledge(entry: dict) -> dict | None:
    """读取知识条目对应的 JSON 文件"""
    fp = entry.get("filePath")
    if not fp:
        return None
    path = _resolve_path(fp)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _enum_text(val) -> str:
    """提取 enum 数组为可读字符串，过滤空值"""
    if isinstance(val, list):
        return "/".join(str(v) for v in val if str(v).strip())
    return str(val)


def _describe_fields(props: dict) -> list[str]:
    """把一个 properties 字典描述成 LLM 可读的字段列表"""
    lines = []
    for name, spec in props.items():
        if not isinstance(spec, dict):
            continue
        desc = spec.get("description", "")
        typ = spec.get("type", "")
        enum = spec.get("enum")
        required = spec.get("readOnly", False)
        hint = ""
        if enum:
            hint = f"（枚举: {_enum_text(enum)}）"
        if required:
            hint += "（只读）"
        if typ in ("string", "integer", "number", "boolean"):
            lines.append(f"- {name}: {typ}{hint} {desc}")
        elif typ == "array":
            items = spec.get("items", {})
            if isinstance(items, dict) and "enum" in items:
                lines.append(f"- {name}: 数组（枚举: {_enum_text(items['enum'])}） {desc}")
            else:
                lines.append(f"- {name}: 数组 {desc}")
        elif typ == "object":
            # 复杂嵌套对象，跳过详细展开
            lines.append(f"- {name}: 复合对象 {desc}")
    return lines


def _summarize_automation_guidance(data: dict) -> list[str]:
    """摘要通用执行规则和基于可见现象的有限恢复策略。"""
    guidance = data.get("automationGuidance", {})
    if not isinstance(guidance, dict) or not guidance:
        return []
    parts = []
    rules = guidance.get("rules", [])
    if rules:
        parts.append("- 自动化执行规则:")
        parts.extend(f"  - {rule}" for rule in rules)
    diagnostics = guidance.get("diagnostics", [])
    if diagnostics:
        parts.append("- 故障诊断:")
    for item in diagnostics:
        if not isinstance(item, dict):
            continue
        symptom = item.get("symptom", "未知现象")
        cause = item.get("cause", "待判断")
        action = item.get("action", "停止并报告")
        retry = item.get("maxRetries")
        retry_text = f"；最多重试 {retry} 次" if retry is not None else ""
        parts.append(
            f"  - 现象：{symptom}；可能原因：{cause}；处理：{action}{retry_text}"
        )
    operations = guidance.get("inputMethodOperations", [])
    if operations:
        parts.append("- 输入法封装操作:")
        parts.extend(f"  - {operation}" for operation in operations)
    return parts


def _summarize_operation_guide(data: dict) -> str:
    """将操作指南型知识（如添加助手场景）转换为 LLM 可读的文本摘要"""
    parts = []
    title = data.get("title", "")
    desc = data.get("description", "")
    if title:
        parts.append(f"【操作指南：{title}】")
    if desc:
        parts.append(f"说明：{desc}")

    # 页面定位
    loc = data.get("pageLocation", {})
    if loc:
        parts.append(f"- 页面路径: {loc.get('navPath', '')}")
        login_url = loc.get("loginUrl", "")
        if login_url:
            parts.append(f"- 登录网址: {login_url}")
        entry = loc.get("entryButton", {})
        if entry:
            parts.append(f"- 入口按钮: {entry.get('name', '')}（{entry.get('position', '')}）")

    scope = data.get("scopeGuard", {})
    if isinstance(scope, dict) and scope:
        allowed = scope.get("allowedOperations", [])
        forbidden = scope.get("forbiddenOperations", [])
        if allowed:
            parts.append(f"- 本任务允许的操作: {', '.join(map(str, allowed))}")
        if forbidden:
            parts.append(f"- 严禁执行的操作: {', '.join(map(str, forbidden))}")

    # 必填字段
    required = data.get("requiredFields", [])
    if required:
        parts.append(f"- 必填字段: {', '.join(required)}")

    # 表单结构概览
    form = data.get("formStructure", {})
    if form:
        parts.append(f"- 表单概览: {form.get('overview', '')}")
        need_scroll = form.get("requiresScrollDown", [])
        if need_scroll:
            parts.append(f"- 需要滚动才能看到: {', '.join(need_scroll)}")

    # 字段详情（只列关键字段）
    field_details = data.get("fieldDetails", {})
    if field_details:
        parts.append("- 关键字段:")
        for name, info in field_details.items():
            if not isinstance(info, dict):
                continue
            label = info.get("label", name)
            typ = info.get("type", "")
            required = info.get("required", False)
            req_mark = "【必填】" if required else ""
            default = info.get("defaultValue", "")
            default_text = f"（默认: {default}）" if default else ""
            options = info.get("options", [])
            opt_text = ""
            if options:
                # 下拉框/选择型字段：每个选项前加上字段名标签，防止模型跨下拉框串项
                if typ in ("select", "dropdown"):
                    opt_text = f" 选项目录（属「{label}」）: {', '.join(options)}"
                elif len(options) <= 5:
                    opt_text = f" 选项: {', '.join(options[:5])}"
                else:
                    opt_text = f" 选项（前5个）: {', '.join(options[:5])}..."
            parts.append(f"  • {label}{req_mark} ({typ}){default_text}{opt_text}")
            visual_parts = []
            if info.get("hasVisibleText") is False:
                visual_parts.append("无可见文字")
            for key, title in (
                ("container", "容器"),
                ("position", "相对位置"),
                ("visualFeature", "视觉特征"),
                ("stableTargetName", "动作目标名"),
                ("interactionRule", "交互规则"),
            ):
                value = info.get(key)
                if value is not None and str(value).strip():
                    visual_parts.append(f"{title}: {value}")
            if info.get("requiresUserMenu") is False:
                visual_parts.append("无需展开用户菜单")
            click_count = info.get("clickCount")
            if click_count is not None:
                visual_parts.append(f"点击次数: {click_count}")
            if visual_parts:
                parts.append(f"    视觉/交互锚点: {'；'.join(visual_parts)}")

    # 标准操作流程
    guide = data.get("operationGuide", {})
    if guide:
        workflow = guide.get("standardWorkflow", [])
        if workflow:
            parts.append("- 标准操作步骤:")
            for step in workflow:
                parts.append(f"  {step.get('step', '')}. {step.get('action', '')} — {step.get('howTo', '')}")

        # 滚动策略（重点！防止死循环）
        scroll = guide.get("scrollStrategy", {})
        if scroll:
            parts.append(f"- ⚠️ 滚动策略（防止死循环）: {scroll.get('criticalWarning', '')}")
            parts.append(f"  正确滚动次数: {scroll.get('correctScrollCount', '')}")
            bottom_signs = scroll.get("bottomSigns", [])
            if bottom_signs:
                parts.append(f"  到底部的标志: {'; '.join(bottom_signs)}")
            alts = scroll.get("alternativeToScroll", [])
            if alts:
                parts.append(f"  滚动替代方案: {'; '.join(alts)}")

        # 快捷键
        shortcuts = guide.get("keyboardShortcuts", {})
        if shortcuts:
            parts.append(f"- 快捷键: {shortcuts.get('tabNavigation', '')}")

    # 常见陷阱（重要，防止 Agent 踩坑）
    traps = data.get("commonTraps", [])
    if traps:
        parts.append("- ⚠️ 常见陷阱与规避方法:")
        for t in traps:
            parts.append(f"  • 陷阱: {t.get('trap', '')}")
            parts.append(f"    解决: {t.get('solution', '')}")

    # 测试用例（只列标题）
    test_cases = data.get("testCases", [])
    if test_cases:
        parts.append(f"- 参考测试用例（共 {len(test_cases)} 个）:")
        for tc in test_cases[:3]:
            parts.append(f"  • {tc.get('caseId', '')}: {tc.get('caseName', '')}")

    parts.extend(_summarize_automation_guidance(data))

    return "\n".join(parts)


def summarize_for_prompt(data: dict) -> str:
    """将知识数据转换为 LLM 可读的文本摘要
    自动检测知识类型：有 operationGuide/commonTraps 的是操作指南型，有 properties 的是页面 Schema 型
    """
    # 检测知识类型：操作指南型优先（更具体、更有指导意义）
    if "operationGuide" in data or "commonTraps" in data:
        return _summarize_operation_guide(data)

    parts = []
    title = data.get("title", "")
    desc = data.get("description", "")
    if title:
        parts.append(f"【页面结构参考：{title}】")
    if desc:
        parts.append(f"说明：{desc}")

    props = data.get("properties", {})

    # 1. 导航栏
    nav = props.get("sidebarNavigation", {})
    if nav:
        nav_props = nav.get("properties", {})
        menus = nav_props.get("menus", {})
        sub_menus = nav_props.get("subMenus", {})
        nav_line = "- 左侧导航栏: "
        # enum 可能直接在对象里，也可能在 items 里（数组类型）
        menus_enum = menus.get("enum") or (menus.get("items", {}) or {}).get("enum")
        if menus_enum:
            nav_line += f"{_enum_text(menus_enum)}"
        sub_enum = sub_menus.get("enum") or (sub_menus.get("items", {}) or {}).get("enum")
        if sub_enum:
            nav_line += f"（子菜单: {_enum_text(sub_enum)}）"
        parts.append(nav_line)

    # 2. 主列表页
    main_list = props.get("mainListPage", {})
    if main_list:
        main_props = main_list.get("properties", {})

        # 搜索条件
        search = main_props.get("searchFilters", {})
        if search:
            parts.append("- 主列表页搜索条件:")
            for line in _describe_fields(search.get("properties", {})):
                parts.append(f"  {line}")

        # 表格
        table = main_props.get("table", {})
        if table:
            items = table.get("items", {})
            if isinstance(items, dict):
                table_props = items.get("properties", {})
                parts.append("- 表格列:")
                for line in _describe_fields(table_props):
                    parts.append(f"  {line}")

        # 底部操作
        bottom = main_props.get("bottomActions", {})
        if bottom:
            parts.append(f"- 底部操作栏: {', '.join(bottom.get('properties', {}).keys())}")

    # 3. 配置抽屉
    drawer = props.get("configDrawer", {})
    if drawer:
        drawer_props = drawer.get("properties", {})
        required = drawer.get("required", [])
        error_msgs = drawer.get("errorMessages", {})
        parts.append("- 配置抽屉(添加/编辑):")
        for line in _describe_fields(drawer_props):
            parts.append(f"  {line}")
        if required:
            parts.append(f"- 配置抽屉必填项: {', '.join(required)}")
        if error_msgs:
            for field, msg in error_msgs.items():
                parts.append(f"  - 校验规则: {field} → '{msg}'")

    # 4. 画布页
    canvas = props.get("workflowCanvas", {})
    if canvas:
        canvas_props = canvas.get("properties", {})
        node_lib = canvas_props.get("nodeLibrary", {})
        if node_lib:
            parts.append("- 画布页左侧节点库:")
            for cat_name, cat_spec in node_lib.get("properties", {}).items():
                if isinstance(cat_spec, dict) and "items" in cat_spec:
                    items = cat_spec["items"]
                    if isinstance(items, dict) and "enum" in items:
                        parts.append(f"  - {cat_name}: {_enum_text(items['enum'])}")
        toolbar = canvas_props.get("bottomToolbar", {})
        if toolbar:
            parts.append(f"- 画布页底部工具栏: {', '.join(toolbar.get('properties', {}).keys())}")

    parts.extend(_summarize_automation_guidance(data))

    return "\n".join(parts)


def _cache_record_is_valid(record: dict | None, mtime: float) -> bool:
    return bool(
        isinstance(record, dict)
        and record.get("mtime") == mtime
        and record.get("version") == SUMMARIZER_VERSION
        and isinstance(record.get("summary"), str)
    )


def _get_entry_summary(entry: dict) -> str:
    """获取单个知识条目的版本化摘要。"""
    fp = entry.get("filePath", "")
    if not fp:
        return ""
    abs_path = _resolve_path(fp)

    try:
        mtime = abs_path.stat().st_mtime
    except OSError:
        mtime = 0

    cached = _memory_cache.get(fp)
    if _cache_record_is_valid(cached, mtime):
        return cached["summary"]

    cached_from_file = _load_from_file_cache(fp)
    if _cache_record_is_valid(cached_from_file, mtime):
        _memory_cache[fp] = cached_from_file
        return cached_from_file["summary"]

    data = load_knowledge(entry)
    if not data:
        return ""
    summary = summarize_for_prompt(data)
    record = {
        "mtime": mtime,
        "version": SUMMARIZER_VERSION,
        "summary": summary,
    }
    _memory_cache[fp] = record
    try:
        _persist_cache(fp, mtime, summary)
    except Exception:
        pass
    return summary


def get_summary(task: str, phase: str | None = None) -> str:
    """组合所有 always 知识与任务命中的最具体知识摘要。"""
    entries = load_index()
    selected = [entry for entry in entries if entry.get("always") is True]
    selected.extend(match_entries_by_task(task))

    unique = []
    seen_paths: set[str] = set()
    for entry in selected:
        phases = entry.get("promptPhases")
        if phase and isinstance(phases, list) and phases and phase not in phases:
            continue
        path = str(entry.get("filePath", ""))
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        unique.append(entry)
    return "\n\n".join(
        summary for summary in map(_get_entry_summary, unique) if summary
    )


def _persist_cache(file_path: str, mtime: float, summary: str) -> None:
    """把单条摘要写入文件缓存"""
    cache_dir = CACHE_FILE.parent
    cache_dir.mkdir(parents=True, exist_ok=True)

    existing = {}
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}

    existing[file_path] = {
        "mtime": mtime,
        "version": SUMMARIZER_VERSION,
        "summary": summary,
    }
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


def warmup_cache() -> None:
    """启动时预加载所有知识摘要到内存缓存"""
    for entry in load_index():
        _get_entry_summary(entry)


def _load_from_file_cache(file_path: str) -> dict | None:
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data.get(file_path)
    except (json.JSONDecodeError, OSError):
        return None


def clear_cache() -> None:
    """清空内存缓存和文件缓存（知识文件修改后调用）"""
    _memory_cache.clear()
    if CACHE_FILE.exists():
        try:
            os.remove(CACHE_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    # 自测：加载 index，匹配示例任务，打印摘要
    warmup_cache()
    test_task = "在工作流AGENT页面新建一个场景"
    summary = get_summary(test_task)
    if summary:
        print("===== 匹配到知识摘要 =====")
        print(summary)
    else:
        print("未匹配到知识")
