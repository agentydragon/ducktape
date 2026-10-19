// gosymtab rebuilds an ELF symbol table for a stripped or garble-obfuscated Go
// binary by recovering function boundaries from .gopclntab.
//
// garble strips .symtab, which breaks `go tool objdump`, `go tool nm`, gdb, and
// symbol import in Ghidra/radare2. It cannot strip .gopclntab — the Go runtime
// needs it to render stack traces — it only XORs the 4-byte magic. So: repair
// the magic, enumerate every function, and write those back as real ELF symbols.
// The output binary works with the entire standard toolchain, including
// `go tool objdump`, which resolves CALL targets to names again.
//
//	gosymtab ./garbled ./garbled.sym
//	go tool objdump -s '^main\.main$' ./garbled.sym
package main

import (
	"bytes"
	"debug/elf"
	"debug/gosym"
	"encoding/binary"
	"fmt"
	"os"
	"sort"
)

// Known pclntab magic values (little-endian uint32 as stored in the binary).
var goMagics = []uint32{
	0xfffffff1, // Go 1.20+
	0xfffffff2, // Go 1.22+
	0xfffffff0, // Go 1.18
	0xfffffffa, // Go 1.16
	0xfffffffb, // Go 1.2
}

const (
	shtSymtab = 2
	shtStrtab = 3
	sttFunc   = 2
	stbGlobal = 1
	symSize   = 24 // sizeof(Elf64_Sym)
	shdrSize  = 64 // sizeof(Elf64_Shdr)

	// ELF64 header field offsets.
	ehShoff     = 0x28
	ehShnum     = 0x3c
	ehShstrndx  = 0x3e
	pclnTextOff = 24 // textStart field within the Go 1.18+ pclntab header
)

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: gosymtab <in> <out>")
		os.Exit(2)
	}
	if err := run(os.Args[1], os.Args[2]); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

// prologueScore is the fraction of recovered function entry addresses whose
// bytes actually begin a function. A correct text base lands every symbol on a
// real instruction boundary; a wrong one lands them mid-instruction, so the
// competing hypotheses separate cleanly in practice (78% vs 37% on a real
// garbled binary).
func prologueScore(t *gosym.Table, text []byte, textAddr, textSize uint64) float64 {
	stackCheck := []byte{0x49, 0x3b, 0x66, 0x10} // CMPQ SP, 0x10(R14)
	pushBP := []byte{0x55, 0x48, 0x89, 0xe5}     // PUSHQ BP; MOVQ SP, BP
	hits, total := 0, 0
	for i := range t.Funcs {
		if i%13 != 0 { // sample: the table can hold tens of thousands of entries
			continue
		}
		a := t.Funcs[i].Entry
		if a < textAddr || a+4 >= textAddr+textSize {
			continue
		}
		b := text[a-textAddr : a-textAddr+4]
		total++
		if bytes.Equal(b, stackCheck) || bytes.Equal(b, pushBP) {
			hits++
		}
	}
	if total == 0 {
		return 0
	}
	return float64(hits) / float64(total)
}

