import { Text } from "@mantine/core";
import { evaluate } from "@mdx-js/mdx";
import type { MDXContent } from "mdx/types";
import { useEffect, useState } from "react";
import * as runtime from "react/jsx-runtime";

import { WIDGETS } from "./widgets.tsx";

// Render an MDX string to React, with the standard widget registry (widgets.tsx) provided as the
// component scope, so authored content can use <Callout>, <PropagationMatrix data={…}/>, etc.
// Plain markdown is valid MDX, so memory/procedure notes render unchanged. Async because evaluate()
// compiles the source at runtime.
//
// SECURITY: evaluate() compiles MDX to a module via `new Function` (eval). haku-ui sets only a
// `frame-ancestors` CSP (no `script-src`), so eval is permitted. This is NOT a new trust boundary:
// Haku authors both this bundle AND the repo content it renders, and the operator views it only
// through Haku's own Authentik-gated, sandboxed cross-origin iframe — the iframe-sandbox boundary
// that isolates haku-ui from the trusted console is unchanged. The widget registry is the only
// surface authored content can reach.
export function Mdx({ source }: { source: string }) {
  const [Content, setContent] = useState<MDXContent | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setError(null);
    evaluate(source, { ...runtime, baseUrl: import.meta.url })
      .then((mod) => alive && setContent(() => mod.default))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [source]);

  if (error)
    return (
      <Text c="red" size="sm">
        Couldn't render this content: {error}
      </Text>
    );
  if (!Content) return null;
  return (
    <div className="md">
      <Content components={WIDGETS} />
    </div>
  );
}
