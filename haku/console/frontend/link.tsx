import { Anchor, type AnchorProps } from "@mantine/core";
import type { ReactNode } from "react";

type AnchorStyleProps = Omit<AnchorProps, "href" | "target" | "rel" | "underline" | "children">;
type ExternalLinkProps = AnchorStyleProps & {
  href: string;
  children: ReactNode;
  title?: string;
};

/** A link to an external page, opened in a new tab. Underlined always, because a screenshot cannot
 * show a hover state and the click affordance shouldn't require moving the pointer — so every
 * external `href` in the console goes through this rather than a bare Mantine `Anchor`. */
export function ExternalLink({ href, children, ...props }: ExternalLinkProps): JSX.Element {
  return (
    <Anchor href={href} target="_blank" rel="noreferrer" underline="always" {...props}>
      {children}
    </Anchor>
  );
}
