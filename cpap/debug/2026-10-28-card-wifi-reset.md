# CPAP card reverted to default WiFi settings

Investigation started 2026-10-29 00:05 UTC (2026-10-28 Pacific).

## Evidence

- Manual Job `cpap-sync-manual-20261029-0010` failed with connection refused
  while listing the card through `cpap-card`. Its Forgejo clone succeeded.
- `optiplex` was Ready without memory, disk, or PID pressure. The gateway VMI
  had been running since 2026-10-28 01:29 UTC, with its guest agent connected
  and no launcher container restarts.
- Gateway health classified `card-away` from boot: the radio could see other
  networks, but not the configured `Rai CPAP ez Share`. This deliberately stops
  `cpap-card-proxy`, explaining the refused connection.
- A fresh NetworkManager scan found `ez Share`, channel 11, signal 67/100,
  BSSID `F4:55:95:11:35:A2`. This matches the card identity recorded in
  `cpap/card.py` and returned by its live `devicemac` API.
- The stored WiFi settings failed association. The default password succeeded,
  and the card reported SSID `ez Share` and firmware `LZ1801EDPG:1.0.0`.
- The guest sees its MT7921U, firmware is present, USB power control is `on`,
  and fresh scans find both 2.4 GHz and 5 GHz networks. Its kernel journal had
  no post-boot USB disconnect/reset or MT7921U failure. This incident does not
  establish the cause of earlier dongle/VM instability.

## Recovery

Cloned a temporary diagnostic NetworkManager profile to test the discovered
SSID. After confirming identity and the default password, modified the existing
`cpap-ezshare` profile **in memory only** to those settings and activated it.
Running `cpap-gateway-health` then reported `connected` and started the proxy.
Deleted the diagnostic profile.

Started `cpap-sync-recovered-20261029-0010`. Its initial listing found 178 files,
with 173 new/changed files totaling 57.8 MiB. Completion is recorded below.

The initial guest profile change was temporary. After the backup completed,
restored the card's original settings as described below and returned the guest
profile to the original SSID/password from its secret disk.

## Reset cause remains unknown

