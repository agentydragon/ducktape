# Thread browser tests

The first visit to a seeded thread bootstraps its Electric shape before tail rows appear: wait for
the tail row with `Locator.wait_for(state="visible")`, not a sleep or a longer target timeout.

Tests using `conftest.py` start the Go executable through `testing/replication_process.py`; use public
HTTP/RPC operations rather than importing a serving implementation. Python app configuration models
remain a cdk8s input; HTTP response models belong in `testing/models.py` and are checked against Go OpenAPI.
