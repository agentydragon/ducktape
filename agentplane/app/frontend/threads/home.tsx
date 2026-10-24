/** The landing page: every Sandbox with its Threads, the same listing the sidebar navigates, wide
 * enough to say each Thread's harness, model and last activity. */
import { Button, Title } from "@mantine/core";
import { IconPlus } from "../icons";
import type { JSX } from "react";
import { Link } from "react-router";

import { ThreadList } from "./thread_list";
import { TopbarActions, TopbarTitle } from "../topbar";

export function Home(): JSX.Element {
  return (
    <section aria-label="Sandboxes and threads">
      <TopbarTitle>
        <Title order={1} size="h4">
          Threads
        </Title>
      </TopbarTitle>
      <TopbarActions>
        <Button component={Link} to="/sandboxes" size="xs" variant="light" leftSection={<IconPlus size={14} />}>
          New sandbox
        </Button>
      </TopbarActions>
      {/* The page is the route's whole content, so nothing stands in the way of a navigation. */}
      <ThreadList detailed onNavigate={() => undefined} />
    </section>
  );
}
