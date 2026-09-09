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


def test_frontend_uses_windos_design_tokens_and_safe_rendering() -> None:
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

    assert "WINDOS" in INDEX_HTML
