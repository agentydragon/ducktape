package main

import (
	"context"
	"fmt"
	"strings"
	"sync"
)

// workerStopped is a worker that returned while the service was serving, which ends the process: its inboxes
// would wait for a worker that is not coming back. It names the error's types, never its message, which a source
// or a delivery can fill with a payload or a credential.
type workerStopped struct {
	worker int
	types  string
}

func (e workerStopped) Error() string {
	return fmt.Sprintf("notification worker %d stopped: %s", e.worker, e.types)
}

// errorTypes names err and every error it wraps by type, outermost first.
func errorTypes(err error) []string {
	if err == nil {
		return nil
	}
	types := []string{fmt.Sprintf("%T", err)}
	switch wrapper := err.(type) {
	case interface{ Unwrap() error }:
		types = append(types, errorTypes(wrapper.Unwrap())...)
	case interface{ Unwrap() []error }:
		for _, wrapped := range wrapper.Unwrap() {
			types = append(types, errorTypes(wrapped)...)
		}
	}
	return types
}

// runWorkers calls work on count goroutines, with their indexes, until ctx ends. A call that returns while ctx
// is live sends a workerStopped on failed, which needs room for one per worker. The group is done when every
// call has returned.
func runWorkers(ctx context.Context, count int, failed chan<- error, work func(ctx context.Context, index int) error) *sync.WaitGroup {
	var running sync.WaitGroup
	for i := range count {
		running.Add(1)
		go func() {
			defer running.Done()
			if err := work(ctx, i); ctx.Err() == nil {
				types := "returned without an error"
				if err != nil {
					types = strings.Join(errorTypes(err), ", ")
				}
				failed <- workerStopped{worker: i, types: types}
			}
		}()
	}
	return &running
}
