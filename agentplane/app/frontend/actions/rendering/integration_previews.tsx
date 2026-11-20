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

export function LiveResourceLink({
  id,
  name,
  href,
  showResolvedId = false,
}: {
  id: string;
  name?: string;
  href: string;
  showResolvedId?: boolean;
}): JSX.Element {
  if (!name) return <Code>{id}</Code>;
  return (
    <>
      <Anchor href={href} target="_blank" rel="noreferrer" inherit>
        {name}
      </Anchor>
      {showResolvedId && (
        <>
          {" "}
          <Code>{id}</Code>
        </>
      )}
    </>
  );
}
