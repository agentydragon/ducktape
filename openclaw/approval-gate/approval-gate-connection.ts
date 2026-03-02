/**
 * Resilient MCP client connection to the approval gate server.
 *
 * Extends `ReconnectingMcpClient` with Bearer auth, session HWM tracking,
 * and resource subscription management. Tracks session HWMs and
 * re-subscribes on reconnect, performing catch-up reads to deliver results
 * that arrived during the outage.
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { ReconnectingMcpClient } from "./reconnecting-mcp-client.js";
import type { LogEntry } from "./types.js";
import type { ScopedLogger } from "./util.js";

const LOG_HWM_PREFIX = "resource://sessions/";
const LOG_HWM_SUFFIX = "/log_hwm";

function logHwmUri(sessionKey: string): string {
  return `${LOG_HWM_PREFIX}${sessionKey}${LOG_HWM_SUFFIX}`;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type NotificationHandler = (notification: any) => Promise<void>;

export class ApprovalGateConnection extends ReconnectingMcpClient {
  private notificationHandler: NotificationHandler | null = null;
  private catchUpHandler: ((sessionKey: string) => Promise<void>) | null = null;
  /** Last seen log entry_id per session — re-subscribed on reconnect. */
  private readonly sessionHwms = new Map<string, number>();

  constructor(
    url: string,
    private readonly apiKey: string,
    log: ScopedLogger
  ) {
    super(url, "openclaw-approval-gate", log);
  }

  protected override createTransport(): StreamableHTTPClientTransport {
    return new StreamableHTTPClientTransport(new URL(this.url), {
      requestInit: {
        headers: { Authorization: `Bearer ${this.apiKey}` },
      },
    });
  }

  protected override async onConnected(client: Client): Promise<void> {
    if (this.notificationHandler) {
      client.setNotificationHandler(
        { method: "notifications/resources/updated" } as Parameters<typeof client.setNotificationHandler>[0],
        this.notificationHandler
      );
    }

    await this.resubscribeTrackedSessions();
  }

  private async resubscribeTrackedSessions(): Promise<void> {
    if (this.sessionHwms.size === 0) return;
    this.log.info(`re-subscribing to ${this.sessionHwms.size} tracked session(s)`);
    for (const [sessionKey] of this.sessionHwms.entries()) {
      try {
        const hwmUri = logHwmUri(sessionKey);
        const client = this.requireClient();
        await client.subscribeResource({ uri: hwmUri });

        this.catchUpHandler?.(sessionKey).catch((err) =>
          this.log.warn(`catch-up failed for session ${sessionKey}: ${String(err)}`)
        );
      } catch (err) {
        this.log.warn(`failed to re-subscribe to session ${sessionKey}: ${String(err)}`);
      }
    }
  }

  setNotificationHandler(handler: NotificationHandler): void {
    this.notificationHandler = handler;
  }

  setCatchUpHandler(handler: (sessionKey: string) => Promise<void>): void {
    this.catchUpHandler = handler;
  }

  async trackSession(sessionKey: string): Promise<void> {
    if (this.sessionHwms.has(sessionKey)) return;
    this.sessionHwms.set(sessionKey, 0);
    const hwmUri = logHwmUri(sessionKey);
    try {
      await this.requireClient().subscribeResource({ uri: hwmUri });
      this.log.info(`tracking session ${sessionKey}`);
    } catch (err) {
      this.log.warn(`failed to subscribe to HWM for session ${sessionKey}: ${String(err)}`);
    }
  }

  updateSessionHwm(sessionKey: string, hwm: number): void {
    const current = this.sessionHwms.get(sessionKey) ?? 0;
    if (hwm > current) {
      this.sessionHwms.set(sessionKey, hwm);
    }
  }

  getSessionHwm(sessionKey: string): number {
    return this.sessionHwms.get(sessionKey) ?? 0;
  }

  async readResourceText(uri: string): Promise<string> {
    const client = this.requireClient();
    const resource = await client.readResource({ uri });
    const content = resource.contents[0];
    if (!content || !("text" in content)) {
      throw new Error(`resource ${uri} returned non-text content`);
    }
    return (content as { text: string }).text;
  }

  async readHwm(sessionKey: string): Promise<number> {
    const uri = logHwmUri(sessionKey);
    const text = await this.readResourceText(uri);
    return parseInt(text, 10);
  }

  async readLogEntry(sessionKey: string, entryId: number): Promise<LogEntry> {
    const uri = `resource://sessions/${sessionKey}/log/${entryId}`;
    const text = await this.readResourceText(uri);
    return JSON.parse(text) as LogEntry;
  }

  async listTools(): Promise<ReturnType<Client["listTools"]>> {
    return this.requireClient().listTools();
  }
}

/** Parse session_key from a log HWM URI like resource://sessions/{key}/log_hwm */
export function parseSessionKeyFromHwmUri(uri: string): string | null {
  if (!uri.startsWith(LOG_HWM_PREFIX) || !uri.endsWith(LOG_HWM_SUFFIX)) return null;
  return uri.slice(LOG_HWM_PREFIX.length, uri.length - LOG_HWM_SUFFIX.length);
}
