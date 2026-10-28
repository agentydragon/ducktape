package client

import (
	"time"

	"github.com/google/go-github/v88/github"
)

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
