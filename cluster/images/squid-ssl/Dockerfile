# Squid with ssl_bump AND ICAP, for the egress-proxy spike.
#
# Base choice is a three-way constraint, and only one combination satisfies all
# of it:
#
#   * `--with-openssl` + `--enable-ssl-crtd` — required for ssl_bump. Ubuntu's
#     squid is GnuTLS and refuses it outright; Debian's DEFAULT `squid` package
#     is GnuTLS with ssl-crtd disabled. (An earlier version of this comment said
#     Debian "ships it disabled" full stop and sent us to Alpine — true of the
#     default package, wrong about the distribution. Debian builds a second
#     flavour, `squid-openssl`, with OpenSSL and cert generation enabled.)
#   * `--enable-icap-client` — Alpine's squid has neither ICAP nor eCAP. That is
#     the only mechanism in Squid for rewriting a header programmatically:
#     external_acl_type cannot (its response keywords are a closed set) and
#     url_rewrite_program only touches URLs. Without it, per-client credential
#     substitution can only be static ACLs regenerated per agent. Debian enables
#     ICAP and eCAP in its shared configure flags, so both flavours have them.
#   * Squid 7.x — upstream supports only the newest series and calls everything
#     older "archival ... not intended for general use in new installations".
#     Debian ships 7.6 only in forky/sid; trixie (stable) is on 6.13.
#
# Hence debian:testing rather than stable: it is the only base carrying
# squid-openssl at 7.x with ICAP. Pinned by digest, so the rolling suite cannot
# move under us silently, and the CI verify step asserts all three properties on
# every build rather than trusting this comment.
#
# curl and openssl are kept in the image on purpose: this is a spike whose whole
# point is exec'ing in and driving requests through the proxy.
#
# CLEANUP(added 2026-08-10): delete with the archived spike snapshot once the
#   reference is no longer useful. See its README for the question list.
FROM debian:testing@sha256:8fe7e20ebb14e121fa7abd75c2d8de93f2fc2e9ce1f92748f10257b29350d276

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        squid-openssl openssl curl ca-certificates gettext-base \
    && rm -rf /var/lib/apt/lists/* \
    && squid -v | head -1 > /etc/squid-version \
    && mkdir -p /var/cache/squid /var/log/squid /run/squid \
    && chown -R proxy:proxy /var/cache/squid /var/log/squid /run/squid

LABEL org.opencontainers.image.source="https://github.com/agentydragon/ducktape" \
      org.opencontainers.image.description="Squid with ssl_bump and ICAP for the egress-proxy spike"

# Debian's squid runs as `proxy`, not Alpine's `squid` — a different uid, which
# the Deployment's runAsUser must match. The verify step prints it so the number
# comes from the build rather than from an assumption.
USER proxy
ENTRYPOINT ["squid"]
