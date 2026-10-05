# The sweep's scenario table, from the scenes the harness mounts (fixtures/scenes.json is the input):
# every scene in each theme at desktop width, then one narrow shot. The narrow shot is there because
# the provider grid collapses to a column and the heading stacks, and that reflow is the part of the
# layout a desktop-only sweep would never exercise.
def shot($name; $scene; $theme; $width; $label): {
  key: $name,
  value: {
    element: "#app",
    viewport: {width: $width, height: 900, deviceScaleFactor: 2},
    colorScheme: $theme,
    readySelectors: [".aiquota-card"],
    query: {scene: $scene},
    label: $label
  }
};

keys_unsorted as $scenes
| [
    ("light", "dark") as $theme
    | $scenes[] as $scene
    | shot("\($scene)-\($theme)"; $scene; $theme; 1200; "\($scene) · \($theme)")
  ]
  + [shot("hot-narrow"; "hot"; "dark"; 420; "hot · dark · narrow"),
     shot("paid_credits-narrow"; "paid_credits"; "dark"; 420; "paid credits · dark · narrow")]
| from_entries
