from __future__ import annotations

import pytest
from hamcrest import all_of, assert_that, has_properties, instance_of

from adgn.agent.loop_control import Auto, Continue
from adgn.agent.reducer import BaseHandler, Reducer


class _SkipHandler(BaseHandler):
    def __init__(self, *, skip: bool, inserts: tuple | None = None, policy: Auto | None = None) -> None:
        self._skip = skip
        self._inserts = inserts or ()
        self._policy = policy or Auto()

    def on_before_sample(self):
        return Continue(self._policy, inserts_input=self._inserts, skip_sampling=self._skip)


def test_all_continue_same_policy_skip_true_merge():
    m1 = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "a"}]}
    m2 = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "b"}]}
    ctrl = Reducer([_SkipHandler(skip=True, inserts=(m1,)), _SkipHandler(skip=True, inserts=(m2,))])
    assert_that(
        ctrl.on_before_sample(),
        all_of(
            instance_of(Continue),
            has_properties(tool_policy=instance_of(Auto), skip_sampling=True, inserts_input=(m1, m2)),
        ),
    )


def test_all_continue_same_policy_no_skip_merge():
    m1 = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x"}]}
    m2 = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "y"}]}
    ctrl = Reducer([_SkipHandler(skip=False, inserts=(m1,)), _SkipHandler(skip=False, inserts=(m2,))])
    assert_that(
        ctrl.on_before_sample(),
        all_of(
            instance_of(Continue),
            has_properties(tool_policy=instance_of(Auto), skip_sampling=False, inserts_input=(m1, m2)),
        ),
    )


def test_mixed_skip_sampling_conflict_raises():
    ctrl = Reducer([_SkipHandler(skip=True), _SkipHandler(skip=False)])
    with pytest.raises(RuntimeError):
        _ = ctrl.on_before_sample()
