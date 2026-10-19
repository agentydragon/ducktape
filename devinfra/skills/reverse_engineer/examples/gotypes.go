// gotypes dumps Go struct definitions — field names, types, offsets and struct
// tags — out of a compiled ELF Go binary, including a garble-obfuscated one.
//
// Why this works when symbols are gone: the Go runtime needs type metadata at
// run time for interface dispatch, type assertions, reflection and map hashing,
// so the linker cannot drop it and garble cannot rewrite it into nothing.
// `.typelink` lists the reachable named types; each entry points at an
// `abi.Type` that we expand recursively. Field *names* are garbled along with
// everything else, but two things survive verbatim and carry most of the
// signal:
//
//   - **struct tags**, because `encoding/json` reads them reflectively at run
//     time — so `json:"session_ingress_token"` is intact even under
//     `-literals`; and
//   - **field offsets and sizes**, which pin the exact wire/memory layout.
//
// That makes this the most reliable way to recover a garbled binary's
// serialization formats: you get every field of every struct, not just the
// tags that happen to appear in a `strings` dump, and you can diff two builds
// structurally.
//
//	gotypes ./binary                      # every struct type
//	gotypes -filter Config ./binary       # only types whose name contains Config
//	gotypes -all-kinds ./binary           # non-struct named types too
//
// If output is empty or names look like garbage, the types base is wrong: it
// defaults to `.rodata`'s address, which is right for most builds, but the
// authoritative value is `moduledata.types`. Override it with -types-base
// (and optionally -etypes to bound the walk).
package main

import (
	"debug/elf"
	"encoding/binary"
	"flag"
	"fmt"
	"os"
	"sort"
	"strings"
)

// abi.Type field offsets for 64-bit targets (see go/src/internal/abi/type.go).
const (
	offSize    = 0
	offTFlag   = 20
	offKind    = 23
	offStr     = 40
	offKindLoc = 48 // start of the kind-specific type struct

	tflagUncommon  = 1 << 0
	tflagExtraStar = 1 << 1

	nameFlagTagged   = 1 << 1
	nameFlagEmbedded = 1 << 3

	structFieldSize = 24 // abi.StructField{Name, Typ, Offset}
)

// abi.Kind values we need to traverse.
const (
	kindArray     = 17
	kindChan      = 18
	kindFunc      = 19
	kindInterface = 20
	kindMap       = 21
	kindPointer   = 22
	kindSlice     = 23
	kindStruct    = 25
)

type image struct {
	f     *elf.File
	data  []byte
	types uint64
}

// at maps a virtual address to a file offset via the program headers, so it
// works regardless of how sections are laid out.
func (m *image) at(vaddr uint64) int64 {
	for _, p := range m.f.Progs {
		if p.Type == elf.PT_LOAD && vaddr >= p.Vaddr && vaddr < p.Vaddr+p.Filesz {
			return int64(vaddr - p.Vaddr + p.Off)
		}
	}
	return -1
}

func (m *image) u8(v uint64) uint8 {
	if o := m.at(v); o >= 0 {
		return m.data[o]
	}
	return 0
}

func (m *image) u32(v uint64) uint32 {
	if o := m.at(v); o >= 0 && int(o)+4 <= len(m.data) {
		return binary.LittleEndian.Uint32(m.data[o:])
	}
	return 0
}

func (m *image) u64(v uint64) uint64 {
	if o := m.at(v); o >= 0 && int(o)+8 <= len(m.data) {
		return binary.LittleEndian.Uint64(m.data[o:])
	}
	return 0
}

func readVarint(b []byte) (val, n int) {
	for n < len(b) {
		x := b[n]
		val += int(x&0x7f) << (7 * n)
		n++
		if x&0x80 == 0 {
			break
		}
	}
	return val, n
}

// name decodes an abi.Name: a flag byte, a varint-length string, and an
// optional varint-length struct tag.
func (m *image) name(v uint64) (name, tag string, embedded bool) {
	if v == 0 {
		return "", "", false
	}
	o := m.at(v)
	if o < 0 {
		return "", "", false
	}
	b := m.data[o:]
	flags := b[0]
	i := 1
	l, n := readVarint(b[i:])
	i += n
	if l < 0 || i+l > len(b) {
		return "", "", false
	}
	name = string(b[i : i+l])
	i += l
	if flags&nameFlagTagged != 0 {
		tl, tn := readVarint(b[i:])
		i += tn
		if tl >= 0 && i+tl <= len(b) {
			tag = string(b[i : i+tl])
		}
	}
	return name, tag, flags&nameFlagEmbedded != 0
}

