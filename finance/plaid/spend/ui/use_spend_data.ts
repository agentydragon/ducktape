import { useEffect, useState } from "react";

import type { SpendConfigurationView, SpendTransactionsView, SpendView } from "./schema";
import type { EstimatePeriodId, SpendTab, TransactionPeriodId } from "./types";

type ConnectionState = "Connecting" | "Live updates" | "Reconnecting" | "Unable to connect";

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
  const [connectionState, setConnectionState] = useState<ConnectionState>("Connecting");
  const [error, setError] = useState<string | null>(null);
  const [configuration, setConfiguration] = useState<SpendConfigurationView | null>(null);
  const [configurationError, setConfigurationError] = useState<string | null>(null);
  const [transactions, setTransactions] = useState<SpendTransactionsView | null>(null);
  const [transactionsLoading, setTransactionsLoading] = useState(false);
  const [transactionsError, setTransactionsError] = useState<string | null>(null);
  const [transactionsErrorPeriodId, setTransactionsErrorPeriodId] = useState<TransactionPeriodId | null>(null);
  const [viewRevision, setViewRevision] = useState(0);
  const estimatePeriodId =
    selectedEstimatePeriodId ??
    (view?.allowance?.forecast.basis_period.id === "rolling_30d" ? "rolling_30d" : "rolling_7d");

  useEffect(() => {
    let mounted = true;
    let authProbe: AbortController | undefined;
    let authProbeStarted = false;
    setConnectionState("Connecting");
    setError(null);
    const estimateQuery = selectedEstimatePeriodId ? `?estimate_period_id=${selectedEstimatePeriodId}` : "";
    const events = new EventSource(`/api/v1/events${estimateQuery}`);
    events.addEventListener("view", (event) => {
      try {
        if (mounted) {
          const snapshot: SpendView = JSON.parse(event.data);
          authProbe?.abort();
          setView(snapshot);
          setError(null);
          setConnectionState("Live updates");
          setViewRevision((revision) => revision + 1);
        }
      } catch (cause) {
        if (mounted) setError(cause instanceof Error ? cause.message : "Could not read live update");
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
      // to detect expired sessions and recover a current view if HTTP still works.
      authProbeStarted = true;
      const controller = new AbortController();
      authProbe = controller;
      void fetch(`/api/v1/view${estimateQuery}`, {
        cache: "no-store",
        credentials: "same-origin",
        signal: controller.signal,
      })
        .then(async (response) => {
          if (!mounted || controller.signal.aborted) return;
          if (response.status === 401) {
            window.location.assign("/auth/login");
          } else if (!response.ok) {
            throw new Error(`Connection check returned ${response.status}`);
          } else {
            const snapshot: SpendView = await response.json();
            if (!mounted || controller.signal.aborted) return;
            setView(snapshot);
            setError(null);
            setViewRevision((revision) => revision + 1);
            setConnectionState("Unable to connect");
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
        const data: SpendConfigurationView = await response.json();
        if (mounted) setConfiguration(data);
      } catch (cause) {
        if (mounted) setConfigurationError(cause instanceof Error ? cause.message : "Configuration request failed");
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
      setTransactionsErrorPeriodId(null);
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
        const data: SpendTransactionsView = await response.json();
        if (!controller.signal.aborted) setTransactions(data);
      } catch (cause) {
        if (!controller.signal.aborted) {
          setTransactionsError(cause instanceof Error ? cause.message : "Transactions request failed");
          setTransactionsErrorPeriodId(requestedPeriod);
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
    configurationError,
    transactions,
    transactionsLoading,
    transactionsError,
    transactionsErrorPeriodId,
    estimatePeriodId,
    estimatePending,
  };
}
