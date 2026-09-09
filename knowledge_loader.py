"""
知识库加载与摘要模块
- 加载 ./knowledge/index.json 作为导航
- 按任务文本匹配页面知识条目
- 将 Schema 结构转换为 LLM 可读的文本摘要
- 带内存缓存 + 文件缓存（以文件 mtime 失效）
"""
from __future__ import annotations

import json
import os
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
CACHE_FILE = KNOWLEDGE_DIR / ".cache" / "summaries.json"

# 内存缓存：file_path -> {mtime, summary}
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


def match_by_task(task: str) -> dict | None:
    """
    按任务文本匹配知识条目：
    1. 任务文本包含 name → 命中（优先级最高）
    2. 关键词双向匹配：name 中的关键词出现在任务中，或任务中的关键词出现在 name 中 → 命中
    3. 任务文本包含 description 关键词 → 命中
    4. 任务文本包含 parentPage → 命中
    """
    task_lower = task.lower()
    entries = load_index()
    if not entries:
        return None

    # 第一优先级：name 完整包含在任务中
    for e in entries:
        name = e.get("name", "")
        if name and name.lower() in task_lower:
            return e

    # 第二优先级：关键词双向匹配（适用于同义词/近义词场景）
    # 提取 name 中的核心关键词（去掉常见动词），看是否在任务中出现
    skip_words = {"添加", "创建", "新增", "新建", "编辑", "修改", "删除", "查看", "查询", "测试", "的", "了", "一个"}
    for e in entries:
        name = e.get("name", "")
        if not name:
            continue
        name_lower = name.lower()
        # 提取关键词：去掉跳过词后的每个字/词
        name_keywords = [w for w in name if w not in skip_words]
        # 如果 name 的关键词大部分都出现在任务中，认为匹配
        hit_count = sum(1 for kw in name_keywords if kw in task_lower and kw.strip())
        if hit_count >= max(2, len(name_keywords) * 0.5):
            return e

    # 第三优先级：反向匹配 - 任务关键词出现在 description 中
    for e in entries:
        desc = e.get("description", "")
        if desc and desc.lower() in task_lower:
            return e

    # 第四优先级：parentPage 匹配（匹配度最低，返回最顶层的即可）
    for e in entries:
        parent = e.get("parentPage", "")
        if parent and parent.lower() in task_lower:
            return e

    return None


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
        entry = loc.get("entryButton", {})
        if entry:
            parts.append(f"- 入口按钮: {entry.get('name', '')}（{entry.get('position', '')}）")

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

    return "\n".join(parts)


def get_summary(task: str) -> str:
    """
    对外入口：根据任务文本获取知识摘要（带缓存）
    未匹配返回空字符串
    """
    entry = match_by_task(task)
    if not entry:
        return ""

    fp = entry.get("filePath", "")
    abs_path = _resolve_path(fp)

    # 文件 mtime 作为缓存键（文件修改即失效）
    try:
        mtime = abs_path.stat().st_mtime
    except OSError:
        mtime = 0

    # 内存缓存命中
    cached = _memory_cache.get(fp)
    if cached and cached.get("mtime") == mtime:
        return cached["summary"]

    # 生成摘要
    data = load_knowledge(entry)
    if not data:
        return ""
    summary = summarize_for_prompt(data)

    # 写内存缓存
    _memory_cache[fp] = {"mtime": mtime, "summary": summary}

    # 异步持久化到文件缓存（best-effort，失败不影响）
    try:
        _persist_cache(fp, mtime, summary)
    except Exception:
        pass

    return summary


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

    existing[file_path] = {"mtime": mtime, "summary": summary}
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


def warmup_cache() -> None:
    """启动时预加载所有知识摘要到内存缓存"""
    for entry in load_index():
        fp = entry.get("filePath", "")
        if not fp:
            continue
        abs_path = _resolve_path(fp)
        if not abs_path.exists():
            continue
        try:
            mtime = abs_path.stat().st_mtime
        except OSError:
            continue
        # 先尝试文件缓存
        cached_from_file = _load_from_file_cache(fp)
        if cached_from_file and cached_from_file.get("mtime") == mtime:
            _memory_cache[fp] = cached_from_file
            continue
        # 文件缓存未命中，重新生成
        data = load_knowledge(entry)
        if data:
            summary = summarize_for_prompt(data)
            _memory_cache[fp] = {"mtime": mtime, "summary": summary}
            try:
                _persist_cache(fp, mtime, summary)
            except Exception:
                pass


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
