"""
Replanner 节点：重新规划或生成最终响应
基于 LangGraph 官方教程实现
"""

from textwrap import dedent
from typing import Any

from langchain_core.prompts import ChatPromptTemplate
from loguru import logger
from pydantic import BaseModel, Field

from app.agent.aiops.models import PlanStep, normalize_plan, step_description
from app.agent.mcp_client import get_mcp_client_with_retry
from app.agent.profiles import get_active_profile
from app.agent.skills.renderer import build_render_context, render_args
from app.agent.skills.verifier import evaluate_success_expr
from app.agent.tool_router import select_relevant_tools
from app.config import config
from app.core.llm_factory import llm_factory, structured_output
from app.tools import analyze_topology, get_current_time, retrieve_knowledge

from .state import PlanExecuteState
from .utils import format_tools_description


class Response(BaseModel):
    """最终响应的格式"""

    response: str = Field(description="对用户的最终响应")


class DiagnosisOutcome(BaseModel):
    """结构化诊断结果（C3）：与 markdown 报告伴生，供下游机读消费。

    字段全部可选：LLM 未提供或摘要抽取失败时使用空值，业务方按需处理。
    """

    root_cause: str = Field(default="", description="根因结论（一句话）")
    evidence: list[str] = Field(default_factory=list, description="关键证据摘要，来自执行步骤")
    actions_taken: list[str] = Field(default_factory=list, description="已完成的排查/只读动作")
    actions_proposed: list[str] = Field(default_factory=list, description="待人工审批的变更提案摘要")
    verification_status: str = Field(
        default="unknown",
        description="Skill 验收状态：passed/failed/partial/unknown",
    )


class Act(BaseModel):
    """重新规划的输出格式"""

    action: str = Field(
        description="""下一步的行动，必须是以下三种之一：
        - 'continue': 当前计划合理，继续执行下一个步骤
        - 'replan': 当前计划需要调整，提供新的步骤列表
        - 'respond': 计划已完成且信息充足，生成最终响应"""
    )
    # action 为 'replan' 时，新的步骤列表（会替换当前剩余计划）
    new_steps: list[PlanStep] = Field(
        default_factory=list,
        description="新的步骤列表（如果 action 是 'replan'，这些步骤会替换剩余计划）；"
        "相互独立的步骤 depends_on 填空列表以支持并行",
    )


# Replanner 提示词
replanner_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            dedent("""
                作为一个重新规划专家，你需要根据已执行的步骤决定下一步行动。

                可用工具列表（用于制定计划时参考）：

                {tools_description}

                注意：你的职责是制定或调整计划，实际的工具调用由 Executor 负责执行。

                你有三个选择（按优先级排序）：

                **1. 'respond' - 信息充足，立即生成最终响应** 【最高优先级】
                   - 使用场景：当前信息已经足够回答用户问题
                   - 决策标准：
                     * 已执行步骤 >= 3 且获取了关键信息
                     * 或者已执行步骤 >= 5（无论结果如何）
                     * 或者当前信息完全满足任务需求
                   - ⚠️ 不要等到"完美"才响应，"足够好"就应该立即 respond

                **2. 'continue' - 当前计划合理，继续执行** 【次优先级】
                   - 使用场景：剩余计划合理且必要
                   - 决策标准：剩余步骤确实能提供关键信息
                   - ⚠️ 如果剩余步骤不是"必需"的，应选择 respond

                **3. 'replan' - 当前计划有严重问题** 【最低优先级，谨慎使用】
                   - 使用场景：原计划明显错误或遗漏关键步骤
                   - ⚠️ **严格限制**：
                     * 新步骤数量必须 <= 当前剩余步骤数
                     * 优先简化计划，不要添加不必要的步骤
                     * 总步骤数已执行 >= 5 次时，禁止 replan，只能 respond

                评估标准：
                - 当前信息是否已经足够解决用户问题？【最关键】
                - 已执行步骤是否成功获取了核心信息？
                - 剩余步骤是否真的"必需"？
                - 已执行步骤数是否过多（>= 5）？如果是，立即 respond

                **决策优先级口诀：** 
                "优先结束 > 保持不变 > 调整计划"
                "信息足够就响应，不要追求完美"
            """).strip(),
        ),
        ("placeholder", "{messages}"),
    ]
)

