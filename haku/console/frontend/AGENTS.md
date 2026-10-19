@README.md

## Visual changes: render and inspect before hand-off

```bash
bbr test //haku/console/frontend:screenshots
```

- `:screenshots` captures the production shell, bridge capability-request drawer, Settings, not-found,
  and session-expiry surfaces in light and dark themes. Browser rendering runs on RBE. Review the
  captured images, not only the pass result.
- A new shell surface needs a matching scene in `harness/harness.tsx` and capture in `test_visual.py`.
- Former enriched tool-call renderers are archived as inert source under
  `../archive/tool_renderers/`; they have no active build or screenshot targets.
