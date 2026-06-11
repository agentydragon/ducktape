# Claude Code Hook Examples

## Common Fields

```json
{
  "session_id": "string",
  "transcript_path": "string"
}
```

## PreToolUse Input

```json
{
  "tool_name": "Write",
  "tool_input": {
    "file_path": "/path/to/file.txt",
    "content": "file content"
  }
}
```

## PostToolUse Input

```json
{
  "tool_name": "Write",
  "tool_input": {
    "file_path": "/path/to/file.txt",
    "content": "file content"
  },
  "tool_response": {
    "filePath": "/path/to/file.txt",
    "success": true
  }
}
```

## Notification Input

```json
{
  "message": "Task completed successfully",
  "title": "Claude Code"
}
```

## Continuing from Stop Hook

```json
{
  "stop_hook_active": true
}
```

## Stop Hook

Blocks stoppage, shows error to Claude.

## Common JSON Stdout

```json
{
  "continue": true,
  "stopReason": "string",
  "suppressOutput": true
}
```

- `continue`: Whether Claude should continue after hook execution (default: true)
- `stopReason`: Message shown when continue is false
- `suppressOutput`: Hide stdout from transcript mode (default: false)
