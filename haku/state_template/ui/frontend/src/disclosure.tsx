import { Collapse, Group, Text, UnstyledButton } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import type { ReactNode } from "react";

// The app's one disclosure affordance: a `▸/▾` chevron header that toggles a Collapse —
// the same visual language as the inbox task cards. Use this (not Spoiler / ad-hoc
// show-more links) for any expandable detail, so disclosure reads the same everywhere.
export function Disclosure({ header, children }: { header: ReactNode; children: ReactNode }) {
  const [opened, { toggle }] = useDisclosure(false);
  return (
    <>
      <UnstyledButton onClick={toggle} aria-expanded={opened} style={{ width: "100%" }}>
        <Group gap="xs" wrap="nowrap" align="baseline" style={{ width: "100%" }}>
          <Text c="dimmed" size="sm" aria-hidden style={{ flexShrink: 0 }}>
            {opened ? "▾" : "▸"}
          </Text>
          {header}
        </Group>
      </UnstyledButton>
      <Collapse expanded={opened}>{children}</Collapse>
    </>
  );
}
