package main

import (
	"debug/elf"
	"debug/gosym"
	"encoding/binary"
	"fmt"
	"os"
	"strconv"

	"github.com/spf13/cobra"
)

// Known pclntab magic values (little-endian uint32 as stored in the binary).
var goMagics = []uint32{
	0xfffffff1, // Go 1.20+
	0xfffffff2, // Go 1.22+
	0xfffffff0, // Go 1.18
	0xfffffffa, // Go 1.16
	0xfffffffb, // Go 1.2
}

func die(format string, a ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", a...)
	os.Exit(1)
}

func main() {
	root := &cobra.Command{
		Use:           "pclntool",
		Short:         "Operate on garble-obfuscated Go ELF binaries via pclntab",
		SilenceUsage:  true,
		SilenceErrors: true,
	}

	root.AddCommand(cmdPC(), cmdPatch())

	if err := root.Execute(); err != nil {
		die("%v", err)
	}
}

func cmdPC() *cobra.Command {
	return &cobra.Command{
		Use:   "pc <binary> <pc>",
		Short: "Map an instruction PC to its containing garbled function name",
		Long: `Map an instruction PC to its containing garbled function name.

garble XORs the .gopclntab magic header, but the rest of the table is intact.
pclntool tries each known Go magic until debug/gosym.NewTable succeeds, then
calls PCToFunc to resolve the name.

pc may be decimal or hex (0x-prefixed).`,
		Args: cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			runPC(args[0], args[1])
			return nil
		},
	}
}

func cmdPatch() *cobra.Command {
	return &cobra.Command{
		Use:   "patch <binary> <output>",
		Short: "Repair obfuscated pclntab magic so redress/GoReSym can parse the binary",
		Long: `Write a copy of <binary> with the obfuscated pclntab magic repaired.

The output binary is identical to the input except for the 4-byte magic at the
start of .gopclntab. redress, GoReSym, and debug/gosym all work on the patched
output.`,
		Args: cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			runPatch(args[0], args[1])
			return nil
		},
	}
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

func runPC(binaryPath, pcStr string) {
	f, err := elf.Open(binaryPath)
	if err != nil {
		die("elf.Open: %v", err)
	}
	defer f.Close()

	pc, err := strconv.ParseUint(pcStr, 0, 64)
	if err != nil {
		die("parse pc: %v", err)
	}

	pclntabData, err := f.Section(".gopclntab").Data()
	if err != nil {
		die(".gopclntab read: %v", err)
	}
	textAddr := f.Section(".text").Addr

	_, table, ok := findWorkingMagic(pclntabData, textAddr)
	if !ok {
		die("could not parse pclntab with any known Go magic")
	}
	fn := table.PCToFunc(pc)
	if fn == nil {
		die("no function at PC %#x", pc)
	}
	fmt.Println(fn.Name)
}

func runPatch(inputPath, outputPath string) {
	raw, err := os.ReadFile(inputPath)
	if err != nil {
		die("read: %v", err)
	}

	f, err := elf.Open(inputPath)
	if err != nil {
		die("elf.Open: %v", err)
	}
	defer f.Close()

	sec := f.Section(".gopclntab")
	if sec == nil {
		die("no .gopclntab section")
	}
	pclntabData, err := sec.Data()
	if err != nil {
		die(".gopclntab read: %v", err)
	}
	fileOffset := sec.Offset
	textAddr := f.Section(".text").Addr

	origMagic := binary.LittleEndian.Uint32(pclntabData[:4])
	magic, _, ok := findWorkingMagic(pclntabData, textAddr)
	if !ok {
		die("could not find working pclntab magic")
	}

	out := make([]byte, len(raw))
	copy(out, raw)
	binary.LittleEndian.PutUint32(out[fileOffset:], magic)

	info, err := os.Stat(inputPath)
	if err != nil {
		die("stat: %v", err)
	}
	if err := os.WriteFile(outputPath, out, info.Mode()); err != nil {
		die("write: %v", err)
	}
	fmt.Fprintf(os.Stderr, "patched .gopclntab magic: %#08x → %#08x\n", origMagic, magic)
	fmt.Fprintf(os.Stderr, "wrote %d bytes to %s\n", len(out), outputPath)
}
