import { Anchor, Code } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import type { JSX } from "react";

import { getIntegrationPreviewNames } from "../client";

export function useIntegrationPreviewNames(
  kind: "gmail" | "tana",
  requestId?: string
): ReadonlyMap<string, string> | undefined {
  const query = useQuery({
    queryKey: ["action-integration-preview", kind, requestId],
    queryFn: ({ signal }) => getIntegrationPreviewNames(kind, requestId ?? "", signal),
    enabled: Boolean(requestId),
    retry: false,
    staleTime: 30_000,
  });
  return query.data;
}

export function LiveResourceLink({ id, name, href }: { id: string; name?: string; href: string }): JSX.Element {
  return (
    <>
      {name && (
        <>
          <Anchor href={href} target="_blank" rel="noreferrer" inherit>
            {name}
          </Anchor>{" "}
        </>
      )}
      <Code>{id}</Code>
    </>
  );
}
