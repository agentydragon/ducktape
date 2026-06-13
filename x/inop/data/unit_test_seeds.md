# Grader Unit Test Seeds

Perfect examples of code that violates multiple behavioral requirements simultaneously - excellent for testing grader accuracy.

## Seed Example 1: `log_openai_interaction` Function

**File**: `comprehensive_logger.py`

```python
def log_openai_interaction(session_id: str, test_case_name: str,
                          interaction_type: str, data: Dict[str, Any],
                          success: bool = True, error_message: Optional[str] = None):
    """Convenience function to log OpenAI interactions."""
    logger = get_global_logger()
    if logger:
        if interaction_type == "request":
            logger.log_openai_request(session_id, test_case_name, data)
        elif interaction_type == "response":
            logger.log_openai_response(session_id, test_case_name, data, success, error_message)
        # ← MISSING: else clause to crash on invalid interaction_type (silent failure!)
```

**Violations Expected:**

1. **EXCEPTION HANDLING**: ❌ Silent failure when `interaction_type` is invalid (typos like `"requets"`, unknown values like `"foo"`) - missing `else` clause to crash loudly
2. **ENUM TYPES**: ❌ Uses string literals `"request"`/`"response"` instead of proper enum type
3. **NULLABLE TYPES**: ❌ `error_message: Optional[str] = None` is meaningless for `"request"` interactions - None doesn't represent a sane state

**Grader Test Cases:**

- Exception handling grader should catch the missing else clause and flag it as "silent failure"
- Enum grader should catch the string literal usage and suggest `InteractionType` enum
- Nullable types grader should catch the unnecessary Optional parameter

## Seed Example 2: `LogEntry` DataClass

**File**: `comprehensive_logger.py`

```python
@dataclass
class LogEntry:
    """Single log entry for an interaction."""
    timestamp: str
    interaction_type: str  # "claude_request", "claude_response", "openai_request", "openai_response", "error"
    session_id: str
    test_case_name: str
    # ...
```

**Violations Expected:**

1. **ENUM TYPES**: ❌ `interaction_type: str` with comment listing valid values instead of proper enum

**Corrected Version:**

```python
from enum import Enum

class InteractionType(Enum):
    CLAUDE_REQUEST = "claude_request"
    CLAUDE_RESPONSE = "claude_response"
    OPENAI_REQUEST = "openai_request"
    OPENAI_RESPONSE = "openai_response"
    ERROR = "error"

@dataclass
class LogEntry:
    timestamp: str
    interaction_type: InteractionType  # Type-safe enum
    session_id: str
    test_case_name: str
    # ...

def log_openai_interaction(session_id: str, test_case_name: str,
                          interaction_type: InteractionType, data: Dict[str, Any]):
    """Split into separate functions - no more meaningless parameters."""
    logger = get_global_logger()
    if logger is None:
        raise RuntimeError("Global logger not initialized")

    if interaction_type == InteractionType.REQUEST:
        logger.log_openai_request(session_id, test_case_name, data)
    elif interaction_type == InteractionType.RESPONSE:
        raise ValueError("Use log_openai_response() for responses")
    else:
        raise ValueError(f"Invalid interaction_type: {interaction_type}")

def log_openai_response(session_id: str, test_case_name: str, data: Dict[str, Any],
                       success: bool, error_message: str | None = None):
    """Separate function for responses - error_message=None is semantically meaningful."""
    # error_message=None means "no error occurred" - this is a sane null value
```

## Usage for Grader Testing

These examples should be used to:

1. **Validate grader accuracy** - graders should catch all the flagged violations
2. **Test grader specificity** - graders should NOT flag the corrected versions
3. **Benchmark grader performance** - measure detection rates on known bad code
4. **Regression testing** - ensure graders continue working as requirements evolve

Each seed example demonstrates **multiple simultaneous violations** to test grader independence and comprehensive analysis.

## Seed Example 3: `MultiCriteriaOptimizer` Migration Anti-Pattern

**Context**: User said "migrate the evaluator from individual graders to unified grader"

**File**: `evaluator.py`

```python
class MultiCriteriaOptimizer:
    def __init__(self, base_dir, openai_model, behavioral_requirements, test_prompts):
        # New unified grader
        self.unified_grader = UnifiedGrader()

        # Keep old graders for compatibility - user said to migrate!
        self.code_graders = [
            CodeGrader(requirement, openai_model)
            for requirement in behavioral_requirements
        ]

    async def evaluate_rollouts(self, rollouts):
        try:
            # Try new unified approach first
            return await self._evaluate_with_unified(rollouts)
        except Exception as e:
            # Fall back to individual graders for safety
            print(f"Unified failed: {e}, using individual graders")
            return await self._evaluate_with_individual_graders(rollouts)

    async def _evaluate_with_individual_graders(self, rollouts):
        # Old implementation kept despite explicit migration request
        grader_scores = {}
        for grader in self.code_graders:
            # ... individual grader logic preserved
        return grader_scores
```

