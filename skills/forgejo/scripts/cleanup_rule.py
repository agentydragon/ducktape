"""Reconcile one native Forgejo container-package cleanup rule through its settings form.

Forgejo v15 does not expose cleanup rules in its REST API. This uses the same owner
settings form as the UI, then reads the saved form back to verify every field.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import httpx

from skills.forgejo.scripts.forgejo import DEFAULT_FORGEJO_URL, login

SETTINGS = "/user/settings/packages"
RULE_LINK_RE = re.compile(r'href=["\'](?:https?://[^/]+)?/user/settings/packages/rules/(\d+)(?:/preview)?["\']')


@dataclass(frozen=True)
class Rule:
    enabled: bool
    type: str
    keep_count: int
    keep_pattern: str
    remove_days: int
    remove_pattern: str
    match_full_name: bool

    @classmethod
    def load(cls, path: Path) -> Rule:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise ValueError("cleanup rule has missing or unknown fields")
        rule = cls(**data)
        if rule.type != "container" or rule.keep_count not in (10, 25, 50, 100):
            raise ValueError("cleanup rule must retain at least ten container versions")
        if rule.remove_days not in (30, 60, 90, 180):
            raise ValueError("cleanup rule must retain at least 30 days")
        if type(rule.enabled) is not bool or type(rule.match_full_name) is not bool:
            raise ValueError("enabled and match_full_name must be booleans")
        if rule.enabled:
            raise ValueError("this reconciler only manages a disabled native cleanup rule")
        if rule.remove_pattern != r"^devel-[0-9]{14}-[0-9a-f]{7}$" or rule.match_full_name:
            raise ValueError("cleanup rule must target only dated CI versions")
        if rule.keep_pattern:
            raise ValueError("keep_pattern requires separate pin-safety review")
        return rule

    def form(self, *, rule_id: int = 0) -> dict[str, str]:
        return {
            "id": str(rule_id),
            "type": self.type,
            "keep_count": str(self.keep_count),
            "keep_pattern": self.keep_pattern,
            "remove_days": str(self.remove_days),
            "remove_pattern": self.remove_pattern,
            "action": "save",
            **({"enabled": "on"} if self.enabled else {}),
            **({"match_full_name": "on"} if self.match_full_name else {}),
        }


class _RuleForm(HTMLParser):
    """Only parse the native rule edit form, ignoring other page forms."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self.found = False
        self._inside = False
        self._select: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form" and re.search(r"/user/settings/packages/rules/(?:add|\d+)$", values.get("action") or ""):
            if self.found:
                raise ValueError("multiple package cleanup forms")
            self.found = self._inside = True
        if not self._inside:
            return
        name = values.get("name")
        if tag == "input" and name:
            if values.get("type") == "checkbox":
                self.fields[name] = "on" if "checked" in values else ""
            else:
                self.fields[name] = values.get("value") or ""
        elif tag == "select":
            self._select = values.get("name")
        elif tag == "option" and self._select and (self._select not in self.fields or "selected" in values):
            self.fields[self._select] = values.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._select = None
        elif tag == "form" and self._inside:
            self._inside = False


def parse_rule_form(html: str) -> tuple[int, Rule]:
    parser = _RuleForm()
    parser.feed(html)
    if not parser.found:
        raise ValueError("Forgejo cleanup rule edit form not found")
    fields = parser.fields
    try:
        return int(fields["id"]), Rule(
            enabled=fields["enabled"] == "on",
            type=fields["type"],
            keep_count=int(fields["keep_count"]),
            keep_pattern=fields["keep_pattern"],
            remove_days=int(fields["remove_days"]),
            remove_pattern=fields["remove_pattern"],
            match_full_name=fields["match_full_name"] == "on",
        )
    except (KeyError, ValueError) as exc:
        raise ValueError("malformed Forgejo cleanup rule edit form") from exc


def _get(client: httpx.Client, url: str) -> str:
    response = client.get(url)
    response.raise_for_status()
    if response.url.path.rstrip("/") == "/user/login":
        raise RuntimeError("Forgejo web session expired")
    return response.text


def read_rules(client: httpx.Client, base_url: str) -> list[tuple[int, Rule]]:
    base = base_url.rstrip("/") + SETTINGS
    ids = sorted({int(value) for value in RULE_LINK_RE.findall(_get(client, base))})
    rules: list[tuple[int, Rule]] = []
    for rule_id in ids:
        parsed_id, actual = parse_rule_form(_get(client, f"{base}/rules/{rule_id}"))
        if parsed_id != rule_id:
            raise ValueError("Forgejo cleanup rule ID changed between list and edit")
        rules.append((rule_id, actual))
    return rules


def reconcile(client: httpx.Client, base_url: str, desired: Rule) -> str:
    base = base_url.rstrip("/") + SETTINGS
    container_rules = [(rule_id, rule) for rule_id, rule in read_rules(client, base_url) if rule.type == "container"]
    if len(container_rules) > 1:
        raise ValueError("multiple container cleanup rules for one owner")
    if container_rules and container_rules[0][1] == desired:
        return f"unchanged disabled container cleanup rule {container_rules[0][0]}"
    rule_id = container_rules[0][0] if container_rules else 0
    path = f"{base}/rules/{rule_id}" if rule_id else f"{base}/rules/add"
    response = client.post(path, data=desired.form(rule_id=rule_id))
    response.raise_for_status()
    if response.url.path.rstrip("/") == "/user/login":
        raise RuntimeError("Forgejo web session expired during rule save")
    # A successful save redirects to /rules/{id}; validation errors render the form at
    # /rules/add or the original edit path without a redirect.
    if not response.history or not response.url.path.startswith(f"{SETTINGS}/rules/"):
        raise RuntimeError("Forgejo did not redirect after cleanup rule save")
    saved_id, saved = parse_rule_form(response.text)
    if saved != desired or (rule_id and saved_id != rule_id) or saved_id < 1:
        raise RuntimeError("saved Forgejo cleanup rule does not match the declared policy")
    return f"saved disabled container cleanup rule {saved_id}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--inspect", action="store_true", help="read existing native rules without writing")
    args = parser.parse_args()
    if not args.inspect and args.config is None:
        parser.error("--config is required unless --inspect is set")
    desired = Rule.load(args.config) if args.config else None
    base_url = os.environ.get("FORGEJO_URL", DEFAULT_FORGEJO_URL)
    with contextlib.closing(
        login(base_url, os.environ["FORGEJO_USER"], os.environ["FORGEJO_PASSWORD"], timeout=60)
    ) as client:
        if args.inspect:
            rules = read_rules(client, base_url)
            if not rules:
                print("no cleanup rules")
            for rule_id, rule in rules:
                print(f"{rule_id}\t{rule.type}\t{'enabled' if rule.enabled else 'disabled'}")
        else:
            assert desired is not None
            print(reconcile(client, base_url, desired))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
