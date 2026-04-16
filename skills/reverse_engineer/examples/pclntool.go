// pclntool operates on garble-obfuscated Go ELF binaries.
//
// garble v0.13.0+ XORs the .gopclntab magic header bytes with a seed-derived
// key, preventing standard tools (go tool objdump, debug/gosym, redress,
// GoReSym) from parsing the table. The rest of the pclntab structure is
// intact, so this tool tries each known Go magic value until
// debug/gosym.NewTable succeeds.
//
// Commands:
//
//	pclntool pc <binary> <pc>
//	    Map an instruction PC to its containing garbled function name.
//	    pc may be decimal or hex (0x-prefixed).
//
//	pclntool patch <binary> <output>
//	    Write a copy of <binary> with the obfuscated pclntab magic repaired.
//	    The output binary is identical to the input except for the 4-byte
//	    magic at the start of .gopclntab. redress, GoReSym, and debug/gosym
//	    all work on the patched binary.
package main

import (
	"debug/elf"
	"debug/gosym"
	"encoding/binary"
	"fmt"
	"os"
	"strconv"
)

// Known pclntab magic values (little-endian uint32 as stored in the binary).
var goMagics = []uint32{
	0xfffffff1, // Go 1.20+
	0xfffffff2, // Go 1.22+
	0xfffffff0, // Go 1.18
	0xfffffffa, // Go 1.16
	0xfffffffb, // Go 1.2
}

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	switch os.Args[1] {
	case "pc":
		if len(os.Args) != 4 {
			usage()
		}
		cmdPC(os.Args[2], os.Args[3])
	case "patch":
		if len(os.Args) != 4 {
			usage()
		}
		cmdPatch(os.Args[2], os.Args[3])
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage:")
	fmt.Fprintln(os.Stderr, "  pclntool pc <binary> <pc>          map PC to garbled function name")
	fmt.Fprintln(os.Stderr, "  pclntool patch <binary> <output>   repair obfuscated pclntab magic")
	os.Exit(1)
}

// findWorkingMagic tries each known Go pclntab magic against pclntabData
// (with .text base addr textAddr) and returns the first that parses, plus
// the resulting symbol table.
func findWorkingMagic(pclntabData []byte, textAddr uint64) (uint32, *gosym.Table, bool) {
	for _, magic := range goMagics {
		buf := make([]byte, len(pclntabData))
		copy(buf, pclntabData)
		binary.LittleEndian.PutUint32(buf[:4], magic)
		lt := gosym.NewLineTable(buf, textAddr)
		table, err := gosym.NewTable(nil, lt)
		if err != nil || len(table.Funcs) == 0 {
			continue
		}
		return magic, table, true
	}
	return 0, nil, false
}

func cmdPC(binaryPath, pcStr string) {
	f, err := elf.Open(binaryPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "elf.Open:", err)
		os.Exit(1)
	}
	defer f.Close()

	pc, err := strconv.ParseUint(pcStr, 0, 64)
	if err != nil {
		fmt.Fprintln(os.Stderr, "parse pc:", err)
		os.Exit(1)
	}

	pclntabData, err := f.Section(".gopclntab").Data()
	if err != nil {
		fmt.Fprintln(os.Stderr, ".gopclntab read:", err)
		os.Exit(1)
	}
	textAddr := f.Section(".text").Addr

	_, table, ok := findWorkingMagic(pclntabData, textAddr)
	if !ok {
		fmt.Fprintln(os.Stderr, "could not parse pclntab with any known Go magic")
		os.Exit(1)
	}
	fn := table.PCToFunc(pc)
	if fn == nil {
		fmt.Fprintf(os.Stderr, "no function at PC %#x\n", pc)
		os.Exit(1)
	}
	fmt.Println(fn.Name)
}

func cmdPatch(inputPath, outputPath string) {
	raw, err := os.ReadFile(inputPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "read:", err)
		os.Exit(1)
	}

	f, err := elf.Open(inputPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "elf.Open:", err)
		os.Exit(1)
	}
	sec := f.Section(".gopclntab")
	if sec == nil {
		fmt.Fprintln(os.Stderr, "no .gopclntab section")
		f.Close()
		os.Exit(1)
	}
	pclntabData, err := sec.Data()
	if err != nil {
		fmt.Fprintln(os.Stderr, ".gopclntab read:", err)
		f.Close()
		os.Exit(1)
	}
	fileOffset := sec.Offset
	textAddr := f.Section(".text").Addr
	f.Close()

	origMagic := binary.LittleEndian.Uint32(pclntabData[:4])
	magic, _, ok := findWorkingMagic(pclntabData, textAddr)
	if !ok {
		fmt.Fprintln(os.Stderr, "could not find working pclntab magic")
		os.Exit(1)
	}

	out := make([]byte, len(raw))
	copy(out, raw)
	binary.LittleEndian.PutUint32(out[fileOffset:], magic)

	info, err := os.Stat(inputPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "stat:", err)
		os.Exit(1)
	}
	if err := os.WriteFile(outputPath, out, info.Mode()); err != nil {
		fmt.Fprintln(os.Stderr, "write:", err)
		os.Exit(1)
	}
	fmt.Fprintf(os.Stderr, "patched .gopclntab magic: %#08x → %#08x\n", origMagic, magic)
	fmt.Fprintf(os.Stderr, "wrote %d bytes to %s\n", len(out), outputPath)
}
