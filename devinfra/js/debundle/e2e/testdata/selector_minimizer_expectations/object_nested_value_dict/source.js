const selectedRegistry = {
  0: { audience: ["alpha"], primed: true, retry: false },
  1: { audience: ["beta"], primed: true, retry: true },
  2: { audience: ["alpha", "beta"], retry: true },
  3: { audience: ["gamma"], primed: true, retry: true },
  4: { audience: ["uniqueDiscriminatorAudience"], primed: true, retry: true },
  5: { audience: ["alpha"], primed: false, retry: true },
  6: { audience: ["beta"], primed: true, retry: false },
  7: { audience: ["gamma"], primed: true, retry: true },
};

const firstSiblingRegistry = {
  0: { audience: ["alpha"], primed: true, retry: false },
  1: { audience: ["beta"], primed: true, retry: true },
  2: { audience: ["alpha", "beta"], retry: true },
  3: { audience: ["gamma"], primed: true, retry: true },
};

const secondSiblingRegistry = {
  5: { audience: ["alpha"], primed: false, retry: true },
  6: { audience: ["beta"], primed: true, retry: false },
  7: { audience: ["gamma"], primed: true, retry: true },
};

const thirdSiblingRegistry = {
  0: { audience: ["alpha"], primed: true, retry: false },
  4: { audience: ["beta"], primed: true, retry: true },
};

export { selectedRegistry };
