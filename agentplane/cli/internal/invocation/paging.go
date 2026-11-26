package invocation

import (
	"fmt"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

const (
	DefaultLimit = 50
	// MaxPage is the most items the services return in one page.
	MaxPage = 128
)

// ShowProtoPages fetches up to limit items (0 means unlimited) and renders the whole result.
// A service without a page-size parameter may return more than requested.
func ShowProtoPages[C any, Item proto.Message](
	a *Invocation, limit int, after C, fetch func(after C, size int) ([]Item, error),
	cursorOf func(Item) C, line func(Item) (string, error), options protojson.MarshalOptions,
) error {
	if limit < 0 {
		return UsageFailure("--limit must not be negative")
	}
	var items []Item
	for {
		size := MaxPage
		if limit > 0 {
			size = min(size, limit-len(items))
		}
		page, err := fetch(after, size)
		if err != nil {
			return err
		}
		items = append(items, page...)
		if limit > 0 && len(items) >= limit {
			items = items[:limit]
			break
		}
		if len(page) < size {
			break
		}
		after = cursorOf(page[len(page)-1])
	}
	if err := ShowProto(a, items, line, options); err != nil {
		return err
	}
	if !a.asJSON && limit > 0 && len(items) == limit {
		fmt.Fprintf(a.stderr, "more may follow: --after %v\n", cursorOf(items[len(items)-1]))
	}
	return nil
}
