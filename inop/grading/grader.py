"""General grading system that handles different grading strategies."""

import json
from datetime import UTC, datetime
from typing import Any

from inop.config import OptimizerConfig
from inop.engine.models import (
    ComparisonGrading,
    Criterion,
    FileBasedGrading,
    Grade,
    GradingContext,
    MessageBasedGrading,
    Rollout,
    RunnerEnvironment,
    ScoreWithRationale,
    TaskDefinition,
)
from inop.grading.strategies import ComparisonGradingStrategy, GradingStrategy, create_grading_strategy
from inop.io.logging_utils import DualOutputLogging
from openai_utils.model import (
    FunctionCallItem,
    FunctionToolParam,
    OpenAIModelProto,
    ResponsesRequest,
    ToolChoiceFunction,
)
from openai_utils.types import build_reasoning_params

logger = DualOutputLogging.get_logger()


async def grade_rollout(
    rollout: Rollout,
    task: TaskDefinition,
    grading_config: FileBasedGrading | ComparisonGrading | MessageBasedGrading,
    model: OpenAIModelProto,
    cfg: OptimizerConfig,
    environment: RunnerEnvironment | None = None,
) -> Grade:
    """Grade a rollout using the appropriate strategy."""
    context = GradingContext(rollout=rollout, task=task, environment=environment)

    # Note: We don't pass config_path since criteria are already resolved in grading_config
    strategy = create_grading_strategy(grading_config)

    artifacts = strategy.collect_artifacts(context)
    prepared = strategy.prepare_for_grader(artifacts, cfg)

    # Handle different grading types
    if isinstance(strategy, ComparisonGradingStrategy):
        # Special handling for code review comparison
        return await _grade_comparison(task=task, prepared=prepared, model=model, cfg=cfg, rollout=rollout)
    # File-based or message-based grading
    criteria = prepared.get("criteria", [])
    return await _grade_with_criteria(
        task=task, prepared=prepared, criteria=criteria, model=model, cfg=cfg, rollout=rollout, strategy=strategy
    )


async def _grade_comparison(
    task: TaskDefinition, prepared: dict[str, Any], model: OpenAIModelProto, cfg: OptimizerConfig, rollout: Rollout
) -> Grade:
    """Grade by comparing agent output to reference (for code reviews).

    Returns a Grade with a single 'overall' axis containing the coverage percentage.
    """
    agent_output = prepared["agent_output"]
    reference = prepared["reference"]

    # Create comparison prompt for code review
    prompt = f"""You are tasked with comparing two code reviews to determine coverage.

Task given to the agent:
{task.prompt}

Review to grade (from agent):
{agent_output}

Reference review (expected issues):
{reference}

Analyze how many issues from the reference review were caught by the agent's review.
- Assign partial credit if the agent identifies the same issue but describes it differently
- Do NOT penalize for false positives (issues the agent found that aren't in reference)
- Focus on whether the core problem was identified, not exact wording

Return a JSON object with:
- covered_percent: percentage of reference issues caught (0-100)
- rationale: explanation of what was caught and what was missed
"""

    # Define the grading tool for comparison
    grading_tool = FunctionToolParam(
        name="submit_comparison_grade",
        description="Submit the coverage comparison results",
        parameters={
            "type": "object",
            "properties": {
                "covered_percent": {
                    "type": "number",
                    "description": "Percentage of reference issues caught (0-100)",
                    "minimum": 0,
                    "maximum": 100,
                },
                "rationale": {"type": "string", "description": "Explanation of what was caught and missed"},
            },
            "required": ["covered_percent", "rationale"],
            "additionalProperties": False,
        },
        strict=True,
    )

    req = ResponsesRequest(
        input=prompt,
        tools=[grading_tool],
        tool_choice=ToolChoiceFunction(name="submit_comparison_grade"),
        reasoning=build_reasoning_params(cfg.grader.reasoning_effort),
    )

    response = await model.responses_create(req)

    # Extract the function call from response
    call: FunctionCallItem | None = None
    for item in response.output:
        if isinstance(item, FunctionCallItem):
            call = item
            break

    if not call or call.name != "submit_comparison_grade":
        logger.error("Grader did not return expected comparison function call", task_id=task.id)
        raise RuntimeError("Grader did not return expected comparison function call")

    # Parse the grading result
    try:
        parsed = json.loads(call.arguments or "{}")
    except json.JSONDecodeError as e:
        logger.error("Failed to parse comparison grading", task_id=task.id, error=str(e))
        raise RuntimeError("Failed to parse comparison grading") from e

    # Convert percentage to 0-10 scale for consistency with other grades
    score = parsed["covered_percent"] / 10.0

    # Create Grade object with just the overall score
    return Grade(
        task_prompt=task.prompt,
        task_id=task.id,
        agent_id=rollout.agent_id,
        axes={"overall": ScoreWithRationale(score=score, rationale=parsed["rationale"])},
        timestamp=datetime.now(UTC),
    )


