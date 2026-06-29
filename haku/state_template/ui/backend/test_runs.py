"""The runs feature read (`reads.read_runs`) + the RunManifest schema.

Guards: per-run manifests pair `runs/<date>/<ulid>.yaml` with their sibling `.md`, parse into
RunManifest, sort newest-first by `started`, and ignore `runs/README.md` / dangling files.
Also asserts the CI schema floor — propagation `action` is one of the allowed verbs. Forgejo
is mocked with httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
from forgejo import Forgejo
from models import RunManifest, ScannedSource, SkippedSource
from pydantic import ValidationError
from reads import read_runs

_API = "http://forgejo.test/api/v1/repos/haku/haku-state"
_HEAD = "deadbeefcafe"

_MANIFEST_A = """
run_id: 01RUNAAA
date: '2026-06-29'
started: '2026-06-29T09:08:00-07:00'
finished: '2026-06-29T09:41:00-07:00'
sources:
  - {source: gmail, bookmark_before: 'after:1', bookmark_after: 'after:2', changes_seen: 2}
  - {source: tana, skipped: 'no egress this run — surfaced as friction'}
checklists:
  - {checklist: kitchen, ref: procedures/propagation/kitchen.md, walked: true}
propagation:
  - change: 'Whole Foods order confirmed'
    source: gmail
    surfaces:
      - {surface: 'kitchen/board.yaml:incoming', action: updated, note: 'added rows'}
      - {surface: 'kitchen/board.yaml:use_soon', action: no_change, note: 'not yet arrived'}
"""
# Older run (earlier `started`) — must sort after A. Uses INT bookmarks (Grocy stock-log id),
# which must parse (bookmarks are heterogeneous: Gmail epoch string vs Grocy/Tana int).
_MANIFEST_B = """
run_id: 01RUNBBB
date: '2026-06-28'
started: '2026-06-28T08:00:00-07:00'
sources:
  - {source: grocy, bookmark_before: 128, bookmark_after: 130, changes_seen: 0}
"""


def _forgejo(tree: list[dict], blobs_by_sha: dict[str, str]) -> Forgejo:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/commits"):
            return httpx.Response(200, json=[{"sha": _HEAD, "commit": {"author": {"email": "x", "date": "d"}}}])
        if path.endswith(f"/git/trees/{_HEAD}"):
            return httpx.Response(200, json={"tree": tree, "truncated": False})
        if path.endswith("/git/blobs"):
            shas = request.url.params["shas"].split(",")
            return httpx.Response(
                200, json=[{"sha": s, "content": base64.b64encode(blobs_by_sha[s].encode()).decode()} for s in shas]
            )
        return httpx.Response(404, text=f"unexpected {path}")

    fj = Forgejo(api_url=_API, username="u", password="p")
    fj._http = httpx.AsyncClient(base_url=_API, transport=httpx.MockTransport(handler))
    return fj


def test_read_runs_pairs_yaml_and_md_sorts_newest_first_and_ignores_readme():
    tree = [
        {"type": "blob", "path": "runs/2026-06-29/01RUNAAA.yaml", "sha": "sha-a-yaml"},
        {"type": "blob", "path": "runs/2026-06-29/01RUNAAA.md", "sha": "sha-a-md"},
        {"type": "blob", "path": "runs/2026-06-28/01RUNBBB.yaml", "sha": "sha-b-yaml"},  # no .md sibling
        {"type": "blob", "path": "runs/README.md", "sha": "sha-readme"},  # ignored
        {"type": "blob", "path": "items/01X.yaml", "sha": "sha-item"},  # not a run, ignored
    ]
    blobs = {
        "sha-a-yaml": _MANIFEST_A,
        "sha-a-md": "## Run notes\n\nQuiet run; folded the WF order into incoming.",
        "sha-b-yaml": _MANIFEST_B,
    }

    async def go():
        async with _forgejo(tree, blobs) as f:
            return await read_runs(f)

    runs = asyncio.run(go())
    assert [r.run_id for r in runs] == ["01RUNAAA", "01RUNBBB"]  # newest `started` first
    a = runs[0]
    assert a.notes_md.startswith("## Run notes")
    assert [s.source for s in a.sources] == ["gmail", "tana"]
    # discriminated union: the tana row parsed to the skipped variant (carries a reason, no count)
    assert isinstance(a.sources[1], SkippedSource)
    assert a.sources[1].skipped is not None
    assert isinstance(a.sources[0], ScannedSource)
    assert a.checklists[0].walked is True
    assert a.propagation[0].source == "gmail"
    assert [t.action for t in a.propagation[0].surfaces] == ["updated", "no_change"]
    assert runs[1].notes_md == ""  # B has no .md sibling
    assert runs[1].sources[0].bookmark_after == 130  # int bookmark parses (Grocy id)


def test_run_manifest_rejects_bad_propagation_action():
    # CI schema floor: an unknown propagation action is a hard error.
    with pytest.raises(ValidationError):
        RunManifest.model_validate(
            {"run_id": "x", "propagation": [{"change": "c", "surfaces": [{"surface": "s", "action": "bogus"}]}]}
        )


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
