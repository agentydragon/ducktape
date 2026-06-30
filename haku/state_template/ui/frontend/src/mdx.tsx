import { Anchor, Text } from "@mantine/core";
import { evaluate } from "@mdx-js/mdx";
import type { MDXContent } from "mdx/types";
import type { ComponentPropsWithoutRef } from "react";
import { useEffect, useMemo, useState } from "react";
import * as runtime from "react/jsx-runtime";

import { WIDGETS } from "./widgets.tsx";

// A link is external if it has a URL scheme (http:, mailto:, …) or is protocol-relative.
export function isExternal(href: string): boolean {
  return /^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith("//");
}

// Resolve a repo-relative markdown link against the file it appears in, so notes can link each
// other with `../runs/x.md` or `procedures/foo.md` (leading `/` = repo root). This is what makes
// the garden an interlinked web rather than a pile of isolated files.
export function resolveRepoPath(base: string | undefined, href: string): string {
  const clean = href.replace(/[#?].*$/, "");
  if (clean.startsWith("/")) return clean.replace(/^\/+/, "");
  const baseDir = base && base.includes("/") ? base.slice(0, base.lastIndexOf("/")) : "";
  const parts = baseDir ? baseDir.split("/") : [];
  for (const seg of clean.split("/")) {
    if (seg === "" || seg === ".") continue;
    if (seg === "..") parts.pop();
    else parts.push(seg);
  }
  return parts.join("/");
}

// Build the MDX component scope: the standard widgets + an `a` override. Internal `.md`/`.mdx`
// links navigate within the garden (via onNavigate); external links open in a new tab; everything
// else stays a plain anchor.
function mdxComponents(basePath: string | undefined, onNavigate: ((path: string) => void) | undefined) {
  function MdLink({ href = "", children, ...rest }: ComponentPropsWithoutRef<"a">) {
    if (!href || href.startsWith("#") || isExternal(href)) {
      const ext = isExternal(href);
      return (
        <Anchor href={href} target={ext ? "_blank" : undefined} rel={ext ? "noreferrer" : undefined} {...rest}>
          {children}
        </Anchor>
      );
    }
    if (onNavigate && /\.mdx?$/.test(href.replace(/[#?].*$/, ""))) {
      const target = resolveRepoPath(basePath, href);
      // Keep the href (a real, focusable link) but intercept the click to navigate in-app.
      return (
        <Anchor
          href={href}
          onClick={(e) => {
            e.preventDefault();
            onNavigate(target);
          }}
          {...rest}
        >
          {children}
        </Anchor>
      );
    }
    return (
      <Anchor href={href} {...rest}>
        {children}
      </Anchor>
    );
  }
  return { ...WIDGETS, a: MdLink };
}

// Render an MDX string to React, with the standard widget registry (widgets.tsx) provided as the
// component scope, so authored content can use <Callout>, <PropagationMatrix data={…}/>, etc., and
// link to other garden files. Plain markdown is valid MDX, so memory/procedure notes render
// unchanged. `basePath` is the rendered file's repo path (for relative-link resolution); pass
// `onNavigate` to make internal links open in-app. Async because evaluate() compiles at runtime.
//
// SECURITY: evaluate() compiles MDX to a module via `new Function` (eval). haku-ui sets only a
// `frame-ancestors` CSP (no `script-src`), so eval is permitted. This is NOT a new trust boundary:
// Haku authors both this bundle AND the repo content it renders, and the operator views it only
// through Haku's own Authentik-gated, sandboxed cross-origin iframe — the iframe-sandbox boundary
// that isolates haku-ui from the trusted console is unchanged. The widget registry + link handler
// are the only surface authored content can reach.
export function Mdx({
  source,
  basePath,
  onNavigate,
}: {
  source: string;
  basePath?: string;
  onNavigate?: (path: string) => void;
}) {
  const [Content, setContent] = useState<MDXContent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const components = useMemo(() => mdxComponents(basePath, onNavigate), [basePath, onNavigate]);

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
      <Content components={components} />
    </div>
  );
}
