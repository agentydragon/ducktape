import { Paper, Stack, Text } from "@mantine/core";
import { type JSX, useLayoutEffect, useRef, useState } from "react";

import { OutputBlock } from "../command_view";
import { Disclosure } from "../disclosure";

export type DisclosureVisualStage =
  "collapsed" | "reasoning-scrolled" | "output-clamped" | "output-scrolled" | "after-output";

const OUTPUT = Array.from({ length: 72 }, (_, index) => {
  const line = String(index + 1).padStart(2, "0");
  return `[${line}] Completed request ${index + 1}: response recorded`;
}).join("\n");

const REASONING = Array.from(
  { length: 20 },
  (_, index) =>
    `I will compare the request with the available evidence, keep the result tied to this step, and check whether another detail changes the conclusion. This is reasoning paragraph ${index + 1}.`
);

const FOLLOWING = Array.from(
  { length: 24 },
  (_, index) =>
    `The tool call is complete. This following content shows that the Output control stops at the end of its own block. Note ${index + 1}.`
);

/** An app-free phone scroll surface for reviewing the shared disclosure's sticky states. */
export function DisclosureVisual({ stage }: { stage: DisclosureVisualStage }): JSX.Element {
  const scroll = useRef<HTMLDivElement>(null);
  const reasoningParagraph = useRef<HTMLParagraphElement>(null);
  const outputBlock = useRef<HTMLDivElement>(null);
  const followingParagraph = useRef<HTMLParagraphElement>(null);
  const outputExpandedByDefault = stage === "output-scrolled" || stage === "after-output";
  const [outputExpanded, setOutputExpanded] = useState(outputExpandedByDefault);
  const accordionOpen = stage !== "collapsed";
  const showingReasoning = stage === "reasoning-scrolled";

  useLayoutEffect(() => {
    const viewport = scroll.current;
    if (!viewport) return;

    const needsScroll = stage === "reasoning-scrolled" || stage === "output-scrolled" || stage === "after-output";
    const needsOutputControl = stage === "output-scrolled" || stage === "after-output";
    const placeAndCheck = (): boolean => {
      const outputControl = viewport.querySelector<HTMLElement>(".agentplane-disclosure-collapse[data-label='Output']");
      if (needsOutputControl && !outputControl) return false;

      const viewportTop = viewport.getBoundingClientRect().top;
      const scrollToTopOf = (element: HTMLElement, offset: number) => {
        const elementTop = element.getBoundingClientRect().top - viewportTop + viewport.scrollTop;
        viewport.scrollTop = Math.max(0, elementTop + offset);
      };

      if (stage === "reasoning-scrolled" && reasoningParagraph.current) {
        scrollToTopOf(reasoningParagraph.current, -235);
      } else if (stage === "output-scrolled" && outputControl) {
        scrollToTopOf(outputControl, 0);
      } else if (stage === "after-output" && followingParagraph.current) {
        scrollToTopOf(followingParagraph.current, 100);
      }

      if (needsScroll && viewport.scrollTop === 0) {
        throw new Error(
          `Disclosure scene ${stage} did not scroll (scrollHeight=${viewport.scrollHeight}, clientHeight=${viewport.clientHeight})`
        );
      }

      const stickySelector =
        stage === "reasoning-scrolled" || stage === "after-output"
          ? ".agentplane-disclosure-heading"
          : stage === "output-scrolled"
            ? ".agentplane-disclosure-collapse[data-label='Output']"
            : null;
      const sticky = stickySelector ? viewport.querySelector<HTMLElement>(stickySelector) : null;
      if (sticky) {
        const top = sticky.getBoundingClientRect().top;
        if (Math.abs(top - viewport.getBoundingClientRect().top) > 24) {
          throw new Error(`Disclosure scene ${stage} did not pin its sticky header (top=${top})`);
        }
      }
      if (
        stage === "after-output" &&
        outputControl &&
        outputControl.getBoundingClientRect().bottom > viewport.getBoundingClientRect().top + 1
      ) {
        throw new Error("Disclosure scene after-output still shows the Output collapse control past its block");
      }

      viewport.dataset.scrollReady = "true";
      return true;
    };

    if (!needsOutputControl) {
      placeAndCheck();
      return;
    }
    if (placeAndCheck()) return;
    const observer = new MutationObserver(() => {
      if (placeAndCheck()) observer.disconnect();
    });
    observer.observe(outputBlock.current ?? viewport, { childList: true, subtree: true });
    if (placeAndCheck()) observer.disconnect();
    return () => observer.disconnect();
  }, [stage]);

  return (
    <main
      id="shot"
      data-disclosure-visual-stage={stage}
      style={{
        position: "relative",
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
          padding: "0 var(--mantine-spacing-sm) var(--mantine-spacing-sm)",
        }}
      >
        <Paper p="sm" withBorder mt="sm" mb="sm">
          <Text size="xs" c="dimmed" mb={4}>
            Earlier message
          </Text>
          <Text size="sm">Can you check this step?</Text>
        </Paper>

        <Paper p="sm" withBorder>
          {showingReasoning ? (
            <Disclosure summary="Reasoning" defaultOpen>
              <Stack gap="sm">
                {REASONING.map((paragraph, index) => (
                  <Text key={index} ref={index === 6 ? reasoningParagraph : undefined} size="sm">
                    {paragraph}
                  </Text>
                ))}
              </Stack>
            </Disclosure>
          ) : (
            <Disclosure
              summary={
                <Stack gap={2}>
                  <Text size="sm">Bash</Text>
                  <Text size="xs" c="dimmed">
                    npm test -- --runInBand
                  </Text>
                </Stack>
              }
              defaultOpen={accordionOpen}
            >
              <Stack gap="sm">
                <Text size="sm" c="dimmed">
                  Command output
                </Text>
                <div ref={outputBlock}>
                  <OutputBlock name="Output" text={OUTPUT} expansion={[outputExpanded, setOutputExpanded]} />
                </div>
                <Stack gap="xs">
                  {FOLLOWING.map((paragraph, index) => (
                    <Text key={index} ref={index === 0 ? followingParagraph : undefined} size="sm">
                      {paragraph}
                    </Text>
                  ))}
                </Stack>
              </Stack>
            </Disclosure>
          )}
        </Paper>
      </div>
    </main>
  );
}