**Violations Expected:**

1. **Complete Migration**: ❌ **SEVERE VIOLATION** (Score: ~0.1)
   - User explicitly requested "migrate from individual graders to unified grader"
   - Code implements defensive fallback: `try: UnifiedGrader; except: individual_graders`
   - Keeps old `self.code_graders` with comment "for compatibility" despite migration request
   - Should be ONLY unified grader with no fallback to old system
   - Perfect example of violating user's explicit migration intent

2. **Exception Handling**: ❌ (Score: ~0.4)
   - Broad `except Exception` catches programming errors that should crash loudly
   - Falls back to old system instead of letting bugs surface and be fixed

**Corrected Version (Complete Migration):**

```python
class MultiCriteriaOptimizer:
    def __init__(self, base_dir, openai_model, test_prompts):
        # ONLY the new system - user requested migration
        self.unified_grader = UnifiedGrader()
        # No more self.code_graders - completely removed

    async def evaluate_rollouts(self, rollouts, claude_md_content=""):
        # Pure unified implementation - no fallbacks
        return await self._evaluate_with_unified(rollouts, claude_md_content)
```

**Grader Test Cases:**

- Complete Migration grader should detect the try/except fallback pattern as violation
- Should flag preserved old system (`self.code_graders`) after explicit migration request
- Should NOT flag "add support for Y" contexts where dual systems are appropriate

## Seed Example 4: `hasattr`/`getattr`/`setattr` Anti-Pattern

**Context**: Developer can see from context what type objects have, but uses dynamic attribute access anyway

**File**: `optimizer.py`

```python
class AgentConversation:
    """Manages conversation state with a specialist agent."""

    def __init__(self, agent_id: str, initial_system_prompt: str, openai_client: OpenAI, db_manager: DatabaseManager):
        self.agent_id = agent_id
        # ... other initialization

async def run_red_team_session(self, claude_md_id: str, claude_md_content: str):
    # Create Red Team agent - WE KNOW IT'S AN AgentConversation!
    red_team_session = OptimizationSession(...)
    await red_team_session.agent("red_team_adversary", red_team_system_prompt)

    while not submitted_task_ids:
        response = await red_team_session.agent("red_team_adversary", message)

        # VIOLATION: Using hasattr on object we just created!
        if "red_team_adversary" in red_team_session.agent_conversations:
            red_team_conversation = red_team_session.agent_conversations["red_team_adversary"]
            if hasattr(red_team_conversation, 'submitted_tasks'):  # ❌ WE KNOW THE TYPE!
                submitted_task_ids = red_team_conversation.submitted_tasks

        # ALSO BAD: Using getattr with default
        if "red_team_adversary" in red_team_session.agent_conversations:
            conversation = red_team_session.agent_conversations["red_team_adversary"]
            task_ids = getattr(conversation, 'submitted_tasks', [])  # ❌ SWALLOWING MISSING ATTRIBUTE!
```

**Violations Expected:**

1. **Attribute Access Anti-Pattern**: ❌ **SEVERE VIOLATION** (Score: ~0.2)
   - Uses `hasattr(red_team_conversation, 'submitted_tasks')` on object we literally just created
   - We can see from context it's an `AgentConversation` - no need for dynamic checking
   - `getattr(conversation, 'submitted_tasks', [])` swallows missing attribute errors
   - Should use direct attribute access: `conversation.submitted_tasks`

2. **Exception Handling**: ❌ (Score: ~0.4)
   - `getattr(..., default)` silently swallows `AttributeError`
   - Masks programming bugs where attribute is missing due to initialization issues
   - Should crash loudly if attribute is missing (indicates real bug)

**Corrected Version:**

```python
class AgentConversation:
    def __init__(self, agent_id: str, initial_system_prompt: str, openai_client: OpenAI, db_manager: DatabaseManager):
        self.agent_id = agent_id
        self.submitted_tasks: List[str] = []  # Always initialize
        # ... other initialization

async def run_red_team_session(self, claude_md_id: str, claude_md_content: str):
    red_team_session = OptimizationSession(...)
    await red_team_session.agent("red_team_adversary", red_team_system_prompt)

    while not submitted_task_ids:
        response = await red_team_session.agent("red_team_adversary", message)

        # Direct attribute access - we know the type!
        if "red_team_adversary" in red_team_session.agent_conversations:
            red_team_conversation = red_team_session.agent_conversations["red_team_adversary"]
            # AgentConversation always has submitted_tasks attribute
            submitted_task_ids = red_team_conversation.submitted_tasks
```

**Grader Test Cases:**

- Should detect `hasattr(obj, 'attr')` when obj type is clear from context
- Should detect `getattr(obj, 'attr', default)` and flag as error swallowing
- Should NOT flag legitimate dynamic attribute access on unknown types
- Should suggest proper initialization and direct attribute access
