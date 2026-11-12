# TODO

- Replace `rai_plugin_layer`'s host `python3`/`tar` extraction genrule with a reproducible layer. Choose the packaging design, normalize archive timestamps and ownership, then verify uncached builds emit byte-identical tar files and the image imports the plugin under InvenTree's Python 3.11 runtime.
- set SupplierPart links
- normalize SupplierPart URLs
- set images on SupplierParts
- link stock items to singleton SupplierPart - with confirmation
