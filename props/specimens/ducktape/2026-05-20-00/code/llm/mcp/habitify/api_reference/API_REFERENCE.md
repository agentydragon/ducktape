# Habitify API Reference

This documentation outlines the available endpoints and usage patterns for the Habitify API based on collected examples and responses.

## Base URL

All API requests should be made to: `https://api.habitify.me`

## Authentication

Authentication is required for all API requests:

```
Authorization: Bearer YOUR_API_KEY
Content-Type: application/json
```

Replace `YOUR_API_KEY` with your Habitify API key.

## Date Format Requirements

All date-dependent endpoints require a specific date format:

1. **Required Format**: ISO-8601 format with timezone: `YYYY-MM-DDThh:mm:ss±hh:mm`
2. **URL Parameters**: When used in URL parameters, the date must be URL-encoded
3. **Example**: For date "May 9, 2025" at UTC, use `2025-05-09T00:00:00+00:00`

Using an incorrect date format will result in a 500 error with the message:

```
"Only accept the format of target_date is YYYY-MM-DDThh:mm:ss±hh:mm"
```

## Response Format

All successful API responses follow this general structure:

```json
{
  "message": "Success",
  "data": [...],  // Response data varies by endpoint
  "version": "v1.2",
  "status": true,
  "errors": []
}
```

Error responses typically have:

```json
{
  "message": "Error message here",
  "data": null,
  "version": "v1.2",
  "status": false,
  "errors": []
}
```

## Available Endpoints

### 1. List All Habits

**Endpoint:** `GET /habits`

**Description:** Retrieve all habits in your Habitify account.

**Request:**

```
GET https://api.habitify.me/habits
```

**Response (200 OK):**

```json
{
  "message": "Success",
  "data": [
    {
      "id": "habit-id-1",
      "name": "Habit Name",
      "is_archived": false,
      "start_date": "2021-11-10T09:08:20.000Z",
      "time_of_day": ["any_time"],
      "goal": {
        "unit_type": "rep",
        "value": 1,
        "periodicity": "daily"
      },
      "goal_history_items": [...],
      "log_method": "manual",
      "recurrence": "DTSTART:20211110T090820Z\nRRULE:FREQ=DAILY",
      "remind": [],
      "area": null,
      "created_date": "2021-11-10T09:08:20.000Z",
      "priority": 7.922816251426434e+28
    },
    // More habits...
  ],
  "version": "v1.2",
  "status": true,
  "errors": []
}
```

### 2. Get Habit Details

**Endpoint:** `GET /habits/{habit_id}`

**Description:** Retrieve detailed information about a specific habit.

**Request:**

```
GET https://api.habitify.me/habits/{habit_id}
```

**Response (200 OK):**

```json
{
  "message": "Success",
  "data": {
    "id": "habit-id",
    "name": "Habit Name",
    "is_archived": false,
    "start_date": "2021-11-10T09:08:20.000Z",
    "time_of_day": ["any_time"],
    "goal": {
      "unit_type": "rep",
      "value": 1,
      "periodicity": "daily"
    },
    "goal_history_items": [...],
    "log_method": "manual",
    "recurrence": "DTSTART:20211110T090820Z\nRRULE:FREQ=DAILY",
    "remind": [],
    "area": null,
    "created_date": "2021-11-10T09:08:20.000Z",
    "priority": 7.922816251426434e+28
  },
  "version": "v1.2",
  "status": true,
  "errors": []
}
```

**Error Response (500):**

```json
{
  "message": "The habit does not exist",
  "data": null,
  "version": "v1.2",
  "status": false,
  "errors": []
}
```

### 3. Get Habit Status

**Endpoint:** `GET /status/{habit_id}`

**Description:** Get the status of a habit for a specific date.

**Parameters:**

- `target_date` (required): Date in ISO-8601 format with timezone (YYYY-MM-DDThh:mm:ss±hh:mm)

**Request:**

```
GET https://api.habitify.me/status/{habit_id}?target_date=2025-05-04T00:00:00+00:00
```

**Response (200 OK):**

```json
{
  "message": "Success",
  "data": {
    "status": "completed" // Possible values: "completed", "failed", "skipped", "none", "in_progress"
  },
  "version": "v1.2",
  "status": true,
  "errors": []
}
```

**Status Values:**

| Status      | Description                                                  |
| ----------- | ------------------------------------------------------------ |
| completed   | The habit was successfully completed for the date            |
| failed      | The habit was attempted but not completed successfully       |
| skipped     | The habit was intentionally skipped for the date             |
| none        | No action has been taken on the habit for the date (default) |
| in_progress | The habit is partially completed (for habits with goals)     |

**Error Response (500):**
Invalid date format:

```json
{
  "message": "Only accept the format of target_date is YYYY-MM-DDThh:mm:ss±hh:mm",
  "data": null,
  "version": "v1.2",
  "status": false,
  "errors": []
}
```

### 4. Update Habit Status

**Endpoint:** `PUT /status/{habit_id}`

**Description:** Update the status of a habit for a specific date.

**Request Body:**