func run(inPath, outPath string) error {
	raw, err := os.ReadFile(inPath)
	if err != nil {
		return err
	}
	f, err := elf.Open(inPath)
	if err != nil {
		return err
	}
	defer f.Close()

	pcSec := f.Section(".gopclntab")
	if pcSec == nil {
		return fmt.Errorf("no .gopclntab section")
	}
	pcData, err := pcSec.Data()
	if err != nil {
		return err
	}
	textSec := f.Section(".text")
	if textSec == nil {
		return fmt.Errorf("no .text section")
	}
	textBytes, err := textSec.Data()
	if err != nil {
		return err
	}

	// debug/gosym ignores the header's textStart for the Go 1.18/1.20 table
	// formats and trusts the caller's value instead ("may be unrelocated"). But
	// the linker can place an entry stub or padding at the start of .text, so
	// the section address is not always the first Go function — on
	// environment-manager .text begins 0x100 bytes early. Passing the section
	// address then shifts every recovered symbol. Try both and let the
	// instruction stream pick the winner.
	var headerTextStart uint64
	if len(pcData) >= pclnTextOff+8 {
		headerTextStart = binary.LittleEndian.Uint64(pcData[pclnTextOff : pclnTextOff+8])
	}
	bases := []uint64{textSec.Addr}
	if headerTextStart != 0 && headerTextStart != textSec.Addr {
		bases = append(bases, headerTextStart)
	}

	var table *gosym.Table
	var goodMagic uint32
	var goodBase uint64
	bestScore := -1.0
	for _, m := range goMagics {
		buf := make([]byte, len(pcData))
		copy(buf, pcData)
		binary.LittleEndian.PutUint32(buf[:4], m)
		for _, base := range bases {
			t, err := gosym.NewTable(nil, gosym.NewLineTable(buf, base))
			if err != nil || len(t.Funcs) == 0 {
				continue
			}
			s := prologueScore(t, textBytes, textSec.Addr, textSec.Size)
			fmt.Fprintf(os.Stderr, "  magic %#08x base %#x: %d funcs, prologue score %.1f%%\n",
				m, base, len(t.Funcs), s*100)
			if s > bestScore {
				bestScore, table, goodMagic, goodBase = s, t, m, base
			}
		}
		if table != nil {
			break // the magic is unambiguous; only the base needs choosing
		}
	}
	if table == nil {
		return fmt.Errorf("could not parse pclntab with any known Go magic")
	}
	fmt.Fprintf(os.Stderr, "pclntab: magic %#08x -> %#08x, base %#x, %d functions\n",
		binary.LittleEndian.Uint32(pcData[:4]), goodMagic, goodBase, len(table.Funcs))

	out := make([]byte, len(raw))
	copy(out, raw)
	// Persist the magic repair so downstream tools can parse the output too.
	binary.LittleEndian.PutUint32(out[pcSec.Offset:pcSec.Offset+4], goodMagic)

	// st_shndx must name the section containing the symbol; objdump discards
	// symbols that point at SHN_UNDEF.
	sectionOf := func(addr uint64) uint16 {
		for i, s := range f.Sections {
			if s.Flags&elf.SHF_ALLOC == 0 {
				continue
			}
			if addr >= s.Addr && addr < s.Addr+s.Size {
				return uint16(i)
			}
		}
		return 0
	}

	funcs := make([]gosym.Func, len(table.Funcs))
	copy(funcs, table.Funcs)
	sort.Slice(funcs, func(i, j int) bool { return funcs[i].Entry < funcs[j].Entry })

	strtab := []byte{0}             // index 0 of a strtab must be NUL
	symtab := make([]byte, symSize) // index 0 is the null symbol
	for _, fn := range funcs {
		nameOff := uint32(len(strtab))
		strtab = append(strtab, fn.Name...)
		strtab = append(strtab, 0)

		var sym [symSize]byte
		binary.LittleEndian.PutUint32(sym[0:4], nameOff)
		sym[4] = byte(stbGlobal<<4 | sttFunc)
		binary.LittleEndian.PutUint16(sym[6:8], sectionOf(fn.Entry))
		binary.LittleEndian.PutUint64(sym[8:16], fn.Entry)
		binary.LittleEndian.PutUint64(sym[16:24], fn.End-fn.Entry)
		symtab = append(symtab, sym[:]...)
	}

	// Extend .shstrtab rather than replacing it: appending keeps every existing
	// sh_name offset valid, so the original section headers copy over untouched.
	oldShoff := binary.LittleEndian.Uint64(raw[ehShoff : ehShoff+8])
	oldShstrndx := binary.LittleEndian.Uint16(raw[ehShstrndx : ehShstrndx+2])
	oldShstrtab, err := f.Sections[oldShstrndx].Data()
	if err != nil {
		return err
	}
	shstrtab := append([]byte{}, oldShstrtab...)
	addName := func(s string) uint32 {
		off := uint32(len(shstrtab))
		shstrtab = append(shstrtab, s...)
		shstrtab = append(shstrtab, 0)
		return off
	}
	nameSymtab := addName(".symtab")
	nameStrtab := addName(".strtab")
	nameShstrtab := addName(".shstrtab.gosymtab")

	align8 := func(b []byte) []byte {
		for len(b)%8 != 0 {
			b = append(b, 0)
		}
		return b
	}

	origSecCount := len(f.Sections)
	idxStrtab := uint32(origSecCount + 1)
	idxShstrtab := uint16(origSecCount + 2)

	out = align8(out)
	offSymtab := uint64(len(out))
	out = append(out, symtab...)
	offStrtab := uint64(len(out))
	out = append(out, strtab...)
	offShstrtab := uint64(len(out))
	out = append(out, shstrtab...)
	out = align8(out)
	newShoff := uint64(len(out))

	out = append(out, raw[oldShoff:oldShoff+uint64(origSecCount*shdrSize)]...)

	mkShdr := func(name, typ uint32, off, size uint64, link, info uint32, align, entsize uint64) []byte {
		var h [shdrSize]byte
		binary.LittleEndian.PutUint32(h[0:4], name)
		binary.LittleEndian.PutUint32(h[4:8], typ)
		binary.LittleEndian.PutUint64(h[8:16], 0)  // sh_flags
		binary.LittleEndian.PutUint64(h[16:24], 0) // sh_addr: not loaded at runtime
		binary.LittleEndian.PutUint64(h[24:32], off)
		binary.LittleEndian.PutUint64(h[32:40], size)
		binary.LittleEndian.PutUint32(h[40:44], link)
		binary.LittleEndian.PutUint32(h[44:48], info)
		binary.LittleEndian.PutUint64(h[48:56], align)
		binary.LittleEndian.PutUint64(h[56:64], entsize)
		return h[:]
	}

	// sh_link points at the strtab holding the names; sh_info is the index of
	// the first non-local symbol (1, since only the null symbol is local).
	out = append(out, mkShdr(nameSymtab, shtSymtab, offSymtab, uint64(len(symtab)), idxStrtab, 1, 8, symSize)...)
	out = append(out, mkShdr(nameStrtab, shtStrtab, offStrtab, uint64(len(strtab)), 0, 0, 1, 0)...)
	out = append(out, mkShdr(nameShstrtab, shtStrtab, offShstrtab, uint64(len(shstrtab)), 0, 0, 1, 0)...)

	binary.LittleEndian.PutUint64(out[ehShoff:ehShoff+8], newShoff)
	binary.LittleEndian.PutUint16(out[ehShnum:ehShnum+2], uint16(origSecCount+3))
	binary.LittleEndian.PutUint16(out[ehShstrndx:ehShstrndx+2], idxShstrtab)

	info, err := os.Stat(inPath)
	if err != nil {
		return err
	}
	if err := os.WriteFile(outPath, out, info.Mode()); err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "wrote %s: %d symbols\n", outPath, len(funcs))
	return nil
}
