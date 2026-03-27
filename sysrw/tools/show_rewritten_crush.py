#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

from sysrw.extract_common import iter_wire_lines
from sysrw.openai_typing import (
    MessageRole,
    dump_response_messages,
    parse_response_messages,
    response_message_content_as_text,
    response_message_role,
)

PROVIDER_WIRE = Path(os.environ.get("CRUSH_WIRE_LOG", str(Path.home() / ".crush" / "logs" / "provider-wire.log")))
DEMO_TEMPLATE = Path(__file__).parent / "demo_template.txt"

ENV_INTRO = "Here is useful information about the environment you are running in:"
MODEL_PREFIX = "You are powered by the model"
MCP_HEADER = "# MCP Server Instructions"
TOOLS_HEADER = "You can use the following tools without requiring user approval:"


def ensure_demo_template() -> Path:
    if not DEMO_TEMPLATE.exists():
        DEMO_TEMPLATE.write_text(
            (
                "You are an interactive CLI tool that helps users with software engineering tasks. Use the instructions below and the tools available to you to assist the user.\n\n"
                "${toolsBlob}\n"
                "${envGitBlobs}${modelLine}${mcpSection}\n"
            ),
            encoding="utf-8",
        )
    return DEMO_TEMPLATE


def maybe_extract_payload(obj: dict[str, Any]) -> dict[str, Any] | None:
    p = obj.get("payload")
    return p if isinstance(p, dict) else None


def extract_system_text_from_responses_input(payload: dict[str, Any]) -> str:
    inp = payload.get("input")
    parsed = parse_response_messages(inp)
    if not parsed:
        return ""
    sys_parts: list[str] = []
    seen_user = False
    for item in parsed:
        role = response_message_role(item)
        text = response_message_content_as_text(item)
        if role == MessageRole.USER:
            seen_user = True
        if (role == MessageRole.SYSTEM and not seen_user) and text:
            sys_parts.append(text)
    return "\n\n".join(p for p in sys_parts if p)


def extract_ccr_blobs(system_text: str) -> dict[str, Any]:
    s = system_text or ""
    env_git_blobs: list[str] = []
    if ENV_INTRO in s:
        env_re = re.compile(re.escape(ENV_INTRO) + r"\n<env>[\s\S]*?</env>\s*", re.MULTILINE)
        env_git_blobs = [m.group(0) for m in env_re.finditer(s)]
    tools_blob = ""
    i_tools = s.find(TOOLS_HEADER)
    if i_tools != -1:
        after = i_tools + len(TOOLS_HEADER)
        nxt = [x for x in [s.find(ENV_INTRO, after), s.find(MODEL_PREFIX, after), s.find(MCP_HEADER, after)] if x != -1]
        end = min(nxt) if nxt else len(s)
        tools_blob = s[after:end]
    mm = re.search(r"^" + re.escape(MODEL_PREFIX) + r"[^\n]*\n?", s, flags=re.MULTILINE)
    model_line = mm.group(0) if mm else ""
    mcp_section = ""
    i_mcp = s.find(MCP_HEADER)
    if i_mcp != -1:
        nl = s.find("\n", i_mcp)
        mcp_section = "" if nl == -1 else s[nl + 1 :]
    return {"toolsBlob": tools_blob, "envGitBlobs": env_git_blobs, "modelLine": model_line, "mcpSection": mcp_section}


def rewrite_system_with_template_py(system_text: str, template_path: Path) -> str:
    template = Path(template_path).read_text(encoding="utf-8")
    blobs = extract_ccr_blobs(system_text)
    placeholders = ["${toolsBlob}", "${envGitBlobs}", "${modelLine}", "${mcpSection}"]
    # Ensure exactly once
    for ph in placeholders:
        cnt = template.count(ph)
        if cnt != 1:
            raise RuntimeError(f"template placeholder {ph} count={cnt} (expected 1)")
    out = (
        template.replace("${toolsBlob}", blobs["toolsBlob"])
        .replace("${envGitBlobs}", "".join(blobs["envGitBlobs"]))
        .replace("${modelLine}", blobs["modelLine"])
        .replace("${mcpSection}", blobs["mcpSection"])
    )
    for ph in placeholders:
        if ph in out:
            raise RuntimeError(f"placeholder {ph} still present after replacement")
    return out


def build_rewritten_request(orig: dict[str, Any], new_system_text: str) -> dict[str, Any]:
    req = copy.deepcopy(orig)
    inp = req.get("input")
    if not isinstance(inp, list):
        req["input"] = [{"role": "system", "content": [{"type": "input_text", "text": new_system_text}]}]
    else:
        # Keep only first 2 non-system items for readability
        # Find first explicit user index
        first_user = None
        for i, it in enumerate(inp):
            if isinstance(it, dict) and (it.get("role") or it.get("message_role") or "").lower() == "user":
                first_user = i
                break
        tail = inp[first_user:] if first_user is not None else []
        tail = tail[:2]
        req["input"] = [{"role": "system", "content": [{"type": "input_text", "text": new_system_text}]}, *tail]

    validated = parse_response_messages(req.get("input"))
    if validated:
        req["input"] = dump_response_messages(validated)
    return req


def main():
    tpl = ensure_demo_template()
    # Find first request
    for line in iter_wire_lines(PROVIDER_WIRE):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("direction") != "request":
            continue
        payload = maybe_extract_payload(e)
        if not payload:
            continue
        # Shorten original for display (limit input to first 3 entries)
        orig = copy.deepcopy(payload)
        if isinstance(orig.get("input"), list) and len(orig["input"]) > 3:
            orig["input"] = orig["input"][:3]
        sys_text = extract_system_text_from_responses_input(payload)
        new_sys = rewrite_system_with_template_py(sys_text, tpl)
        rewritten = build_rewritten_request(payload, new_sys)
        if isinstance(rewritten.get("input"), list) and len(rewritten["input"]) > 3:
            rewritten["input"] = rewritten["input"][:3]
        print(
            json.dumps(
                {"original_crush_request": orig, "rewritten_crush_request": rewritten}, ensure_ascii=False, indent=2
            )
        )
        return 0
    print(json.dumps({"error": "no request found", "path": str(PROVIDER_WIRE)}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
