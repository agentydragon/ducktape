import { useEffect, useState } from "react";

import type {
  EstimatePeriodId,
  SpendConfiguration,
  SpendTab,
  SpendView,
  TransactionPeriodId,
  TransactionsView,
} from "./types";
import { decodeSpendConfiguration, decodeSpendTransactions, decodeSpendView } from "./wire";

export function useSpendData({
  activeTab,
  selectedEstimatePeriodId,
  transactionPeriodId,
}: {
  activeTab: SpendTab;
  selectedEstimatePeriodId: EstimatePeriodId | null;
  transactionPeriodId: TransactionPeriodId;
}) {
  const [view, setView] = useState<SpendView | null>(null);
  const [connectionState, setConnectionState] = useState("Connecting");
  const [error, setError] = useState<string | null>(null);
  const [configuration, setConfiguration] = useState<SpendConfiguration | null>(null);
  const [configurationLoading, setConfigurationLoading] = useState(false);
  const [configurationError, setConfigurationError] = useState<string | null>(null);
  const [transactions, setTransactions] = useState<TransactionsView | null>(null);
  const [transactionsLoading, setTransactionsLoading] = useState(false);
  const [transactionsError, setTransactionsError] = useState<string | null>(null);
  const [viewRevision, setViewRevision] = useState(0);
  const estimatePeriodId =
    selectedEstimatePeriodId ??
    (view?.allowance?.forecast.basis_period.id === "rolling_30d" ? "rolling_30d" : "rolling_7d");

  useEffect(() => {
    let mounted = true;
    let authProbe: AbortController | undefined;
    let authProbeStarted = false;
    const estimateQuery = selectedEstimatePeriodId ? `?estimate_period_id=${selectedEstimatePeriodId}` : "";
    const events = new EventSource(`/api/v1/events${estimateQuery}`);
    events.addEventListener("view", (event) => {
      try {
        if (mounted) {
          const snapshot = decodeSpendView(event.data);
          authProbe?.abort();
          setView(snapshot);
          setError(null);
          setConnectionState("Live updates");
          setViewRevision((revision) => revision + 1);
        }
      } catch (cause) {
        if (mounted) {
          setConnectionState("Invalid update");
          setError(cause instanceof Error ? cause.message : "Could not read live update");
        }
      }
    });
    events.onopen = () => {
      authProbeStarted = false;
    };
    events.onerror = () => {
      if (!mounted) return;
      setConnectionState("Reconnecting");
      if (events.readyState !== EventSource.CLOSED || authProbeStarted) return;

      // EventSource hides HTTP status codes. Probe once after a terminal failure
      // so an expired browser session can still return to the login flow.
      authProbeStarted = true;
      const controller = new AbortController();
      authProbe = controller;
      void fetch(`/api/v1/view${estimateQuery}`, {
        cache: "no-store",
        credentials: "same-origin",
        signal: controller.signal,
      })
        .then((response) => {
          if (!mounted || controller.signal.aborted) return;
          if (response.status === 401) {
            window.location.assign("/auth/login");
          } else if (!response.ok) {
            throw new Error(`Connection check returned ${response.status}`);
          } else {
            setConnectionState("Unable to connect");
            setError("The live update stream closed unexpectedly");
          }
        })
        .catch((cause: unknown) => {
          if (mounted && !controller.signal.aborted) {
            setConnectionState("Unable to connect");
            setError(cause instanceof Error ? cause.message : "Connection check failed");
          }
        });
    };
    return () => {
      mounted = false;
      authProbe?.abort();
      events.close();
    };
  }, [selectedEstimatePeriodId]);

  useEffect(() => {
    if (activeTab !== "configuration" || configuration != null) return;
    let mounted = true;
    const load = async () => {
      setConfigurationLoading(true);
      setConfigurationError(null);
      try {
        const response = await fetch("/api/v1/configuration", {
          cache: "no-store",
          credentials: "same-origin",
        });
        if (response.status === 401) {
          window.location.assign("/auth/login");
          return;
        }
        if (!response.ok) throw new Error(`Configuration request returned ${response.status}`);
        const data = decodeSpendConfiguration(await response.text());
        if (mounted) setConfiguration(data);
      } catch (cause) {
        if (mounted) setConfigurationError(cause instanceof Error ? cause.message : "Configuration request failed");
      } finally {
        if (mounted) setConfigurationLoading(false);
      }
    };
    void load();
    return () => {
      mounted = false;
    };
  }, [activeTab, configuration]);

  useEffect(() => {
    if (activeTab !== "transactions" && activeTab !== "spending") return;
    const controller = new AbortController();
    const requestedPeriod = activeTab === "spending" ? estimatePeriodId : transactionPeriodId;
    const load = async () => {
      setTransactionsLoading(true);
      setTransactionsError(null);
      try {
        const response = await fetch(`/api/v1/transactions?period=${requestedPeriod}`, {
          cache: "no-store",
          credentials: "same-origin",
          signal: controller.signal,
        });
        if (response.status === 401) {
          window.location.assign("/auth/login");
          return;
        }
        if (!response.ok) throw new Error(`Transactions request returned ${response.status}`);
        const data = decodeSpendTransactions(await response.text());
        if (!controller.signal.aborted) setTransactions(data);
      } catch (cause) {
        if (!controller.signal.aborted) {
          setTransactionsError(cause instanceof Error ? cause.message : "Transactions request failed");
        }
      } finally {
        if (!controller.signal.aborted) setTransactionsLoading(false);
      }
    };
    void load();
    return () => controller.abort();
  }, [activeTab, estimatePeriodId, transactionPeriodId, viewRevision]);

  const estimatePending =
    selectedEstimatePeriodId != null &&
    view?.allowance != null &&
    view.allowance.forecast.basis_period.id !== selectedEstimatePeriodId;

  return {
    view,
    connectionState,
    error,
    clearViewError: () => setError(null),
    configuration,
    configurationLoading,
    configurationError,
    transactions,
    transactionsLoading,
    transactionsError,
    estimatePeriodId,
    estimatePending,
  };
}
