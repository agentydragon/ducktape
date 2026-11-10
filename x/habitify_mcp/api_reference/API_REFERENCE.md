# Habitify API reference

These are observations from the retained request/response captures and prior probes, not a fresh check of the live API. The endpoint table links every retained YAML example. Base URL: `https://api.habitify.me`.

## Authentication

The client sends `Authorization: <API key>` with no `Bearer` prefix, plus `Content-Type: application/json`. Captures mask the key as `API_KEY_MASKED`; see [`habitify_client.py`](../habitify_client.py) and [`collect_references.py`](collect_references.py).

## Dates and observed errors

Endpoints that take `target_date` use `YYYY-MM-DDThh:mm:ss±hh:mm`; encode it when placing it in a URL query. The retained capture accepted `2025-05-09T00:00:00+00:00` for `GET /status/{habit_id}`. Earlier probes recorded `Z`, a bare date, and a date-time without an offset as rejected; only the bare-date rejection has a retained `500` response. Omitting `target_date` was previously recorded as `412`, without a retained response capture. The captured invalid-date response says `Only accept the format of target_date is YYYY-MM-DDThh:mm:ss±hh:mm`: [`get_habit_status.yaml`](get_habit_status.yaml), [`get_habit_status_(invalid_date_format).yaml`](<get_habit_status_(invalid_date_format).yaml>).

Invalid IDs for both `GET /habits/{habit_id}` and `GET /status/{habit_id}` were captured as `500` with `The habit does not exist` (not `404`): [`get_habit_invalid_id.yaml`](get_habit_invalid_id.yaml), [`get_status_invalid_id.yaml`](get_status_invalid_id.yaml).

Captured JSON responses use `message`, `data`, `version`, boolean `status`, and `errors`; successful status writes return `data: null`.

## Endpoints and fields

| Endpoint                 | Parameters or body                                                              | Captures                                                                                                                                                                                                                                                                                           |
| ------------------------ | ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /habits`            | None                                                                            | [`get_habits.yaml`](get_habits.yaml)                                                                                                                                                                                                                                                               |
| `GET /habits/{habit_id}` | Habit ID in path                                                                | [`get_habit_by_id.yaml`](get_habit_by_id.yaml), [`get_habit_invalid_id.yaml`](get_habit_invalid_id.yaml)                                                                                                                                                                                           |
| `GET /status/{habit_id}` | Required `target_date`                                                          | [`get_habit_status.yaml`](get_habit_status.yaml), [`get_status_invalid_id.yaml`](get_status_invalid_id.yaml), [`get_habit_status_(invalid_date_format).yaml`](<get_habit_status_(invalid_date_format).yaml>)                                                                                       |
| `PUT /status/{habit_id}` | `status`, `target_date`; optional `note` and `value`                            | [`set_habit_status_(completed).yaml`](<set_habit_status_(completed).yaml>), [`set_habit_status_(skipped).yaml`](<set_habit_status_(skipped).yaml>), [`set_habit_status_(failed).yaml`](<set_habit_status_(failed).yaml>), [`set_habit_status_(no_value).yaml`](<set_habit_status_(no_value).yaml>) |
| `GET /areas`             | None                                                                            | [`get_areas.yaml`](get_areas.yaml)                                                                                                                                                                                                                                                                 |
| `GET /journal`           | Required `target_date`; optional `order_by`, `status`, `time_of_day`, `area_id` | [`get_journal.yaml`](get_journal.yaml), [`get_journal_filtered.yaml`](get_journal_filtered.yaml)                                                                                                                                                                                                   |

The journal filters documented here are `order_by=priority`, `reminder_time`, or `status`; comma-separated `status` values `none`, `in_progress`, `completed`, `failed`, `skipped`; comma-separated `time_of_day` values `morning`, `afternoon`, `evening`, `any_time`; and `area_id`.

| Habit field                  | Type           | Meaning                                                     |
| ---------------------------- | -------------- | ----------------------------------------------------------- |
| `id`, `name`                 | string         | Identifier and name                                         |
| `is_archived`                | boolean        | Whether archived                                            |
| `start_date`, `created_date` | string         | ISO-8601 timestamps                                         |
| `time_of_day`                | array          | Scheduled time(s): morning, afternoon, evening, or any time |
| `goal`                       | object or null | Goal configuration; see below                               |
| `goal_history_items`         | array          | Goal-change history                                         |
| `log_method`                 | string         | How the habit is logged, such as `manual`                   |
| `recurrence`                 | string         | iCalendar recurrence rule                                   |
| `remind`                     | array          | Reminder times                                              |
| `area`                       | object or null | Category/area                                               |
| `priority`                   | number         | Sorting priority                                            |

Journal records also include `status`, `progress` for goal habits, and `habit_type`. An area has `id` and `name` as strings and `priority` as a string.

| Goal field    | Type   | Meaning                                                                |
| ------------- | ------ | ---------------------------------------------------------------------- |
| `unit_type`   | string | Measurement unit, including repetitions (`rep`) and time (`min`, `hr`) |
| `value`       | number | Target value                                                           |
| `periodicity` | string | `daily`, `weekly`, or `monthly`; the goal resets at that interval      |

The documented status vocabulary is:

| Status        | Meaning                                  | Retained example              |
| ------------- | ---------------------------------------- | ----------------------------- |
| `completed`   | Successfully completed for the date      | Status read and status writes |
| `failed`      | Attempted but not completed successfully | Status write                  |
| `skipped`     | Intentionally skipped for the date       | Status write                  |
| `none`        | No action taken for the date (default)   | Journal response              |
| `in_progress` | Partially completed, for a goal habit    | Journal response              |

For goal habits, `value` records progress; a partial value can accompany `in_progress`, and the target value can accompany `completed`. Status is independent of goal progress. The captured completion without `value` demonstrates omission for a completion-only habit; `failed` and `skipped` may also omit it. Captured writes exercise `completed`, `skipped`, and `failed`.

`note` is optional on `PUT /status/{habit_id}` and is attached to that status update's date. The prior guide documented status updates as the only way to add a note; no separate notes endpoint is covered here. Retained writes include notes with valued and valueless status updates.

## Historical unsupported-write observations

Earlier probes of `POST /habits`, `POST /areas`, `PUT /habits/{habit_id}`, and `DELETE /habits/{habit_id}` returned `404`. This records what those probes observed; it is not a current support guarantee. The retained examples cover status updates as the write operation exercised successfully.
