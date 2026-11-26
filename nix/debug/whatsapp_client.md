# WhatsApp desktop client on wyrm2

Goal: GUI WhatsApp client in nixpkgs with video calls. Installed on wyrm2, rugged and iguana via <../home/modules/karere.nix> (autostarts hidden to tray). Status as of 2026-11-26.

## Chosen: `pkgs.karere` (2.5.5)

Picked from tracker evidence only; not yet tested here. Closed `tobagin/karere` issues "Your browser doesn't support calling", "WhatsApp Web Calls", "No audio output devices listed during calls" → fixed by moving to CEF. Unchecked: nixpkgs 2.5.5 includes that fix. Try with `nix run nixpkgs#karere`, link phone, place a call.

## Whatsie 5.0.0: login broken, dropped

Symptom: scanning the link-device QR with WhatsApp's own scanner opened `https://web.whatsapp.com/login/?v=124.123` in desktop Chrome, not Whatsie. "Log in with phone number" did nothing visible.

Cause (read from source, not traced at runtime): `src/webenginepage.cpp` `acceptNavigationRequest` sends every `NavigationTypeLinkClicked` URL to `QDesktopServices::openUrl`, including links within `web.whatsapp.com`. The `?v=` is Whatsie's own random startup query.

Not a linkage bug: `whatsie.desktop` correctly owns `x-scheme-handler/whatsapp`.

Tried: a one-line patch (open externally only if `url.host() != "web.whatsapp.com"`); it built against 5.0.0, never run, then deleted.

Gotcha: nixpkgs pins 5.0.0, upstream is 6.2.0 (2026-11-10). `src/webenginepage.cpp` is absent on upstream master, so 6.x may not have the bug. Retry Whatsie once nixpkgs catches up.

## Candidates (GitHub, 2026-11-26)

| Client              | nixpkgs | Stars | Last push  | Calls                                                       |
| ------------------- | ------- | ----- | ---------- | ----------------------------------------------------------- |
| `whatsie`           | 5.0.0   | 3478  | 2026-11-12 | no tracker evidence; LinuxLinks claims yes                  |
| `zapzap`            | 6.5.2.1 | 1737  | 2026-11-18 | used but flaky: camera video, Wayland screen sharing issues |
| `karere`            | 2.5.5   | 229   | 2026-11-26 | fixed via CEF (closed issues)                               |
| `altus`             | 5.7.2   | 714   | 2026-11-25 | no; open since 2022 + Feb 2026 report                       |
| `whatsapp-electron` | 1.1.1   | 26    | 2026-10-07 | unknown                                                     |
| `nchat`             | 5.14.44 | 2035  | 2026-11-22 | terminal, none                                              |
| `wasistlos`         | absent  | 1159  | 2024-10-22 | archived                                                    |
| `WALC`              | absent  | 280   | 2024-02-17 | stale                                                       |
| `whatevr`           | absent  | 20    | 2026-11-05 | native (`whatsmeow`); no call evidence                      |

Not checked: WhatsDesk (GitLab).

## Fallback

WhatsApp Web as a Chromium/Chrome PWA, if the wrappers fail.
