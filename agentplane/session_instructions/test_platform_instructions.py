import pytest
import pytest_bazel

from agentplane.session_instructions.platform_instructions import SessionRuntime, render_platform_instructions


@pytest.mark.parametrize("runtime", list(SessionRuntime))
def test_platform_guide_describes_the_selected_runtime(runtime: SessionRuntime) -> None:
    guide = render_platform_instructions(
        egress_api_url="http://test-egress.invalid",
        actions_service_url="http://test-actions.invalid",
        sessions_service_url="http://test-sessions.invalid",
        notifications_service_url="http://test-notifications.invalid",
        session_runtime=runtime,
    )

    assert "Environment Runtime" in guide
    assert "http://test-actions.invalid" in guide
    assert "http://test-sessions.invalid" in guide
    match runtime:
        case SessionRuntime.NATIVE_HARNESS:
            assert "You run inside an Agentplane Sandbox" in guide
            assert "Harness Host\n  runs your native harness" in guide
            assert "outside the Sandbox" not in guide
        case SessionRuntime.CENTRAL_SAMPLER:
            assert "outside the Sandbox" in guide
            assert "You run inside an Agentplane Sandbox" not in guide
            assert "this Session's provider loop and Code Mode host" in guide


if __name__ == "__main__":
    pytest_bazel.main()