# 最终响应生成提示词
response_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            dedent("""
                根据原始任务和已执行步骤的结果，生成一个全面的最终响应。

                响应要求：
                - 清晰、结构化
                - 基于实际数据，不要编造
                - 如果某些步骤失败，要诚实说明
                - 使用 Markdown 格式
            """).strip(),
        ),
        ("placeholder", "{messages}"),
    ]
)


async def replanner(state: PlanExecuteState) -> dict[str, Any]:
    """
    重新规划节点：决定是继续、调整计划还是生成最终响应

    三种决策：
    1. continue - 继续执行当前计划
    2. replan - 调整计划（替换剩余步骤）
    3. respond - 生成最终响应
    """
    logger.info("=== Replanner：重新规划 ===")

    input_text = state.get("input", "")
    plan = state.get("plan", [])
    past_steps = state.get("past_steps", [])
    skill_context = state.get("skill_context") or {}
    skill_id = state.get("skill_id") or (
        skill_context.get("skill_id") if isinstance(skill_context, dict) else ""
    )

    logger.info(f"剩余计划步骤: {len(plan)}")
    logger.info(f"已执行步骤: {len(past_steps)}")

    # Skill 验收闭环（P0-2）：命中 Skill 时优先跑 verifications 决定收敛
    if skill_id and isinstance(skill_context, dict):
        skill_decision = _skill_verification_decision(state, skill_context)
        if skill_decision is not None:
            return skill_decision

    # ⚠️ 强制限制：未命中 Skill 时保留原阈值兜底，profile 可覆盖。
    # profile 未配置时保留原硬编码 8 的语义（O5：Skill 版仍走 skill 分支）。
    _FALLBACK_MAX_STEPS = 8
    try:
        profile_max = get_active_profile().replanner_max_steps_before_force_respond
    except Exception:
        profile_max = None
    max_steps = int(profile_max) if isinstance(profile_max, int) and profile_max > 0 else _FALLBACK_MAX_STEPS
    if len(past_steps) >= max_steps:
        logger.warning(
            f"已执行 {len(past_steps)} 个步骤，超过最大限制 {max_steps}，强制生成最终响应"
        )
        return await _generate_response(state)

    # 获取可用工具列表
    try:
        # 获取本地工具
        local_tools = [get_current_time, retrieve_knowledge, analyze_topology]

        # 获取 MCP 工具
        mcp_client = await get_mcp_client_with_retry()
        mcp_tools = await mcp_client.get_tools()

        # 合并所有工具
        all_tools = local_tools + mcp_tools
        logger.info(f"可用工具数量: 本地 {len(local_tools)} + MCP {len(mcp_tools)}")

        # 按用户输入裁剪工具，只把相关工具描述注入决策 prompt
        exposed_tools = select_relevant_tools(
            input_text, all_tools, limit=config.aiops_max_exposed_tools
        )

        # 格式化工具描述
        tools_description = format_tools_description(exposed_tools)
    except Exception as e:
        logger.warning(f"获取工具列表失败: {e}")
        tools_description = "无法获取工具列表"

    # 创建 LLM（用于 continue/replan/respond 决策）
    llm = llm_factory.create_chat_model(
        model=config.rag_model,
        temperature=0,
        streaming=False,
        max_tokens=1024,
    )

    # 格式化已执行的步骤
    steps_summary = "\n".join(
        [f"步骤: {step}\n结果: {result[:300]}..." for step, result in past_steps]
    )

    # 如果还有剩余计划，进行决策
    if plan:
        logger.info("还有剩余计划，评估下一步行动")

        replanner_chain = replanner_prompt | structured_output(llm, Act)

        try:
            messages = [
                ("user", f"原始任务: {input_text}"),
                ("user", f"已执行的步骤:\n{steps_summary}"),
                (
                    "user",
                    "剩余计划: "
                    + ", ".join(step_description(step) for step in plan),
                ),
                (
                    "user",
                    f"⚠️ 重要提示：已执行 {len(past_steps)} 个步骤，请优先考虑是否信息已足够生成响应（respond）",
                ),
            ]

            act = await replanner_chain.ainvoke(
                {"messages": messages, "tools_description": tools_description}
            )

            # 处理返回结果
            if isinstance(act, Act):
                action = act.action
                new_steps = list(act.new_steps)
            else:
                # 如果返回的是字典
                action = act.get("action", "continue")  # type: ignore
                new_steps = list(act.get("new_steps", []) or [])  # type: ignore

            logger.info(f"Replanner 决策: {action}")

            if action == "respond":
                logger.info("决定生成最终响应")
                return await _generate_response(state)

            elif action == "replan":
                # ⚠️ 强制限制：新步骤数不能超过当前剩余步骤数
                if len(new_steps) > len(plan):
                    logger.warning(
                        f"新步骤数 {len(new_steps)} > 剩余步骤数 {len(plan)}，"
                        f"强制截断为 {len(plan)} 个步骤"
                    )
                    new_steps = new_steps[: len(plan)]

                # ⚠️ 二次检查：如果已执行步骤 >= 5，禁止 replan
                if len(past_steps) >= 5:
                    logger.warning(f"已执行 {len(past_steps)} 个步骤，禁止重新规划，强制生成响应")
                    return await _generate_response(state)

                logger.info(f"决定调整计划，新步骤数量: {len(new_steps)}")
                if new_steps:
                    # 替换剩余计划；id_offset 避开历史已完成 step_id，防止依赖误判
                    return {
                        "plan": normalize_plan(new_steps, id_offset=len(past_steps))
                    }
                else:
                    logger.warning("replan 但未提供新步骤，继续执行原计划")
                    return {}

            else:  # action == "continue"
                logger.info("决定继续执行当前计划")
                return {}  # 不修改状态，继续执行

        except Exception as e:
            logger.error(f"重新规划失败: {e}, 继续执行剩余计划")
            return {}

    else:
        # 没有剩余计划，生成最终响应
        logger.info("计划已执行完毕，生成最终响应")
        return await _generate_response(state)


