package storagecodec

import (
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

type MergeabilityPayload struct {
	Repository           string                     `json:"repository"`
	PullRequestNumber    int64                      `json:"pullRequestNumber"`
	BaseRef              string                     `json:"baseRef"`
	HeadSHA              string                     `json:"headSha"`
	BaseSHA              string                     `json:"baseSha"`
	PreviousMergeability model.GitHubMergeableState `json:"previousMergeability"`
	Mergeability         model.GitHubMergeableState `json:"mergeability"`
	ObservedAt           time.Time                  `json:"observedAt"`
	MergeCommit          *model.GitHubMergeCommit   `json:"mergeCommit,omitempty"`
}
