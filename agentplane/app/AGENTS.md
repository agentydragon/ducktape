# Thread browser tests

The first visit to a seeded thread bootstraps its Electric shape before tail rows appear: wait for
the tail row with `Locator.wait_for(state="visible")`, not a sleep or a longer target timeout.
