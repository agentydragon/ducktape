import type { OpenClawPluginApi } from "openclaw/plugin-sdk";

export type ScopedLogger = {
  info: (msg: string) => void;
  warn: (msg: string) => void;
  error: (msg: string) => void;
};

export function scopedLogger(api: OpenClawPluginApi, prefix: string): ScopedLogger {
  const { logger } = api;
  return {
    info: (msg: string) => logger.info(`${prefix}: ${msg}`),
    warn: (msg: string) => logger.warn(`${prefix}: ${msg}`),
    error: (msg: string) => logger.error(`${prefix}: ${msg}`),
  };
}
