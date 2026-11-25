// Preserve saved positions across upgrades. Ignore retired items and append new ones in schema order.
export function panelOrder(settings) {
  const items = settings.settings_schema.get_key("panel-order").get_default_value().deepUnpack();
  const saved = settings.get_strv("panel-order").filter((id) => items.includes(id));
  return [...new Set([...saved, ...items])];
}
