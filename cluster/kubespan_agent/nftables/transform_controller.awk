# Transform Talos NfTablesChainController (nftables_chain.go) for kubespand.
#
# Applied by genrule at build time to the upstream Talos source. Transformations:
#   - Remove MPL license header
#   - Rename package network -> nftables
#   - Remove networkadapter import (Talos internal/) and its group separator
#   - Rename NfTablesChainController -> KubespandNfTablesChainController
#   - Replace networkadapter.NfTablesRule -> NfTablesRule (local package)
#   - Remove preCreateIptablesNFTable call and method (Talos-specific)

BEGIN { prev_blank = 0; skip_lines = 0 }

# Skip MPL license header (3 comment lines).
/^\/\/ This Source Code Form/ { next }
/^\/\/ License, v\. 2\.0/ { next }
/^\/\/ file, You can obtain one at/ { next }

# Package rename.
/^package network$/ { print "package nftables"; prev_blank = 0; next }

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
    gsub(/NfTablesChainController/, "KubespandNfTablesChainController")
    gsub(/networkadapter\.NfTablesRule/, "NfTablesRule")
    print
}
