package invocation

import (
	"fmt"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

const (
	DefaultLimit = 50
	// maxPage is the most items the services return in one page.
	MaxPage = 128
)

// collect follows server pages after the cursor until limit items (0 is unlimited) or the last
// page. fetch asks for at most size items; a service that cannot be asked may return more, which
// are cut at limit.
func Collect[C, Item any](
	limit int, after C, fetch func(after C, size int) ([]Item, error), cursorOf func(Item) (C, error),
) ([]Item, error) {
	if limit < 0 {
		return nil, UsageFailure("--limit must not be negative")
	}
	var items []Item
	for {
		size := MaxPage
		if limit > 0 {
			size = min(MaxPage, limit-len(items))
		}
		page, err := fetch(after, size)
		if err != nil {
			return nil, err
		}
		items = append(items, page...)
		if limit > 0 && len(items) >= limit {
			return items[:limit], nil
		}
		if len(page) < size {
			return items, nil
		}
		if after, err = cursorOf(page[len(page)-1]); err != nil {
			return nil, err
		}
	}
}

// ShowProtoPage prints a collected list. In text mode, a list cut at limit says on stderr where to resume.
func ShowProtoPage[C any, Item proto.Message](a *Invocation, items []Item, limit int, cursorOf func(Item) (C, error), line func(Item) (string, error), options protojson.MarshalOptions) error {
	if err := ShowProto(a, items, line, options); err != nil {
		return err
	}
	if a.asJSON || limit == 0 || len(items) < limit {
		return nil
	}
	last, err := cursorOf(items[len(items)-1])
	if err != nil {
		return err
	}
	fmt.Fprintf(a.stderr, "more may follow: --after %v\n", last)
	return nil
}
