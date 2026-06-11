#!/usr/bin/env python3
"""
Template system for generating behavioral requirements with minimal duplication.

Eliminates ~90% of boilerplate by using standardized templates while allowing
each requirement to specify only its unique characteristics.
"""

from dataclasses import dataclass
from typing import Any

from claude.claude_optimizer.generic_graders import BehavioralRequirement


@dataclass
class RequirementSpec:
    """Specification for a behavioral requirement with only unique elements."""

    id: str
    name: str
    description: str
    evaluation_criteria: str
    problematic_patterns: list[str]
    good_patterns: list[str]
    problem_fields: dict[str, Any]  # field_name -> schema definition (can be string or dict)
    extra_response_fields: dict[str, Any] | None = None  # Additional schema fields


def create_behavioral_requirement(spec: RequirementSpec) -> BehavioralRequirement:
    """Generate a complete BehavioralRequirement from a minimal spec."""

    # Generate standardized prompt template
    problematic_list = "\n".join(f"{i + 1}. {pattern}" for i, pattern in enumerate(spec.problematic_patterns))
    good_list = "\n".join(f"- {pattern}" for pattern in spec.good_patterns)

    function_name = f"analyze_{spec.id}"

    prompt_template = f"""Analyze this code for {spec.name.lower()} anti-patterns:

**Generated Code:**
```python
{{code}}
```

**Original Task:** {{task_prompt}}
**Claude Instructions:** {{claude_md_content}}

**Behavioral Requirement:** {{requirement_description}}
**Evaluation Criteria:** {{evaluation_criteria}}

Look for these PROBLEMATIC patterns:
{problematic_list}

**GOOD patterns:**
{good_list}

Call the {function_name} function with your analysis."""

    # Generate standardized function schema
    problem_properties = {}
    problem_required = []

    for field_name, field_spec in spec.problem_fields.items():
        if isinstance(field_spec, str):
            # Simple string description -> string field
            problem_properties[field_name] = {"type": "string", "description": field_spec}
        else:
            # Full schema specification
            problem_properties[field_name] = field_spec
        problem_required.append(field_name)

    # Base schema properties that all requirements share
    base_properties = {
        "has_problems": {"type": "boolean", "description": f"Whether the code contains {spec.name.lower()} problems"},
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": problem_properties,
                "required": problem_required,
                "additionalProperties": False,
            },
        },
        "assessment": {"type": "string", "description": f"Brief assessment of the {spec.name.lower()} quality"},
        "score": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": f"Quality score from 0.0 to 1.0, where 1.0 = perfect {spec.name.lower()}",
        },
    }

    # Add any extra response fields specific to this requirement
    if spec.extra_response_fields:
        base_properties.update(spec.extra_response_fields)

    # Base required fields
    base_required = ["has_problems", "problems", "assessment", "score"]
    if spec.extra_response_fields:
        base_required.extend(spec.extra_response_fields.keys())

    function_schema = {
        "name": function_name,
        "description": f"Analyze Python code for {spec.name.lower()} anti-patterns",
        "parameters": {
            "type": "object",
            "properties": base_properties,
            "required": base_required,
            "additionalProperties": False,
        },
        "strict": True,
    }

    return BehavioralRequirement(
        id=spec.id,
        name=spec.name,
        description=spec.description,
        evaluation_criteria=spec.evaluation_criteria,
        analysis_prompt_template=prompt_template,
        function_schema=function_schema,
    )
