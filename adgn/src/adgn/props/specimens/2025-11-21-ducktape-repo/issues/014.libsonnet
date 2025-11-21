local I = import '../../specimens/lib.libsonnet';

// iss-014: on_call_tool should progressively mutate one ToolCallRecord, not construct N independent instances

I.issueOneOccurrence(
  rationale=|||
    The `on_call_tool` method constructs multiple independent `ToolCallRecord` instances with
    verbose redundant field assignments at each state transition. It should instead hold *one*
    `ToolCallRecord` instance and progressively mutate it, saving updates as state changes.

    **Current pattern - multiple redundant constructions:**

    The method constructs ToolCallRecord instances at 8 different locations, each repeating
    the same verbose field assignments:

    **1. Line 150-158: Initial pending record**
    ```python
    pending_record = ToolCallRecord(
        call_id=call_id,
        run_id=str(self._run_id) if self._run_id is not None else None,
        agent_id=self._agent_id,
        tool_call=tool_call,
        decision=None,
        execution=None,
    )
    ```

    **2. Line 180-188: ALLOW path - executing record**
    ```python
    executing_record = ToolCallRecord(
        call_id=call_id,
        run_id=str(self._run_id) if self._run_id is not None else None,
        agent_id=self._agent_id,
        tool_call=ToolCall(name=name, call_id=call_id, args_json=json.dumps(arguments) if arguments else None),
        decision=decision_obj,
        execution=None,
    )
    ```

    **3. Line 195-205: ALLOW path - completed record**
    ```python
    completed_record = ToolCallRecord(
        call_id=call_id,
        run_id=str(self._run_id) if self._run_id is not None else None,
        agent_id=self._agent_id,
        tool_call=ToolCall(name=name, call_id=call_id, args_json=json.dumps(arguments) if arguments else None),
        decision=decision_obj,
        execution=execution_obj,
    )
    ```

    **4. Line 263-271: DENY_ABORT path**
    **5. Line 278-286: DENY_CONTINUE path**
    **6. Line 302-310: ASK/approved - executing record**
    **7. Line 317-327: ASK/approved - completed record**
    **8. Line 339-347: ASK/denied record**

    All repeat the same pattern with redundant field assignments.

    **Why this is problematic:**
    - Massive code duplication (same 4-7 field assignments repeated 8 times)
    - Obscures the actual state transitions (buried in verbose construction)
    - Error-prone: easy to forget updating one construction when fields change
    - Inefficient: creates multiple objects when one would suffice
    - Hard to read: cognitive overhead from repeated verbose patterns
    - Violates DRY principle

    **Recommended approach:**

    Create ONE record at the start and progressively mutate it:

    ```python
    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: CallNext[Any, ToolResult]) -> ToolResult:
        name = context.message.name
        arguments = context.message.arguments
        call_id = "pg:" + uuid.uuid4().hex

        # Create tool call and record ONCE
        tool_call = ToolCall(name=name, call_id=call_id, args_json=json.dumps(arguments) if arguments else None)
        record = ToolCallRecord(
            call_id=call_id,
            run_id=str(self._run_id) if self._run_id is not None else None,
            agent_id=self._agent_id,
            tool_call=tool_call,
            decision=None,
            execution=None,
        )

        # Save initial PENDING state
        await self._persistence.save_tool_call(record)

        # ... evaluate policy decision ...

        if decision is ApprovalDecision.ALLOW:
            # Update decision, save EXECUTING state
            record.decision = Decision(outcome=ApprovalOutcome.POLICY_ALLOW, decided_at=_now(), reason=rationale)
            await self._persistence.save_tool_call(record)

            try:
                call_result = await call_next(context)

                # Update execution, save COMPLETED state
                record.execution = ToolCallExecution(completed_at=_now(), output=convert_fastmcp_result(call_result))
                await self._persistence.save_tool_call(record)

                # ... handle reserved codes ...
                return call_result
            except McpError as e:
                _raise_if_reserved_code(e, name)
                raise

        if decision is ApprovalDecision.DENY_ABORT:
            # Update decision, save DENIED state
            record.decision = Decision(outcome=ApprovalOutcome.POLICY_DENY_ABORT, decided_at=_now(), reason=rationale)
            await self._persistence.save_tool_call(record)
            raise _policy_denied_error(ApprovalDecision.DENY_ABORT, name, rationale)

        # ... similar pattern for other branches ...
    ```

    **Benefits:**
    - Single source of truth for record fields
    - Clear state transitions (just update what changed)
    - Less code (eliminates ~100 lines of redundancy)
    - Easier to maintain (field changes in one place)
    - Clearer intent (mutations show what actually changed)
    - More efficient (one object, multiple updates)

    **Implementation notes:**
    - ToolCallRecord needs to be mutable (dataclass with frozen=False, or Pydantic with frozen=False)
    - Each save updates the same record reference
    - State transitions become obvious: set .decision, save; set .execution, save
  |||,
  properties=['dry-principle', 'duplication', 'maintainability', 'clarity'],
  filesToRanges={
    'adgn/src/adgn/mcp/policy_gateway/middleware.py': [
      [142, 358],  // Entire on_call_tool method
      [150, 158],  // Construction 1: pending_record
      [180, 188],  // Construction 2: ALLOW executing_record
      [195, 205],  // Construction 3: ALLOW completed_record
      [263, 271],  // Construction 4: DENY_ABORT denied_record
      [278, 286],  // Construction 5: DENY_CONTINUE denied_record
      [302, 310],  // Construction 6: ASK/approved executing_record
      [317, 327],  // Construction 7: ASK/approved completed_record
      [339, 347],  // Construction 8: ASK/denied denied_record
    ],
  },
)
