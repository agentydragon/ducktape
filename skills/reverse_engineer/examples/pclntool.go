// pclntool maps an instruction PC to its containing function name in a
// garble-obfuscated Go ELF binary.
//
// garble v0.13.0+ XORs the .gopclntab magic header bytes with a seed-derived
// key, preventing standard tools (go tool objdump, debug/gosym) from parsing
// the table. The rest of the pclntab structure is intact, so this tool tries
// each known Go magic value until debug/gosym.NewTable succeeds, then returns
// the garbled function name for the given PC.
//
// Usage: pclntool <binary> <pc>
//
//	pc may be decimal or hex (0x-prefixed).
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
	if len(os.Args) < 3 {
		fmt.Fprintln(os.Stderr, "usage: pclntool <binary> <pc>")
		os.Exit(1)
	}
	f, err := elf.Open(os.Args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, "elf.Open:", err)
		os.Exit(1)
	}
	defer f.Close()

	pc, err := strconv.ParseUint(os.Args[2], 0, 64)
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

	for _, magic := range goMagics {
		buf := make([]byte, len(pclntabData))
		copy(buf, pclntabData)
		binary.LittleEndian.PutUint32(buf[:4], magic)

		lt := gosym.NewLineTable(buf, textAddr)
		table, err := gosym.NewTable(nil, lt)
		if err != nil || len(table.Funcs) == 0 {
			continue
		}
		fn := table.PCToFunc(pc)
		if fn == nil {
			fmt.Fprintf(os.Stderr, "no function at PC %#x\n", pc)
			os.Exit(1)
		}
		fmt.Println(fn.Name)
		return
	}
	fmt.Fprintln(os.Stderr, "could not parse pclntab with any known Go magic")
	os.Exit(1)
}
