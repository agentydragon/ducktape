# Neuron tracking experiment

Explore using one Codex conversation to segment every visible pixel of each
neuron in microscopy video, separate its body and branches, preserve its identity
across frames, and inspect and correct its rendered masks before hand-off.

This is a planning scaffold. The video has been inspected; annotation, rendering,
and export have not been implemented or evaluated. The draft annotation
instructions are in `annotate.md`; the trial mask format is in
`annotation_format.md`.

Keep local input media in `data/` and generated frames, annotations, overlays,
videos, and session logs in `runs/`. Both directories and common media extensions
are ignored. Source files can also remain outside the checkout. Do not commit
media blobs or local symlinks to them.
