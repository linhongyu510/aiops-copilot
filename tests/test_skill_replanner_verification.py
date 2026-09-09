"""P0-2：Replanner Skill 验收闭环。

只覆盖 `_skill_verification_decision` 的分支逻辑，避免触碰真实 LLM / MCP。
"""

import importlib

replanner_module = importlib.import_module("app.agent.aiops.replanner")


def _base_skill_context(**overrides):
    ctx = {
        "skill_id": "es_red_health",
        "match_score": 0.9,
        "match_reasons": [],
        "verifications": [],
        "labels": {"cluster": "es-01"},
        "cluster": "es-01",
    }
    ctx.update(overrides)
    return ctx


def test_skill_decision_returns_none_when_plan_not_finished():
    """剩余计划还没执行完 —— 让 Executor 继续，不做验收判定。"""
    state = {
        "input": "ES 变红",
        "plan": [{"step_id": "sk:collect", "description": "x", "depends_on": []}],
        "past_steps": [],
        "skill_id": "es_red_health",
        "skill_context": _base_skill_context(),
    }
    assert replanner_module._skill_verification_decision(state, state["skill_context"]) is None


def test_skill_decision_no_verifications_falls_back_to_default_respond(monkeypatch):
    """Skill 未声明 verifications：直接落回默认「无剩余计划 → respond」路径。"""
    recorded: list[tuple[str, str]] = []

    def _record(skill_id, outcome):
        recorded.append((skill_id, outcome))

    monkeypatch.setattr(replanner_module, "_record_skill_outcome", _record)

    state = {
        "input": "ES 变红",
        "plan": [],
        "past_steps": [("查询健康", "green")],
        "skill_id": "es_red_health",
        "skill_context": _base_skill_context(verifications=[]),
    }
    outcome = replanner_module._skill_verification_decision(state, state["skill_context"])
    assert outcome is None
    assert recorded == [("es_red_health", "success")]


def test_skill_decision_injects_verification_steps_when_pending():
    """有 verifications 但都没跑过 → 作为下一批 plan 步骤返回，附上渲染后的 args。"""
    verifications = [
        {
            "tool": "query_es_health",
            "args": {"cluster": "{{ context.cluster }}"},
            "success_expr": "'green' in result",
            "description": "健康再确认",
        }
    ]
    state = {
        "input": "ES 变红",
        "plan": [],
        "past_steps": [("上一步", "任意结果")],
        "skill_id": "es_red_health",
        "skill_context": _base_skill_context(verifications=verifications),
    }
    outcome = replanner_module._skill_verification_decision(state, state["skill_context"])

    assert outcome is not None and "plan" in outcome
    verify_steps = outcome["plan"]
    assert len(verify_steps) == 1
    step = verify_steps[0]
    assert step["description"].startswith("[verify] #0:")
    assert step["tool_hint"] == "query_es_health"
    # 参数模板已被渲染为具体值
    assert step["tool_args_template"] == {"cluster": "es-01"}


def test_skill_decision_all_verifications_passed_records_success(monkeypatch):
    """所有 verification 都跑过且 success_expr 通过 → 记 success，返回 None 让默认路径生成响应。"""
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        replanner_module,
        "_record_skill_outcome",
        lambda sid, oc: recorded.append((sid, oc)),
    )

    verifications = [
        {
            "tool": "query_es_health",
            "args": {"cluster": "{{ context.cluster }}"},
            "success_expr": "'green' in result",
            "description": "健康再确认",
        }
    ]
    # 已执行 verification 的 past_steps 记录，description 必须与生成的一致
    executed_desc = "[verify] #0: 健康再确认"
    state = {
        "input": "ES 变红",
        "plan": [],
        "past_steps": [
            ("采集集群健康", "cluster status: green ok"),
            (executed_desc, "cluster status green"),
        ],
        "skill_id": "es_red_health",
        "skill_context": _base_skill_context(verifications=verifications),
    }

    outcome = replanner_module._skill_verification_decision(state, state["skill_context"])
    assert outcome is None
    assert recorded == [("es_red_health", "success")]


def test_skill_decision_verifications_failed_returns_none(monkeypatch):
    """verifications 已跑完但 success_expr 判 False → 返回 None，让 LLM 决策路径补步。"""
    monkeypatch.setattr(
        replanner_module, "_record_skill_outcome", lambda *_a, **_kw: None
    )

    verifications = [
        {
            "tool": "query_es_health",
            "args": {},
            "success_expr": "'green' in result",  # 明显不成立
            "description": "健康再确认",
        }
    ]
    executed_desc = "[verify] #0: 健康再确认"
    state = {
        "input": "ES 变红",
        "plan": [],
        "past_steps": [(executed_desc, "cluster status: red")],
        "skill_id": "es_red_health",
        "skill_context": _base_skill_context(verifications=verifications),
    }

    outcome = replanner_module._skill_verification_decision(state, state["skill_context"])
    # 失败时不追加 plan，也不主动 respond，交给 LLM 决策路径
    assert outcome is None


def test_skill_decision_absolute_cap_marks_abandoned(monkeypatch):
    """执行步骤超过 _SKILL_ABSOLUTE_STEP_CAP → 记录 abandoned 并回落默认路径。"""
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        replanner_module,
        "_record_skill_outcome",
        lambda sid, oc: recorded.append((sid, oc)),
    )

    verifications = [
        {
            "tool": "query_es_health",
            "args": {},
            "success_expr": "True",
            "description": "健康再确认",
        }
    ]
    cap = replanner_module._SKILL_ABSOLUTE_STEP_CAP
    state = {
        "input": "ES 变红",
        "plan": [],
        "past_steps": [(f"step-{i}", "x") for i in range(cap)],
        "skill_id": "es_red_health",
        "skill_context": _base_skill_context(verifications=verifications),
    }

    outcome = replanner_module._skill_verification_decision(state, state["skill_context"])
    assert outcome is None
    assert recorded == [("es_red_health", "abandoned")]


async def test_replanner_end_to_end_when_skill_has_pending_verifications():
    """走完整 replanner 入口：命中 Skill + plan 已空 + 有 pending verification → 返回 plan。"""
    verifications = [
        {
            "tool": "query_es_health",
            "args": {"cluster": "{{ context.cluster }}"},
            "success_expr": "'green' in result",
            "description": "健康再确认",
        }
    ]
    state = {
        "input": "ES 变红",
        "plan": [],
        "past_steps": [("上一步", "结果")],
        "skill_id": "es_red_health",
        "skill_context": _base_skill_context(verifications=verifications),
    }

    result = await replanner_module.replanner(state)
    assert "plan" in result and len(result["plan"]) == 1
    assert result["plan"][0]["tool_hint"] == "query_es_health"
    assert result["plan"][0]["tool_args_template"] == {"cluster": "es-01"}
