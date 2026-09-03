import { Badge, Select, Stack, Text, Tooltip } from "@mantine/core";
import { useEffect, useState } from "react";

import { api, displayableError, type BindingView, type ProfileView } from "./client";

/** The profiles that exist, read once: the create form's options, and what a badge is checked against. */
export function useProfiles(onError: (message: string) => void): ProfileView[] | null {
  const [profiles, setProfiles] = useState<ProfileView[] | null>(null);
  useEffect(() => {
    void (async () => {
      const { data, error } = await api.GET("/egress/profiles");
      if (error) onError(displayableError(error));
      else setProfiles(data);
    })();
  }, [onError]);
  return profiles;
}

// A binding the proxy will not act on grants nothing, however its policies read.
const NOT_IN_EFFECT: Record<BindingView["approval"], string | null> = {
  approved: null,
  pending: "pending approval, not in effect",
  denied: "denied, not in effect",
};

/** What a profile grants, one line per binding: its policies, the hosts they open, and its approval. */
function grants(profile: ProfileView): string {
  return profile.bindings
    .map((binding) => {
      const policies = [
        ...binding.policies.map(
          (policy) => `${policy.name} (${policy.rules.flatMap((rule) => rule.hosts).join(", ")})`
        ),
        ...binding.missing_policies.map((name) => `${name}: no such policy`),
      ].join("; ");
      const state = NOT_IN_EFFECT[binding.approval];
      return `${binding.name}: ${policies || "no policies"}${state ? ` — ${state}` : ""}`;
    })
    .join("\n");
}

/**
 * A sandbox's profile as the bindings see it. A profile no binding selects is the failure that is
 * otherwise silent: the sandbox comes up reaching less than whoever launched it asked for.
 */
export function ProfileBadge({
  profile,
  profiles,
}: {
  profile: string | null;
  profiles: ProfileView[] | null;
}): JSX.Element {
  if (profile === null) {
    return (
      <Tooltip label="No profile: only the bindings that select every managed sandbox apply." withArrow>
        <Badge variant="outline" color="gray">
          no profile
        </Badge>
      </Tooltip>
    );
  }
  const known = profiles?.find((candidate) => candidate.name === profile);
  if (profiles !== null && !known) {
    return (
      <Tooltip label={`No EgressBinding selects ${profile}; the sandbox reaches nothing this profile names.`} withArrow>
        <Badge variant="light" color="red">
          {profile}: no binding
        </Badge>
      </Tooltip>
    );
  }
  return (
    <Tooltip label={known ? grants(known) : profile} multiline style={{ whiteSpace: "pre-line" }} withArrow>
      <Badge variant="light">{profile}</Badge>
    </Tooltip>
  );
}

/** Pick a profile from the ones bindings actually select on, and read what the pick grants. */
export function ProfileSelect({
  profiles,
  value,
  onChange,
}: {
  profiles: ProfileView[] | null;
  value: string | null;
  onChange: (profile: string | null) => void;
}): JSX.Element {
  const picked = profiles?.find((candidate) => candidate.name === value);
  return (
    <Stack gap={4} style={{ flex: "1 1 14rem" }}>
      <Select
        label="Profile"
        description="What the sandbox may reach, as the egress bindings select it"
        data={profiles?.map((profile) => profile.name) ?? []}
        value={value}
        onChange={onChange}
        placeholder={profiles && profiles.length === 0 ? "no binding selects on a profile" : "none"}
        disabled={profiles !== null && profiles.length === 0}
        clearable
        searchable
      />
      {picked && (
        <Text size="xs" c="dimmed" style={{ whiteSpace: "pre-line" }}>
          {grants(picked)}
        </Text>
      )}
    </Stack>
  );
}
