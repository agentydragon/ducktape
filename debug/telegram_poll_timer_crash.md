# Telegram Desktop 6.4.1: Poll Timer Overflow Crash

## Summary

Telegram Desktop crashes on startup (SIGABRT) when loading a forum/group
containing a poll with `close_date` more than ~24.8 days in the future.
The poll closing timer value overflows `INT_MAX` milliseconds.

## Environment

- First seen: Telegram Desktop 6.4.1 (`/nix/store/dn3kb64kyhhg9h07l6sqk6hhrpg5kpj3-telegram-desktop-6.4.1`), 2026-04-06
- Still reproduces on Telegram Desktop 6.6.2 (`/nix/store/fr4f8n1ix5azxazf2s3cjp17c0ql6s7z-telegram-desktop-6.6.2`), 2026-05-04..05 (6+ SIGABRTs)
- NixOS 25.11 (wyrm2; reportedly also reproduces on at least one other NixOS host)
- Qt6, OpenGL ES 3.2, NVIDIA GeForce RTX 5090, driver 580.119.02 / 580.142
- Same crashing-thread call chain on 6.6.2: `Forum::applyReceivedTopics` → `processMessages` → `processPoll` → `Timer::start` → assertion in `Timer::setTimeout` (`timer.cpp:100`).

## Symptoms

- App starts, loads chats, locks up briefly, then crashes
- Three reproducible crashes on 2026-04-06 (PIDs 2553129, 2565906, 3576868)
- Coredumps show SIGABRT in all cases

## Log output

```
[2026.04.06 14:06:44] Assertion Failed! "timeout >= 0 && timeout <= std::numeric_limits<int>::max()" timer.cpp:100
```

## Backtrace

```
#0  __pthread_kill_implementation (libc.so.6)
#1  raise (libc.so.6)
#2  abort (libc.so.6)
#3  base::Timer::setTimeout(long)
#4  base::Timer::start(long, Qt::TimerType, Timer::Repeat)
#5  Data::Session::processPoll(MTPpoll const&)
#6  Data::Session::processPoll(MTPDmessageMediaPoll const&)
#7  HistoryItem::CreateMedia(...)
#8  HistoryItem::setMedia(...)
#9  HistoryItem::HistoryItem(...)
#10 History::createItem(...)
#11 History::addNewMessage(...)
#12 Data::Session::addNewMessage(...)
#13 Data::Session::processMessages(...)
#14 Data::Forum::applyReceivedTopics(...)
#15 Data::Forum::applyReceivedTopics(...)  [overload]
#16 ... MTP callback chain ...
```

## Root Cause

`checkPollsClosings()` in
`Telegram/SourceFiles/data/data_session.cpp` (~line 4353):

```cpp
if (closest) {
    _pollsClosingTimer.callOnce((closest - now) * crl::time(1000));
}
```

`closest` is a Unix timestamp (`TimeId`, i.e., `int32`) of the nearest poll
close date. `now` is `base::unixtime::now()`. The difference in seconds is
multiplied by 1000 to get milliseconds, then passed to `callOnce()`.

`Timer::setTimeout()` in `desktop-app/lib_base` at `base/timer.cpp:100`
asserts:

```cpp
Expects(timeout >= 0 && timeout <= std::numeric_limits<int>::max());
```

`std::numeric_limits<int>::max()` = 2,147,483,647 ms = ~24.855 days.

Any poll with `close_date` more than ~25 days out overflows this and crashes
the client.

## Precedent

The exact same class of bug was fixed for TTL (auto-delete) timers in 2021:

