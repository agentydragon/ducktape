const a = { [await Promise.resolve("x")]() {} };
