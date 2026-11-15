package invocation

import (
	"encoding/json"
	"fmt"
)

const (
	DefaultLimit = 50
	// maxPage is the most items the services return in one page.
	MaxPage = 128
)

// collect follows server pages after the cursor until limit items (0 is unlimited) or the last
// page. fetch asks for at most size items; a service that cannot be asked may return more, which
// are cut at limit.
func Collect[C any](
	limit int, after C, fetch func(after C, size int) ([]json.RawMessage, error), cursorOf func(json.RawMessage) (C, error),
) ([]json.RawMessage, error) {
	if limit < 0 {
		return nil, UsageFailure("--limit must not be negative")
	}
	var items []json.RawMessage
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

// showPage prints a collected list. In text mode, a list cut at limit says on stderr where to resume.
func ShowPage[C any](a *Invocation, items []json.RawMessage, limit int, cursorOf func(json.RawMessage) (C, error), line func(json.RawMessage) (string, error)) error {
	if err := a.Show(items, line); err != nil {
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

func IDOf(raw json.RawMessage) (string, error) {
	item, err := Decode[struct {
		ID string `json:"id"`
	}](raw)
	return item.ID, err
}

func SequenceOf(raw json.RawMessage) (int, error) {
	item, err := Decode[struct {
		Sequence int `json:"sequence"`
	}](raw)
	return item.Sequence, err
}
