import { Anchor, type AnchorProps } from "@mantine/core";
import type { ReactNode } from "react";

type AnchorStyleProps = Omit<AnchorProps, "href" | "target" | "rel" | "underline" | "children">;

/** A link to an external page, opened in a new tab. Underlined by default — a screenshot can't
 * show a hover state, and even live the click affordance shouldn't require moving the pointer —
 * so every external `href` in the console goes through this instead of a bare Mantine `Anchor`. */
export function ExternalLink({ href, children, ...props }: AnchorStyleProps & { href: string; children: ReactNode }) {
  return (
    <Anchor href={href} target="_blank" rel="noreferrer" underline="always" {...props}>
      {children}
    </Anchor>
  );
}
