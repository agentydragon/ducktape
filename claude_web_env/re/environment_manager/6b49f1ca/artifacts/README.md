# Extraction Artifacts

All original artifacts have been removed — they are reproducible from the
reference binary at `claude_web_env/reference/environment-manager.gz`.

Embedded scripts (previously in `embedded_scripts/`) are now in the
reconstructed source at `src/internal/envtype/anthropic/install_scripts/scripts.go`.

## Regenerating artifacts

```bash
gunzip -k claude_web_env/reference/environment-manager.gz
BIN=claude_web_env/reference/environment-manager

# Build info (module deps, build flags)
go version -m "$BIN"

# Source file paths from DWARF
go tool objdump "$BIN" 2>/dev/null | grep -oP '(?<=TEXT )\S+' | sort -u

# Application functions with addresses
go tool nm "$BIN" | grep -E '^0x[0-9a-f]+ T (cmd\.|internal/)' | sort

# Application string literals
strings "$BIN" | grep -vE '^(runtime\.|go\.|reflect\.|sync\.)' | sort -u
```
