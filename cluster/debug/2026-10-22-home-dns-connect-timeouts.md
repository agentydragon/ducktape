# Home DNS connection timeouts

Observed 2026-10-23 03:50–03:51 UTC during staging OAuth callback verification. The
callback token endpoint returned HTTP 200, then both app pods had intermittent
connect/read timeouts while fetching JWKS.

Fresh resolver checks reproduced intermittent DNS failures independently of the
callback: uncached requests from the app to the CoreDNS Pod on Wyrm2 failed 2 of 8,
while requests to the OVH CoreDNS Pod passed 8 of 8. On Wyrm2, UDP queries to the home
router failed 1 of 8 and queries to `1.1.1.1` failed 3 of 8; TCP queries to both passed
6 of 6. Direct TLS checks to all six resolved gateway addresses returned HTTP 200, and
no Cilium DNS drops were observed during the sample.

This is consistent with the documented home WAN new-connection drops in
[#9604](https://github.com/agentydragon/ducktape/issues/9604). Keeping CoreDNS on the
OVH zone is a temporary placement mitigation; this sample does not establish that the
home WAN issue is fixed or characterize its root cause further. Remove the pin only
after #9604 is fixed and cold DNS probes pass from both home nodes and OVH workers, then
replace the bootstrap patch with explicit CoreDNS addon/topology ownership.