func (m *image) typeName(t uint64) string {
	if t == 0 {
		return "<nil>"
	}
	strOff := int32(m.u32(t + offStr))
	nm, _, _ := m.name(m.types + uint64(strOff))
	// Types recorded with a leading '*' that they don't actually have.
	if m.u8(t+offTFlag)&tflagExtraStar != 0 && len(nm) > 0 {
		nm = nm[1:]
	}
	return nm
}

// children returns the types referenced by t, so the walk reaches types that
// are only mentioned as a field or element and never named in .typelink.
func (m *image) children(t uint64) []uint64 {
	var out []uint64
	switch m.u8(t+offKind) & 0x1f {
	case kindArray:
		out = append(out, m.u64(t+offKindLoc), m.u64(t+offKindLoc+8))
	case kindChan, kindPointer, kindSlice:
		out = append(out, m.u64(t+offKindLoc))
	case kindMap:
		out = append(out, m.u64(t+offKindLoc), m.u64(t+offKindLoc+8))
	case kindFunc:
		// InCount/OutCount are packed into the word at offKindLoc; the
		// argument types follow the (optional) uncommon struct.
		counts := m.u32(t + offKindLoc)
		in := uint64(counts & 0xffff)
		outc := uint64((counts >> 16) & 0x7fff)
		base := t + offKindLoc + 8
		if m.u8(t+offTFlag)&tflagUncommon != 0 {
			base += 16
		}
		for i := uint64(0); i < in+outc && i < 64; i++ {
			out = append(out, m.u64(base+i*8))
		}
	case kindInterface:
		methods := m.u64(t + offKindLoc + 8)
		n := m.u64(t + offKindLoc + 16)
		for i := uint64(0); i < n && i < 128; i++ {
			// abi.Imethod{Name NameOff, Typ TypeOff}, 8 bytes.
			out = append(out, m.types+uint64(int32(m.u32(methods+i*8+4))))
		}
	case kindStruct:
		fields := m.u64(t + offKindLoc + 8)
		n := m.u64(t + offKindLoc + 16)
		for i := uint64(0); i < n && i < 512; i++ {
			out = append(out, m.u64(fields+i*structFieldSize+8))
		}
	}
	return out
}

