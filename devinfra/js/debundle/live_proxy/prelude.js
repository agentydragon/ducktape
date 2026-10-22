(() => {
  const tag = "[js-debundle-live-proxy]";
  const noopRegistration = {
    active: null,
    installing: null,
    scope: "/",
    waiting: null,
    addEventListener() {},
    async unregister() {
      return true;
    },
    async update() {},
  };
  const serviceWorkerStub = {
    controller: null,
    ready: Promise.resolve(noopRegistration),
    addEventListener() {},
    removeEventListener() {},
    register: async () => noopRegistration,
    getRegistration: async () => undefined,
    getRegistrations: async () => [],
    startMessages() {},
  };
  document.documentElement.dataset.jsDebundleLiveProxy = "true";
  const existing = navigator.serviceWorker;
  if (existing && typeof existing.getRegistrations === "function") {
    existing
      .getRegistrations()
      .then((registrations) => {
        for (const registration of registrations) {
          registration.unregister().catch(() => {});
        }
      })
      .catch(() => {});
  }
  try {
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: serviceWorkerStub,
    });
  } catch (error) {
    if (existing) {
      // Some browsers expose these fallback properties as readonly too.
      try {
        existing.register = serviceWorkerStub.register;
      } catch {
        /* Readonly. */
      }
      try {
        existing.getRegistration = serviceWorkerStub.getRegistration;
      } catch {
        /* Readonly. */
      }
      try {
        existing.getRegistrations = serviceWorkerStub.getRegistrations;
      } catch {
        /* Readonly. */
      }
      try {
        existing.ready = serviceWorkerStub.ready;
      } catch {
        /* Readonly. */
      }
    }
    console.warn(tag, "unable to replace navigator.serviceWorker directly", error);
  }
  console.info(tag, "active", globalThis.__jsDebundleLiveProxy);
})();