- Commit: `b2e829904fb7976784618dea18700dbb5568b42f`
- Fix: cap timeout at `TimeId(86400)` (1 day) before multiplying by 1000
- Debian bug: [#993243](https://bugs.debian.org/993243)
- GitHub issue: [telegramdesktop/tdesktop#16719](https://github.com/telegramdesktop/tdesktop/issues/16719)

The poll closing timer was not given the same treatment.

## Suggested Fix

```cpp
if (closest) {
    const auto maxTimeout = 24 * 3600 * crl::time(1000);  // 1 day in ms
    _pollsClosingTimer.callOnce(
        std::min((closest - now) * crl::time(1000), maxTimeout));
}
```

This matches the pattern used by `scheduleNextTTLs()` and
`scheduleNextFormattedDateUpdate()` in the same file.

## Clock Skew Ruled Out

```
NTP service: active
System clock synchronized: yes
```

Local clock matched Google's HTTP `Date` header within 1 second. Not a clock
issue.

## Workaround

Use Telegram on mobile to find and close (or shorten the duration of) any
polls in groups/forums with a close date far in the future. Alternatively,
remove the local cache to skip past the problematic chat on next load:

```bash
cp -r ~/.local/share/TelegramDesktop/tdata ~/.local/share/TelegramDesktop/tdata.bak
# then selectively clear cache, or log out and back in
```

**Note**: a full tdata wipe + re-login does **not** help. Once the forum's
topics re-sync, the same `MTPpoll` arrives via `Forum::applyReceivedTopics`
and re-triggers the assertion. The poll has to be closed/shortened
server-side (mobile/web), or the client patched, or the bad poll's chat
filtered out.

## Recovery Options

Ranked from least invasive to most. Pick by goal: "just need to read
Telegram" → A or B. "want continuity / programmatic access" → D.

### A. Telegram Web (immediate, zero work)

Open <https://web.telegram.org/k/> in a browser, log in via QR scan from
phone. Different codebase, no poll-timer bug. **Fastest path back to
working Telegram.** Doesn't fix desktop, but unblocks reading/sending.

### B. Close offending poll from mobile (immediate, fixes desktop)

Open Telegram on phone, scan groups/forums you're in for a poll closing
more than 25 days out, close it (or shorten its duration to under 25 days).
Next desktop launch will succeed. Annoying because there's no good way to
enumerate "all polls with `close_date > now+25d`" from the client side; in
practice recent forum activity is the place to look.

### C. Patched telegram-desktop derivation (available, not yet wired)

Implemented at <nix/packages/telegram-desktop.nix>, with the patch at
<nix/packages/patches/telegram-desktop-poll-timer-debug.patch>. Exposed
as `ducktapePackages.telegram-desktop` but **not yet swapped into any
host's `home.packages`** — pending validation via the manual
clone+ninja loop at
<nix/packages/patches/telegram_desktop_iterate.md>. To deploy on a
host, replace `pkgs.telegram-desktop` with
`ducktapePackages.telegram-desktop` in the host's home-manager file
(e.g., <nix/home/hosts/wyrm2.nix>), then `home-manager switch
--flake .#<host>`.

The patch:

1. Clamps `_pollsClosingTimer.callOnce(...)` at 1 day, mirroring the
   2021 TTL-timer fix (`b2e8299...`). The timer simply re-fires daily
   until the real `close_date` is within INT_MAX ms.
2. When clamping kicks in, logs the offending poll's id, closeDate,
   computed delta in days, and first 120 chars of question text. Greppable
   via `grep "Poll Debug" ~/.local/share/TelegramDesktop/log.txt`.

The derivation also sets `cmakeBuildType = "RelWithDebInfo"` and
`dontStrip = true`, so any future telegram-desktop coredumps will have
DWARF + a build-id (the stock nixpkgs build has neither, which made the
original RCA expensive — function symbols only, no struct walking).

Cost: one full tdesktop rebuild per package bump (hours, large closure).
Substituters won't have it — this is local-only. Carry until the
upstream poll-timer fix lands.

For tweaking the patch itself (log line shape, clamp threshold, debug
instrumentation) without paying for the full nix rebuild on every
edit, use the Docker-based recipe at
<../nix/packages/patches/telegram_desktop_iterate.md>. Build through
tdesktop's official `tdesktop:centos_env` image (Rocky 8 + statically
compiled deps) against a local clone — incremental ninja rebuilds
after the first full build drop edit→binary to seconds.

### D. Extract MTProto session from tdata → drive Telethon / API directly

Telegram Desktop stores its MTProto auth key under
`~/.local/share/TelegramDesktop/tdata/`. With no local passcode set
this decrypts with a default key; with one, you need that passcode.

Tool: [OpenTele](https://github.com/thedemons/opentele) — Python lib that
reads tdata and emits a Telethon `.session` (SQLite). From there:
message export, userbot, MCP server backend, etc.

Sketch (uv hashbang, no manual venv per repo policy):

```python
#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["opentele", "telethon"]
# ///
import asyncio
from opentele.td import TDesktop
from opentele.api import UseCurrentSession

async def main():
    tdata = TDesktop("/home/agentydragon/.local/share/TelegramDesktop/tdata")
    assert tdata.isLoaded()
    client = await tdata.ToTelethon("rai.session", flag=UseCurrentSession)
    await client.connect()
    print((await client.get_me()).stringify())

asyncio.run(main())
```

Output `.session` is a full credential — anyone with the file has
account access (and can bypass 2FA on existing sessions). Treat like a
private key; don't commit, don't ship to a multi-tenant box without
encryption. If a local passcode is set, pass `passcode=` to `TDesktop`.

### E. Filter the bad chat from sync (not implemented)

Telegram Desktop has no public knob to skip a specific chat/forum from
initial sync. Possible but invasive: archive + mute on mobile won't help
(forum topics still come through `applyReceivedTopics`). Leaving the
forum on mobile would, but loses the chat. Not recommended unless A–D
all fail.

## Upstream

The fix is local (5 lines, well-precedented by the 2021 TTL-timer fix at
`b2e829904fb7976784618dea18700dbb5568b42f`). No upstream issue or PR
filed yet. **Action item**: file
<https://github.com/telegramdesktop/tdesktop/issues> with this RCA + a
PR carrying the patch. Worth doing — bug clearly affects more than one
user (multi-host repro here, and the TTL precedent shows tdesktop
maintainers will accept the fix).

## Status

- **Upstream issue**: not yet filed
- **Upstream repo**: <https://github.com/telegramdesktop/tdesktop>
- **Affected component**: `Telegram/SourceFiles/data/data_session.cpp` (`checkPollsClosings`)
- **Affected library**: `desktop-app/lib_base` (`base/timer.cpp`, `Timer::setTimeout`)
- **Versions confirmed broken**: 6.4.1 (2026-04-06), 6.6.2 (2026-05-04..05). 8 months,
  two minor releases later, no fix.
