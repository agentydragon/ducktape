# Thread browser tests

The first visit to a seeded thread bootstraps its Electric shape before the tail rows appear. Wait
for the actual tail row with `Locator.wait_for(state="visible")`; it returns as soon as the row is
visible and uses Playwright's normal action timeout. Do not replace it with a sleep or raise the
Bazel target timeout.
