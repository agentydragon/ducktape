def claude_user_text:
  if (.message.content | type) == "string" then
    .message.content
  elif (.message.content | type) == "array" then
    [.message.content[]? | select(.type == "text") | .text // ""] | join("\n")
  else
    ""
  end;

def codex_user_text:
  [.payload.content[]? |
    select(.type == "input_text" or .type == "text") | .text // ""] | join("\n");

def user_text($entry):
  if $harness == "claude" then ($entry | claude_user_text)
  else ($entry | codex_user_text)
  end;

def is_user($entry):
  if $harness == "claude" then
    ($entry.type == "user" and (($entry | claude_user_text) | length > 0)
     and (($entry | claude_user_text | startswith("<task-notification>") | not))
     and (($entry | claude_user_text | startswith("<system-reminder>") | not)))
  else
    ($entry.type == "response_item" and $entry.payload.type == "message"
     and $entry.payload.role == "user" and (($entry | codex_user_text) | length > 0))
  end;

def assistant_text($entry):
  if $harness == "claude" then
    [.message.content[]? |
      if .type == "text" then "[text]\n" + (.text // "")
      elif .type == "thinking" then "[thinking]\n" + (.thinking // "")
      elif .type == "tool_use" then "[tool_use: " + (.name // "unknown") + "]"
      else empty
      end] | join("\n")
  else
    [.payload.content[]? | select(.type == "output_text" or .type == "text") |
      .text // ""] | join("\n")
  end;

def is_assistant($entry):
  if $harness == "claude" then $entry.type == "assistant"
  else ($entry.type == "response_item" and $entry.payload.type == "message"
        and $entry.payload.role == "assistant")
  end;

def is_compaction($entry):
  if $harness == "claude" then
    ($entry.type == "system" and $entry.subtype == "compact_boundary")
  else
    ($entry.type == "event_msg" and $entry.payload.type == "context_compacted")
  end;

def display_text($text):
  "...(cut; pass --max-display-text-length >= 100)..." as $marker |
  if ($text | length) <= $max_display_text_length then
    $text
  else
    ($max_display_text_length - ($marker | length)) as $remaining |
    (($remaining / 2) | floor) as $prefix_length |
    ($remaining - $prefix_length) as $suffix_length |
    ($text[:$prefix_length] + $marker + $text[-$suffix_length:])
  end;

def render_recent:
  if length == 0 then
    "(no preceding assistant message in transcript)\n"
  else
    to_entries |
    map("--- preceding assistant message \(.key + 1) @ \(.value.timestamp) ---\n\(.value.text | display_text(.))\n") |
    join("\n")
  end;

foreach .[] as $entry
  ({recent: [], user_count: 0};
   .emitted = null |
   if is_compaction($entry) then
     .emitted = "\n### Compaction marker @ " + ($entry.timestamp // "unknown") +
       " — keep scanning; earlier JSONL entries remain part of this conversation.\n"
   elif is_assistant($entry) then
     .recent += [{timestamp: ($entry.timestamp // "unknown"), text: ($entry | assistant_text($entry))}] |
     .recent = .recent[-2:]
   elif is_user($entry) then
     .user_count += 1 |
     ($entry | user_text($entry)) as $text |
     .emitted =
       ("\n## User message " + (.user_count | tostring) + " @ " + ($entry.timestamp // "unknown") + "\n" +
        (.recent | render_recent) +
        "--- user message ---\n" + ($text | display_text(.)) + "\n")
   else
     .
   end;
   .emitted // empty)
