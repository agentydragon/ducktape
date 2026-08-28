# `bb remote` runner image: where devinfra/ci/bazel_ci.sh runs.
#
# The runner is the Bazel *client* -- it runs bazel-diff over the affected set
# and drives the invocation. It needs the repo's developer tooling; a Bazel
# action does not, and gets its tools from runfiles instead.
#
# Deviation from devinfra/rbe_image/Dockerfile, which this extends: that image's
# digest is in every action's cache key, so it must almost never move. This
# one's digest appears only in devinfra/bbr.json, which no action hashes, so it
# is free to churn with every artifact-pin bump.
#
# BASE_REF is the whole base reference, passed by
# .github/workflows/container-images.yml as the rbe-worker image at
# devinfra/image_pins.json's rbe_worker digest. Building against the pin rather
# than `:latest` keeps the runner on the same OS the actions execute on, and
# makes a worker repin cascade into a runner rebuild instead of silently
# diverging. It is the whole reference and not just the digest so the image can
# also be built against a locally built worker, which is how it gets exercised
# without pulling the private GHCR package.
ARG BASE_REF
FROM ${BASE_REF}
LABEL org.opencontainers.image.source=https://github.com/agentydragon/ducktape

# --- Nix + devtools ---
# Install Nix (single-user, no daemon) and the flake devtools package.
# This gives the runner the same tools as the local devshell (pre-commit,
# bazelisk, ruff, buildifier, bb, bbapi, gh, kubectl, etc.) without
# maintaining a separate apt/pip install list.
#
# The devtools closure is fetched from cache.nixos.org (and attic when
# available). Only the symlinkJoin derivation itself needs a local build.

# Copy only the repo files needed to evaluate and build `.#rbetools`. Keep
# this list in sync with the bbr-runner path filters in
# .github/workflows/container-images.yml. Pull requests build this image
# without publishing it, so missing evaluation inputs fail before merge.
COPY flake.nix flake.lock /tmp/flake/
COPY nix/artifact-pins.json /tmp/flake/nix/artifact-pins.json
COPY nix/packages /tmp/flake/nix/packages
# nix/packages/gaffer.nix reads ../gaffer-pins.json during package-set
# evaluation, even though rbetools does not include the private artifacts.
COPY nix/gaffer-pins.json /tmp/flake/nix/gaffer-pins.json
COPY skills/skills_registry.json /tmp/flake/skills/skills_registry.json
SHELL ["/bin/bash", "-c"]
RUN mkdir -p ~/.config/nix \
    # nix.conf MUST be written BEFORE the installer runs — the installer
    # internally runs nix-env which reads this config. Without build-users-group=,
    # it defaults to 'nixbld' which doesn't exist in the container.
    && printf '%s\n' \
         'build-users-group =' \
         'experimental-features = nix-command flakes' \
         'sandbox = false' \
         'max-jobs = auto' \
       > ~/.config/nix/nix.conf \
    && curl -fsSL https://nixos.org/nix/install -o /tmp/nix-install.sh \
    && sh /tmp/nix-install.sh --no-daemon \
    && rm /tmp/nix-install.sh \
    && export PATH="$HOME/.nix-profile/bin:$PATH" \
    && cd /tmp/flake \
    # Use nix build (not nix profile install) to avoid the profile manifest
    # keeping a reference to the nixpkgs source tree (~457MB).
    && RBETOOLS=$(nix build .#rbetools \
         --no-link --print-out-paths) \
    # Register as GC root so nix-collect-garbage keeps the closure.
    && nix-store --add-root /nix/var/nix/gcroots/rbetools -r "$RBETOOLS" \
    # Symlink binaries into /usr/local/bin (goinit PATH).
    && for bin in "$RBETOOLS"/bin/*; do \
         ln -sfn "$bin" /usr/local/bin/"$(basename "$bin")"; \
       done \
    # Remove profile, flake source, and anything not in the rbetools closure.
    # Run GC before removing nix-profile (which has nix binaries on PATH).
    && nix-collect-garbage -d \
    && rm -rf ~/.nix-profile ~/.nix-defexpr /tmp/flake
