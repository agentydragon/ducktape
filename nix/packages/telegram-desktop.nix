# Patched telegram-desktop:
#   1. Fix for the poll-closing timer INT_MAX overflow that crashes the
#      client at startup whenever a forum chat contains a poll closing
#      more than ~24.8 days out (assertion in base::Timer::setTimeout,
#      timer.cpp:100). Same fix shape as the 2021 TTL-timer fix
#      (b2e8299...). Logs the offending poll's id, close_date, and
#      question text whenever clamping kicks in so the user can find
#      and close the bad poll on mobile.
#
#   2. Built RelWithDebInfo + dontStrip so future telegram-desktop
#      coredumps have DWARF and a build-id, and gdb can show source
#      lines and walk struct fields. The stock nixpkgs binary has
#      neither, which is what made the original RCA expensive.
#
# See <debug/telegram_poll_timer_crash.md>.
# Patch-iteration recipe: <patches/telegram_desktop_iterate.md>.
{ telegram-desktop }:
telegram-desktop.override {
  unwrapped = telegram-desktop.unwrapped.overrideAttrs (old: {
    patches = (old.patches or [ ]) ++ [
      ./patches/telegram-desktop-poll-timer-debug.patch
    ];
    dontStrip = true;
    cmakeBuildType = "RelWithDebInfo";
  });
}
