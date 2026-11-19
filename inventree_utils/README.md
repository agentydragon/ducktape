# inventree_utils

InvenTree inventory management plugins.

## Tools

See `inv --list`.

## Plugin deployment

`rai_plugin` ships in the `inventree` CI image (`//inventree_utils/rai_plugin:image`, listed in `devinfra/ci/image_targets.json`). `cluster/parked/inventree/app` pulls that image and enables the plugin with `INVENTREE_PLUGINS_MANDATORY: raiplugin`.
