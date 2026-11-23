# haku/console/frontend TODO

- Cover `startGeolocationWatch` and `stopGeolocationWatch` in `bridge.test.ts`. `parseInbound`
  handles both, but the spec exercises only `requestGeolocation`, and nothing forces the parser to
  learn a new `Inbound` variant. Consider inferring the parser from a shared runtime schema, or an
  exhaustive per-variant test.
