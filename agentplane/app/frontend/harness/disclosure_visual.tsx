import { Stack, Text } from "@mantine/core";
import { type JSX, useLayoutEffect, useRef } from "react";

import { ClampedBlock } from "../clamped_block";
import { Disclosure } from "../disclosure";
import { type DisclosureVisualStage } from "./scenario";

const ABOVE_COPY = Array.from(
  { length: 4 },
  (_, index) =>
    `This content comes before the disclosure block. It provides enough page context to see the header approach the top. Paragraph ${index + 1}.`
);

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
    `This is sample content inside the nested disclosure. Scroll through it to see the nested heading stack below its parent. Paragraph ${index + 1}.`
);

const NESTED_FOLLOWING_COPY = Array.from(
  { length: 16 },
  (_, index) =>
    `This content follows the nested disclosure while remaining inside its parent section. Paragraph ${index + 1}.`
);

const OUTSIDE_COPY = Array.from(
  { length: 32 },
  (_, index) => `This content follows the disclosure block in normal page flow. Paragraph ${index + 1}.`
);

const TOOL_OUTPUT = Array.from(
  { length: 20 },
  (_, index) => `Result row ${index + 1}: matching files and relevant source locations returned by the tool call.`
);

const TOOL_OUTPUT_BEFORE_COPY = Array.from(
  { length: 4 },
  (_, index) =>
    `The tool call is preparing to show its output. This copy lets the reader reach the nested heading. Paragraph ${index + 1}.`
);

const TOOL_OUTPUT_FOLLOWING_COPY = Array.from(
  { length: 10 },
  (_, index) =>
    `The tool call continues after its output disclosure. This text shows where the output heading releases. Paragraph ${index + 1}.`
);

const WRAPPED_OUTER_TITLE = "Assistant turn: planning, reasoning, and coordinating several tool calls";
const WRAPPED_INNER_TITLE = "Tool call: searching the workspace and reviewing the matching source files";

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

