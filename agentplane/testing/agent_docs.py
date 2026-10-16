"""Check the served documentation graph using each service's ordinary HTTP app."""

import re
from urllib.parse import urljoin, urlsplit

import httpx


async def assert_agent_docs(client: httpx.AsyncClient) -> None:
    pending = {"/llms.txt"}
    visited: set[str] = set()
    while pending:
        path = pending.pop()
        visited.add(path)
        response = await client.get(path)
        assert response.status_code == 200, (path, response.status_code, response.text)
        assert response.headers["content-type"].startswith("text/"), path
        assert response.text.startswith("# "), path
        for link in re.findall(r"\[[^\]]+\]\(([^)]+)\)", response.text):
            target = urlsplit(urljoin(path, link))
            if not target.netloc and target.path.startswith("/docs/agents/") and target.path not in visited:
                pending.add(target.path)
    schema = await client.get("/openapi.json")
    assert schema.status_code == 200
    assert schema.json()["openapi"].startswith("3.")
