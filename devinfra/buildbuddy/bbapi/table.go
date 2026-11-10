package main

import (
	"fmt"
	"os"
	"strings"
	"text/tabwriter"
)

// table renders tab-aligned columns to stdout.
type table struct {
	w *tabwriter.Writer
}

func newTable() *table {
	return &table{w: tabwriter.NewWriter(os.Stdout, 0, 4, 2, ' ', 0)}
}

// header writes a header row and is a no-op if --json is set.
func (t *table) header(cols ...string) {
	fmt.Fprintln(t.w, strings.Join(cols, "\t"))
}

// row writes a data row.
func (t *table) row(vals ...any) {
	fmts := make([]string, len(vals))
	for i := range fmts {
		fmts[i] = "%v"
	}
	fmt.Fprintf(t.w, strings.Join(fmts, "\t")+"\n", vals...)
}

// flush flushes the underlying tabwriter.
func (t *table) flush() {
	t.w.Flush()
}
