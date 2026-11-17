# Thread browser navigation waits for entity readiness

[Invocation `80e31b7f`](https://app.buildbuddy.io/invocation/80e31b7f-65df-52ac-be77-bb8abeb9d670), shard 3 of `//agentplane/app:test_thread_browser`, failed `test_switching_threads_starts_at_each_threads_tail` after switching to thread 1. The thread name assertion passed, then a five-second `expect(...).to_be_visible()` missed message 129. The failure's accessibility snapshot showed `Catching up thread…` and an empty history region.

The Playwright trace shows the destination's Electric entity and text-chunk requests succeeding during that wait, including a response containing `Thread 1 message 129`; its post-failure snapshot contains the message. The existing thread-view marks show the destination rows being measured near the assertion deadline. This is a late entity/text readiness boundary, not evidence that the frontend permanently lost the tail.

The first visit in the same test already waits for the visible tail with `Locator.wait_for(state="visible")`, as the app test instructions require while the first Electric shape bootstraps. The switch now waits for the same exact row and visible state. It keeps the older-page request, tail, navigation and screenshot checks; it does not substitute metadata readiness for content readiness.

A [fresh RBE run](https://app.buildbuddy.io/invocation/f09eb530-244e-45d3-87bf-fa1672850768) selected this navigation case and the streamed-admission case; both passed uncached on one real-browser TestRunner. This validates the predicate wait under that run, not a measured recurrence reduction.
