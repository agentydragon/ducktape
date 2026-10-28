/** Run one schema conversion while preventing remote references from reaching the network. */
export async function withRemoteReferencesDisabled(operation, onRemoteReference = () => undefined) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    onRemoteReference(url);
    return new Response(`Remote schema references are disabled: ${url}`, {
      status: 400,
      statusText: "Remote references disabled",
    });
  };

  try {
    return await operation();
  } finally {
    globalThis.fetch = originalFetch;
  }
}
