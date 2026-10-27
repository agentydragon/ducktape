import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Anchor, Box, Burger, Center, Container, Grid, Group, MantineProvider, Menu, Paper, Tabs } from "@mantine/core";
import "@mantine/core/styles.css";
import "./styles.css";

import { ConfigurationPage } from "./configuration";
import { SpendingPage } from "./spending";
import { SustainabilityPage } from "./sustainability";
import { TransactionsPage } from "./transactions";
import { useSpendData } from "./use_spend_data";
import type { EstimatePeriodId, SpendTab, TransactionPeriodId } from "./types";

const spendTabs: { value: SpendTab; label: string }[] = [
  { value: "spending", label: "Spending" },
  { value: "transactions", label: "Transactions" },
  { value: "configuration", label: "Configuration" },
  { value: "sustainability", label: "Sustainability" },
];

function tabForHash(hash: string): SpendTab {
  const path = hash.split("?")[0];
  if (path === "#/transactions") return "transactions";
  if (path === "#/configuration") return "configuration";
  if (path === "#/sustainability") return "sustainability";
  return "spending";
}

function App() {
  const [activeTab, setActiveTab] = useState<SpendTab>(() => tabForHash(window.location.hash));
  const [navigationMenuOpened, setNavigationMenuOpened] = useState(false);
  const [selectedEstimatePeriodId, setSelectedEstimatePeriodId] = useState<EstimatePeriodId | null>(null);
  const [purchase, setPurchase] = useState<number | string>("");
  const [transactionPeriodId, setTransactionPeriodId] = useState<TransactionPeriodId>("rolling_30d");

  useEffect(() => {
    if (!window.location.hash) window.history.replaceState(null, "", "#/spending");
    const updateTab = () => setActiveTab(tabForHash(window.location.hash));
    window.addEventListener("hashchange", updateTab);
    return () => window.removeEventListener("hashchange", updateTab);
  }, []);

  const spendData = useSpendData({ activeTab, selectedEstimatePeriodId, transactionPeriodId });

  return (
    <MantineProvider defaultColorScheme="auto">
      <Tabs
        value={activeTab}
        onChange={(tab) => {
          if (tab) window.location.hash = `/${tab}`;
        }}
        keepMounted={false}
        variant="pills"
        color="teal"
      >
        <Paper component="header" radius={0} withBorder>
          <Container size="lg" py="xs">
            <Grid align="center" gap="xs">
              <Grid.Col span={{ base: 6, lg: 3 }} order={1}>
                <Anchor href="#/spending" size="lg" fw={700} c="var(--mantine-color-text)" underline="never">
                  Spend
                </Anchor>
              </Grid.Col>
              <Grid.Col span={{ base: 6, lg: 3 }} order={{ base: 2, lg: 3 }}>
                <Group justify="flex-end" gap="xs">
                  <Box>
                    <Menu position="bottom-end" opened={navigationMenuOpened} onChange={setNavigationMenuOpened}>
                      <Menu.Target>
                        <Burger
                          opened={navigationMenuOpened}
                          aria-label={navigationMenuOpened ? "Close page navigation" : "Open page navigation"}
                          size="sm"
                        />
                      </Menu.Target>
                      <Menu.Dropdown>
                        <Menu.Label>Pages</Menu.Label>
                        {spendTabs.map((tab) => (
                          <Menu.Item
                            key={tab.value}
                            data-active={activeTab === tab.value}
                            aria-current={activeTab === tab.value ? "page" : undefined}
                            onClick={() => {
                              window.location.hash = `/${tab.value}`;
                            }}
                          >
                            {tab.label}
                          </Menu.Item>
                        ))}
                        <Menu.Divider />
                        <form action="/auth/logout" method="post">
                          <Menu.Item type="submit">Sign out</Menu.Item>
                        </form>
                      </Menu.Dropdown>
                    </Menu>
                  </Box>
                </Group>
              </Grid.Col>
              <Grid.Col span={6} order={2} visibleFrom="lg">
                <Center>
                  <Tabs.List aria-label="Spend pages">
                    {spendTabs.map((tab) => (
                      <Tabs.Tab key={tab.value} value={tab.value}>
                        {tab.label}
                      </Tabs.Tab>
                    ))}
                  </Tabs.List>
                </Center>
              </Grid.Col>
            </Grid>
          </Container>
        </Paper>
        <Container component="main" size="lg" py="xl">
          <Tabs.Panel value="spending">
            <SpendingPage
              view={spendData.view}
              connectionState={spendData.connectionState}
              error={spendData.error}
              transactions={spendData.transactions}
              transactionsLoading={spendData.transactionsLoading}
              transactionsError={spendData.transactionsError}
              estimatePeriodId={spendData.estimatePeriodId}
              estimatePending={spendData.estimatePending}
              purchase={purchase}
              onPurchaseChange={setPurchase}
              onEstimatePeriodChange={(periodId) => {
                spendData.clearViewError();
                setSelectedEstimatePeriodId(periodId);
              }}
            />
          </Tabs.Panel>
          <Tabs.Panel value="transactions">
            <TransactionsPage
              transactions={
                spendData.transactions?.requested_period_id === transactionPeriodId ? spendData.transactions : null
              }
              loading={spendData.transactionsLoading}
              error={spendData.transactionsError}
              periodId={transactionPeriodId}
              onPeriodChange={setTransactionPeriodId}
            />
          </Tabs.Panel>
          <Tabs.Panel value="configuration">
            <ConfigurationPage
              configuration={spendData.configuration}
              loading={spendData.configurationLoading}
              error={spendData.configurationError}
            />
          </Tabs.Panel>
          <Tabs.Panel value="sustainability">
            <SustainabilityPage />
          </Tabs.Panel>
        </Container>
      </Tabs>
    </MantineProvider>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
