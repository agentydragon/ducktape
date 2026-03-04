# Transform Talos peer_spec.go for kubespand.
#
# Applied by genrule at build time to the upstream Talos source.
# Only functionally necessary changes — no cosmetic renames or license stripping.
#
# Transformations:
#   - Remove gen/xslices and slices imports (replaced inline)
#   - Change cluster.Identity → kubespan.Identity (kubespand has no cluster.Identity)
#   - Change Identity input namespace from cluster to kubespan
#   - Change self-identification from NodeID to PublicKey
#   - Merge affiliate self-skip into PublicKey check (delete separate block)
#   - Inject endpoint filter initialization (kubespand-specific feature)
#   - Replace slices.Clone(Endpoints) with filterEndpoints (applies configured filters)
#   - Replace xslices.Map in dumpSet with inline range loop

BEGIN { skip = 0; prev = "" }

# Remove xslices import line.
/\"github\.com\/siderolabs\/gen\/xslices\"/ { next }

# Remove slices import line (slices.Clone replaced by filterEndpoints).
/^\t\"slices\"$/ { next }

# Delete the affiliate.Metadata().ID() self-check block (4 lines: if, comment, continue, }).
# After the transform, self-skip is handled by the PublicKey == "" check instead.
/affiliate\.Metadata\(\)\.ID\(\)/ {
    skip = 3
    next
}
skip > 0 { skip--; next }

# Apply global substitutions.
# Order matters: IdentityType before Identity to avoid double-transform.
{
    gsub(/cluster\.IdentityType/, "kubespan.IdentityType")
    gsub(/cluster\.LocalIdentity/, "kubespan.LocalIdentity")
    gsub(/cluster\.Identity/, "kubespan.Identity")
    gsub(/\.NodeID/, ".PublicKey")
    gsub(/localAffiliateID/, "localPublicKey")
    gsub(/no kubespan information, skip it/, "no kubespan information or self, skip it")
}

# Fix Identity input namespace: when current line has kubespan.IdentityType,
# the buffered previous line contains the namespace that needs changing.
/kubespan\.IdentityType/ {
    if (prev ~ /cluster\.NamespaceName/) {
        gsub(/cluster\.NamespaceName/, "kubespan.NamespaceName", prev)
    }
}

# Extend PublicKey == "" check to also skip self (combined check).
/spec\.KubeSpan\.PublicKey == ""/ {
    gsub(/spec\.KubeSpan\.PublicKey == ""/, "spec.KubeSpan.PublicKey == \"\" || spec.KubeSpan.PublicKey == localPublicKey")
}

# Replace Endpoints clone with filter call.
/slices\.Clone\(spec\.KubeSpan\.Endpoints\)/ {
    gsub(/slices\.Clone\(spec\.KubeSpan\.Endpoints\)/, "filterEndpoints(spec.KubeSpan.Endpoints, filters)")
}

# Replace xslices.Map in dumpSet with inline range loop.
/xslices\.Map\(set\.Ranges\(\), netipx\.IPRange\.String\)/ {
    flush_prev()
    print "\tranges := set.Ranges()"
    print "\tresult := make([]string, len(ranges))"
    print "\tfor i, r := range ranges {"
    print "\t\tresult[i] = r.String()"
    print "\t}"
    print "\treturn result"
    next
}

# Inject endpoint filter initialization right after the localPublicKey line.
/localPublicKey := localIdentity/ {
    flush_prev()
    print
    print ""
    print "\t\t\tfilters := parseEndpointFilters(cfg.TypedSpec().EndpointFilters)"
    next
}

# Line buffering for namespace look-ahead.
{
    flush_prev()
    prev = $0
}

END { flush_prev() }

function flush_prev() {
    if (prev != "") {
        print prev
        prev = ""
    }
}
