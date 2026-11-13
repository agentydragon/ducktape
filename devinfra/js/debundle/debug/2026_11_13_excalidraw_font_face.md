# Excalidraw font face extraction boundary

The original `fonts/ExcalidrawFontFace.ts` class body (`editor` `unit:881`)
passes the full gate. It retains URL construction, Unicode-range filtering,
fetching, glyph subsetting and CSS generation. Its imports still include the
editor entry for the subsetter, promise helper and local-font protocol.

The adjacent `ASSETS_FALLBACK_URL` initialization (`unit:882`) resolves through
an anonymous statement selector anchored by that property and the class, but
moving it with the class rejects with the existing 410-unit atomic group.
The canonical spec leaves that initialization in the entry. Reproduce by adding
`anonymous_statements[].source_match.match` selecting
`ANYTHING(ExcalidrawFontFace, "ASSETS_FALLBACK_URL", ANYTHING);` and building
the canonical `excalidraw:debundle` target. This rejection does not establish
that the upstream initialization has a runtime cycle.

The browser probe loads the application before importing the class and checks
its real FontFace descriptors, Unicode ranges, skipped CSS generation,
data-URL fetching, local/remote URLs, asset-path fallbacks and initialized
fallback URL. Glyph subsetting and fetch-failure fallback are not directly
covered by this probe. Reduce the initializer's constraint witness before
changing inference or adding an author assertion.