async def _grade_with_criteria(
    task: TaskDefinition,
    prepared: dict[str, Any],
    criteria: list[Criterion],
    model: OpenAIModelProto,
    cfg: OptimizerConfig,
    rollout: Rollout,
    strategy: GradingStrategy,
) -> Grade:
    """Grade using specific criteria (file-based or message-based).

    This is the traditional grading approach with multiple criteria axes.
    """
    # Build the grading tool schema
    properties: dict[str, Any] = {}
    required_keys: list[str] = []

    for crit in criteria:
        properties[crit.name] = {
            "type": "object",
            "description": crit.description,
            "properties": {"score": {"type": "number", "minimum": 0, "maximum": 10}, "rationale": {"type": "string"}},
            "required": ["score", "rationale"],
            "additionalProperties": False,
        }
        required_keys.append(crit.name)

    # Always include overall
    properties["overall"] = {
        "type": "object",
        "description": "Overall assessment of the solution, score from 0 to 10",
        "properties": {"score": {"type": "number", "minimum": 0, "maximum": 10}, "rationale": {"type": "string"}},
        "required": ["score", "rationale"],
        "additionalProperties": False,
    }
    required_keys.append("overall")

    grading_tool = FunctionToolParam(
        name="submit_grades",
        description="Return scores and rationales for each grading criterion",
        parameters={
            "type": "object",
            "properties": properties,
            "required": required_keys,
            "additionalProperties": False,
        },
        strict=True,
    )

    # Generate the grading prompt using the strategy
    prompt = strategy.get_grading_prompt(prepared, task)

    # Add criteria descriptions
    criteria_text = "\n\n".join([f"- {crit.name}: {crit.description}" for crit in criteria])

    full_prompt = f"{prompt}\n\nGrade the solution on these criteria:\n{criteria_text}"

    req = ResponsesRequest(
        input=full_prompt,
        tools=[grading_tool],
        tool_choice=ToolChoiceFunction(name="submit_grades"),
        reasoning=build_reasoning_params(cfg.grader.reasoning_effort),
    )

    response = await model.responses_create(req)

    # Extract the function call (accept SDK or adapter types)
    call = None
    for item in response.output:
        if isinstance(item, FunctionCallItem):
            call = item
            break

    if not call or call.name != "submit_grades":
        logger.error("Grader did not return expected grades function call", task_id=task.id)
        raise RuntimeError("Grader did not return expected grades function call")

    # Parse the grades
    try:
        # Adapter-only: SDK types are not accepted in this layer
        assert isinstance(call, FunctionCallItem), (
            f"Unexpected function call item type: {type(call).__name__}. "
            "Only adapter FunctionCallItem is supported in this layer."
        )
        arguments_str = call.arguments or ""
        parsed = json.loads(arguments_str or "{}")
    except json.JSONDecodeError as e:
        logger.error("Failed to parse grades", task_id=task.id, error=str(e))
        raise RuntimeError("Failed to parse grades") from e

    # Build Grade object
    axes = {}
    for facet, data in parsed.items():
        axes[facet] = ScoreWithRationale(score=data["score"], rationale=data["rationale"])

    return Grade(
        task_prompt=task.prompt, task_id=task.id, agent_id=rollout.agent_id, axes=axes, timestamp=datetime.now(UTC)
    )
