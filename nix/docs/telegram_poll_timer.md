# Telegram Desktop poll timer crash

Telegram Desktop 6.4.1 crashed on startup on 2026-04-06 while loading a forum
with a poll closing more than about 24.8 days later. The same crash reproduced
on 6.6.2 on 2026-05-04–05, with six or more `SIGABRT`s.

The assertion was:

```text
Assertion Failed! "timeout >= 0 && timeout <= std::numeric_limits<int>::max()" timer.cpp:100
```

The observed call chain was `Forum::applyReceivedTopics` → `processMessages` →
`processPoll` → `Timer::start` → `base::Timer::setTimeout`. In the affected
versions, `Data::Session::checkPollsClosings()` passed
`(closest - now) * 1000` milliseconds to the timer without a bound. The timer
accepts at most `INT_MAX` milliseconds (2,147,483,647, or about 24.855 days),
so a more distant `close_date` triggered the assertion. A similar one-day cap
was used for TTL timers in [upstream commit
`b2e8299`](https://github.com/telegramdesktop/tdesktop/commit/b2e829904fb7976784618dea18700dbb5568b42f).

The current root [flake lock](../../flake.lock) selects the `nixpkgs` revision
[`b349185`](https://github.com/NixOS/nixpkgs/commit/b349185bb79c7cf7ad8ea8c097931730bf473c71),
whose [Telegram Desktop derivation](https://github.com/NixOS/nixpkgs/blob/b349185bb79c7cf7ad8ea8c097931730bf473c71/pkgs/applications/networking/instant-messengers/telegram/telegram-desktop/unwrapped.nix)
sets version `7.2.9`. Its
[`data_session.cpp`](https://github.com/telegramdesktop/tdesktop/blob/v7.2.9/Telegram/SourceFiles/data/data_session.cpp#L5004-L5008)
caps the poll timer at 24 hours and checks again later. This confirms the
source selected by the current lock; it does not establish which package a
host has activated.

If the crash recurs, record the activated Telegram store path and backtrace
before treating the historical reproduction as current. The original stock
build's stripped symbols left only function names in its core dumps, so the
exact package version and source were needed to locate the failing timer.
