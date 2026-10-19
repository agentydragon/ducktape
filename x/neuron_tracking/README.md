# Neuron tracking experiment

Explore using one Codex conversation to annotate small dark neurons in microscopy
video, preserve their identities across frames, and inspect and correct its own
rendered annotations before hand-off.

This is a planning scaffold. The video has been inspected; annotation, rendering,
and export have not been implemented or evaluated. The draft annotation
instructions are in `annotate.md`.

Keep local input media in `data/` and generated frames, annotations, overlays,
videos, and session logs in `runs/`. Both directories and common media extensions
are ignored. Source files can also remain outside the checkout. Do not commit
media blobs or local symlinks to them.
