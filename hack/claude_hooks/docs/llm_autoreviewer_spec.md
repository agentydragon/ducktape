# LLM AUTOREVIEWER Hook Specification

## Overview

Use a LLM to review Claude's code changes against coding requirements and standards.
Train/eval on examples of my past review feedback.

## Implementation

Could be any of:

- OpenAI API
- Non-interactive Claude Code or Codex agent with read-only tools

## Architecture Principles

Trigger on some combination of `PreToolUse`/`PostToolUse`/`Stop`, as appropriate by
criteria including severity. Can be blocking/non-blocking as appropriate.

## Training Data

- GitHub code reviews
- Couple past commits adding feedback

## LLM Reviewer Architecture

Build prompts from training examples.

- Group examples by category
- Creates reviewer guidelines based on past patterns

Structured output through function calling, each finding with:

- Line number
- Identification for well-known finding types (e.g., `hides-errors`)
- Free-text feedback for Claude
- Severify/action: `PreToolUse` block / `Stop` inhibit / only insert nonblocking feedback

## Training Management

### Incremental Learning

- Adds new examples from user feedback
- Auto-categorizes feedback using keyword analysis
- Updates reviewer with expanded training set
- Maintains category balance and example limits

### Cache Management

- Stores trained reviewers based on training data hash
- Loads cached reviewers for consistent performance
- Rebuilds reviewers when training data changes
- Manages cache lifecycle and cleanup

## Configuration

Uses YAML configuration for:

- LLM model selection and parameters
- Review behavior settings (blocking, suggestions)
- File exclusion patterns
- Training data management
- Cache and performance settings

## Security Considerations

- **LLM API calls**: Secure API keys, rate limiting
- **Training data**: Avoid sensitive code in examples
- **Code exposure**: Limit external LLM access
- **Fallback behavior**: Always allow continuation on failures

## Future Enhancements

1. **Active learning**: User classification of edge cases
2. **Contextual awareness**: Surrounding code context analysis
3. **Project-specific training**: Different reviewers per project
4. **Integration with MCP**: Long-term feedback pattern storage
