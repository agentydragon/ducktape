async ({ method, url, headers, body, referrer }) => {
  const options = {
    method,
    headers,
    credentials: "same-origin",
    mode: "same-origin",
    redirect: "manual",
    cache: "no-store",
    signal: AbortSignal.timeout(20000),
  };
  if (referrer) options.referrer = referrer;
  if (method !== "GET" && method !== "HEAD") {
    options.body = Uint8Array.from(atob(body), (character) => character.charCodeAt(0));
  }
  const response = await fetch(url, options);
  const reader = response.body?.getReader();
  const chunks = [];
  let size = 0;
  if (reader) {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > 16 * 1024 * 1024) {
        await reader.cancel();
        throw new Error("response size");
      }
      // Bound the argument count when turning response bytes into a string for btoa.
      for (let offset = 0; offset < value.length; offset += 16384) {
        chunks.push(String.fromCharCode(...value.subarray(offset, offset + 16384)));
      }
    }
  }
  return {
    status: response.type === "opaqueredirect" ? 302 : response.status,
    body: btoa(chunks.join("")),
    content_type: response.headers.get("content-type") || "application/octet-stream",
    csrf: response.headers.get("x-csrf-token"),
    challenge: response.headers.get("cf-mitigated") === "challenge",
  };
};
