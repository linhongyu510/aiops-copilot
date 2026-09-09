"""P0-2：Skill 参数模板渲染。"""

from app.agent.skills.renderer import (
    build_render_context,
    render_args,
    render_string,
)


def test_render_string_replaces_top_level_input_and_dotted_paths() -> None:
    ctx = build_render_context(
        input_text="cluster es-01 red",
        skill_context={"cluster": "es-01"},
        labels={"env": "prod"},
    )

    assert render_string("查询 {{ input }} 的健康", ctx) == "查询 cluster es-01 red 的健康"
    assert render_string("目标: {{ context.cluster }}", ctx) == "目标: es-01"
    assert render_string("env={{ labels.env }}", ctx) == "env=prod"
    # 简写：非 dotted 的裸键回退到 context.<key>
    assert render_string("{{ cluster }}", ctx) == "es-01"


def test_render_string_leaves_unresolved_placeholders_intact() -> None:
    ctx = build_render_context(input_text="hi")
    text = "unknown={{ context.missing }} labels={{ labels.absent }}"
    rendered = render_string(text, ctx)
    # 未命中的占位符原样保留（Executor 层的 LLM 仍可兜底判断）
    assert rendered == text


def test_render_args_recurses_dict_and_list_and_preserves_scalars() -> None:
    ctx = build_render_context(
        input_text="in",
        skill_context={"nested": {"topic": "svc-a", "level": 3}},
    )
    template = {
        "topic": "{{ context.nested.topic }}",
        "extras": ["{{ context.nested.level }}", 42, None, True],
        "meta": {"input_echo": "{{ input }}"},
    }

    rendered = render_args(template, ctx)

    assert rendered == {
        "topic": "svc-a",
        # 所有 str 字段被替换；非 str 原样透传
        "extras": ["3", 42, None, True],
        "meta": {"input_echo": "in"},
    }


def test_build_render_context_protects_reserved_keys() -> None:
    """skill_context 里的 input/step/labels 保留键不能覆盖框架默认值。"""
    ctx = build_render_context(
        input_text="the-input",
        step_id_to_result={"sk:collect": "logs-A"},
        skill_context={
            "input": "attacker-input",      # 应被忽略
            "step": {"sk:collect": "hijack"},  # 应被忽略
            "labels": {"env": "hijack"},    # 应被忽略
            "cluster": "es-01",             # 允许注入
        },
        labels={"env": "prod"},
    )

    assert ctx["input"] == "the-input"
    assert ctx["step"] == {"sk:collect": "logs-A"}
    assert ctx["labels"] == {"env": "prod"}
    assert ctx["cluster"] == "es-01"


def test_render_string_short_circuits_when_no_placeholder() -> None:
    # 无占位符时直接返回，不进入正则替换分支（性能兜底）
    ctx = build_render_context(input_text="x")
    assert render_string("plain text", ctx) == "plain text"
    assert render_args(123, ctx) == 123
