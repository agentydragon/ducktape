import { useEffect, useState } from "react";

import { getFrontendConfig } from "./app_config_client";
import type { FrontendConfig } from "../app_service_pb";

/** Load browser-safe app settings once per page. An unavailable endpoint hides optional features. */
export function useFrontendConfig(): FrontendConfig | null {
  const [config, setConfig] = useState<FrontendConfig | null>(null);
  useEffect(() => {
    let current = true;
    void getFrontendConfig()
      .then((data) => {
        if (current) setConfig(data);
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