The user reports no deliberate reset. The [vendor manual, Q7](https://fccid.io/A7JEZS128/User-Manual/Users-Manual-1903595.pdf)
documents resetting SSID/password by deleting root `ezshare.cfg`, so a physical
button is not necessary. File loss, configuration corruption, or firmware
behavior are hypotheses, not established causes; WiFi APIs cannot diagnose NAND
health or the underlying filesystem.

The current root listing reports `ezshare.cfg` at zero bytes with timestamp
1231858834. The previous committed manifest already records `EZSHARE.CFG` with
exactly those values, so this metadata alone is not evidence of new corruption.
No files were deleted or formatted. The later API recovery changed only the
card's WiFi/admin settings and applied them through its WiFi restart endpoint.

The card currently lists 31 `DATALOG` date directories from 2026-09-27 through
2026-10-28. The existing archive retains 237 dates, 2025-10-04 through
2026-09-27; sync does not delete archived files absent from the card. The newly
downloaded `STR.EDF` has 395 daily records starting 2025-09-28. The summary
history therefore survived; the directory range alone does not establish why
older detailed files are no longer on the card.

The default WiFi password successfully granted HTTP file access. Anyone within
radio range who knew it could use the same access path until the original
nondefault credentials were restored.

## Verified settings API

Read the firmware's own `/i-share/res/help.js` and `/i-share/res/config.js`.
The `/pdclient.cgi` endpoints in `cpap/card.py` do not work on this firmware.

1. `/publicdir/sendurl?client=1&password=<admin>` returns `clientLogin=1`
   and the device MAC when administrator authentication succeeds.
2. `/config?vtype=0` returns the settings form, including the current channel.
3. `/config?auth=<admin>&ssid=<ssid>&pass=<wifi>&channel=<channel>` saves
   settings; the client requires `config_success` in the response.
4. `/config?reset=1` applies the saved settings and restarts WiFi, matching the
   web UI's `responseRest()` implementation. This applies settings rather than
   resetting them to defaults.

Restored `Rai CPAP ez Share` and both passwords from `cpap-sync/cpap-ezshare`,
preserving channel 11. Both stored passwords differ from the factory defaults.
Verified the new SSID/device MAC through the card API, successful association
with the stored WiFi password, gateway health `connected`, an active bridge,
and successful administrator login with the stored admin password. Logged out
and closed the temporary local port-forward afterward. Credentials were not
printed or included in tool command arguments.

Automatic detection and recovery is proposed in
[PR #12807](https://github.com/agentydragon/ducktape/pull/12807).
The focused `//cpap/gateway:all` RBE build/test passed for its final source tree
([invocation](https://app.buildbuddy.io/invocation/96ad5256-1cef-4d7f-8887-77cea45e0952)).
Merged as `fd79eeb13f6805a87f2cfd0543d0b235ede38c3c`.

### Rollout

The merge-triggered image build still consumed the old
`cpap-gateway-health-15135a862953` artifact pin while the guest service gained
new CLI flags. Publishing that combination would break health-check startup.
Canceled image runs `40794828702` and `40795082925` before publication.

The normal devel release was queued behind older CI. Retrieved the validated
unstamped wheel from the passing invocation, built the stamped wheel at the
merged source through RBE invocation
`0ecc7772-9147-4786-aa08-3f3d5687f143`, and compared its `health.py` and
`restore.py` with the merged files. Both wheels have SHA256
`545fd14a47c4412171d4d821d6b17ae8e28a6caccb002fa8bf8fce5b24b6f0f8`.
Published the standard content-addressed release
`cpap-gateway-health-545fd14a47c4`, targeting the merge commit, and triggered
existing pin-sync workflow `40795270288`.

Pin-sync landed as `91b5e94191dc635c18435de0e0b33fae81d77392` and started image
workflow `40795522456` with the updated wheel pin. It published
`devel-20261029004410-91b5e94` successfully. The upload transferred a
1,697,551,076-byte layer; its PATCH completed in 435 seconds, followed by blob
finalization, so the long publish was an active transfer rather than a wedge.

Image automation committed the tag as `fff0784b74a2a9161caef34d7cacad566688b013`.
The restart controller gracefully replaced the VMI and recorded phase `Ready`
at 00:58:58 UTC. Guest health reached `connected`, its bridge was active, and
Flux reported `Ready/Healthy`. Job `cpap-sync-post-rollout-20261029-0100`
completed successfully in 12 seconds: 178 card files, zero changes to fetch.

### Administrator denial protocol

During rollout, a read-only check with the factory administrator password against
the restored card returned `<clientLogin>-1</clientLogin>`, not `0`. The original
HTTP fixture had assumed `0`. This affects the stored-admin retry following an
interrupted save, not initial recovery with factory credentials.
[PR #12810](https://github.com/agentydragon/ducktape/pull/12810) corrects the
response and fixture; focused Bazel build/test passed in invocation
`66dde442-67d9-4216-81c4-779e8c883d0f`. The user merged it as
`f388797caa75bae90d6e37d9d689146250c60cbb`.

Its hosted CI failed in the graph/query phase. The log includes deprecated
`//external` package errors, but the terminal failure is a Bazel JVM crash
(`NegativeArraySizeException`), so those package errors are not an established
cause. Invocation `810716c6-e1ac-4947-82c8-23e0c853ecd2` is a failed `query`,
not a CPAP test run. Built the corrected wheel at the exact merge
through RBE invocation `33aa845c-dcf4-42cf-84b7-6d67d6625aed`, compared its
gateway files with merged sources, and published
`cpap-gateway-health-58d73b1a77dc`.

Pin-sync `40798038708` adopted that wheel, and image workflow `40798341710`
published `devel-20261029011051-14b3871`. Image automation committed it as
`b6884f3ae1142fd5e59ebe80ef6798cf3a48b0b5`. The restart controller completed
template generation 84 on OptiPlex, and Flux reported `Ready/Healthy` at
01:19:26 UTC.

Verified the final guest's installed `restore.py` recognizes `clientLogin=-1`,
its normal card connection is active, health state is `connected`, and both
the card bridge and health timer are active. Job
`cpap-sync-final-20261029-0120` completed successfully at 01:20:25 UTC, with
178 files matching the archive and no changes to download. Both merged changes
are deployed. No deliberate reset of the live card was performed to exercise
factory-reset recovery end to end.

## Result

Job `cpap-sync-recovered-20261029-0010` completed successfully at
2026-10-29 00:13:23 UTC. It pushed 173 changed files to archive `main`, advancing
`51d4341` to `e757a50`. No download errors were reported. This verifies the live
card-to-WiFi-to-gateway-to-Forgejo path after adapting the guest profile; it does
not diagnose the settings reset or establish the card's future reliability.
