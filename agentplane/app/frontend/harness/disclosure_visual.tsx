import { Stack, Text } from "@mantine/core";
import { type JSX, useLayoutEffect, useRef } from "react";

import { Disclosure } from "../disclosure";
import { type DisclosureVisualStage } from "./scenario";

const LONG_COPY = Array.from(
  { length: 20 },
  (_, index) =>
    `This is sample content inside a reusable disclosure. It is long enough to scroll through while keeping the section heading available. Paragraph ${index + 1}.`
);

const FOLLOWING_COPY = Array.from(
  { length: 24 },
  (_, index) =>
    `This content follows the disclosure block. It shows where the sticky heading stops and normal page content continues. Paragraph ${index + 1}.`
);

const NESTED_COPY = Array.from(
  { length: 16 },
  (_, index) =>
    `This is sample content inside the nested disclosure. Scroll through it to see the nested heading take the sticky position. Paragraph ${index + 1}.`
);

function Summary({ title }: { title: string }): JSX.Element {
  return (
    <Stack gap={2}>
      <Text size="sm">{title}</Text>
      <Text size="xs" c="dimmed">
        Optional supporting text
      </Text>
    </Stack>
  );
}

/** An app-free phone specimen for the shared disclosure's Control and Panel behavior. */
export function DisclosureVisual({ stage }: { stage: DisclosureVisualStage }): JSX.Element {
  const scroll = useRef<HTMLDivElement>(null);
  const longParagraph = useRef<HTMLParagraphElement>(null);
  const followingParagraph = useRef<HTMLParagraphElement>(null);
  const nestedParagraph = useRef<HTMLParagraphElement>(null);
  const nestedFollowingParagraph = useRef<HTMLParagraphElement>(null);

  useLayoutEffect(() => {
    const viewport = scroll.current;
    if (!viewport) return;

    const frame = requestAnimationFrame(() => {
      // Mantine measures the expanded panel after mount. Position the scene after that first layout
      // so the screenshot and its assertions observe the settled component geometry.
      viewport.scrollTop = 0;
      const viewportTop = viewport.getBoundingClientRect().top;
      const scrollTo = (element: HTMLElement | null, screenTop: number) => {
        if (!element) throw new Error(`Disclosure scene ${stage} is missing its scroll target`);
        const elementTop = element.getBoundingClientRect().top - viewportTop + viewport.scrollTop;
        viewport.scrollTop = Math.max(0, elementTop - screenTop);
      };
      const heading = (selector: string): HTMLElement => {
        const element = viewport.querySelector<HTMLElement>(selector);
        if (!element) throw new Error(`Disclosure scene ${stage} is missing ${selector}`);
        return element;
      };
      const expectPinned = (element: HTMLElement, label: string) => {
        const top = element.getBoundingClientRect().top;
        if (Math.abs(top - viewportTop) > 4) {
          throw new Error(`Disclosure scene ${stage} did not pin ${label} (top=${top})`);
        }
      };
      const expectReleased = (element: HTMLElement, label: string) => {
        const bottom = element.getBoundingClientRect().bottom;
        if (bottom > viewportTop + 1) {
          throw new Error(`Disclosure scene ${stage} still shows ${label} past its block (bottom=${bottom})`);
        }
      };

      switch (stage) {
        case "long-scrolled":
          scrollTo(longParagraph.current, 64);
          expectPinned(heading(".demo-main .agentplane-disclosure-heading"), "the section Control");
          break;
        case "after-disclosure":
          scrollTo(followingParagraph.current, 96);
          expectReleased(heading(".demo-main .agentplane-disclosure-heading"), "the section Control");
          break;
        case "nested-child-scrolled":
          scrollTo(nestedParagraph.current, 64);
          expectPinned(heading(".demo-inner .agentplane-disclosure-heading"), "the nested Control");
          break;
        case "nested-after-child":
          scrollTo(nestedFollowingParagraph.current, 64);
          expectPinned(heading(".demo-outer .agentplane-disclosure-heading"), "the outer Control");
          expectReleased(heading(".demo-inner .agentplane-disclosure-heading"), "the nested Control");
          break;
        case "collapsed":
        case "short-expanded":
        case "long-top": {
          const mainHeading = heading(".demo-main .agentplane-disclosure-heading");
          if (stage === "short-expanded" && viewport.scrollHeight > viewport.clientHeight) {
            throw new Error("Disclosure scene short-expanded should fit without scrolling");
          }
          if (stage === "long-top" && viewport.scrollHeight <= viewport.clientHeight) {
            throw new Error("Disclosure scene long-top should have scrollable content");
          }
          const top = mainHeading.getBoundingClientRect().top;
          if (stage === "long-top" && (viewport.scrollTop !== 0 || top <= viewportTop || top > viewportTop + 160)) {
            throw new Error(`Disclosure scene long-top should show the Control at the top (top=${top})`);
          }
          break;
        }
      }

      if (
        (stage === "long-scrolled" ||
          stage === "after-disclosure" ||
          stage === "nested-child-scrolled" ||
          stage === "nested-after-child") &&
        viewport.scrollTop === 0
      ) {
        throw new Error(`Disclosure scene ${stage} did not scroll`);
      }

      viewport.dataset.scrollReady = "true";
    });

    return () => cancelAnimationFrame(frame);
  }, [stage]);

  const isNested = stage === "nested-child-scrolled" || stage === "nested-after-child";
  const isShort = stage === "short-expanded";
  const isOpen = stage !== "collapsed";

  return (
    <main
      id="shot"
      data-disclosure-visual-stage={stage}
      style={{
        position: "fixed",
        top: 0,
        left: "50%",
        transform: "translateX(-50%)",
        width: "100%",
        maxWidth: "26rem",
        height: "100dvh",
        marginInline: "auto",
        overflow: "hidden",
        background: "var(--mantine-color-body)",
      }}
    >
      <div
        ref={scroll}
        data-disclosure-demo-scroll
        style={{
          boxSizing: "border-box",
          position: "absolute",
          inset: 0,
          overflowY: "auto",
          padding: "0 var(--mantine-spacing-md) var(--mantine-spacing-md)",
        }}
      >
        <Stack gap="md">
          <Text size="sm" c="dimmed" pt="md">
            Content above the disclosure block.
          </Text>

          {isNested ? (
            <Disclosure className="demo-outer" summary={<Summary title="Outer section" />} defaultOpen>
              <Stack gap="md">
                <Text size="sm">This outer panel contains another disclosure.</Text>
                <Disclosure className="demo-inner" summary={<Summary title="Nested section" />} defaultOpen>
                  <Stack gap="md">
                    {NESTED_COPY.map((paragraph, index) => (
                      <Text key={index} component="p" ref={index === 6 ? nestedParagraph : undefined} size="sm" m={0}>
                        {paragraph}
                      </Text>
                    ))}
                  </Stack>
                </Disclosure>
                <Stack gap="md">
                  {FOLLOWING_COPY.map((paragraph, index) => (
                    <Text
                      key={index}
                      component="p"
                      ref={index === 5 ? nestedFollowingParagraph : undefined}
                      size="sm"
                      m={0}
                    >
                      {paragraph}
                    </Text>
                  ))}
                </Stack>
              </Stack>
            </Disclosure>
          ) : (
            <Disclosure className="demo-main" summary={<Summary title="Section details" />} defaultOpen={isOpen}>
              <Stack gap="md">
                {isShort ? (
                  <Text size="sm">
                    This short panel fits in the viewport, so its Control stays in normal document flow.
                  </Text>
                ) : (
                  LONG_COPY.map((paragraph, index) => (
                    <Text key={index} component="p" ref={index === 6 ? longParagraph : undefined} size="sm" m={0}>
                      {paragraph}
                    </Text>
                  ))
                )}
              </Stack>
            </Disclosure>
          )}

          {stage === "after-disclosure" && (
            <Stack gap="md">
              {FOLLOWING_COPY.map((paragraph, index) => (
                <Text key={index} component="p" ref={index === 2 ? followingParagraph : undefined} size="sm" m={0}>
                  {paragraph}
                </Text>
              ))}
            </Stack>
          )}
        </Stack>
      </div>
    </main>
  );
}
