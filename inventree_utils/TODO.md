# TODO

- Replace `rai_plugin_layer`'s host `python3`/`tar` extraction genrule with a reproducible layer. Choose the packaging design, normalize archive timestamps and ownership, then verify uncached builds emit byte-identical tar files and run `bbr test //inventree_utils/rai_plugin:test_image` to check discovery and import in the pinned image.
- set SupplierPart links
- normalize SupplierPart URLs
- set images on SupplierParts
- link stock items to singleton SupplierPart - with confirmation
