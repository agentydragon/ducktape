import { Anchor, Code } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import type { JSX } from "react";
import { useCompactPane } from "./entry";

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
  return query.isSuccess ? query.data : undefined;
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
  const compact = useCompactPane();
  if (!name) return <Code>{id}</Code>;
  if (compact)
    return (
      <>
        {name}
        {showResolvedId && (
          <>
            {" "}
            <Code>{id}</Code>
          </>
        )}
      </>
    );
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
