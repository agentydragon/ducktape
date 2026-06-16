const selectedDefinition = {
  id: "uniqueDiscriminatorId",
  prose: `
This is a long block of descriptive prose attached to the definition.
It spans many lines and repeats boilerplate that is shared verbatim with
every sibling definition in this module, so it carries no discriminating
power on its own. A robust selector should never anchor on this value:
it is large, brittle to edits, and identical across siblings. The short
sibling key "id" already uniquely identifies this definition.
More filler. More filler. More filler. More filler. More filler.
Yet more filler so the value is unmistakably the largest node here.
`,
  enabled: true,
  rank: 3,
};

const firstSiblingDefinition = {
  id: "alphaId",
  prose: `
This is a long block of descriptive prose attached to the definition.
It spans many lines and repeats boilerplate that is shared verbatim with
every sibling definition in this module, so it carries no discriminating
power on its own. A robust selector should never anchor on this value:
it is large, brittle to edits, and identical across siblings. The short
sibling key "id" already uniquely identifies this definition.
More filler. More filler. More filler. More filler. More filler.
Yet more filler so the value is unmistakably the largest node here.
`,
  enabled: true,
  rank: 1,
};

const secondSiblingDefinition = {
  id: "bravoId",
  prose: `
This is a long block of descriptive prose attached to the definition.
It spans many lines and repeats boilerplate that is shared verbatim with
every sibling definition in this module, so it carries no discriminating
power on its own. A robust selector should never anchor on this value:
it is large, brittle to edits, and identical across siblings. The short
sibling key "id" already uniquely identifies this definition.
More filler. More filler. More filler. More filler. More filler.
Yet more filler so the value is unmistakably the largest node here.
`,
  enabled: false,
  rank: 2,
};

export { selectedDefinition };
