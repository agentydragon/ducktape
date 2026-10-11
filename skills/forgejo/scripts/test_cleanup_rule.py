from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import pytest_bazel

from skills.forgejo.scripts.cleanup_rule import Rule, parse_rule_form, reconcile


def desired() -> Rule:
    return Rule(
        enabled=False,
        type="container",
        keep_count=10,
        keep_pattern="",
        remove_days=30,
        remove_pattern=r"^devel-[0-9]{14}-[0-9a-f]{7}$",
        match_full_name=False,
    )


def edit_page(rule: Rule, rule_id: int) -> str:
    return f"""
        <form action="/user/settings/packages/rules/{rule_id}" method="post">
          <input type="hidden" name="id" value="{rule_id}">
          <input type="checkbox" name="enabled" {"checked" if rule.enabled else ""}>
          <select name="type"><option value="generic">Generic</option><option value="container" selected>Container</option></select>
          <input type="checkbox" name="match_full_name" {"checked" if rule.match_full_name else ""}>
          <select name="keep_count"><option value="0">0</option><option value="{rule.keep_count}" selected>{rule.keep_count}</option></select>
          <input name="keep_pattern" value="{rule.keep_pattern}">
          <select name="remove_days"><option value="0">0</option><option value="{rule.remove_days}" selected>{rule.remove_days}</option></select>
          <input name="remove_pattern" value="{rule.remove_pattern}">
        </form>
    """


def test_parse_native_edit_form() -> None:
    assert parse_rule_form(edit_page(desired(), 7)) == (7, desired())


def test_reconcile_creates_and_then_is_idempotent() -> None:
    state: dict[str, Rule | None] = {"rule": None}
    posts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/user/settings/packages" and request.method == "GET":
            link = '<a href="/user/settings/packages/rules/7">Container</a>' if state["rule"] else ""
            return httpx.Response(200, text=link)
        if path == "/user/settings/packages/rules/7" and request.method == "GET":
            assert state["rule"] is not None
            return httpx.Response(200, text=edit_page(state["rule"], 7))
        if path == "/user/settings/packages/rules/add" and request.method == "POST":
            posts.append(path)
            assert "enabled=" not in request.content.decode()
            state["rule"] = desired()
            return httpx.Response(302, headers={"Location": "/user/settings/packages/rules/7"})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        assert reconcile(client, "https://forgejo.example", desired()) == "saved disabled container cleanup rule 7"
        assert reconcile(client, "https://forgejo.example", desired()) == "unchanged disabled container cleanup rule 7"
    assert posts == ["/user/settings/packages/rules/add"]


def test_reconcile_updates_drifted_rule() -> None:
    state = {"rule": Rule(**{**desired().__dict__, "keep_count": 25})}
    posts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/user/settings/packages" and request.method == "GET":
            return httpx.Response(200, text='<a href="/user/settings/packages/rules/9">Container</a>')
        if path == "/user/settings/packages/rules/9" and request.method == "GET":
            return httpx.Response(200, text=edit_page(state["rule"], 9))
        if path == "/user/settings/packages/rules/9" and request.method == "POST":
            posts.append(path)
            assert "keep_count=10" in request.content.decode()
            state["rule"] = desired()
            return httpx.Response(302, headers={"Location": path})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        assert reconcile(client, "https://forgejo.example", desired()) == "saved disabled container cleanup rule 9"
    assert posts == ["/user/settings/packages/rules/9"]


def test_validation_response_is_not_accepted_as_save() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="no rules")
        return httpx.Response(200, text=edit_page(desired(), 0))

    with (
        httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client,
        pytest.raises(RuntimeError, match="did not redirect"),
    ):
        reconcile(client, "https://forgejo.example", desired())


def test_config_cannot_enable_deletion(tmp_path: Path) -> None:
    config = tmp_path / "rule.json"
    config.write_text(json.dumps({**desired().__dict__, "enabled": True}))
    with pytest.raises(ValueError, match="disabled"):
        Rule.load(config)


if __name__ == "__main__":
    pytest_bazel.main()
