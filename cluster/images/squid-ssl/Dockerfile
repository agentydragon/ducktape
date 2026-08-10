# Squid with ssl_bump, built from Alpine because no usable public image exists:
# Ubuntu's squid is gnutls and rejects ssl-bump outright, Debian's ships it
# disabled, and the Alpine package is the one built --with-openssl. Alpine also
# tracks Squid 6.x, where the 3.5 image the spike originally used is from 2017.
#
# curl and openssl are kept in the image on purpose: this is a spike whose whole
# point is exec'ing in and driving requests through the proxy.
#
# CLEANUP(added 2026-08-10): delete with cluster/k8s/agents/squid-egress-spike
#   once the spike has answered its questions and either graduates to a real
#   fence or is abandoned. See that directory's README for the question list.
FROM alpine:3.22@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1

RUN apk add --no-cache squid openssl curl ca-certificates gettext \
    && squid -v | head -1 > /etc/squid-version \
    && mkdir -p /var/cache/squid /var/log/squid \
    && chown -R squid:squid /var/cache/squid /var/log/squid

LABEL org.opencontainers.image.source="https://github.com/agentydragon/ducktape" \
      org.opencontainers.image.description="Squid with ssl_bump for the egress-proxy spike"

USER squid
ENTRYPOINT ["squid"]
