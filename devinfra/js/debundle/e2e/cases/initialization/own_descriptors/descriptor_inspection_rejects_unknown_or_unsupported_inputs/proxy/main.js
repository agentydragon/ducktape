const descriptors = Object.getOwnPropertyDescriptors(
  new Proxy(
    {},
    {
      ownKeys() {
        return later;
      },
    }
  )
);

const later = 42;
