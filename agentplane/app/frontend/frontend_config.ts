import { useEffect, useState } from "react";

import { api } from "./client";
import type { components } from "./api/schema";

export type FrontendConfig = components["schemas"]["FrontendConfig"];

/** Load browser-safe app settings once per page. An unavailable endpoint hides optional features. */
export function useFrontendConfig(): FrontendConfig | null {
  const [config, setConfig] = useState<FrontendConfig | null>(null);
  useEffect(() => {
    let current = true;
    void api
      .GET("/frontend-config")
      .then(({ data, error }) => {
        if (current) setConfig(error === undefined ? (data ?? null) : null);
      })
      .catch(() => {
        if (current) setConfig(null);
      });
    return () => {
      current = false;
    };
  }, []);
  return config;
}