```json
{
  "status": "completed", // Possible values: "completed", "failed", "skipped"
  "target_date": "2025-05-04T00:00:00+00:00", // Required ISO format with timezone
  "note": "Optional note", // Optional
  "value": 1.0 // Optional, for habits with goals
}
```

**Response (200 OK):**

```json
{
  "message": "Success",
  "data": null,
  "version": "v1.2",
  "status": true,
  "errors": []
}
```

See corresponding YAML example file for the complete request and response structure.

**Notes:**

- For habits without goals, you can omit the `value` field
- The `note` field is optional and allows you to add a comment to the habit log
- This is the only way to add notes to habits - notes are attached to status updates for specific dates
- Once a note is added, it's stored with the habit status for that date

### 5. List Areas/Categories

**Endpoint:** `GET /areas`

**Description:** Get all habit areas (categories) in your Habitify account.

See corresponding YAML example file for the complete request and response structure.

### 6. Journal (Daily Habits View)

**Endpoint:** `GET /journal`

**Description:** Get habits for a specific date with their status information.

**Parameters:**

- `target_date` (required): Date in ISO-8601 format with timezone (YYYY-MM-DDThh:mm:ss±hh:mm)
- `order_by` (optional): Order habits by ("priority", "reminder_time", "status")
- `status` (optional): Filter by status (comma-separated: "none", "completed", "failed", "skipped", "in_progress")
- `time_of_day` (optional): Filter by time of day (comma-separated: "morning", "afternoon", "evening", "any_time")
- `area_id` (optional): Filter by area/category ID

See corresponding YAML example file for the complete request and response structure.

## Write Operations and Limitations

The following write operations were tested but returned 404 errors, indicating they may not be supported in the current API version:

- `POST /habits` - Create a new habit
- `POST /areas` - Create a new area/category
- `PUT /habits/{habit_id}` - Update a habit
- `DELETE /habits/{habit_id}` - Delete a habit

### Missing or Limited Functionality

Based on our testing and the Habitify documentation, the following functionality appears to be limited or unavailable in the current API version:

The only working write operations are habit status updates via `PUT /status/{habit_id}`. There might be more to implement but currently not implemented.

This suggests that the current API is primarily read-only, with limited write functionality focused on habit status updates.

## Habit Object Structure

A habit object typically includes:

| Field              | Type    | Description                                                                |
| ------------------ | ------- | -------------------------------------------------------------------------- |
| id                 | string  | Unique habit identifier                                                    |
| name               | string  | Habit name                                                                 |
| is_archived        | boolean | Whether the habit is archived                                              |
| start_date         | string  | Start date of the habit (ISO-8601)                                         |
| time_of_day        | array   | When the habit should be performed (morning, afternoon, evening, any_time) |
| goal               | object  | Goal configuration (null for habits without goals)                         |
| goal_history_items | array   | History of goal changes                                                    |
| log_method         | string  | How the habit is logged (e.g., "manual")                                   |
| recurrence         | string  | Recurrence pattern in iCalendar format                                     |
| remind             | array   | Reminder times                                                             |
| area               | object  | Category/area the habit belongs to (null if none)                          |
| created_date       | string  | Creation date (ISO-8601)                                                   |
| priority           | number  | Priority value used for sorting                                            |

When retrieved via `/journal`, habits also include:

- `status` - Current status for the specified date
- `progress` - Progress information for habits with goals
- `habit_type` - Habit type identifier

## Goal Object Structure

A goal object typically includes:

| Field       | Type   | Description                                   |
| ----------- | ------ | --------------------------------------------- |
| unit_type   | string | Unit of measurement (e.g., "rep", "min")      |
| value       | number | Target value                                  |
| periodicity | string | Goal frequency ("daily", "weekly", "monthly") |

### Goal Types and Units

The Habitify API supports several types of goals and measurement units:

1. **Repetition Goals**
   - `unit_type`: "rep"
   - `value`: Number of repetitions (e.g., 1, 5, 10)
   - Example: "Do 10 pushups daily"

2. **Time-based Goals**
   - `unit_type`: "min" or "hr"
   - `value`: Duration in minutes or hours
   - Example: "Meditate for 20 minutes daily"

3. **Periodicity Options**
   - `daily`: Goal resets each day
   - `weekly`: Goal resets each week
   - `monthly`: Goal resets each month

### Updating Goals

When updating a habit status with goals:

1. Use the `value` field to specify progress toward the goal
2. For habits without explicit goals, omit the `value` field and just use status
3. The `status` can be set independently of goal progress

**Example**: For a habit with a goal of "10 reps daily":

- Partial completion: Set `value` to a number between 0-10 and `status` to "in_progress"
- Full completion: Set `value` to 10 and `status` to "completed"
- Failed attempt: Set `status` to "failed" with whatever `value` was achieved

## Area Object Structure

An area (category) object includes:

| Field    | Type   | Description            |
| -------- | ------ | ---------------------- |
| id       | string | Unique area identifier |
| name     | string | Area/category name     |
| priority | string | Priority level         |

## Error Handling

Common errors include:

- **500 - Habit does not exist**: When using an invalid habit ID
- **500 - Invalid date format**: When not using the required ISO-8601 format with timezone
- **404 - Endpoint not found**: When attempting to use write operations that may not be supported
