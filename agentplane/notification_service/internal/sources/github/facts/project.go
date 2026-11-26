// Package facts builds the stable, compact payload retained for one GitHub webhook receipt.
package facts

import (
	"encoding/json"
	"errors"
)

type object map[string]json.RawMessage

func decodeObject(raw json.RawMessage) object {
	var value object
	if json.Unmarshal(raw, &value) != nil || value == nil {
		return nil
	}
	return value
}

func selected(source object, keys ...string) json.RawMessage {
	if source == nil {
		return nil
	}
	result := make(object)
	for _, key := range keys {
		if value := source[key]; len(value) != 0 && string(value) != "null" {
			result[key] = value
		}
	}
	if len(result) == 0 {
		return nil
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		return nil
	}
	return encoded
}

func selectedNested(raw json.RawMessage, keys []string, nested map[string][]string) json.RawMessage {
	source := decodeObject(raw)
	if source == nil {
		return nil
	}
	result := make(object)
	for _, key := range keys {
		value := source[key]
		if len(value) == 0 || string(value) == "null" {
			continue
		}
		if nestedKeys, ok := nested[key]; ok {
			if projected := selectedValue(key, value, nestedKeys); projected != nil {
				result[key] = projected
			}
			continue
		}
		result[key] = value
	}
	if len(result) == 0 {
		return nil
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		return nil
	}
	return encoded
}

func selectedValue(key string, raw json.RawMessage, keys []string) json.RawMessage {
	switch key {
	case "head", "base":
		return selectedNested(raw, []string{"sha", "ref", "repo"}, map[string][]string{"repo": repositoryFields})
	case "pull_requests":
		return selectedArray(raw, []string{"number", "head"}, map[string][]string{"head": {"sha"}})
	case "steps":
		return selectedArray(raw, keys, nil)
	case "labels":
		return selectedArray(raw, []string{"name", "color", "description"}, nil)
	case "assignees":
		return selectedArray(raw, userFields, nil)
	case "repo", "repository", "head_repository":
		return selectedNested(raw, repositoryFields, nil)
	case "user":
		return selectedNested(raw, userFields, nil)
	case "commit":
		return selectedNested(raw, []string{"sha", "url"}, nil)
	default:
		return selectedNested(raw, keys, nil)
	}
}

func selectedArray(raw json.RawMessage, keys []string, nested map[string][]string) json.RawMessage {
	var values []json.RawMessage
	if json.Unmarshal(raw, &values) != nil || values == nil {
		return nil
	}
	result := make([]json.RawMessage, 0, len(values))
	for _, value := range values {
		projected := decodeObject(value)
		if projected == nil {
			continue
		}
		for key, nestedKeys := range nested {
			if child := selectedValue(key, projected[key], nestedKeys); child != nil {
				projected[key] = child
			}
		}
		if item := selected(projected, keys...); item != nil {
			result = append(result, item)
		}
	}
	if len(result) == 0 {
		return nil
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		return nil
	}
	return encoded
}

func put(out object, source object, key string) {
	if value := source[key]; len(value) != 0 && string(value) != "null" {
		out[key] = value
	}
}

func putObject(out, source object, key string, keys []string, nested map[string][]string) {
	if value := selectedNested(source[key], keys, nested); value != nil {
		out[key] = value
	}
}

func putArray(out, source object, key string, keys []string, nested map[string][]string) {
	if value := selectedArray(source[key], keys, nested); value != nil {
		out[key] = value
	}
}

