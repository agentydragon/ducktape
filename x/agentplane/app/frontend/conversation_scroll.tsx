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
    const onScroll = (): void => {
      // scrollTop can be fractional, while the two heights are rounded CSS pixels.
      following = area.scrollHeight - area.clientHeight - area.scrollTop <= 2;
    };
    const follow = (): void => {
      if (following) area.scrollTop = area.scrollHeight;
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
