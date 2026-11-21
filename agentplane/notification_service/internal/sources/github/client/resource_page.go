package client

import (
	"time"

	"github.com/google/go-github/v88/github"
)

// pageSize is the page size every paginated read asks for, the most GitHub's REST API returns.
const pageSize = 100

type resourcePage[T any] struct {
	value    T
	response *github.Response
}

func timestampTime(timestamp *github.Timestamp) *time.Time {
	if timestamp == nil {
		return nil
	}
	value := timestamp.Time.UTC()
	return &value
}
