"""P0-2：Skill 验收表达式安全求值。"""

from app.agent.skills.verifier import evaluate_expr, evaluate_success_expr


def test_success_expr_supports_boolean_and_membership_ops() -> None:
    result = {"status": "green", "count": 3}
    assert evaluate_success_expr("result.status == 'green'", result, {}) is True
    assert evaluate_success_expr(
        "result.status in ('green', 'yellow')", result, {}
    ) is True
    assert evaluate_success_expr(
        "result.count > 0 and result.status != 'red'", result, {}
    ) is True


def test_success_expr_returns_false_when_expr_is_blank_or_invalid() -> None:
    # 空表达式：显式判 False，避免误判为成功
    assert evaluate_success_expr("", "anything", {}) is False
    assert evaluate_success_expr("   ", "anything", {}) is False
    # 语法错误：捕获后返回 False，不向外抛
    assert evaluate_success_expr("result. == 1", "x", {}) is False


def test_success_expr_rejects_dangerous_nodes() -> None:
    # 函数调用被 AST 白名单拒绝
    assert evaluate_success_expr("len(result) > 0", "abc", {}) is False
    # 双下划线属性被显式拒绝
    assert evaluate_success_expr("result.__class__ == str", "abc", {}) is False
    # 显式拒绝导入 / lambda / 赋值
    assert evaluate_success_expr("lambda x: x", "abc", {}) is False


def test_success_expr_accepts_result_as_string_via_membership() -> None:
    """result 是字符串时也能通过 in / == 判定。"""
    log = "ERROR 数据库连接池耗尽"
    assert evaluate_success_expr("'连接池' in result", log, {}) is True
    assert evaluate_success_expr("result == 'OK'", log, {}) is False


def test_success_expr_can_read_context_labels() -> None:
    ctx = {"labels": {"env": "prod"}, "cluster": "es-01"}
    assert evaluate_success_expr(
        "context.labels.env == 'prod' and context.cluster == 'es-01'",
        None,
        ctx,
    ) is True


def test_evaluate_expr_raises_for_disallowed_nodes() -> None:
    # evaluate_expr 是内部低层接口，直接调用应抛异常暴露非法节点
    try:
        evaluate_expr("__import__('os')", {})
    except ValueError as exc:
        assert "不允许" in str(exc) or "非法" in str(exc)
    else:
        raise AssertionError("函数调用应被拒绝")
