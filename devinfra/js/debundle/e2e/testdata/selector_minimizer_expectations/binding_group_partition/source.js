const selectedPrimary = makeEntry("primary", {
    transientNoise: Date.now(),
    enabled: true,
  }),
  selectedSecondary = makeEntry("secondary", {
    transientNoise: Date.now(),
    enabled: true,
  }),
  nearbyOther = makeEntry("nearby", {
    transientNoise: Date.now(),
  });

const unrelatedPrimary = makeEntry("primary", {
  enabled: false,
});

const selectedStandalone = registerRoute("settings", {
  kind: "panel",
  cacheKey: Math.random(),
  title: "Settings",
});

const sameRouteDifferentKind = registerRoute("settings", {
  kind: "dialog",
  cacheKey: Math.random(),
});

function makeEntry(kind, options) {
  return { kind, options };
}

function registerRoute(name, options) {
  return { name, options };
}

export { selectedPrimary, selectedSecondary, selectedStandalone };
