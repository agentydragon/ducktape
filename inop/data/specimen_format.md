# Test Specimen Format

Structured format for code specimens that can be used to:

1. **Unit test grader accuracy** - validate graders catch known violations
2. **Seed case generation** - elicit specific bad behaviors from Claude agents

## Specimen Structure

```python
@dataclass
class TestSpecimen:
    """A code specimen with expected grader behavior."""

    # Identity
    id: str                    # Unique specimen ID (e.g., "silent_failure_001")
    name: str                  # Human-readable name
    description: str           # What this specimen demonstrates

    # Code content
    code: str                  # The actual code to analyze
    context: str              # Additional context/setup code if needed

    # Expected violations
    expected_violations: List[SpecimenViolation]

    # Seed case usage
    prompt_template: Optional[str]  # Template for generating similar bad code
    variation_hints: List[str]      # Ways to vary this specimen for seeds

@dataclass
class SpecimenViolation:
    """Expected violation that graders should detect."""

    grader_id: str            # Which grader should catch this (e.g., "exception_handling")
    violation_type: str       # Type of violation (e.g., "silent_failure")
    severity: str            # "critical", "major", "minor"

    # Location information
    line_range: Optional[Tuple[int, int]]  # Which lines contain the violation
    pattern: str             # Description of the problematic pattern

    # Expected grader response
    should_detect: bool      # Should this grader flag this violation?
    expected_score_range: Tuple[float, float]  # Expected score range (e.g., (0.0, 0.3))
    expected_keywords: List[str]  # Keywords that should appear in feedback
```

## Usage Modes

### Mode A: Grader Unit Testing

```python
# Test that graders correctly identify violations
specimen = TestSpecimen(
    id="silent_failure_001",
    code="if x == 'request': handle_request()\nelif x == 'response': handle_response()",
    expected_violations=[
        SpecimenViolation(
            grader_id="exception_handling",
            violation_type="silent_failure",
            should_detect=True,
            expected_score_range=(0.0, 0.4),
            expected_keywords=["silent", "missing else", "invalid input"]
        )
    ]
)

# Run grader and validate results
result = grader.grade_specimen(specimen)
assert result.score in specimen.expected_violations[0].expected_score_range
assert any(keyword in result.feedback for keyword in specimen.expected_violations[0].expected_keywords)
```

### Mode B: Seed Case Generation

```python
# Use specimen as template to generate prompts that elicit bad behavior
specimen = TestSpecimen(
    id="enum_string_literals_001",
    prompt_template="""Create a {system_type} that handles different {operation_types}.
    Use if/elif statements to branch on the operation type.""",
    variation_hints=[
        "system_type: logging system, payment processor, file handler",
        "operation_types: request types, status codes, error categories"
    ]
)

# Generate test prompts that likely produce enum violations
test_prompts = generate_test_prompts_from_specimen(specimen)
# Result: "Create a logging system that handles different request types..."
```

## Specimen Categories

### 1. Exception Handling Specimens

- Silent failures (missing else clauses)
- Broad exception catching
- Fallback returns that hide errors
- Test code that swallows exceptions

### 2. Enum Type Specimens

- String literals instead of enums
- Magic constants
- Inconsistent value representations

### 3. Nullable Type Specimens

- Unnecessary Optional parameters
- Missing Optional where None is meaningful
- Unclear null semantics

### 4. Configuration Hierarchy Specimens

- Multiple levels setting defaults
- Scattered configuration values
- Implicit vs explicit configuration

## Specimen Database Structure

```
specimens/
├── exception_handling/
│   ├── silent_failure_001.json
│   ├── broad_catch_001.json
│   └── fallback_return_001.json
├── enum_types/
│   ├── string_literals_001.json
│   └── magic_constants_001.json
├── nullable_types/
│   ├── unnecessary_optional_001.json
│   └── missing_optional_001.json
└── multi_violation/
    ├── log_function_001.json    # Violates 3 requirements
    └── config_class_001.json    # Violates 2 requirements
```

## JSON Format Example

```json
{
  "id": "silent_failure_001",
  "name": "Missing Else Clause in String Branch",
  "description": "Function that silently ignores invalid string parameters",

  "code": "def log_openai_interaction(session_id: str, interaction_type: str, data: dict):\n    logger = get_logger()\n    if logger:\n        if interaction_type == \"request\":\n            logger.log_request(session_id, data)\n        elif interaction_type == \"response\":\n            logger.log_response(session_id, data)\n        # Missing else clause - silent failure!",

  "context": "# This is part of a logging utility\nfrom typing import Dict, Any\n\ndef get_logger(): pass",

  "expected_violations": [
    {
      "grader_id": "exception_handling",
      "violation_type": "silent_failure",
      "severity": "critical",
      "line_range": [4, 8],
      "pattern": "Missing else clause for invalid input handling",
      "should_detect": true,
      "expected_score_range": [0.0, 0.3],
      "expected_keywords": ["silent", "missing else", "invalid", "crash"]
    },
    {
      "grader_id": "enum_types",
      "violation_type": "string_literals",
      "severity": "major",
      "line_range": [5, 7],
      "pattern": "String literals used instead of enum",
      "should_detect": true,
      "expected_score_range": [0.0, 0.5],
      "expected_keywords": ["enum", "string literal", "magic string"]
    }
  ],

  "prompt_template": "Create a {system_type} function that processes different {item_types}. The function should handle {item_type1} and {item_type2} differently.",

  "variation_hints": [
    "system_type: logging, monitoring, processing, handling",
    "item_types: request types, message types, event types, command types",
    "item_type1: requests, events, commands, inputs",
    "item_type2: responses, notifications, outputs, results"
  ]
}
```

## Implementation Plan

1. **Create specimen database** with known violations
2. **Build specimen loader** to parse JSON specimens
3. **Implement grader testing harness** using specimens
4. **Create seed prompt generator** from specimen templates
5. **Add specimen collection tools** to capture new violations from optimization runs

This gives us both validation of grader accuracy AND a systematic way to generate challenging test cases for the optimization loop.
