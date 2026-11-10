import { Stack, Text } from "@mantine/core";
import { type JSX } from "react";

import { ClampedBlock } from "../../../../util/frontend/clamped_block";
import { Disclosure, DisclosureBody } from "../../../../util/frontend/disclosure";

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
export interface DisclosureVisualProps {
  nested?: boolean;
  short?: boolean;
  open?: boolean;
  wrappedHeadings?: boolean;
  toolOutput?: boolean;
  outputOpen?: boolean;
  beforeOutput?: boolean;
  afterOutput?: boolean;
  followingSection?: boolean;
}

export function DisclosureVisual({
  nested = false,
  short = false,
  open = true,
  wrappedHeadings = false,
  toolOutput = false,
  outputOpen = true,
  beforeOutput = true,
  afterOutput = true,
  followingSection = false,
}: DisclosureVisualProps): JSX.Element {
  const aboveCopy = short ? ABOVE_COPY.slice(0, 1) : ABOVE_COPY;
  const outsideCopy = short ? OUTSIDE_COPY.slice(0, 1) : OUTSIDE_COPY;
  const outerTitle = wrappedHeadings ? WRAPPED_OUTER_TITLE : "Outer section";
  const innerTitle = wrappedHeadings ? WRAPPED_INNER_TITLE : "Nested section";

  return (
    <main
      id="shot"
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

          {nested ? (
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
                <DisclosureBody>
                  <Stack gap="md">
                    <Text component="p" data-demo-target="outer-paragraph" size="sm" m={0}>
                      This outer panel contains nested reasoning and tool-call disclosures.
                    </Text>
                  </Stack>
                </DisclosureBody>
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
                  {toolOutput ? (
                    <>
                      {beforeOutput && (
                        <DisclosureBody>
                          <Stack gap="md">
                            {TOOL_OUTPUT_BEFORE_COPY.map((paragraph, index) => (
                              <Text
                                key={index}
                                component="p"
                                data-demo-target={index === 3 ? "before-output" : undefined}
                                size="sm"
                                m={0}
                              >
                                {paragraph}
                              </Text>
                            ))}
                          </Stack>
                        </DisclosureBody>
                      )}
                      <Disclosure
                        className="demo-output"
                        summary={<Summary title="Tool output" />}
                        summaryAside={
                          <Text component="span" size="xs" c="dimmed">
                            20 results
                          </Text>
                        }
                        defaultOpen={outputOpen}
                      >
                        <ClampedBlock maxHeightRem={8} expansion={[true, () => undefined]} stickyCollapse={false}>
                          <Stack gap="md">
                            {TOOL_OUTPUT.map((paragraph, index) => (
                              <Text
                                key={index}
                                component="p"
                                data-demo-target={index === 6 ? "output-paragraph" : undefined}
                                size="sm"
                                m={0}
                              >
                                {paragraph}
                              </Text>
                            ))}
                          </Stack>
                        </ClampedBlock>
                      </Disclosure>
                      {afterOutput && (
                        <DisclosureBody>
                          <Stack gap="md">
                            {TOOL_OUTPUT_FOLLOWING_COPY.map((paragraph, index) => (
                              <Text
                                key={index}
                                component="p"
                                data-demo-target={index === 4 ? "following-output" : undefined}
                                size="sm"
                                m={0}
                              >
                                {paragraph}
                              </Text>
                            ))}
                          </Stack>
                        </DisclosureBody>
                      )}
                    </>
                  ) : (
                    <DisclosureBody>
                      <Stack gap="md">
                        {NESTED_COPY.map((paragraph, index) => (
                          <Text
                            key={index}
                            component="p"
                            data-demo-target={index === 6 ? "nested-paragraph" : undefined}
                            size="sm"
                            m={0}
                          >
                            {paragraph}
                          </Text>
                        ))}
                      </Stack>
                    </DisclosureBody>
                  )}
                </Disclosure>
                <DisclosureBody>
                  <Stack gap="md">
                    {NESTED_FOLLOWING_COPY.map((paragraph, index) => (
                      <Text
                        key={index}
                        component="p"
                        data-demo-target={index === 5 ? "following-nested" : undefined}
                        size="sm"
                        m={0}
                      >
                        {paragraph}
                      </Text>
                    ))}
                  </Stack>
                </DisclosureBody>
              </Disclosure>
              <Stack gap="md">
                {outsideCopy.map((paragraph, index) => (
                  <Text
                    key={index}
                    component="p"
                    data-demo-target={index === 5 ? "following-outer" : undefined}
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
              <Disclosure className="demo-main" summary={<Summary title="Section details" />} defaultOpen={open}>
                {short ? (
                  <DisclosureBody>
                    <Text size="sm">
                      This short panel fits in the viewport, so its Control stays in normal document flow.
                    </Text>
                  </DisclosureBody>
                ) : (
                  <DisclosureBody>
                    <Stack gap="md">
                      {LONG_COPY.map((paragraph, index) => (
                        <Text
                          key={index}
                          component="p"
                          data-demo-target={index === 6 ? "long-paragraph" : undefined}
                          size="sm"
                          m={0}
                        >
                          {paragraph}
                        </Text>
                      ))}
                    </Stack>
                  </DisclosureBody>
                )}
              </Disclosure>
              <Stack gap="md">
                {(followingSection ? FOLLOWING_COPY : outsideCopy).map((paragraph, index) => (
                  <Text
                    key={index}
                    component="p"
                    data-demo-target={followingSection && index === 5 ? "following-disclosure" : undefined}
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
