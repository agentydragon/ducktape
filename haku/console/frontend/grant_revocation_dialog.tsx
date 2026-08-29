import { Button, Group, Modal, Stack, Text, Textarea } from "@mantine/core";
import { useEffect, useState } from "react";

export type GrantRevocationTarget = {
  agentId: string;
  agentDisplayName: string;
  grantId: string;
};

export function GrantRevocationDialog({
  item,
  busy,
  onClose,
  onConfirm,
}: {
  item: GrantRevocationTarget | null;
  busy: boolean;
  onClose: () => void;
  onConfirm: (reason: string) => void;
}): JSX.Element {
  const [reason, setReason] = useState("");
  useEffect(() => setReason(""), [item?.grantId]);
  return (
    <Modal opened={item !== null} onClose={busy ? () => undefined : onClose} title="Revoke grant" centered returnFocus>
      <Stack gap="sm">
        <Text size="sm">
          End this active grant for <strong>{item?.agentDisplayName}</strong> immediately.
        </Text>
        {item && (
          <Text size="xs" c="dimmed" ff="monospace">
            {item.grantId}
          </Text>
        )}
        <Textarea
          label="Revocation reason"
          description="Required and retained with the grant's audit history."
          placeholder="Why is this grant being revoked?"
          value={reason}
          onChange={(event) => setReason(event.currentTarget.value)}
          minRows={3}
          maxLength={500}
          required
          disabled={busy}
          autoFocus
        />
        <Group justify="flex-end">
          <Button variant="subtle" color="gray" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button color="red" onClick={() => onConfirm(reason.trim())} disabled={!reason.trim()} loading={busy}>
            Revoke grant
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
