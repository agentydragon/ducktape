@README.md

## Visual changes: render and inspect before hand-off

```bash
bbr test //haku/console/frontend:screenshots
bbr test //haku/console/frontend/tool_rendering/haku_routine:previews
```

- `:screenshots` captures the production shell, bridge capability-request drawer, Settings, not-found,
  and session-expiry surfaces in light and dark themes. Browser rendering runs on RBE. Review the
  captured images, not only the pass result.
- A new shell surface needs a matching scene in `harness/harness.tsx` and capture in `test_visual.py`.
- The historical routine card is covered by `//haku/console/frontend/tool_rendering/haku_routine:previews`.
  Its fixtures cover both variants and color schemes. Review the focused card captures.
- A single-component scene renders inside its real production container (preview cards:
  `.haku-shell-panels`) and screenshots that element — no hardcoded width or full-viewport shot
  (<../../../testing/frontend_visual/README.md>).

## Historical tool-call rendering

`tool_call_card.tsx` renders recorded titles, status, and arguments. The sole specialized widget
shows `haku_routine.launch_routine` instructions or the routine default. Compact mode clamps
instructions and detailed mode can open **Raw arguments**. Other calls use the generic JSON argument
view. Completed results are shown as copyable JSON in detailed mode. Historical destructive calls
retain their red title cue.
