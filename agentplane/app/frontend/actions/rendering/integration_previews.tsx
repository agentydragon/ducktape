import { Code } from "@mantine/core";
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

export function LiveResourceName({ id, name }: { id: string; name?: string }): JSX.Element {
  return (
    <>
      {name && <>{name} </>}
      <Code>{id}</Code>
    </>
  );
}