var (
	repositoryFields  = []string{"id", "full_name"}
	userFields        = []string{"id", "login", "type"}
	pullRequestFields = map[string][]string{
		"head": {"sha", "ref"}, "base": {"sha", "ref"},
		"user": userFields, "labels": {"name", "color", "description"}, "assignees": userFields,
	}
	prFields = []string{
		"id", "number", "title", "body", "state", "locked", "merged", "merged_at", "closed_at",
		"created_at", "updated_at", "html_url", "diff_url", "patch_url", "commits", "additions", "deletions",
		"changed_files", "head", "base", "user", "labels", "assignees",
	}
	issueFields = []string{
		"id", "number", "title", "body", "state", "locked", "created_at", "updated_at", "closed_at",
		"html_url", "user", "labels", "assignees", "pull_request",
	}
	issueNested = map[string][]string{
		"user": {"id", "login", "type"}, "labels": {"name", "color", "description"}, "assignees": userFields,
		"pull_request": {"url", "html_url", "diff_url", "patch_url"},
	}
	commentFields = []string{
		"id", "body", "html_url", "created_at", "updated_at", "author_association", "path", "line", "original_line",
		"position", "original_position", "commit_id", "original_commit_id", "diff_hunk", "user",
	}
	userNested     = map[string][]string{"user": userFields}
	checkRunFields = []string{
		"id", "name", "head_sha", "status", "conclusion", "details_url", "external_id", "started_at", "completed_at",
		"created_at", "updated_at", "html_url", "check_suite", "app", "output", "pull_requests",
	}
	checkRunNested = map[string][]string{
		"check_suite":   {"id", "head_branch", "head_sha"},
		"app":           {"id", "name", "slug"},
		"output":        {"title", "summary", "text"},
		"pull_requests": {"number", "head"},
	}
	checkSuiteFields = []string{
		"id", "head_branch", "head_sha", "status", "conclusion", "created_at", "updated_at",
		"latest_check_runs_count", "check_runs_url", "url", "pull_requests",
	}
	workflowRunFields = []string{
		"id", "name", "node_id", "head_branch", "head_sha", "run_number", "run_attempt", "event", "status", "conclusion",
		"created_at", "updated_at", "html_url", "jobs_url", "check_suite_id", "workflow_id", "display_title",
		"head_repository", "pull_requests",
	}
	workflowJobFields = []string{
		"id", "run_id", "run_url", "workflow_name", "head_branch", "head_sha", "run_attempt", "name", "status", "conclusion",
		"started_at", "completed_at", "created_at", "steps", "check_run_url", "labels", "runner_id", "runner_name",
		"runner_group_id", "runner_group_name",
	}
	workflowJobNested = map[string][]string{"steps": {"number", "name", "status", "conclusion"}}
	correlationNested = map[string][]string{"pull_requests": {"number", "head"}}
)

// Project returns the notification payload for a webhook body. It copies only documented
// notification fields; it never duplicates the complete signed body.
func Project(event string, raw []byte) (json.RawMessage, error) {
	body := decodeObject(raw)
	if body == nil {
		return nil, errors.New("GitHub webhook body is not a JSON object")
	}
	out := object{}
	put(out, body, "action")
	putObject(out, body, "repository", repositoryFields, nil)
	putObject(out, body, "sender", userFields, nil)

	switch event {
	case "pull_request":
		putObject(out, body, "pull_request", prFields, pullRequestFields)
	case "issues", "issue_comment":
		putObject(out, body, "issue", issueFields, issueNested)
		if event == "issue_comment" {
			putObject(out, body, "comment", commentFields, userNested)
		}
	case "pull_request_review":
		putObject(out, body, "pull_request", prFields, pullRequestFields)
		putObject(out, body, "review", []string{"id", "body", "state", "commit_id", "submitted_at", "html_url", "author_association", "user"}, userNested)
	case "pull_request_review_comment":
		putObject(out, body, "pull_request", prFields, pullRequestFields)
		putObject(out, body, "comment", commentFields, userNested)
	case "check_run":
		putObject(out, body, "check_run", checkRunFields, checkRunNested)
	case "check_suite":
		putObject(out, body, "check_suite", checkSuiteFields, correlationNested)
	case "workflow_run":
		putObject(out, body, "workflow_run", workflowRunFields, map[string][]string{
			"head_repository": repositoryFields, "pull_requests": {"number", "head"},
		})
	case "workflow_job":
		putObject(out, body, "workflow_job", workflowJobFields, workflowJobNested)
	case "status":
		for _, key := range []string{"sha", "name", "context", "state", "description", "target_url", "created_at", "updated_at"} {
			put(out, body, key)
		}
		putArray(out, body, "branches", []string{"name", "commit"}, map[string][]string{"commit": {"sha", "url"}})
	case "push":
		for _, key := range []string{"ref", "before", "after", "created", "deleted", "forced", "compare"} {
			put(out, body, key)
		}
		putObject(out, body, "head_commit", []string{"id", "tree_id", "distinct", "message", "timestamp", "url", "author", "committer"},
			map[string][]string{"author": {"name", "username"}, "committer": {"name", "username"}})
	case "create", "delete":
		put(out, body, "ref")
		put(out, body, "ref_type")
	case "installation", "installation_repositories":
		putObject(out, body, "installation", []string{"id", "repository_selection", "account"},
			map[string][]string{"account": {"id", "login", "type"}})
		putArray(out, body, "repositories_added", repositoryFields, nil)
		putArray(out, body, "repositories_removed", repositoryFields, nil)
	default:
		return nil, errors.New("unsupported GitHub webhook event")
	}

	encoded, err := json.Marshal(out)
	if err != nil {
		return nil, err
	}
	return encoded, nil
}
