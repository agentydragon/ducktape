import { timestampDate } from "@bufbuild/protobuf/wkt";
import { Badge, Group, Stack, Text, Tooltip } from "@mantine/core";
import type { JSX } from "react";

import type { PolicyBinding } from "../../../policy/v1/policy_service_pb";
import { TimeAgo } from "../time_ago";
import type { KubernetesAccounts } from "./accounts";
import type { IdentityOrigin } from "./origin";

const ORIGIN_HELP: Record<IdentityOrigin, string> = {
  git: "Applied by Flux from git, which recreates it: change it in the repository.",
  unknown: "Not from git. Whether the app or a Session created it is not recorded on the identity yet.",
};

export function OriginBadge({ origin }: { origin: IdentityOrigin }): JSX.Element {
  return (
    <Tooltip label={ORIGIN_HELP[origin]} withArrow multiline w={280}>
      <Badge variant="light" color={origin === "git" ? "blue" : "gray"} tabIndex={0}>
        {origin === "git" ? "from git" : "origin unknown"}
      </Badge>
    </Tooltip>
  );
}

function expiry(binding: PolicyBinding): Date | null {
  const value = binding.spec?.expiresAt;
  return value ? timestampDate(value) : null;
}

/** Each binding naming the identity, with when the Policy Controller deletes it. */
export function BindingExpiries({ bindings }: { bindings: readonly PolicyBinding[] }): JSX.Element {
  if (bindings.length === 0)
    return (
      <Text size="sm" c="dimmed">
        none
      </Text>
    );
  return (
    <Stack gap={2}>
      {bindings.map((binding) => {
        const expiresAt = expiry(binding);
        return (
          <Text key={binding.identity?.uid} size="sm" style={{ overflowWrap: "anywhere" }}>
            {binding.identity?.name}{" "}
            <Text span size="xs" c={expiresAt && expiresAt.getTime() < Date.now() ? "red" : "dimmed"}>
              {expiresAt ? (
                <>
                  expires <TimeAgo value={expiresAt} />
                </>
              ) : (
                "no expiry"
              )}
            </Text>
          </Text>
        );
      })}
    </Stack>
  );
}

export function AccountsReached({ reached }: { reached: KubernetesAccounts }): JSX.Element {
  return (
    <Group gap={4}>
      {reached.accounts.map((account) => (
        <Badge key={account} variant="outline" color="gray" style={{ textTransform: "none" }}>
          {account}
        </Badge>
      ))}
      {reached.accounts.length === 0 && reached.complete && (
        <Text size="sm" c="dimmed">
          none
        </Text>
      )}
      {!reached.complete && (
        <Text size="xs" c="dimmed">
          {reached.accounts.length ? "and possibly more: " : ""}a named set or credential is not in view
        </Text>
      )}
    </Group>
  );
}