/** An app-free phone specimen for the shared Disclosure's Control, Panel, and nested stack behavior. */
export function DisclosureVisual({ stage }: { stage: DisclosureVisualStage }): JSX.Element {
  const scroll = useRef<HTMLDivElement>(null);
  const longParagraph = useRef<HTMLParagraphElement>(null);
  const followingParagraph = useRef<HTMLParagraphElement>(null);
  const outerParagraph = useRef<HTMLParagraphElement>(null);
  const nestedParagraph = useRef<HTMLParagraphElement>(null);
  const nestedFollowingParagraph = useRef<HTMLParagraphElement>(null);
  const outerFollowingParagraph = useRef<HTMLParagraphElement>(null);
  const outputBeforeParagraph = useRef<HTMLParagraphElement>(null);
  const outputParagraph = useRef<HTMLParagraphElement>(null);
  const outputFollowingParagraph = useRef<HTMLParagraphElement>(null);

  // TODO: Move stage scrolling and geometry checks to Python Playwright.
  useLayoutEffect(() => {
    const viewport = scroll.current;
    if (!viewport) return;

    const frame = requestAnimationFrame(() => {
      // Position each fixture after Mantine has laid out its expanded panels and the component has
      // measured the sticky row heights. The PNG and its geometry checks then see the settled UI.
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
      const rowHeight = (element: HTMLElement) => element.getBoundingClientRect().height;
      const expectAt = (element: HTMLElement, offset: number, label: string) => {
        const actual = element.getBoundingClientRect().top - viewportTop;
        if (Math.abs(actual - offset) > 4) {
          throw new Error(`Disclosure scene ${stage} placed ${label} at ${actual}px, expected ${offset}px`);
        }
      };
      const expectReleased = (element: HTMLElement, label: string) => {
        const bottom = element.getBoundingClientRect().bottom;
        if (bottom > viewportTop + 1) {
          throw new Error(`Disclosure scene ${stage} still shows ${label} past its block (bottom=${bottom})`);
        }
      };
      const expectCoveredByParent = (child: HTMLElement, parent: HTMLElement, label: string) => {
        const childRect = child.getBoundingClientRect();
        const parentRect = parent.getBoundingClientRect();
        const childZIndex = Number.parseInt(getComputedStyle(child).zIndex, 10);
        const parentZIndex = Number.parseInt(getComputedStyle(parent).zIndex, 10);
        if (childRect.bottom > parentRect.bottom + 1 || parentZIndex <= childZIndex) {
          throw new Error(`Disclosure scene ${stage} did not release ${label} behind its parent`);
        }
      };
      const stackContentTop = (...rows: HTMLElement[]) => rows.reduce((sum, row) => sum + rowHeight(row), 0) + 16;

      switch (stage) {
        case "long-scrolled":
          scrollTo(longParagraph.current, 64);
          expectAt(heading(".demo-main .agentplane-disclosure-heading"), 0, "the section Control");
          break;
        case "after-disclosure":
          scrollTo(followingParagraph.current, 64);
          expectReleased(heading(".demo-main .agentplane-disclosure-heading"), "the section Control");
          break;
        case "nested-parent-only-scrolled": {
          const outer = heading(".demo-outer .agentplane-disclosure-heading");
          const inner = heading(".demo-inner .agentplane-disclosure-heading");
          scrollTo(outerParagraph.current, rowHeight(outer) + 8);
          expectAt(outer, 0, "the outer Control");
          const innerTop = inner.getBoundingClientRect().top;
          if (
            innerTop <= outer.getBoundingClientRect().bottom + 4 ||
            innerTop >= viewport.getBoundingClientRect().bottom
          ) {
            throw new Error(`Disclosure scene ${stage} did not leave the nested Control below the outer slot`);
          }
          break;
        }
        case "nested-child-scrolled":
        case "nested-wrapped-headings": {
          const outer = heading(".demo-outer .agentplane-disclosure-heading");
          const inner = heading(".demo-inner .agentplane-disclosure-heading");
          scrollTo(nestedParagraph.current, stackContentTop(outer, inner));
          expectAt(outer, 0, "the outer Control");
          expectAt(inner, rowHeight(outer), "the nested Control below the outer Control");
          if (stage === "nested-wrapped-headings" && (rowHeight(outer) < 56 || rowHeight(inner) < 56)) {
            throw new Error(`Disclosure scene ${stage} did not wrap both mobile headings`);
          }
          break;
        }
        case "nested-after-child": {
          const outer = heading(".demo-outer .agentplane-disclosure-heading");
          const inner = heading(".demo-inner .agentplane-disclosure-heading");
          scrollTo(nestedFollowingParagraph.current, rowHeight(outer) + 16);
          expectAt(outer, 0, "the outer Control");
          expectCoveredByParent(inner, outer, "the nested Control");
          break;
        }
        case "nested-after-outer": {
          const outer = heading(".demo-outer .agentplane-disclosure-heading");
          const inner = heading(".demo-inner .agentplane-disclosure-heading");
          scrollTo(outerFollowingParagraph.current, 64);
          expectReleased(inner, "the nested Control");
          expectReleased(outer, "the outer Control");
          break;
        }
        case "nested-expanded-output": {
          const outer = heading(".demo-outer .agentplane-disclosure-heading");
          const inner = heading(".demo-inner .agentplane-disclosure-heading");
          const output = heading(".demo-output .agentplane-disclosure-heading");
          scrollTo(outputParagraph.current, stackContentTop(outer, inner, output));
          expectAt(outer, 0, "the outer Control");
          expectAt(inner, rowHeight(outer), "the nested Control below the outer Control");
          expectAt(output, rowHeight(outer) + rowHeight(inner), "the output Disclosure below both parent Controls");
          break;
        }
        case "nested-before-output": {
          const outer = heading(".demo-outer .agentplane-disclosure-heading");
          const inner = heading(".demo-inner .agentplane-disclosure-heading");
          const output = heading(".demo-output .agentplane-disclosure-heading");
          scrollTo(outputBeforeParagraph.current, stackContentTop(outer, inner));
          expectAt(outer, 0, "the outer Control");
          expectAt(inner, rowHeight(outer), "the nested Control below the outer Control");
          const outputTop = output.getBoundingClientRect().top - viewportTop;
          if (outputTop <= rowHeight(outer) + rowHeight(inner) + 4 || outputTop >= viewport.clientHeight) {
            throw new Error(`Disclosure scene ${stage} did not show the output heading entering below its parents`);
          }
          break;
        }
        case "nested-output-collapsed": {
          const outer = heading(".demo-outer .agentplane-disclosure-heading");
          const inner = heading(".demo-inner .agentplane-disclosure-heading");
          const output = heading(".demo-output .agentplane-disclosure-heading");
          const parentStackHeight = rowHeight(outer) + rowHeight(inner);
          scrollTo(output, parentStackHeight);
          expectAt(outer, 0, "the outer Control");
          expectAt(inner, rowHeight(outer), "the nested Control below the outer Control");
          expectAt(output, parentStackHeight, "the collapsed output Control below both parents");
          if (output.dataset.expanded !== "false") {
            throw new Error(`Disclosure scene ${stage} should show the collapsed output Control`);
          }
          break;
        }
        case "nested-after-output": {
          const outer = heading(".demo-outer .agentplane-disclosure-heading");
          const inner = heading(".demo-inner .agentplane-disclosure-heading");
          const output = heading(".demo-output .agentplane-disclosure-heading");
          scrollTo(outputFollowingParagraph.current, rowHeight(outer) + rowHeight(inner) + 16);
          expectAt(outer, 0, "the outer Control");
          expectAt(inner, rowHeight(outer), "the nested Control below the outer Control");
          expectReleased(output, "the output Disclosure");
          break;
        }
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
          const top = mainHeading.getBoundingClientRect().top - viewportTop;
          if (stage === "long-top" && (viewport.scrollTop !== 0 || top <= 0 || top > viewport.clientHeight * 0.6)) {
            throw new Error(`Disclosure scene long-top should show the Control before it sticks (top=${top})`);
          }
          break;
        }
      }

      const requiresScroll = !["collapsed", "short-expanded", "long-top"].includes(stage);
      if (requiresScroll && viewport.scrollTop === 0) {
        throw new Error(`Disclosure scene ${stage} did not scroll`);
      }

      viewport.dataset.scrollReady = "true";
    });

    return () => cancelAnimationFrame(frame);
  }, [stage]);

  const isNested = stage.startsWith("nested-");
  const isShort = stage === "short-expanded";
  const isOpen = stage !== "collapsed";
  const aboveCopy = isShort ? ABOVE_COPY.slice(0, 1) : ABOVE_COPY;
  const outsideCopy = isShort ? OUTSIDE_COPY.slice(0, 1) : OUTSIDE_COPY;
  const outerTitle = stage === "nested-wrapped-headings" ? WRAPPED_OUTER_TITLE : "Outer section";
  const innerTitle = stage === "nested-wrapped-headings" ? WRAPPED_INNER_TITLE : "Nested section";
  const hasToolOutput = [
    "nested-before-output",
    "nested-expanded-output",
    "nested-after-output",
    "nested-output-collapsed",
  ].includes(stage);

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
          {aboveCopy.map((paragraph, index) => (
            <Text key={index} component="p" size="sm" m={0} pt={index === 0 ? "md" : undefined}>
              {paragraph}
            </Text>
          ))}

          {isNested ? (
            <>
              <Disclosure
                className="demo-outer"
                summary={<Summary title={outerTitle} />}
                summaryAside={
                  <Text component="span" size="xs" c="dimmed">
                    3 steps
                  </Text>
                }
                defaultOpen
              >
                <Stack gap="md">
                  <Text component="p" ref={outerParagraph} size="sm" m={0}>
                    This outer panel contains nested reasoning and tool-call disclosures.
                  </Text>
                  <Disclosure
                    className="demo-inner"
                    summary={<Summary title={innerTitle} />}
                    summaryAside={
                      <Text component="span" size="xs" c="dimmed">
                        Complete
                      </Text>
                    }
                    defaultOpen
                  >
                    {hasToolOutput ? (
                      <>
                        {stage !== "nested-output-collapsed" && (
                          <Stack gap="md">
                            {TOOL_OUTPUT_BEFORE_COPY.map((paragraph, index) => (
                              <Text
                                key={index}
                                component="p"
                                ref={index === 3 ? outputBeforeParagraph : undefined}
                                size="sm"
                                m={0}
                              >
                                {paragraph}
                              </Text>
                            ))}
                          </Stack>
                        )}
                        <Disclosure
                          className="demo-output"
                          summary={<Summary title="Tool output" />}
                          summaryAside={
                            <Text component="span" size="xs" c="dimmed">
                              20 results
                            </Text>
                          }
                          defaultOpen={stage !== "nested-output-collapsed"}
                        >
                          <ClampedBlock maxHeightRem={8} expansion={[true, () => undefined]} stickyCollapse={false}>
                            <Stack gap="md">
                              {TOOL_OUTPUT.map((paragraph, index) => (
                                <Text
                                  key={index}
                                  component="p"
                                  ref={index === 6 ? outputParagraph : undefined}
                                  size="sm"
                                  m={0}
                                >
                                  {paragraph}
                                </Text>
                              ))}
                            </Stack>
                          </ClampedBlock>
                        </Disclosure>
                        {stage !== "nested-expanded-output" && (
                          <Stack gap="md">
                            {TOOL_OUTPUT_FOLLOWING_COPY.map((paragraph, index) => (
                              <Text
                                key={index}
                                component="p"
                                ref={index === 4 ? outputFollowingParagraph : undefined}
                                size="sm"
                                m={0}
                              >
                                {paragraph}
                              </Text>
                            ))}
                          </Stack>
                        )}
                      </>
                    ) : (
                      <Stack gap="md">
                        {NESTED_COPY.map((paragraph, index) => (
                          <Text
                            key={index}
                            component="p"
                            ref={index === 6 ? nestedParagraph : undefined}
                            size="sm"
                            m={0}
                          >
                            {paragraph}
                          </Text>
                        ))}
                      </Stack>
                    )}
                  </Disclosure>
                  <Stack gap="md">
                    {NESTED_FOLLOWING_COPY.map((paragraph, index) => (
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
              <Stack gap="md">
                {outsideCopy.map((paragraph, index) => (
                  <Text
                    key={index}
                    component="p"
                    ref={index === 5 ? outerFollowingParagraph : undefined}
                    size="sm"
                    m={0}
                  >
                    {paragraph}
                  </Text>
                ))}
              </Stack>
            </>
          ) : (
            <>
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
              <Stack gap="md">
                {(stage === "after-disclosure" ? FOLLOWING_COPY : outsideCopy).map((paragraph, index) => (
                  <Text
                    key={index}
                    component="p"
                    ref={stage === "after-disclosure" && index === 5 ? followingParagraph : undefined}
                    size="sm"
                    m={0}
                  >
                    {paragraph}
                  </Text>
                ))}
              </Stack>
            </>
          )}
        </Stack>
      </div>
    </main>
  );
}
