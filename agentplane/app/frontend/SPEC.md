# Thread history scrolling

A thread's conversation can grow long, include messages of different heights, and change while
someone is reading it. The following are promises to the reader; they do not prescribe how the
browser loads, stores, or draws the conversation. These guarantees cover the supported Chromium
browser on desktop and phone.

## Guarantees

- **Opening a thread** — including switching from another thread — scrolls to the thread's tail;
  the most recent message is what the reader sees first.
- **Recent history is immediately readable** — opening a thread shows enough preceding conversation
  to scroll naturally without a page-by-page wait. A short thread shows its full history.
- **Older history arrives before it is needed** — scrolling toward the start of the visible history
  reveals earlier messages without stopping at an empty edge.
- **An appended message, reader at the bottom**: the view follows it into view. "At the bottom"
  tolerates a small amount of slack — a reader a few pixels off the exact bottom (scroll momentum,
  subpixel rounding) still counts as following.
- **An appended message, reader scrolled up**: nothing moves. The reader keeps reading exactly
  where they were.
- **A message actively streaming, reader following, it is the last message**: the view tracks its
  growth so the growing edge stays visible — the message's own start must never scroll off the top
  of the screen while it is still generating.
- **A message actively streaming or otherwise resizing anywhere else** — off-screen above or below
  the viewport, or while the reader is not following — must not move the reader's scroll position,
  even by a pixel.
- **Opening or closing a row** — a run, a tool call, an output past its height cap — keeps the row
  the reader clicked in place, growing or shrinking below it, while messages arrive or the reader
  follows the bottom. The click disengages following until the reader returns to the bottom,
  unless the whole thread fits the view and has nothing to scroll away from.
- **A deliberate scroll** (wheel, drag, keyboard, touch) away from the bottom disengages following
  immediately, without lag or fighting the input.
- **Returning to the bottom** re-engages following, with the same tolerance as above.
- **A running thread whose end is off screen** — the reader scrolled up, or opened a row that grew past
  the view — shows a "Jump to latest" control; one click returns to the end and resumes following.
- **Scrolling into older messages** keeps the row the reader was viewing in the same place as
  earlier content appears — no blank flash or temporary jump, even in a short thread.
- **A window/viewport resize** (browser resize, mobile address-bar collapse, orientation change, an
  on-screen keyboard opening): following stays pinned to the bottom through the resize; not
  following keeps the reader's current row in place and shifts only off-screen content.
- **Text selection, copy, and find-in-page** work across the entire thread, not just the
  portion currently on screen.
- None of the above ever produces a visible flash of blank space or a scrollbar that visibly jumps
  and then corrects.
