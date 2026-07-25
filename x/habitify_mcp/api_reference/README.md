# Habitify API Reference Examples

This directory contains reference examples of Habitify API requests and responses. These examples are useful for:

1. Understanding the Habitify API structure and behavior
2. Implementing clients that interact with the Habitify API
3. Testing mock implementations
4. Documenting the API capabilities and expected responses

## Example Format

Each API endpoint has its own YAML file containing:

- `name`: Human-readable name of the API operation
- `request`: Details of the API request, containing:
  - `method`: HTTP method (GET, POST, PUT, DELETE)
  - `url`: Full URL including the base URL and endpoint path
  - `headers`: HTTP headers with the API key properly masked for security
  - `params`: Query parameters (for GET requests)
  - `body`: Request body (for POST/PUT requests)
- `response`: The API response, containing:
  - `status_code`: HTTP status code
  - `headers`: Response headers
  - `json`: Response body as JSON (or `text` if not valid JSON)

## Available References

This directory includes both successful API call examples and deliberately documented error cases.

### Successful Operations

| File                                | Description                                                    |
| ----------------------------------- | -------------------------------------------------------------- |
| `get_habits.yaml`                   | List all habits                                                |
| `get_habit_by_id.yaml`              | Get details for a specific habit                               |
| `get_habit_status.yaml`             | Get habit status with ISO-8601 date format and +00:00 timezone |
| `get_journal.yaml`                  | Get all habits for a specific day (basic journal view)         |
| `get_journal_filtered.yaml`         | Get filtered habits (by time_of_day and status)                |
| `get_areas.yaml`                    | Get all areas (categories) from the account                    |
| `set_habit_status_(completed).yaml` | Set habit status to "completed" with value                     |
| `set_habit_status_(skipped).yaml`   | Set habit status to "skipped"                                  |
| `set_habit_status_(failed).yaml`    | Set habit status to "failed"                                   |
| `set_habit_status_(no_value).yaml`  | Set habit status to "completed" without value parameter        |

### Error Cases

| File                                          | Description                                                                                                    |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `get_habit_invalid_id.yaml`                   | Example of requesting a non-existent habit ID (returns 500 with "The habit does not exist" message)            |
| `get_status_invalid_id.yaml`                  | Example of requesting status for a non-existent habit ID (returns 500 with "The habit does not exist" message) |
| `get_habit_status_(invalid_date_format).yaml` | Example of invalid date format (returns 500 with format requirement message)                                   |

Additional write operations (create, update, delete) will be added in future updates.

## Generating Fresh Examples

To generate fresh reference examples:

```bash
# Set your API key
export HABITIFY_API_KEY=your_api_key_here

# Run the collector script
python collect_references.py
```

The script will:

1. Retrieve the list of habits (successful API call)
2. Select an existing habit from your account
3. Fetch details for that habit (successful API call)
4. Deliberately generate and document expected error cases
5. Save all API interactions (both successful and deliberate errors) as reference files

The script follows a "fail fast" philosophy - it expects certain operations to succeed and certain errors to occur exactly as expected.
If anything deviates from these expectations, the script will exit with an error code.

## Security Note

API key is automatically masked in saved files, showing only first and last 4 characters. The full API key is never saved to disk.

## Confirmed Working API Endpoints

Based on our testing and the [official Habitify API documentation](https://docs.habitify.me/):

- `GET /habits` - Get list of all habits
- `GET /habits/{id}` - Get details for a specific habit by ID
- `GET /status/{id}` - Get habit status for a specific date
- `PUT /status/{id}` - Update habit status (completed, failed, or skipped)
- `GET /journal` - Get filtered habits based on date and other parameters
- `GET /areas` - Get all habit categories/areas

`/journal` filtering parameters:

- `target_date` - **Required**, in ISO-8601 format with timezone
- `order_by` - Options: `priority`, `reminder_time`, `status`
- `status` - Filter by status (comma-separated list): `none`, `in_progress`, `completed`, `failed`, `skipped`
- `time_of_day` - Filter by time (comma-separated list): `morning`, `afternoon`, `evening`, `any_time`
- `area_id` - Filter by specific area/category ID

Invalid habit IDs (`/habits/invalid-id`, `/status/invalid-id`) return **500** (not 404) with "The habit does not exist".

## Date Format Requirements

Per the [official docs](https://docs.habitify.me/date-format), all date-dependent endpoints require ISO-8601 with an explicit timezone offset: `YYYY-MM-DDThh:mm:ss±hh:mm` (URL-encoded in query params). Confirmed by testing:

- ✅ **Works**: `2023-05-21T00:00:00+00:00`
- ❌ **Fails**: `2023-05-21T00:00:00Z`, `2023-05-21`, `2023-05-21T00:00:00` — the `Z` shorthand and bare-date/no-timezone forms are all rejected
- ❌ **Fails**: omitting the date parameter (412 Precondition Failed)

```python
import datetime
import urllib.parse

date = datetime.datetime.now()
formatted_date = f"{date.strftime('%Y-%m-%d')}T00:00:00+00:00"
encoded_date = urllib.parse.quote(formatted_date)
status_url = f"/status/{habit_id}?target_date={encoded_date}"
```

## Habits With and Without Values

Value-tracking habits (e.g. "Drink 8 glasses of water") accept an optional `value` param on `PUT /status/{id}` (e.g. `"value": 8.0`); completion-only habits (e.g. "Meditate") omit it. Either kind can be set to "skipped"/"failed" without a value.

See individual reference files for detailed request and response structures.
