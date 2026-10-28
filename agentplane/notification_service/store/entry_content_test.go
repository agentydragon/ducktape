package store

import (
	"encoding/json"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/agentydragon/ducktape/agentplane/notification_service/entrystoragepb"
	"google.golang.org/protobuf/proto"
)

func TestMergeabilityContentRoundTripsOptionalMergeCommit(t *testing.T) {
	const basePayload = `{"repository":"owner/repository","pullRequestNumber":3,"baseRef":"main","headSha":"head","baseSha":"base","previousMergeability":"UNKNOWN","mergeability":"CONFLICTING","observedAt":"2026-01-01T00:00:00Z"}`
	const mergeCommitPayload = `{"repository":"owner/repository","pullRequestNumber":3,"baseRef":"main","headSha":"head","baseSha":"base","previousMergeability":"UNKNOWN","mergeability":"CONFLICTING","observedAt":"2026-01-01T00:00:00Z","mergeCommit":{"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","baseSha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","headSha":"cccccccccccccccccccccccccccccccccccccccc"}}`
	event := model.GitHubMergeabilityEvent{PullRequestNumber: 3}

	for _, test := range []struct {
		name             string
		payload          string
		wantMergeCommit  *model.GitHubMergeCommit
		wantCommitAbsent bool
	}{
		{
			name:             "legacy row without optional proof",
			payload:          basePayload,
			wantCommitAbsent: true,
		},
		{
			name:    "legacy row with exact proof",
			payload: mergeCommitPayload,
			wantMergeCommit: &model.GitHubMergeCommit{
				SHA: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", BaseSHA: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
				HeadSHA: "cccccccccccccccccccccccccccccccccccccccc",
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			stored, err := marshalEntryStorageContent(event, json.RawMessage(test.payload))
			if err != nil {
				t.Fatal(err)
			}
			if stored.typedPayload == nil {
				t.Fatal("legacy mergeability JSON did not produce typed storage")
			}
			var content entrystoragepb.EntryContent
			if err := proto.Unmarshal(stored.typedPayload, &content); err != nil {
				t.Fatalf("decode typed content: %v", err)
			}
			mergeability := content.GetMergeability()
			if mergeability == nil {
				t.Fatal("typed mergeability content is missing")
			}
			if test.wantMergeCommit != nil {
				got := mergeability.GetMergeCommit()
				if got == nil || got.GetSha() != test.wantMergeCommit.SHA || got.GetBaseSha() != test.wantMergeCommit.BaseSHA ||
					got.GetHeadSha() != test.wantMergeCommit.HeadSHA {
					t.Fatalf("typed merge commit = %v, want %+v", got, test.wantMergeCommit)
				}
			} else if got := mergeability.GetMergeCommit(); got != nil {
				t.Fatalf("typed merge commit = %v, want nil", got)
			}

			reconstructed, err := entryPayloadFromStoredContent(event, []byte(test.payload), stored.typedPayload, true)
			if err != nil {
				t.Fatalf("reconstruct typed payload: %v", err)
			}
			var got struct {
				MergeCommit *model.GitHubMergeCommit `json:"mergeCommit"`
			}
			if err := json.Unmarshal(reconstructed, &got); err != nil {
				t.Fatalf("decode reconstructed payload: %v", err)
			}
			if test.wantMergeCommit != nil {
				if got.MergeCommit == nil || *got.MergeCommit != *test.wantMergeCommit {
					t.Fatalf("reconstructed merge commit = %+v, want %+v", got.MergeCommit, test.wantMergeCommit)
				}
			} else if !test.wantCommitAbsent || got.MergeCommit != nil {
				t.Fatalf("reconstructed optional merge commit = %+v, want omitted", got.MergeCommit)
			}
		})
	}
}
