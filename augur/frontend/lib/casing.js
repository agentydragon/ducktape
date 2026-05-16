const SPECIAL_SNAKE_KEYS = {
  from_value: "from",
  to_value: "to",
};

export function snakeToCamelKey(key) {
  if (SPECIAL_SNAKE_KEYS[key]) {
    return SPECIAL_SNAKE_KEYS[key];
  }
  return key.replace(/_([a-z0-9])/g, (_, char) => char.toUpperCase());
}

export function camelToSnakeKey(key) {
  return key.replace(/[A-Z]/g, (char) => `_${char.toLowerCase()}`);
}

function convertObjectKeys(value, keyFn) {
  if (Array.isArray(value)) {
    return value.map((item) => convertObjectKeys(item, keyFn));
  }
  if (!value || typeof value !== "object") {
    return value;
  }
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [keyFn(key), convertObjectKeys(item, keyFn)]));
}

export function camelizeObjectKeys(value) {
  return convertObjectKeys(value, snakeToCamelKey);
}

export function decamelizeObjectKeys(value) {
  return convertObjectKeys(value, camelToSnakeKey);
}
