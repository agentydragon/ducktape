# Transform Talos peer_status.go (kubespan adapter) for kubespand.
#
# Applied by genrule at build time to the upstream Talos source.
# Only functionally necessary changes — no cosmetic renames or license stripping.
#
# Transformations:
#   - Remove internal wireguard adapter import and its group separator
#   - Add local PeerDownInterval constant (from wireguard adapter)
#   - Replace wireguard.PeerDownInterval -> PeerDownInterval

BEGIN { prev_blank = 0 }

# Remove internal wireguard adapter import (discards any buffered blank line before it).
/talos\/internal\/app\/machined\/pkg\/adapters\/wireguard/ { prev_blank = 0; next }

# After EndpointConnectionTimeout, inject PeerDownInterval constant.
/^const EndpointConnectionTimeout/ {
    if (prev_blank) { print ""; prev_blank = 0 }
    print
    print ""
    print "// PeerDownInterval is the time since last handshake when established peer is considered to be down."
    print "//"
    print "// WG whitepaper defines a downed peer as being:"
    print "// Handshake Timeout (180s) + Rekey Timeout (5s) + Rekey Attempt Timeout (90s)"
    print "//"
    print "// This interval is applied when the link is already established."
    print "// Ref: talos/internal/app/machined/pkg/adapters/wireguard/wireguard.go"
    print "const PeerDownInterval = (180 + 5 + 90) * time.Second"
    next
}

# Buffer blank lines so we can suppress the import group separator.
/^[[:space:]]*$/ {
    if (prev_blank) print ""
    prev_blank = 1
    next
}

# Non-blank line: flush buffered blank, apply substitutions, print.
{
    if (prev_blank) { print ""; prev_blank = 0 }
    gsub(/wireguard\.PeerDownInterval/, "PeerDownInterval")
    print
}
