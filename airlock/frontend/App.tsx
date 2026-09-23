import { Alert, Anchor, Badge, Box, Button, Container, Group, Stack, Text, Title } from "@mantine/core";
import { type JSX, useEffect, useState } from "react";

import { getApiClient } from "./api";
import { isAuthenticationFailurePage, isAuthenticationRedirectStarted } from "./auth";
import type { DeploymentInfo } from "./types";
import { OAuthProviders } from "./OAuthProviders";

export default function App(): JSX.Element {
  const [deploymentInfo, setDeploymentInfo] = useState<DeploymentInfo | null>(null);
  const signInFailed = isAuthenticationFailurePage();

  useEffect(() => {
    void getApiClient()
      .getDeploymentInfo()
      .then(setDeploymentInfo)
      .catch((error: unknown) => {
        if (!isAuthenticationRedirectStarted() && !isAuthenticationFailurePage()) {
          console.error("Failed to load Airlock deployment info", error);
        }
      });
  }, []);

  return (
    <Stack gap={0} mih="100vh">
      <Box component="header" style={{ backgroundColor: "#073642" }}>
        <Container size="lg" py="md">
          <Group justify="space-between">
            <Stack gap={0}>
              <Title order={1} size="h3" c="white">
                Airlock
              </Title>
              <Text size="sm" c="gray.3">
                OAuth credential broker
              </Text>
            </Stack>
            <Box component="form" action="/auth/logout" method="post">
              <Button variant="subtle" color="gray" type="submit" c="white">
                Sign out
              </Button>
            </Box>
          </Group>
        </Container>
      </Box>

      <Container component="main" size="lg" py="xl" style={{ flex: 1 }}>
        {signInFailed && (
          <Alert color="red" title="Sign-in failed" mb="lg" role="alert">
            Airlock could not verify your sign-in. <Anchor href="/auth/login">Try again</Anchor>.
          </Alert>
        )}
        <OAuthProviders />
      </Container>

      {deploymentInfo && (deploymentInfo.image_tag || deploymentInfo.source_commit) && (
        <Container component="footer" size="lg" py="md">
          <Group justify="center" gap="xs" wrap="wrap">
            <Text size="xs" c="dimmed">
              Deployed commit
            </Text>
            {deploymentInfo.source_commit_url ? (
              <Anchor
                href={deploymentInfo.source_commit_url}
                target="_blank"
                rel="noreferrer"
                size="xs"
                ff="monospace"
                title={deploymentInfo.source_commit ?? undefined}
              >
                {deploymentInfo.source_commit?.slice(0, 7) ?? "unknown"}
              </Anchor>
            ) : (
              <Text size="xs" ff="monospace" c="dimmed">
                {deploymentInfo.source_commit?.slice(0, 7) ?? "unknown"}
              </Text>
            )}
            {deploymentInfo.image_tag && (
              <Badge variant="light" color="gray" title={deploymentInfo.image_tag}>
                {deploymentInfo.image_tag}
              </Badge>
            )}
          </Group>
        </Container>
      )}
    </Stack>
  );
}
