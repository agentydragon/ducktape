# Squid with ssl_bump, built from Alpine because no usable public image exists:
# Ubuntu's squid is gnutls and rejects ssl-bump outright, Debian's ships it
# disabled, and the Alpine package is the one built --with-openssl.
#
# 3.23 rather than 3.22 for the Squid version: 3.22 ships 6.12, and upstream
# supports only the newest series, calling everything older "archival ... not
# intended for general use in new installations". 3.23 ships 7.6, the current
# stable. This is not hygiene — the spike's first run died on
# `tls_outgoing_options flags=DONT_VERIFY_PEER`, a directive that survived a
# major bump with its meaning changed and errored at neither parse nor request
# time. Answering the substitution questions on a Squid we would not deploy
# invites exactly that failure again, and for a fence the silent form is the
# dangerous one: a rule that quietly stops matching forwards the real credential
# instead of returning an error.
#
# curl and openssl are kept in the image on purpose: this is a spike whose whole
# point is exec'ing in and driving requests through the proxy.
#
# CLEANUP(added 2026-08-10): delete with cluster/k8s/x/squid-egress-spike
#   once the spike has answered its questions and either graduates to a real
#   fence or is abandoned. See that directory's README for the question list.
FROM alpine:3.23@sha256:fd791d74b68913cbb027c6546007b3f0d3bc45125f797758156952bc2d6daf40

RUN apk add --no-cache squid openssl curl ca-certificates gettext \
    && squid -v | head -1 > /etc/squid-version \
    && mkdir -p /var/cache/squid /var/log/squid \
    && chown -R squid:squid /var/cache/squid /var/log/squid

LABEL org.opencontainers.image.source="https://github.com/agentydragon/ducktape" \
      org.opencontainers.image.description="Squid with ssl_bump for the egress-proxy spike"

USER squid
ENTRYPOINT ["squid"]