# ------------------------- Skill 验收闭环 -------------------------

# Skill 命中时的默认阈值：单次 Skill 会话最多补 2 个验收采样步骤；
# 命中 Skill 后已执行超过该基线仍未通过验收 → 视为 abandoned，直接 respond。
_SKILL_MAX_EXTRA_STEPS = 2
_SKILL_ABSOLUTE_STEP_CAP = 12


def _skill_verification_decision(
    state: PlanExecuteState, skill_context: dict[str, Any]
) -> dict[str, Any] | None:
    """命中 Skill 时的 Replanner 分支：跑 verifications 决定收敛/补步。

    返回 None 表示不做特殊处理，交给下游默认逻辑（LLM 决策）；
    否则直接作为 replanner 节点返回值。
    """
    plan = state.get("plan", []) or []
    past_steps = state.get("past_steps", []) or []
    skill_id = str(skill_context.get("skill_id") or "")
    verifications = skill_context.get("verifications") or []

    # 计划还没跑完：让 Executor 继续把 skill 主流程步骤走完再判断验收
    if plan:
        return None

    # 绝对上限：Skill 会话总步骤不能无限膨胀，避免病态 verification 永远失败
    if len(past_steps) >= _SKILL_ABSOLUTE_STEP_CAP:
        logger.warning(
            f"Skill '{skill_id}' 已执行 {len(past_steps)} 步，超过硬上限，强制 respond（abandoned）"
        )
        _record_skill_outcome(skill_id, "abandoned")
        return None  # 交给外层原始阈值路径，最终会走 respond

    if not verifications:
        # 无验收清单：按标准路径走 respond
        _record_skill_outcome(skill_id, "success")
        return None

    # 已执行 verification 数（我们把 verification 结果当作 past_steps 的一部分回灌，
    # 通过 description 前缀 "[verify:xxx]" 标识）
    verify_prefix = "[verify]"
    executed_verifications: list[tuple[str, str]] = [
        (desc, result)
        for desc, result in past_steps
        if isinstance(desc, str) and desc.startswith(verify_prefix)
    ]

    render_ctx = build_render_context(
        input_text=state.get("input", ""),
        past_steps=past_steps,
        step_id_to_result=dict(past_steps),
        skill_context=skill_context,
        labels=(
            skill_context.get("labels") if isinstance(skill_context, dict) else None
        ),
    )

    # 每条 verification 至多提交一次；已跑过的按 description 匹配跳过
    pending_verifications = []
    executed_desc_set = {desc for desc, _ in executed_verifications}
    for index, verify in enumerate(verifications):
        desc = f"{verify_prefix} #{index}: {verify.get('description') or verify.get('tool', '')}"
        if desc in executed_desc_set:
            continue
        pending_verifications.append((desc, verify, index))

    # verifications 全部已执行 → 判定 success/pending 补步
    if not pending_verifications:
        passed = 0
        failed_details: list[str] = []
        for index, verify in enumerate(verifications):
            desc = f"{verify_prefix} #{index}: {verify.get('description') or verify.get('tool', '')}"
            result_text = next(
                (result for d, result in executed_verifications if d == desc), ""
            )
            # 直接把工具原始输出当作 result 传给 expr；作者写 expr 时按结构约定
            expr = str(verify.get("success_expr") or "")
            ok = evaluate_success_expr(expr, result_text, render_ctx)
            if ok:
                passed += 1
            else:
                failed_details.append(f"{desc}: expr={expr}")

        if passed == len(verifications):
            logger.info(f"Skill '{skill_id}' 全部 verification 通过，收敛响应")
            _record_skill_outcome(skill_id, "success")
            skill_context["verification_status"] = "passed"
            # 不主动生成响应；返回空 dict 让下游 LLM 决策/或直接 respond
            # 优先走「无剩余计划 → 生成响应」路径
            return None
        # 允许最多 1 轮追补：verifications 已跑完但失败，且步数没超上限
        # 由于此处无法直接产出「补什么步」，交给 LLM 决策路径处理
        logger.warning(
            f"Skill '{skill_id}' 验收失败 {len(failed_details)}/{len(verifications)}；"
            f"允许 LLM 决策补充采样：{failed_details}"
        )
        skill_context["verification_status"] = (
            "partial" if passed > 0 else "failed"
        )
        return None

    # 有未执行的 verification：把它们作为下一批 plan 步骤返回
    extra_step_slots = max(
        _SKILL_MAX_EXTRA_STEPS,
        int(skill_context.get("max_extra_steps", _SKILL_MAX_EXTRA_STEPS) or 0),
    )
    verify_steps: list[dict[str, Any]] = []
    for desc, verify, index in pending_verifications[:extra_step_slots]:
        tool = str(verify.get("tool") or "")
        args_template = verify.get("args") or {}
        rendered_args = render_args(args_template, render_ctx)
        verify_steps.append(
            {
                "step_id": f"sk:verify:{skill_id}:{index}",
                "description": desc,
                "tool_hint": tool,
                "expected_output": verify.get("description") or "",
                "depends_on": [],
                "skill_id": skill_id,
                "skill_step_id": f"verify:{index}",
                "tool_args_template": rendered_args if isinstance(rendered_args, dict) else {},
                "optional": False,
            }
        )

    if not verify_steps:
        return None

    logger.info(
        f"Skill '{skill_id}' 追加 {len(verify_steps)} 个 verification 采样步骤"
    )
    return {"plan": normalize_plan(verify_steps)}


