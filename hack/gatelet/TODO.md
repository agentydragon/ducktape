# Gatelet TODO

This file lists outstanding tasks required to finish the Home Assistant API integration project described in `ha-api-task.txt`.

## Remaining work

Key-in-path and challenge-response authentication are implemented and tested.
Webhook storage and browsing work, and the admin login with key management pages
is available. The tasks below track what is still needed to fully implement the
plan in `ha-api-task.txt` plus a bunch more added.

- Finish the human admin interface:
  - ~~List and invalidate active admin sessions~~ (done)
  - ~~List and invalidate active LLM sessions~~ (done)
  - ~~Expose log inspection pages~~ (done)
- Expand the Home Assistant integration with history views for configured entities with varied interval
  sizes, practical for realistic application. Rememer the history is primarily intended for consumption
  by LLMs that don't have a graphical browser, so any fancy graphical widgets are just a waste of time.
  - ~~When showing data to a human, provide links into the Home Assistant instance for entities.~~ (done)
- Make a useful "dashboard" for LLMs listing Home Assistant & webhooks from, say, last hour, up to some limits,
  with links that let the LLM dig in, look at history, look at individual integrations, etc.
  - Establish thorough the report that we shall call this "hub" / "landing page" _dashboard_, for consistency.
  - The purpose of the dashboard is to expose to the LLM data that's most likely to useful to it _right now_
    for reacting to roughly real-time events in the user's life.
- For Home Assistant:
  - ~~Sensors that are discrete (e.g. on/off, open/closed) -> dashboard and expanded view should list current value and list of change events with timestamp.~~ (done)
  - Sensors that are continuous -> should list regularly spaced samples of values; with unit if exposed from HA. Present as table in a way that saves space.
  - Note that you'll be also showing things like states of smart plugs etc.
  - Show entities grouped by their area (from HA; again, for _human only_, linked to HA instance). Represent entities also by their display name so model better
    understands.
  - Show values from all continuous sensors on dashboard in _one table_ to save repeating datetime values, column headers, etc.
  - On dashboard, buttons to trigger actions/automations in Home Assistant, with description from JSON file.
    - Log those when model does them.
- Provide reporter scripts to send device events to Gatelet
  - Long-running deamons
  - Do not duplicate ActivityWatch
- Resolve remaining TODO comments in the code (e.g. redirect after login)
- Allow LLMs to start whitelisted automations on Home Assistant, listed in config file. Again, this must
  be done LLM-accessibly.
- Expose ActivityWatch data to LLM. Architecture:
  - Probably: ActivityWatch API server pointer in configuration. Server will aggregate data from _multiple hosts_.
  - On dashboard, show LLM current activity within say last 10 minutes, aggregated.
    Allow LLM to click around to get tables for different aggregations - different bucket sizes, aggregation on app level / window level, etc.
  - Remember: it's for LLM -> basic UI.
