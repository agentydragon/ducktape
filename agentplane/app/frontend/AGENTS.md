## Verifying a visual change — see the rendered image, not just a passing test

A passing `bbr test //agentplane/app/frontend/... --test_tag_filters=visual` only proves every scene mounted
without an uncaught error — it says nothing about whether it looks right (there are no checked-in
pixel baselines here; see `util/testing/frontend_visual/README.md`). Before calling a visual
change (`threads/projected_session.tsx`, `threads/projected_session.css`, or any other component/stylesheet here) done, actually look
at a rendered PNG of every state you touched. Either of these satisfies that — the point is seeing
the real pixels, not a specific mechanism for getting there:

- **Wait for CI's `pr-visuals` comment** on the PR: it renders every visual scenario and posts
  before/after/diff thumbnails automatically once pushed.
- **Or run a specific target** — `bbr test //agentplane/app/frontend:visual_history
--test_filter=<test-name> --noremote_accept_cached --nocache_test_results` — and download the PNG it
  writes to the test's
  undeclared outputs (`buildbuddy_api` skill: `bbapi artifact list <invocation-id>` for the exact
  name, passed to `view.capture()`, then `bbapi artifact download <invocation-id>
"<name>.png"`), then view it. A failure in another shard: `bbapi target log <invocation-id>
//agentplane/app/frontend:visual_history --failed`.

A local interactive browser session (running the app, clicking through it by hand) is neither
required nor the goal here — it's extra machinery for the same answer a screenshot already gives.
Reach for it only when a _static_ screenshot genuinely can't show what changed (verifying motion
itself, not an animation's start/end frames — which visual tests disable anyway).

## Adding a visual test

Tests configure mock services through `AgentplaneFixture` in `visual_app.py`, then
explicitly mount the app at a route (or mount the isolated disclosure specimen).
The TypeScript harness exposes callable data builders and component mounts; it does not
select setup from a scene name or interpret a recipe object. Keep actual recorded payloads
such as rollout rows as data, not route/flag catalogs.

```python
@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_recovery(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.recovery("tools")
    await app.mount_thread(IDLE_THREAD)
    # Assert readiness, interact with locators, then capture.
    await view.capture()
```

Python tests own readiness, clicks, scrolls, assertions and capture. Pass browser geometry
explicitly using `util/testing/viewports.py`, and pass crop locators to `view.capture()`.
Load both `util.testing.visual_fixtures` and `agentplane.app.frontend.visual_fixtures`
plugins. The shared `view` yield fixture owns browser cleanup; the app plugin provides
`app` and the default desktop viewport. Default screenshot names follow the pytest case
ID, not a parallel output-name table. A screenshot name identifies an artifact; it must
not select behavior. Use explicit capture names only for additional checkpoints.

Do not add harness switches that click, focus, scroll or inspect the DOM. Add an ordinary
Python behavior test and an explicit screenshot checkpoint instead. BUILD names the test
module, not individual scenes. Each `visual_*` test target owns one Python test file.
Run a target directly, or select the group with Bazel's `--test_tag_filters=visual`; there is no aggregate runner or suite.
Filter within a target with the test function or parametrized case ID.
