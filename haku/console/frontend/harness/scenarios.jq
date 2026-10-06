# The sweep's scenario table (`--null-input`): every console scene, in light and dark, named `<scene>-<scheme>`.
# `harness.tsx` mounts the scene `window.__SCENE__` names; the sweep drives it as an operator would.

# Every scene that frames Haku's UI waits for the mocked document (`mock_haku_ui.html`) to load.
def haku_ui_frame: "iframe[src^='https://haku-ui.test/']";

def tab($name): {
  selector: "[role='tab']:has-text('\($name)')",
  expectVisible: ["[role='tab'][aria-selected='true']:has-text('\($name)')"]
};

# The approvals drawer opens by itself when approvals are pending. While it is open it renders its own
# tool-call cards, whose controls look like the page's, so a scene that is not about the drawer closes it
# before anything else is clicked.
def close_approvals: {
  selector: ".haku-shell-drawer [aria-label='Close approvals']",
  expectHidden: [".haku-shell-drawer"]
};

# The scenes. `ready` is what must be on the page, `clicks` how a scene is reached, `frame` that it is
# the production shell around the mocked Haku UI.
{
  "console": {width: 1200, height: 800, closeApprovals: true, frame: true},
  "aiquota": {width: 1200, height: 800},
  "console-drawer": {width: 1200, height: 800, frame: true},
  "console-mobile": {width: 390, height: 760, frame: true},
  "not-found": {width: 900, height: 600, closeApprovals: true, ready: [":text('Page not found')"], frame: true},
  "approvals-embed": {width: 560, height: 820, ready: ["button:has-text('Approve')"]},
  "settings": {width: 1200, height: 1000, closeApprovals: true, frame: true},
  "settings-mobile": {width: 390, height: 760, closeApprovals: true, frame: true},
  "settings-agents": {
    width: 1200, height: 1000, closeApprovals: true, frame: true,
    clicks: [tab("Agents") | .expectVisible += [":text('Public Coder')"]]
  },
  "settings-grants": {
    width: 1200, height: 1000, closeApprovals: true, frame: true,
    clicks: [tab("Grants") | .expectVisible += [":text('Public Coder')"]]
  },
  "settings-grants-history": {
    width: 1200, height: 1000, closeApprovals: true, frame: true,
    clicks: [
      tab("Grants"),
      {
        selector: ":text('History')",
        expectVisible: [":text('Pilot complete; return to standard diagnostics.')"]
      }
    ]
  },
  "settings-grants-revoke": {
    width: 1200, height: 1000, closeApprovals: true, frame: true,
    clicks: [
      tab("Grants"),
      # Each grant row has one; the first row's is the one under review.
      {selector: "button:has-text('Revoke') >> nth=0", expectVisible: [":text('Confirm')"]}
    ]
  },
  "settings-notifications": {
    width: 1200, height: 1000, closeApprovals: true, frame: true,
    clicks: [tab("Notifications") | .expectVisible += [":text('This browser')"]]
  },
  "settings-system": {
    width: 1200, height: 1000, closeApprovals: true, frame: true,
    clicks: [tab("System") | .expectVisible += [":text('Mixed revisions')"]]
  },
  "agent-enrollment": {width: 1200, height: 900, closeApprovals: true, frame: true},
  "agent-enrollment-reconnect": {width: 1200, height: 900, closeApprovals: true, frame: true},
  "agent-enrollment-mobile": {width: 390, height: 760, closeApprovals: true, frame: true},
  # The only scene that renders the detailed variant: its first row is toggled to Full and that row's
  # Metadata disclosure opened, so detail-only rendering is actually visually reviewed.
  "history": {
    width: 1200, height: 1500, closeApprovals: true, frame: true,
    clicks: [
      {selector: "[aria-label='Full'] >> nth=0", expectVisible: ["summary:has-text('Metadata')"]},
      {
        selector: "summary:has-text('Metadata')",
        expectVisible: [".haku-shell-disclosure[open] .haku-shell-disclosure-body"]
      }
    ]
  },
  # Toggling "Show auto-approved" reveals the unconditionally-auto-approved sample row that the default
  # `history` scene hides. The refetch it fires is async, so what is waited for is the provenance line
  # only an auto-approved row renders.
  "history-auto-approved": {
    width: 1200, height: 1500, closeApprovals: true, frame: true,
    clicks: [
      {
        selector: "[aria-label='Show auto-approved']",
        expectVisible: [":text('Auto-approved by unconditional_v1')"]
      }
    ]
  },
  # A ledger deeper than one page: the "Load older calls" affordance at the bottom, and the placeholders
  # code_block.tsx leaves where a row's editor is not built yet.
  "history-paged": {
    width: 1200, height: 900, closeApprovals: true, frame: true,
    ready: ["button:has-text('Load older calls')"],
    scrollToBottom: ".haku-page-scroll"
  },
  "sync-current": {
    width: 600, height: 420,
    clicks: [{selector: "[aria-label='Up to date']", expectVisible: ["[aria-label='Sync status']"]}]
  },
  "sync-syncing": {
    width: 600, height: 420,
    clicks: [{selector: "[aria-label='Syncing']", expectVisible: ["[aria-label='Sync status']"]}]
  },
  "sync-error": {
    width: 600, height: 420,
    clicks: [{selector: "[aria-label='Sync error']", expectVisible: ["[aria-label='Sync status']"]}]
  },
  "session-expiring": {
    width: 600, height: 420,
    clicks: [{selector: "[aria-label='Session expiring soon']", expectVisible: ["[aria-label='Console session']"]}]
  }
}
| to_entries as $scenes
| [
    ("light", "dark") as $scheme
    | $scenes[]
    | .key as $name
    | .value as $scene
    | {
        key: "\($name)-\($scheme)",
        value: {
          element: "#app",
          # The viewport, not `#app`'s extent: a shell that overflows is captured clipped.
          captureViewport: true,
          viewport: {width: $scene.width, height: $scene.height, deviceScaleFactor: 2},
          colorScheme: $scheme,
          label: "\($name) - \($scheme)",
          windowGlobals: {__SCENE__: $name, __COLOR_SCHEME__: $scheme},
          readySelectors: ($scene.ready // []),
          readyFrames: (if $scene.frame then {(haku_ui_frame): "main"} else {} end),
          clicks: ((if $scene.closeApprovals then [close_approvals] else [] end) + ($scene.clicks // [])),
          # Gone before capture, whichever scene it is: the lazily loaded lists' spinners, the approval
          # cards' Approve/Deny while they arm, and (where the shell syncs on mount) the rail's sync icon.
          # `sync-syncing` clicks that icon and wants it left showing, so only the framed scenes wait on it.
          hiddenSelectors: ([
            "button:has-text('Approve'):disabled",
            "button:has-text('Deny'):disabled",
            "[aria-label='Loading MCP servers']",
            "[aria-label='Loading Agents']",
            "[aria-label='Loading Agent enrollment']",
            "[aria-label='Checking connection status']"
          ] + (if $scene.frame then ["[aria-label='Syncing']"] else [] end)),
          scrollToBottom: ($scene.scrollToBottom // null)
        }
      }
  ]
| from_entries
