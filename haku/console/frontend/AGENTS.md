@README.md

## Editing console visuals — always screenshot and eyeball

When you change anything visual in this SPA — the past-tool-calls history view
(`tool_calls_page.tsx`), the console panel/drawer (`console_panel.tsx`), the panel toggle,
the tool-call cards, or their styles (`styles.src.css`) — regenerate and **look at** the
screenshots before handing off:

```bash
bbr test //haku/console/frontend:screenshots
```

It renders each surface (`screenshots/harness.tsx`) to a PNG (one per scene: `history`,
`drawer`, `drawer-access`) in the test's **undeclared outputs**. Browser rendering runs on
the RBE worker, so this is a `bbr` test, not a local `bb run`. Fetch the PNGs with the
`buildbuddy_api` skill (download the target's undeclared test outputs), then open every one
and actually check it looks good — spacing, alignment, contrast, truncation, that nothing
overflows or collides, and that both content and chrome read clearly — not merely that it
rendered. The test is a generator, not a pixel-diff gate: it passes as long as every scene
renders, so it never blocks CI on "looks different", but a blank/crashed scene fails it.

Add a scene to `screenshots/harness.tsx` (and the `SCENES` list in `screenshots/render.mjs`)
whenever you add a new surface.
