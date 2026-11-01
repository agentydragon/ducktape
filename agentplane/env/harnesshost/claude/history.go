package claude

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/harness"
	"github.com/anthropics/anthropic-sdk-go"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

type claudeTranscriptEntry struct {
	Type        string         `json:"type"`
	UUID        string         `json:"uuid"`
	ParentUUID  *string        `json:"parentUuid"`
	IsSidechain bool           `json:"isSidechain"`
	Message     *claudeMessage `json:"message"`
}

type claudeMessage struct {
	Role    string          `json:"role"`
	Content json.RawMessage `json:"content"`
	ID      *string         `json:"id"`
}

func holdsOnlyThinking(blocks []anthropic.ContentBlockUnion) bool {
	if len(blocks) == 0 {
		return false
	}
	for _, block := range blocks {
		switch block.AsAny().(type) {
		case anthropic.ThinkingBlock, anthropic.RedactedThinkingBlock:
		default:
			return false
		}
	}
	return true
}

func answeredMessageIDs(messages []claudeMessageWithBlocks) map[string]bool {
	answered := make(map[string]bool)
	for _, message := range messages {
		if !holdsOnlyThinking(message.Blocks) && message.Message.ID != nil {
			answered[*message.Message.ID] = true
		}
	}
	return answered
}

type claudeMessageWithBlocks struct {
	Message claudeMessage
	Blocks  []anthropic.ContentBlockUnion
	Offset  int
}

// readClaudeHistory returns the items Claude's resume loader keeps from its main transcript chain.
func readClaudeHistory(directory, sessionID string) (map[string]harness.ObservedItem, error) {
	paths, err := filepath.Glob(filepath.Join(directory, "projects", "*", sessionID+".jsonl"))
	if err != nil {
		return nil, err
	}
	if len(paths) != 1 {
		return nil, fmt.Errorf("expected one Claude transcript for session %q, found %d", sessionID, len(paths))
	}
	file, err := os.Open(paths[0])
	if err != nil {
		return nil, err
	}
	defer file.Close()

	entries := make(map[string]claudeTranscriptEntry)
	var leaf *claudeTranscriptEntry
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 64*1024), harness.MaxFrameLineBytes)
	for scanner.Scan() {
		line := scanner.Bytes()
		var raw map[string]json.RawMessage
		if err := json.Unmarshal(line, &raw); err != nil {
			return nil, err
		}
		var typeName string
		if err := json.Unmarshal(raw["type"], &typeName); err == nil && typeName == "system" {
			var subtype string
			if json.Unmarshal(raw["subtype"], &subtype) == nil && subtype == "compact_boundary" {
				return nil, fmt.Errorf("Claude compacted this conversation, so items before the boundary cannot be compared")
			}
		}
		if _, hasUUID := raw["uuid"]; !hasUUID {
			continue
		}
		if _, hasParent := raw["parentUuid"]; !hasParent {
			continue
		}
		var entry claudeTranscriptEntry
		if err := json.Unmarshal(line, &entry); err != nil {
			return nil, err
		}
		if entry.Type != "user" && entry.Type != "assistant" {
			entry.Message = nil
		}
		entries[entry.UUID] = entry
		if (entry.Type == "user" || entry.Type == "assistant") && !entry.IsSidechain {
			copy := entry
			leaf = &copy
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}

	chain := make([]claudeTranscriptEntry, 0)
	visited := make(map[string]bool)
	for leaf != nil {
		if visited[leaf.UUID] {
			return nil, fmt.Errorf("cycle in Claude's persisted conversation")
		}
		visited[leaf.UUID] = true
		chain = append(chain, *leaf)
		if leaf.ParentUUID == nil {
			break
		}
		parent, ok := entries[*leaf.ParentUUID]
		if !ok {
			return nil, fmt.Errorf("Claude's persisted conversation has an entry whose parent is missing")
		}
		parentCopy := parent
		leaf = &parentCopy
	}

	resolved := make(map[string]bool)
	for _, entry := range chain {
		if entry.Message == nil {
			continue
		}
		if entry.Message.Role != "user" {
			continue
		}
		results, err := claudeToolResults(entry.Message.Content)
		if err != nil {
			return nil, err
		}
		for _, result := range results {
			resolved[result.ToolUseID] = true
		}
	}

	kept := make([]claudeMessageWithBlocks, 0)
	blockCounts := make(map[string]int)
	for i := len(chain) - 1; i >= 0; i-- {
		entry := chain[i]
		if entry.Message == nil {
			continue
		}
		if entry.Message.Role == "user" {
			kept = append(kept, claudeMessageWithBlocks{Message: *entry.Message})
			continue
		}
		blocks, err := claudeBlocks(entry.Message.Content)
		if err != nil {
			return nil, err
		}
		messageID := ""
		if entry.Message.ID != nil {
			messageID = *entry.Message.ID
		}
		offset := blockCounts[messageID]
		if entry.Message.ID != nil {
			blockCounts[messageID] = offset + len(blocks)
		}
		calls := make([]string, 0)
		for _, block := range blocks {
			if tool, ok := block.AsAny().(anthropic.ToolUseBlock); ok {
				calls = append(calls, tool.ID)
			}
		}
		allUnresolved := len(calls) > 0
		for _, call := range calls {
			if resolved[call] {
				allUnresolved = false
			}
		}
		if !allUnresolved {
			kept = append(kept, claudeMessageWithBlocks{Message: *entry.Message, Blocks: blocks, Offset: offset})
		}
	}
	answered := answeredMessageIDs(kept)
	items := make(map[string]harness.ObservedItem)
	for _, entry := range kept {
		message := entry.Message
		if holdsOnlyThinking(entry.Blocks) && (message.ID == nil || !answered[*message.ID]) {
			continue
		}
		if message.Role == "user" {
			results, err := claudeToolResults(message.Content)
			if err != nil {
				return nil, err
			}
			for _, result := range results {
				item, ok := items[result.ToolUseID]
				if !ok {
					continue
				}
				output, err := result.resultText()
				if err != nil {
					return nil, err
				}
				item.Output, item.Completed = output, true
				items[result.ToolUseID] = item
			}
			continue
		}
		for index, block := range entry.Blocks {
			itemID := ""
			switch block := block.AsAny().(type) {
			case anthropic.TextBlock:
				if message.Role != "assistant" || message.ID == nil {
					continue
				}
				itemID = fmt.Sprintf("%s#%d", *message.ID, entry.Offset+index)
				items[itemID] = harness.ObservedItem{ItemID: itemID, Kind: eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT, Text: block.Text}
			case anthropic.ThinkingBlock:
				if message.ID == nil {
					continue
				}
				itemID = fmt.Sprintf("%s#%d", *message.ID, entry.Offset+index)
				items[itemID] = harness.ObservedItem{ItemID: itemID, Kind: eventpb.ItemKind_ITEM_KIND_REASONING, Text: block.Thinking}
			case anthropic.ToolUseBlock:
				arguments := block.JSON.Input.Raw()
				itemID = block.ID
				items[itemID] = harness.ObservedItem{ItemID: itemID, Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, Arguments: arguments}
			}
		}
	}
	return items, nil
}
