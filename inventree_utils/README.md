# inventree_utils

InvenTree inventory management plugins.

## Tools

See `inv --list`.

## Plugin deployment

`rai_plugin` ships in the `inventree` image (`//inventree_utils/rai_plugin:image`). While InvenTree is parked the image is off the publish roster (`devinfra/ci/image_targets.json`); a revival restores the entry (<../cluster/docs/decisions.md>). `cluster/parked/inventree/app` pulls that image and enables the plugin with `INVENTREE_PLUGINS_MANDATORY: raiplugin`.
