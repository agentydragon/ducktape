package storagecodec

import (
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model"
)

type MergeabilityPayload struct {
	Repository           string               `json:"repository"`
	PullRequestNumber    int64                `json:"pullRequestNumber"`
	BaseRef              string               `json:"baseRef"`
	HeadSHA              string               `json:"headSha"`
	BaseSHA              string               `json:"baseSha"`
	PreviousMergeability model.MergeableState `json:"previousMergeability"`
	Mergeability         model.MergeableState `json:"mergeability"`
	ObservedAt           time.Time            `json:"observedAt"`
	MergeCommit          *model.MergeCommit   `json:"mergeCommit,omitempty"`
}
