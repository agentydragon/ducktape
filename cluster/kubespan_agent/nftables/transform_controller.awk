# Transform Talos NfTablesChainController (nftables_chain.go) for kubespand.
#
# Applied by genrule at build time to the upstream Talos source.
# Only functionally necessary changes — no cosmetic renames or license stripping.
#
# Transformations:
#   - Remove networkadapter import (Talos internal/) and its group separator
#   - Replace networkadapter.NfTablesRule -> NfTablesRule (now in same package)
#   - Remove preCreateIptablesNFTable call and method (Talos-specific iptables compat)

BEGIN { prev_blank = 0; skip_lines = 0 }

# Remove networkadapter import (discards any buffered blank line before it).
/networkadapter.*talos\/internal/ { prev_blank = 0; next }

# Remove preCreateIptablesNFTable method definition (last function in file).
# Must come before the call-site pattern since both contain "preCreateIptablesNFTable"
# but only the method def starts at column 0 with "func".
/^func.*preCreateIptablesNFTable/ { exit }

# Remove preCreateIptablesNFTable call block: the if-err line + 2 body lines + blank.
/preCreateIptablesNFTable\(logger/ { skip_lines = 3; prev_blank = 0; next }
skip_lines > 0 { skip_lines--; next }

# Buffer blank lines so we can suppress the import group separator.
/^[[:space:]]*$/ {
    if (prev_blank) print ""
    prev_blank = 1
    next
}

# Non-blank line: flush buffered blank, apply substitutions, print.
{
    if (prev_blank) { print ""; prev_blank = 0 }
    gsub(/networkadapter\.NfTablesRule/, "NfTablesRule")
    print
}
