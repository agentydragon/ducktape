// Pure link helpers for the MDX renderer (mdx.tsx). Kept out of mdx.tsx so that file only
// exports React components (Vite fast-refresh boundary; see eslint react-refresh rule), and
// so these can be unit-tested in isolation (mdx.test.ts).

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
