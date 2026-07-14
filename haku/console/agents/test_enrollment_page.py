from __future__ import annotations

from dataclasses import replace

import pytest
import pytest_bazel
from bs4 import BeautifulSoup

from haku.console.agents.enrollment_page import (
    AGENT_NAME_MAX_LENGTH,
    AgentEnrollmentPageView,
    ReconnectAgentView,
    render_agent_enrollment_page,
)

_CSP_NONCE = "test_nonce_0123456789abcdef0123456789abcdef"


def _view() -> AgentEnrollmentPageView:
    return AgentEnrollmentPageView(
        create_form_action="/auth/agent-enrollment/interaction-123/new",
        reconnect_form_action="/auth/agent-enrollment/interaction-123/reconnect",
        deny_form_action="/auth/agent-enrollment/interaction-123/deny",
        form_token="form-token",
        operator_display_name="Rai",
        client_software="Claude.ai",
        redirect_host="claude.ai",
        scopes=("mcp:tools", "offline_access"),
        suggested_agent_name="Kitchen Claude",
        reconnect_agents=(
            ReconnectAgentView(agent_id="agent-1", display_name="Desk Claude"),
            ReconnectAgentView(agent_id="agent-2", display_name="Travel Claude"),
        ),
    )


def test_enrollment_page_presents_new_reconnect_and_deny_actions() -> None:
    response = render_agent_enrollment_page(_view(), csp_nonce=_CSP_NONCE)
    page = BeautifulSoup(response.body, "html.parser")

    assert response.status_code == 200
    assert page.title is not None
    assert page.title.string == "Connect an agent · Haku"
    heading = page.find("h1")
    intro = page.find(class_="intro")
    delegation = page.find(class_="delegation")
    assert heading is not None
    assert intro is not None
    assert delegation is not None
    assert heading.get_text(strip=True) == "Connect an agent"
    assert "Claude.ai wants to connect an MCP agent" in intro.get_text(" ", strip=True)
    assert "use tools as Rai" in intro.get_text(" ", strip=True)
    assert "auto-approval policy" in delegation.get_text(" ", strip=True)

    forms = page.find_all("form")
    assert len(forms) == 3
    assert [form["action"] for form in forms] == [
        "/auth/agent-enrollment/interaction-123/new",
        "/auth/agent-enrollment/interaction-123/reconnect",
        "/auth/agent-enrollment/interaction-123/deny",
    ]
    assert [form.find("input", attrs={"name": "form_token"})["value"] for form in forms] == [
        "form-token",
        "form-token",
        "form-token",
    ]

    create_form = forms[0]
    name_input = create_form.find("input", attrs={"name": "agent_name"})
    assert name_input is not None
    assert name_input["required"] == ""
    assert name_input["maxlength"] == str(AGENT_NAME_MAX_LENGTH)
    assert name_input["value"] == "Kitchen Claude"

    reconnect_form = forms[1]
    assert [option["value"] for option in reconnect_form.find_all("option")] == ["agent-1", "agent-2"]
    assert [option.get_text(strip=True) for option in reconnect_form.find_all("option")] == [
        "Desk Claude",
        "Travel Claude",
    ]
    deny_button = forms[2].find("button")
    assert deny_button is not None
    assert deny_button["formnovalidate"] == ""

    details_element = page.find("details")
    assert details_element is not None
    details = details_element.get_text(" ", strip=True)
    assert "Signed in as Rai" in details
    assert "Client software Claude.ai" in details
    assert "Redirect host claude.ai" in details
    assert "mcp:tools" in details
    assert "offline_access" in details


def test_enrollment_page_autoescapes_every_untrusted_value_and_locks_down_browser() -> None:
    hostile = '<script>alert("cookie")</script><img src=x onerror="steal()">'
    response = render_agent_enrollment_page(
        replace(
            _view(),
            operator_display_name=hostile,
            form_token=hostile,
            client_software=hostile,
            redirect_host=hostile,
            scopes=(hostile,),
            suggested_agent_name=hostile,
            reconnect_agents=(ReconnectAgentView(agent_id=hostile, display_name=hostile),),
            error=hostile,
        ),
        csp_nonce=_CSP_NONCE,
        status_code=422,
    )
    body = response.body.decode()
    page = BeautifulSoup(body, "html.parser")
    csp = response.headers["Content-Security-Policy"]

    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Permissions-Policy"] == "geolocation=(), display-capture=()"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert csp == (
        "default-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
        f"script-src 'none'; style-src 'nonce-{_CSP_NONCE}'"
    )
    assert "unsafe-inline" not in csp
    assert f'<style nonce="{_CSP_NONCE}">' in body
    assert hostile not in body
    assert "&lt;script&gt;alert" in body
    assert page.find("script") is None
    assert page.find("img") is None
    assert page.find("link") is None
    error = page.find(class_="error")
    option = page.find("option")
    assert error is not None
    assert option is not None
    assert error.get_text() == hostile
    assert option.get_text() == hostile
    assert option["value"] == hostile
    assert all(form.find("input", attrs={"name": "form_token"})["value"] == hostile for form in page.find_all("form"))


def test_enrollment_page_explains_when_reconnect_is_unavailable() -> None:
    page = BeautifulSoup(
        render_agent_enrollment_page(replace(_view(), reconnect_agents=()), csp_nonce=_CSP_NONCE).body, "html.parser"
    )

    assert len(page.find_all("form")) == 2
    assert page.find("select", attrs={"name": "agent_id"}) is None
    unavailable = page.find(class_="unavailable")
    assert unavailable is not None
    assert unavailable.get_text(strip=True) == "You do not have an existing agent available to reconnect."


def test_enrollment_page_rejects_a_nonce_that_could_inject_csp() -> None:
    with pytest.raises(ValueError, match="URL-safe base64"):
        render_agent_enrollment_page(_view(), csp_nonce="nonce'; script-src 'unsafe-inline'")


if __name__ == "__main__":
    pytest_bazel.main()
