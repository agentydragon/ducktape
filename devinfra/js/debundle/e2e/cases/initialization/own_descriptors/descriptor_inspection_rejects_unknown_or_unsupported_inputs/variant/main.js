globalThis.saved = () => later;
const descriptors = Object.getOwnPropertyDescriptors(globalThis.record);

const later = 42;
