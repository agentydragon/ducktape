## Verifying a visual change

Visual tests (`--test_tag_filters=visual`) have no pixel baselines; passing only means every scene
mounted without an uncaught error. Before calling a component/stylesheet change done, look at a
rendered PNG of every state you touched, via either:

- CI's `pr-visuals` PR comment (before/after/diff for every visual scenario), or
- `bbr test //agentplane/app/frontend:visual_history --test_filter=<test-name> --noremote_accept_cached --nocache_test_results`,
  then fetch the PNG from undeclared outputs with `bbapi artifact list <invocation-id>` (name as
  passed to `view.capture()`) and `bbapi artifact download <invocation-id> "<name>.png"`. Failure
  in another shard: `bbapi target log <invocation-id> //agentplane/app/frontend:visual_history --failed`.

Run the app interactively only when a static screenshot cannot show the change (motion itself;
visual tests disable animations).

## Adding a visual test

Configure mock services through `AgentplaneFixture` (`testing/app.py`), then explicitly mount the app
at a route (or the isolated disclosure specimen). The TypeScript harness exposes data builders and
component mounts only: it never selects setup from a scene name or interprets a recipe object, and
gets no switches that click, focus, scroll or inspect the DOM. Keep recorded payloads (e.g. rollout
rows) as data, not route/flag catalogs.

```python
@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_recovery(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.recovery("tools")
    await app.mount_thread(IDLE_THREAD)
    # Assert readiness, interact with locators, then capture.
    await view.capture()
```

- Python owns readiness, clicks, scrolls, assertions and capture. Pass geometry explicitly from
  `util/testing/viewports.py` and crop locators to `view.capture()`.
- Load both `util.testing.visual_fixtures` and `agentplane.app.frontend.testing.visual_fixtures`
  plugins. `view` owns browser cleanup; the app plugin provides `app` and the default desktop
  viewport.
- Screenshot names default to the pytest case ID and never select behavior; pass explicit names
  only for extra checkpoints.
- Each `visual_*` target owns one Python test file; BUILD names the module, not scenes. No
  aggregate runner: run a target, or the group via `--test_tag_filters=visual`; filter by test
  function or case ID.

## Scrolling review scenes

- Do not center a sticky heading with `scrollIntoView()` (its pinned position makes the result
  depend on the starting offset). Position ordinary content, then assert its heading and content are
  visible before capture.
- After setup clicks, place the pointer explicitly: scrolling can move a different hover target
  under a stationary pointer. Hover containers mounted under the pointer may miss `pointerenter`;
  move outside, then hover deliberately.
- `data-layout-settled=true` covers mounted rows only; off-screen rows may keep loading-height
  estimates. For repeatable scrolled screenshots, establish the same measured content before scene
  interactions. Cover unmeasured-row and delayed-content behavior separately; never hide scroll
  drift by masking screenshots or weakening reader-position assertions.
