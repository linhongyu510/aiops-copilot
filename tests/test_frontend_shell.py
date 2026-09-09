import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (PROJECT_ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = (PROJECT_ROOT / "static" / "app.js").read_text(encoding="utf-8")
STYLES = (PROJECT_ROOT / "static" / "styles.css").read_text(encoding="utf-8")


def test_frontend_contains_required_interaction_targets_once() -> None:
    required_ids = {
        # KPI 卡片
        "apiStatusValue",
        "milvusStatusValue",
        "toolSuccessValue",
        "toolCallsValue",
        "errorBudgetValue",
        "guardStatusValue",
        # 输入与操作
        "messageInput",
        "sendButton",
        "cancelButton",
        "fileInput",
        "toolsBtn",
        "toolsMenu",
        "uploadFileItem",
        "modeSelectorBtn",
        "modeDropdown",
        # 导航与状态
        "mobileMenuBtn",
        "mobileCloseBtn",
        "authConfigBtn",
        "operationPanel",
        "connectionChip",
        "newChatBtn",
        "chatHistoryList",
        "chatMessages",
        # 主题切换与 API Key 模态框
        "themeToggleBtn",
        "apiKeyModal",
        "apiKeyInput",
        "apiKeySave",
        "apiKeyClear",
        "apiKeyCancel",
    }
    ids = re.findall(r'\bid="([^"]+)"', INDEX_HTML)

    assert len(ids) == len(set(ids)), "HTML element IDs must be unique"
    for element_id in required_ids:
        assert ids.count(element_id) == 1, f"{element_id} 必须在 index.html 中恰好出现一次"

    assert "querySelectorAll('[data-prompt]')" in APP_JS
    assert "Promise.allSettled" in APP_JS
    assert "/metrics/reliability" in APP_JS
    assert "reconnectAttempt < 2" in APP_JS
    assert "'Last-Event-ID'" in APP_JS
    assert "headers['X-API-Key'] = this.apiKey" in APP_JS
    assert "requestAnimationFrame" in APP_JS
    assert "/api/chat/clear" in APP_JS
    assert "themeToggleBtn" in APP_JS


def test_frontend_uses_design_tokens_and_safe_rendering() -> None:
    for token in ("--blue:", "--marine:", "--surface:", "--line:"):
        assert token in STYLES

    # 浅色主题选择器
    assert 'data-theme="light"' in STYLES

    assert "DOMPurify.sanitize" in APP_JS
    assert "console.log" not in APP_JS
    assert "<svg" not in APP_JS
    assert "<svg" not in INDEX_HTML
    assert "style=" not in INDEX_HTML


def test_hidden_attribute_overrides_component_display_rules() -> None:
    """Regression: elements toggled via the `hidden` property must actually hide.

    `.upload-status` and the modals set `display` in their base rule, so without a
    global `[hidden]` override the "正在上传…" banner stayed on screen permanently
    even though JS had set `hidden = true`.
    """
    assert "[hidden] { display: none !important; }" in STYLES

    # Every element that JS hides via the `hidden` property must be declared
    # `hidden` in the markup so it does not flash before scripts run.
    for element_id in ("uploadStatus", "apiKeyModal", "ragInspectorModal"):
        pattern = rf'id="{element_id}"[^>]*>'
        match = re.search(pattern, INDEX_HTML)
        assert match is not None, f"{element_id} 必须存在于 index.html"
        assert " hidden" in match.group(0), f"{element_id} 初始必须带 hidden 属性"


def test_frontend_assets_are_versioned_and_local() -> None:
    # Assert the cache-busting contract rather than one hardcoded version, so
    # bumping assets does not require editing this test. Both local assets must
    # carry the same ?v= query string.
    style_versions = re.findall(r'href="/static/styles\.css\?v=([^"]+)"', INDEX_HTML)
    script_versions = re.findall(r'src="/static/app\.js\?v=([^"]+)"', INDEX_HTML)

    assert len(style_versions) == 1, "styles.css 必须以带版本号的本地路径引入一次"
    assert len(script_versions) == 1, "app.js 必须以带版本号的本地路径引入一次"
    assert style_versions == script_versions, "CSS 与 JS 的版本号必须同步递增"
    assert re.fullmatch(r"\d+\.\d+\.\d+", style_versions[0]), "版本号应为 x.y.z"


def test_frontend_is_vendor_neutral() -> None:
    """前端不得硬编码任何可选集成的品牌或工具名，否则未启用该集成的部署会点出死按钮。"""
    for asset_name, asset in (("index.html", INDEX_HTML), ("styles.css", STYLES)):
        assert "windos" not in asset.lower(), f"{asset_name} 不应引用可选集成"


def test_no_dead_style_hooks_for_removed_markup() -> None:
    """删除展示模块时必须同步删掉样式，避免留下无对应元素的死规则。"""
    for selector in (
        ".hero-grid",
        ".hero-evidence-card",
        ".evidence-flow",
        ".hero-trust-row",
        ".topbar-scope",
    ):
        assert selector not in STYLES, f"{selector} 的样式已无对应元素，应删除"
        assert selector.lstrip(".") not in INDEX_HTML


def test_network_error_message_is_actionable() -> None:
    """报错必须给出可执行的恢复动作。

    原文案是「请确认服务已启动后重试」——它描述了状态却没给动作，用户看完
    仍不知道敲什么命令。回归点：错误分支必须点名具体启动命令。
    """
    assert "quickstart.py" in APP_JS, "连接失败文案必须给出具体启动命令"
    assert "请确认服务已启动后重试" not in APP_JS, "不要用无动作的兜底文案"


def test_failed_send_preserves_question_and_offers_retry() -> None:
    """发送失败不得让用户重打问题。

    sendMessage 发出后会立刻清空输入框，若失败路径只丢一句报错，用户就只能
    手动重新输入。这里锁定三件事：失败走 addErrorMessage、渲染重试按钮、
    并且 sendMessage 能接收程序化传入的问题（而非只读输入框）。
    """
    assert "addErrorMessage(" in APP_JS
    assert "message-retry-btn" in APP_JS, "失败消息必须带可点击的重试按钮"
    assert "async sendMessage(presetMessage)" in APP_JS, (
        "sendMessage 必须支持传入问题，否则重试无法重发原问题"
    )
    assert "this.sendMessage(failedQuestion)" in APP_JS


def test_retrieval_degradation_is_surfaced_next_to_answers() -> None:
    """检索降级必须在对话区显式说明归因。

    Milvus 未连接时模型会回答「知识库中没有相关内容」，而 aiops-docs/ 里其实
    有对应 Runbook。若降级只写在右上角 KPI 卡的 tooltip 里，用户会把「检索没
    跑」误读成「语料缺失」。回归点：横幅存在、由健康检查驱动、且点明该歧义。
    """
    assert 'id="degradeBanner"' in INDEX_HTML
    assert 'role="status"' in INDEX_HTML, "降级提示应对读屏软件可见"
    assert "setRetrievalDegraded(" in APP_JS
    assert "this.setRetrievalDegraded(!milvusConnected)" in APP_JS, (
        "横幅必须与 KPI 卡共用同一份 /health 响应，避免两处状态不一致"
    )
    assert "不代表语料缺失" in APP_JS, "必须点明「检索未跑」不等于「语料缺失」"
    assert ".degrade-banner" in STYLES


def test_accent_borders_avoid_oklch_hue_rotation() -> None:
    """暖色描边不得用 oklch 与中性线条混合。

    --amber(hue 70)/--red(hue 25) 与 --line(hue 245) 在 oklch 下插值会绕经
    青色，实测把琥珀描边渲染成了 hue 175 的青绿。既有代码对这类混合一律用
    srgb，这里锁定该约定。
    """
    for accent in ("--amber", "--red"):
        bad = f"color-mix(in oklch, var({accent})"
        assert bad not in STYLES, (
            f"{accent} 与中性色混合应使用 in srgb，oklch 会绕色相导致偏青"
        )


def test_banner_text_does_not_rely_on_inherited_color() -> None:
    """横幅标题必须显式绑定颜色 token，不能从 body 继承。

    实测发现：浏览器扩展会给所有元素注入 `transition: all`，主题切换时 body 的
    color 过渡会被拖住，继承来的文字色可能停在上一主题——浅色主题下标题对比度
    只有 1.08（近白字压near-white 背景，几乎不可见）。显式绑定后两个主题均达
    到 WCAG AA。
    """
    assert ".degrade-copy strong" in STYLES
    strong_rule = STYLES.split(".degrade-copy strong")[1].split("}")[0]
    assert "color:" in strong_rule, (
        "标题必须显式指定颜色，否则主题切换时可能继承到错误的文字色"
    )
