from __future__ import annotations

from adgn.agent.agent import MiniCodex
from adgn.agent.reducer import AutoHandler
from adgn.mcp._shared.naming import build_mcp_function
from adgn.openai_utils.model import FunctionCallItem, FunctionCallOutputItem, ReasoningItem
from tests.llm.support.openai_mock import FakeOpenAIModel


async def test_reasoning_threading_filters_reasoning_from_next_input(
    reasoning_model: str, responses_factory, make_pg_compositor_echo
) -> None:
    """Test that reasoning items are properly threaded with their function calls across turns."""

    # Create function calls with explicit id and status to verify preservation
    fc1 = FunctionCallItem(
        name=build_mcp_function("echo", "echo"),
        arguments='{"text": "hi"}',
        call_id="call_1",
        id="fc_id_1",  # Must be preserved
        status="completed",  # Must be preserved
    )
    fc2 = FunctionCallItem(
        name=build_mcp_function("echo", "echo"),
        arguments='{"text": "bye"}',
        call_id="call_2",
        id="fc_id_2",
        status="in_progress",
    )

    # Multi-turn sequence to test proper threading:
    # Turn 1: reasoning + tool call
    # Turn 2: another reasoning + tool call (should include Turn 1's complete sequence)
    # Turn 3: final assistant message (should include both previous sequences)
    seq = [
        # Turn 1: reasoning + tool call
        responses_factory.make(responses_factory.make_item_reasoning(id="rs_turn1"), fc1),
        # Turn 2: another reasoning + tool call
        responses_factory.make(responses_factory.make_item_reasoning(id="rs_turn2"), fc2),
        # Turn 3: final message
        responses_factory.make_assistant_message("done"),
    ]
    client = FakeOpenAIModel(seq)

    async with make_pg_compositor_echo() as (mcp_client, _comp):
        agent = await MiniCodex.create(
            model=responses_factory.model, mcp_client=mcp_client, system="test", client=client, handlers=[AutoHandler()]
        )

        res = await agent.run("say hi")

    # Assertions
    assert res.text.strip() == "done"
    assert client.calls == 3, f"Expected 3 API calls, got {client.calls}"

    # Helper to get item types and match by id/call_id
    def get_sequence_summary(items):
        """Get a concise summary of item types with their IDs."""
        result = []
        for item in items:
            name = type(item).__name__
            if hasattr(item, "id") and item.id:
                result.append(f"{name}(id={item.id})")
            elif hasattr(item, "call_id") and item.call_id:
                result.append(f"{name}(call_id={item.call_id})")
            else:
                result.append(name)
        return result

    # Turn 1: [UserMessage, SystemMessage]
    turn1_input = list(client.captured[0].input or [])
    turn1_types = get_sequence_summary(turn1_input)
    assert "UserMessage" in turn1_types, f"Turn 1 missing UserMessage: {turn1_types}"
    assert "ReasoningItem" not in [type(i).__name__ for i in turn1_input], "Turn 1 shouldn't have reasoning"

    # Turn 2: [UserMessage, SystemMessage, ReasoningItem(rs_turn1), FunctionCallItem(call_1), FunctionCallOutputItem(call_1)]
    turn2_input = list(client.captured[1].input or [])
    turn2_types = get_sequence_summary(turn2_input)

    # Find and verify Turn 1's sequence
    ri1_idx = next(
        (i for i, item in enumerate(turn2_input) if isinstance(item, ReasoningItem) and item.id == "rs_turn1"), None
    )
    assert ri1_idx is not None, f"Turn 2 missing ReasoningItem(rs_turn1): {turn2_types}"

    fc1_item = turn2_input[ri1_idx + 1]
    assert isinstance(fc1_item, FunctionCallItem), (
        f"Turn 2: ReasoningItem not followed by FunctionCallItem: {turn2_types}"
    )
    fc1 = fc1_item
    # Verify call_id, id and status all preserved
    assert (fc1.call_id, fc1.id, fc1.status) == ("call_1", "fc_id_1", "completed"), (
        f"Turn 2: FC1 fields not preserved: call_id={fc1.call_id}, id={fc1.id}, status={fc1.status}, types={turn2_types}"
    )

    fco1 = turn2_input[ri1_idx + 2]
    assert isinstance(fco1, FunctionCallOutputItem), (
        f"Turn 2: FunctionCallItem not followed by FunctionCallOutputItem: {turn2_types}"
    )
    assert fco1.call_id == "call_1", f"Turn 2: Expected call_1, got {fco1.call_id}: {turn2_types}"

    # Turn 3: [UserMessage, SystemMessage, RI1, FC1, function_call_output1, RI2, FC2, function_call_output2]
    turn3_input = list(client.captured[2].input or [])
    turn3_types = get_sequence_summary(turn3_input)

    # Verify Turn 1's sequence still intact
    ri1_idx = next(
        (i for i, item in enumerate(turn3_input) if isinstance(item, ReasoningItem) and item.id == "rs_turn1"), None
    )
    assert ri1_idx is not None, f"Turn 3 missing ReasoningItem(rs_turn1): {turn3_types}"

    fc1_item = turn3_input[ri1_idx + 1]
    assert isinstance(fc1_item, FunctionCallItem)
    fc1 = fc1_item
    assert (fc1.call_id, fc1.id) == ("call_1", "fc_id_1")
    fco1 = turn3_input[ri1_idx + 2]
    assert isinstance(fco1, FunctionCallOutputItem)
    assert fco1.call_id == "call_1"

    # Verify Turn 2's sequence
    ri2_idx = next(
        (i for i, item in enumerate(turn3_input) if isinstance(item, ReasoningItem) and item.id == "rs_turn2"), None
    )
    assert ri2_idx is not None, f"Turn 3 missing ReasoningItem(rs_turn2): {turn3_types}"

    fc2_item = turn3_input[ri2_idx + 1]
    assert isinstance(fc2_item, FunctionCallItem), f"Turn 3: RI2 not followed by FunctionCallItem: {turn3_types}"
    fc2 = fc2_item
    # Verify call_id, id and status all preserved
    assert (fc2.call_id, fc2.id, fc2.status) == ("call_2", "fc_id_2", "in_progress"), (
        f"Turn 3: FC2 fields not preserved: call_id={fc2.call_id}, id={fc2.id}, status={fc2.status}, types={turn3_types}"
    )

    fco2 = turn3_input[ri2_idx + 2]
    assert isinstance(fco2, FunctionCallOutputItem)
    assert fco2.call_id == "call_2"