def _record_skill_outcome(skill_id: str, outcome: str) -> None:
    """把 skill outcome 回写 SkillRegistry 度量，异常吞掉不影响主流程。"""
    if not skill_id:
        return
    try:
        from app.agent.skills import get_skill_registry

        get_skill_registry().record_outcome(skill_id, outcome)
    except Exception as exc:
        logger.warning(f"回写 Skill outcome 失败: {exc}")


async def _generate_response(state: PlanExecuteState) -> dict[str, Any]:
    """生成最终响应（完整 Markdown 报告，使用更大的 max_tokens 避免截断）"""
    logger.info("生成最终响应...")

    input_text = state.get("input", "")
    past_steps = state.get("past_steps", [])
    # 优先引用执行存档（完整原文），past_steps 只是压缩视图（P0.4）
    artifacts = state.get("artifacts", {}) or {}

    # DeepSeek 使用 V4 Pro 生成最终报告；工具循环继续使用 Flash 非思考模式。
    # 注意：DeepSeek 思考模式拒绝 tool_choice（HTTP 400 "Thinking mode does not
    # support this tool_choice"），而 structured_output 走 function calling 必须
    # 携带 tool_choice，因此报告生成必须关闭思考模式。
    use_deepseek_pro = config.llm_provider.strip().lower() == "deepseek"
    llm = llm_factory.create_chat_model(
        model=config.llm_reasoning_model if use_deepseek_pro else config.rag_model,
        temperature=0,
        streaming=False,
        max_tokens=4096,
        thinking=False,
    )

    # 格式化执行历史（报告优先使用存档完整原文，还原证据链）
    execution_history = "\n\n".join(
        [
            f"### 步骤: {step}\n**结果:**\n{artifacts.get(step, result)}"
            for step, result in past_steps
        ]
    )

    response_gen = response_prompt | structured_output(llm, Response)

    try:
        messages = [
            ("user", f"原始任务: {input_text}"),
            ("user", f"执行历史:\n{execution_history}"),
            ("user", "请基于以上信息生成全面的最终响应"),
        ]

        response_obj = await response_gen.ainvoke({"messages": messages})

        # 处理返回结果
        if isinstance(response_obj, Response):
            final_response = response_obj.response
        else:
            # 如果返回的是字典
            final_response = response_obj.get("response", "")  # type: ignore

        logger.info(f"最终响应生成完成，长度: {len(final_response)}")

        outcome = _build_diagnosis_outcome(state, final_response)
        return {"response": final_response, "diagnosis_outcome": outcome}

    except Exception as e:
        logger.error(f"生成响应失败: {e}")
        # 生成简单的后备响应
        fallback_response = f"""# 任务执行结果

## 原始任务
{input_text}

## 执行的步骤
{_format_simple_steps(past_steps)}

## 说明
由于系统异常，无法生成完整响应。以上是已收集的信息。
"""
        outcome = _build_diagnosis_outcome(state, fallback_response)
        return {"response": fallback_response, "diagnosis_outcome": outcome}


