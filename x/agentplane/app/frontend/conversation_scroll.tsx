import { ScrollArea, Stack } from "@mantine/core";
import { type JSX, type ReactNode, useLayoutEffect, useRef } from "react";

/** Content growth follows the bottom, never a reader who has scrolled into earlier history. */
export function ConversationScroll({ children }: { children: ReactNode }): JSX.Element {
  const viewport = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const area = viewport.current;
    const body = content.current;
    if (!area || !body) return;
    let following = true;
    let lastTop = area.scrollTop;
    let contentHeight = area.scrollHeight;
    let viewportHeight = area.clientHeight;
    const onScroll = (): void => {
      // scrollTop can be fractional, while the two heights are rounded CSS pixels.
      if (area.scrollHeight - area.clientHeight - area.scrollTop <= 2) following = true;
      // A scroll event from our own write may arrive after another content resize. Only an
      // actual upward movement opts out; being momentarily behind new content does not.
      else if (area.scrollTop < lastTop && area.scrollHeight === contentHeight && area.clientHeight === viewportHeight)
        following = false;
      lastTop = area.scrollTop;
    };
    const follow = (): void => {
      if (following) {
        area.scrollTop = area.scrollHeight;
        lastTop = area.scrollTop;
      }
      contentHeight = area.scrollHeight;
      viewportHeight = area.clientHeight;
    };
    const observer = new ResizeObserver(follow);
    observer.observe(body);
    observer.observe(area);
    area.addEventListener("scroll", onScroll, { passive: true });
    follow();
    return () => {
      observer.disconnect();
      area.removeEventListener("scroll", onScroll);
    };
  }, []);

  return (
    <ScrollArea
      viewportRef={viewport}
      viewportProps={{ "aria-label": "Thread history", role: "region", tabIndex: 0 }}
      style={{ flex: 1, minHeight: 0 }}
    >
      <Stack ref={content}>{children}</Stack>
    </ScrollArea>
  );
}
