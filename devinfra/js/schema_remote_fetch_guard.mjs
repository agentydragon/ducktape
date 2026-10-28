/** Run one schema conversion while preventing remote references from reaching the network. */
export async function withRemoteReferencesDisabled(operation) {
  const originalFetch = globalThis.fetch;
  // Return an HTTP error rather than reject: the pinned Hey resolver clears its
  // abort timer only after fetch resolves, so a rejection leaves a live 60s timer.
  globalThis.fetch = async () => {
    return new Response("Remote schema references are disabled during code generation", {
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