def _build_diagnosis_outcome(
    state: PlanExecuteState, response_text: str
) -> dict[str, Any]:
    """从执行历史与响应中抽取结构化 DiagnosisOutcome（C3）。

    这一层只做启发式摘要（截断/前缀匹配），避免二次 LLM 调用带来的成本与失败面；
    verification_status 严格来自 Skill 验收路径回写的 skill_context 字段。
    """
    past_steps = state.get("past_steps", []) or []
    skill_context = state.get("skill_context") or {}
    actions_taken = [
        _summarize_step_description(desc)
        for desc, _ in past_steps
        if isinstance(desc, str) and desc.strip()
    ][:10]

    evidence: list[str] = []
    for _, result in past_steps:
        text = str(result or "").strip()
        if not text:
            continue
        evidence.append(text[:200] + ("…" if len(text) > 200 else ""))
        if len(evidence) >= 5:
            break

    # 变更提案标记可能位于步骤描述或执行结果，兼容两种历史记录格式。
    actions_proposed: list[str] = []
    for desc, result in past_steps:
        desc_text = str(desc or "")
        result_text = str(result or "")
        if "[变更提案" not in desc_text and "[变更提案" not in result_text:
            continue
        summary = result_text if "[变更提案" in result_text else desc_text
        actions_proposed.append(summary[:200])
        if len(actions_proposed) >= 5:
            break

    # verification_status 由 Skill 分支回写；未命中 skill 时保持 unknown
    verification_status = "unknown"
    if isinstance(skill_context, dict):
        raw = str(skill_context.get("verification_status") or "").strip().lower()
        if raw in {"passed", "failed", "partial", "unknown"}:
            verification_status = raw

    root_cause = _extract_root_cause(response_text)

    outcome = DiagnosisOutcome(
        root_cause=root_cause,
        evidence=evidence,
        actions_taken=actions_taken,
        actions_proposed=actions_proposed,
        verification_status=verification_status,
    )
    return outcome.model_dump()


def _summarize_step_description(desc: str) -> str:
    text = desc.strip()
    return text if len(text) <= 120 else text[:119] + "…"


def _extract_root_cause(response_text: str) -> str:
    """从 markdown 报告中提取根因段第一句作为 root_cause。"""
    if not response_text:
        return ""
    lines = [line.strip() for line in response_text.splitlines()]
    for idx, line in enumerate(lines):
        if any(marker in line for marker in ("根因", "root cause", "Root Cause")):
            for follow in lines[idx : idx + 4]:
                cleaned = follow.lstrip("#*-> ").strip()
                if cleaned and not cleaned.startswith(("根因", "root cause", "Root Cause")):
                    return cleaned[:200]
            # 只匹配到标题、无正文时也用标题本身
            return line.lstrip("#*-> ").strip()[:200]
    # 找不到显式根因段：取首个非空行前 200 字
    for line in lines:
        if line:
            return line.lstrip("#*-> ").strip()[:200]
    return ""


def _format_simple_steps(past_steps: list) -> str:
    """格式化步骤列表（简单版）"""
    if not past_steps:
        return "无"

    formatted = []
    for i, (step, result) in enumerate(past_steps, 1):
        result_preview = result[:200] + "..." if len(result) > 200 else result
        formatted.append(f"{i}. **{step}**\n   {result_preview}\n")

    return "\n".join(formatted)
