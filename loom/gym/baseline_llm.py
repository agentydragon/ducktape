"""Shared answer-schema + parse library for gym contestants.

A gym question has a strict per-question answer shape (a binary probability, a
quantile vector, or a categorical distribution). This module derives, from each
`Question`, the single source of truth for that shape:

- `question_schema` — the JSON schema a tool/endpoint advertises so the model is
  forced to emit a well-formed answer object;
- `answer_instruction` — the human-readable instruction describing the same shape;
- `parse_answer` — validates a model's structured tool input against that shape
  and builds the scoring `Answer` (which adds the semantic checks).

The agent harness (`inspect_harness.py`) carries `question_schema` as its submit
tool's parameters and parses submissions with `parse_answer`. It was also the
shared core of the now-removed bare one-shot LLM baseline.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, create_model

from loom.gym.scoring import QUANTILE_LEVELS, Answer, BinaryAnswer, CategoricalAnswer, QuantileAnswer
from loom.gym.task import BinaryQuestion, CategoricalQuestion, Question, ScalarQuestion, Task

# extra="forbid" closes every object (additionalProperties: false in the schema);
# the per-question required keys (the fixed quantile levels, the question's
# category set) are real fields, so the Anthropic-shaped API enforces exactly the
# answer shape. Semantic checks (quantiles non-decreasing, probabilities sum to 1)
# stay on the scoring Answer models that parse_answer builds from a validated input.
_FORBID = ConfigDict(extra="forbid")


def _model(name: str, **fields: Any) -> type[BaseModel]:
    # **fields: Any so pydantic's loose create_model overload accepts the
    # (type, FieldInfo) tuples (it types field_definitions as Any | tuple[str, Any]).
    return create_model(name, __config__=_FORBID, **fields)


def _answer_input_model(question: Question) -> type[BaseModel]:
    """Per-question model that is the single source for both the submit tool's
    input schema (`question_schema`) and the shape validation of the model's tool
    call (`parse_answer`). Nested objects use aliased fields so the JSON keys are
    the quantile levels / category names."""
    match question:
        case BinaryQuestion():
            p = Field(gt=0, lt=1, description="Probability the question resolves YES, strictly in (0, 1).")
            return _model("BinaryAnswerInput", p=(float, p))
        case ScalarQuestion(unit=unit):
            quantiles = _model(
                "QuantilesInput",
                **{
                    f"q{index}": (float, Field(alias=str(level), description=f"value at the {level} quantile"))
                    for index, level in enumerate(QUANTILE_LEVELS)
                },
            )
            description = f"your value (in {unit}) at each quantile level, non-decreasing in level"
            return _model("ScalarAnswerInput", quantiles=(quantiles, Field(description=description)))
        case CategoricalQuestion(categories=categories):
            probabilities = _model(
                "ProbabilitiesInput",
                **{
                    f"c{index}": (float, Field(alias=category, ge=0, description=f"probability of {category!r}"))
                    for index, category in enumerate(categories)
                },
            )
            description = "probability for each category; values must sum to 1"
            return _model("CategoricalAnswerInput", probabilities=(probabilities, Field(description=description)))


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline Pydantic's `$defs`/`$ref` into one self-contained schema (merging
    sibling keys like a field description over the referenced def). inspect's
    JSONSchema and a plain Anthropic `input_schema` don't resolve `$ref`, so a
    nested model would otherwise lose its structure."""
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                target = defs[node["$ref"].rsplit("/", 1)[-1]]
                return walk({**target, **{key: value for key, value in node.items() if key != "$ref"}})
            if "allOf" in node and len(node["allOf"]) == 1:
                merged = node["allOf"][0] | {key: value for key, value in node.items() if key != "allOf"}
                return walk(merged)
            return {key: walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return cast("dict[str, Any]", walk(schema))


def question_schema(question: Question) -> dict[str, object]:
    """JSON schema for the answer to `question`, derived from its input model."""
    return _inline_refs(_answer_input_model(question).model_json_schema())


def answer_instruction(question: Question) -> str:
    match question:
        case BinaryQuestion():
            return "answer with p = your probability that the question resolves YES"
        case ScalarQuestion(unit=unit):
            levels = ", ".join(f'"{level}"' for level in QUANTILE_LEVELS)
            return f"answer with your {levels} quantiles for the value in {unit}, non-decreasing in level"
        case CategoricalQuestion():
            return "answer with your probability for each listed category; probabilities must sum to 1"


def parse_answer(task: Task, tool_input: dict[str, object]) -> Answer:
    # Validate the strict per-question shape via the same model the schema came
    # from, then build the scoring Answer (which adds the semantic checks).
    # by_alias dumps back to the wire keys (quantile levels / category names).
    data = _answer_input_model(task.question).model_validate(tool_input).model_dump(by_alias=True)
    match task.question:
        case BinaryQuestion():
            return BinaryAnswer.model_validate(data)
        case ScalarQuestion():
            return QuantileAnswer.model_validate(data)
        case CategoricalQuestion():
            return CategoricalAnswer.model_validate(data)