func main() {
	filter := flag.String("filter", "", "only print types whose name contains this substring")
	allKinds := flag.String("all-kinds", "", "also report non-struct named types (any non-empty value)")
	typesBase := flag.Uint64("types-base", 0, "moduledata.types (default: .rodata address)")
	etypes := flag.Uint64("etypes", 0, "moduledata.etypes, bounding the walk (default: unbounded)")
	flag.Parse()
	if flag.NArg() != 1 {
		fmt.Fprintln(os.Stderr, "usage: gotypes [flags] <binary>")
		flag.PrintDefaults()
		os.Exit(2)
	}
	if err := run(flag.Arg(0), *filter, *allKinds != "", *typesBase, *etypes); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

// detectTypesBase finds moduledata.types, the base that .typelink offsets are
// relative to. It is *near* .rodata's start but not equal to it -- on a real
// garbled binary the two differed by 0x20, and using the section address made
// every name and nested type offset resolve to garbage while still "working"
// well enough to print a plausible-looking handful of types. So don't guess:
// score candidate bases by how many .typelink entries decode into a sane
// abi.Type and take the winner.
func detectTypesBase(f *elf.File, raw, tld []byte, rodata uint64) (uint64, error) {
	sample := len(tld) / 4
	if sample > 256 {
		sample = 256
	}
	if sample == 0 {
		return 0, fmt.Errorf(".typelink is empty")
	}

	best, bestScore := uint64(0), -1
	for delta := uint64(0); delta <= 0x1000; delta += 8 {
		base := rodata + delta
		m := &image{f: f, data: raw, types: base}
		score := 0
		for i := 0; i < sample; i++ {
			t := base + uint64(int32(binary.LittleEndian.Uint32(tld[i*4:])))
			kind := m.u8(t+offKind) & 0x1f
			if kind == 0 || kind > 26 {
				continue
			}
			// A valid type has a decodable, printable name.
			name, _, _ := m.name(base + uint64(int32(m.u32(t+offStr))))
			if name == "" || len(name) > 200 {
				continue
			}
			ok := true
			for _, r := range name {
				if r < 0x20 || r > 0x7e {
					ok = false
					break
				}
			}
			if ok {
				score++
			}
		}
		if score > bestScore {
			best, bestScore = base, score
		}
		if bestScore == sample {
			break // perfect; no better candidate exists
		}
	}
	if bestScore <= 0 {
		return 0, fmt.Errorf("could not locate moduledata.types near .rodata (%#x); pass -types-base", rodata)
	}
	fmt.Fprintf(os.Stderr, "types base %#x (%d/%d typelink entries decode)\n", best, bestScore, sample)
	return best, nil
}

func run(path, filter string, allKinds bool, typesBase, etypes uint64) error {
	f, err := elf.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}

	tl := f.Section(".typelink")
	if tl == nil {
		return fmt.Errorf("no .typelink section: not a Go binary, or it was built with -ldflags=-w in a way that dropped type links")
	}
	tld, err := tl.Data()
	if err != nil {
		return err
	}
	if typesBase == 0 {
		rodata := f.Section(".rodata")
		if rodata == nil {
			return fmt.Errorf("no .rodata section; pass -types-base explicitly")
		}
		typesBase, err = detectTypesBase(f, raw, tld, rodata.Addr)
		if err != nil {
			return err
		}
	}
	m := &image{f: f, data: raw, types: typesBase}

	// Seed from .typelink, then expand transitively through composite types.
	queue := make([]uint64, 0, len(tld)/4)
	for i := 0; i+4 <= len(tld); i += 4 {
		queue = append(queue, typesBase+uint64(int32(binary.LittleEndian.Uint32(tld[i:]))))
	}

	seen := map[uint64]bool{}
	var found []uint64
	for len(queue) > 0 {
		t := queue[len(queue)-1]
		queue = queue[:len(queue)-1]
		if t == 0 || seen[t] || t < typesBase || (etypes != 0 && t >= etypes) {
			continue
		}
		seen[t] = true
		if kind := m.u8(t+offKind) & 0x1f; kind == kindStruct || allKinds {
			found = append(found, t)
		}
		queue = append(queue, m.children(t)...)
	}

	type decl struct{ name, body string }
	var out []decl
	for _, t := range found {
		name := m.typeName(t)
		if filter != "" && !strings.Contains(name, filter) {
			continue
		}
		size := m.u64(t + offSize)
		kind := m.u8(t+offKind) & 0x1f
		if kind != kindStruct {
			out = append(out, decl{name, fmt.Sprintf(
				"// vaddr %#x size=%#x\ntype %s = <kind %d>\n", t, size, name, kind,
			)})
			continue
		}

		fields := m.u64(t + offKindLoc + 8)
		n := m.u64(t + offKindLoc + 16)
		var sb strings.Builder
		fmt.Fprintf(&sb, "// vaddr %#x size=%#x fields=%d\ntype %s struct {\n", t, size, n, name)
		for i := uint64(0); i < n && i < 512; i++ {
			fv := fields + i*structFieldSize
			fname, ftag, embedded := m.name(m.u64(fv))
			ftype := m.typeName(m.u64(fv + 8))
			note := fmt.Sprintf("// +%#x", m.u64(fv+16))
			if embedded {
				note += " (embedded)"
			}
			if ftag != "" {
				fmt.Fprintf(&sb, "\t%-18s %-38s `%s` %s\n", fname, ftype, ftag, note)
			} else {
				fmt.Fprintf(&sb, "\t%-18s %-38s %s\n", fname, ftype, note)
			}
		}
		sb.WriteString("}\n")
		out = append(out, decl{name, sb.String()})
	}

	sort.Slice(out, func(a, b int) bool { return out[a].name < out[b].name })
	for _, d := range out {
		fmt.Print(d.body)
	}
	fmt.Fprintf(os.Stderr, "%d types printed (%d walked)\n", len(out), len(seen))
	return nil
}
