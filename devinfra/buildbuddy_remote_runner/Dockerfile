# BuildBuddy Remote Runner image: where devinfra/ci/bazel_ci.py runs.
#
# The runner is the Bazel *client* -- it runs bazel-diff over the affected set
# and drives the invocation. It needs the repo's developer tooling; a Bazel
# action does not, and gets its tools from runfiles instead.
#
# This extends the RBE container image in devinfra/rbe_container_image/Dockerfile: its
# digest is in every action's cache key, so it must almost never move. The
# runner image is selected by devinfra/bbr.json and is outside every action's
# cache key, so it can churn with artifact-pin bumps.
#
# BASE_REF is the whole base reference, passed by
# .github/workflows/container-images.yml from
# devinfra/image_pins.json's rbe_container digest. Building against the pin rather
# than `:latest` keeps the runner on the same OS the actions execute on, and
# makes an RBE container repin cascade into a remote runner rebuild instead of silently
# diverging. It is the whole reference and not just the digest so the image can
# also be built against a locally built RBE container image, which is how it gets exercised
# without pulling the private GHCR package.
ARG BASE_REF
FROM ${BASE_REF}
LABEL org.opencontainers.image.source=https://github.com/agentydragon/ducktape

# --- Nix + Bazel runner tools ---
# Install Nix (single-user, no daemon) and the focused runner package. This VM
# drives Bazel CI with Bazelisk and bazel-diff, and uploads best-effort runner
# diagnostics with bb. Pre-commit runs in a separate GitHub Actions workflow.
#
# The runner-tools closure is fetched from the anonymous Attic public cache,
# with cache.nixos.org as a fallback. Only the symlinkJoin derivation needs
# a local build.

# Copy only the repo files needed to evaluate and build `.#rbetools`. Keep
# this list in sync with the buildbuddy-remote-runner path filters in
# .github/workflows/container-images.yml. Pull requests build this image
# without publishing it, so missing evaluation inputs fail before merge.
COPY flake.nix flake.lock /tmp/flake/
COPY nix/artifact-pins.json /tmp/flake/nix/artifact-pins.json
COPY nix/attic-pubkeys.json /tmp/flake/nix/attic-pubkeys.json
COPY nix/flake /tmp/flake/nix/flake
COPY nix/packages /tmp/flake/nix/packages
# nix/packages/gaffer.nix reads ../gaffer-pins.json during package-set
# evaluation, even though rbetools does not include the private artifacts.
COPY nix/gaffer-pins.json /tmp/flake/nix/gaffer-pins.json
COPY skills/skills_registry.json /tmp/flake/skills/skills_registry.json
SHELL ["/bin/bash", "-c"]
RUN mkdir -p ~/.config/nix \
    && ATTIC_PUBLIC_KEY="$(python3 -c 'import json; print(next(key for key in json.load(open("/tmp/flake/nix/attic-pubkeys.json")) if key.startswith("public:")))')" \
    # nix.conf MUST be written BEFORE the installer runs — the installer
    # internally runs nix-env which reads this config. Without build-users-group=,
    # it defaults to 'nixbld' which doesn't exist in the container.
    && printf '%s\n' \
         'build-users-group =' \
         'experimental-features = nix-command flakes' \
         "extra-trusted-public-keys = $ATTIC_PUBLIC_KEY" \
         'extra-substituters = https://cache.allegedly.works/public' \
         'sandbox = true' \
         'max-jobs = auto' \
       > ~/.config/nix/nix.conf \
    && curl -fsSL https://nixos.org/nix/install -o /tmp/nix-install.sh \
    && sh /tmp/nix-install.sh --no-daemon \
    && rm /tmp/nix-install.sh \
    && export PATH="$HOME/.nix-profile/bin:$PATH" \
    && cd /tmp/flake \
    # Use nix build (not nix profile install) to avoid the profile manifest
    # keeping a reference to the nixpkgs source tree (~457MB).
    #
    # With the public Attic cache current, only rbetools' symlinkJoin needs a
    # local build. Without those substitutes, Nix's sandbox fallback in this
    # nested BuildKit environment lets Python wheel builds create
    # /homeless-shelter, which makes later derivations fail. Cap parallelism
    # too in case the closure is temporarily cold.
    && RBETOOLS=$(nix build .#rbetools \
         --max-jobs 2 --no-link --print-out-paths) \
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
