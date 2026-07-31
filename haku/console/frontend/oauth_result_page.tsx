import { Button, Center, Loader, Paper, Stack, Text, ThemeIcon, Title } from "@mantine/core";
import { useEffect, useState } from "react";

import { consumeOAuthConnectionResult, type OAuthConnectionResult } from "./client";
import { rememberedEmbedPath } from "./routing";
import { SUCCESS_COLOR } from "./theme";

export function OAuthResultView({
  result,
  onClose = () => window.close(),
}: {
  result: OAuthConnectionResult;
  onClose?: () => void;
}) {
  const succeeded = result.status === "success";
  return (
    <Center component="main" className="haku-oauth-result-page">
      <Paper component="section" className="haku-oauth-result-card" withBorder radius="md" p="xl">
        <Stack align="center" gap="md">
          <Text className="haku-oauth-result-eyebrow">Haku Console</Text>
          <ThemeIcon
            aria-hidden="true"
            color={succeeded ? SUCCESS_COLOR : "red"}
            variant="light"
            radius="xl"
            size={58}
            className="haku-oauth-result-icon"
          >
            {succeeded ? "✓" : "!"}
          </ThemeIcon>
          <Title order={1} ta="center">
            {result.title}
          </Title>
          <Text c="dimmed" ta="center" className="haku-oauth-result-message">
            {result.message}
          </Text>
          <Stack gap="xs" w="100%" mt="xs">
            <Button color={succeeded ? undefined : "red"} onClick={onClose} fullWidth>
              Close this window
            </Button>
            <Button component="a" href={rememberedEmbedPath()} variant="subtle" color="gray" fullWidth>
              Return to Haku Console
            </Button>
          </Stack>
          <Text size="xs" c="dimmed" ta="center">
            You can safely close this window and return to Haku Console.
          </Text>
        </Stack>
      </Paper>
    </Center>
  );
}

export function OAuthResultPage({ resultId }: { resultId: string }) {
  const [result, setResult] = useState<OAuthConnectionResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void consumeOAuthConnectionResult(resultId).then(
      (next) => {
        if (alive) setResult(next);
      },
      (reason: unknown) => {
        if (alive) setError(reason instanceof Error ? reason.message : String(reason));
      }
    );
    return () => {
      alive = false;
    };
  }, [resultId]);

  if (error) {
    return (
      <OAuthResultView
        result={{
          status: "error",
          title: "Connection result unavailable",
          message: error,
        }}
      />
    );
  }
  if (result === null)
    return (
      <Center component="main" className="haku-oauth-result-page" aria-label="Loading connection result">
        <Loader />
      </Center>
    );
  return <OAuthResultView result={result} />;
}
