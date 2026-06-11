/**
 * Base class for MCP client connections with automatic reconnection.
 *
 * Provides connect/reconnect lifecycle with exponential backoff.
 * Subclasses override `createTransport` and `onConnected` to customize
 * transport options (e.g. auth headers) and post-connect behavior
 * (e.g. re-subscribing to resources).
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type { ScopedLogger } from "./util.js";

const INITIAL_RETRY_DELAY_MS = 5_000;
const MAX_RETRY_DELAY_MS = 60_000;

export class ReconnectingMcpClient {
  private client: Client | null = null;
  private connecting = false;
  private retryDelay = INITIAL_RETRY_DELAY_MS;

  constructor(
    protected readonly url: string,
    protected readonly clientName: string,
    protected readonly log: ScopedLogger
  ) {}

  /** Override to customize transport (e.g. add auth headers). */
  protected createTransport(): StreamableHTTPClientTransport {
    return new StreamableHTTPClientTransport(new URL(this.url));
  }

  /** Called after a successful connection. Override for post-connect setup. */
  protected async onConnected(_client: Client): Promise<void> {}

  /** Called when the connection is lost. Override for cleanup. */
  protected onDisconnected(): void {}

  async connect(): Promise<void> {
    if (this.connecting) return;
    this.connecting = true;
    try {
      const transport = this.createTransport();
      const client = new Client({ name: this.clientName, version: "0.1.0" });

      client.onclose = () => {
        this.log.warn("MCP connection closed");
        this.client = null;
        this.onDisconnected();
        this.scheduleReconnect();
      };

      client.onerror = (error: Error) => {
        this.log.warn(`MCP client error: ${error.message}`);
      };

      await client.connect(transport);
      this.client = client;
      this.retryDelay = INITIAL_RETRY_DELAY_MS;
      this.log.info(`connected to ${this.url}`);

      await this.onConnected(client);
    } catch (err) {
      this.log.error(`failed to connect: ${String(err)}`);
      this.scheduleReconnect();
    } finally {
      this.connecting = false;
    }
  }

  private scheduleReconnect(): void {
    const delay = this.retryDelay;
    this.retryDelay = Math.min(this.retryDelay * 2, MAX_RETRY_DELAY_MS);
    this.log.info(`reconnecting in ${delay}ms`);
    setTimeout(() => this.connect(), delay);
  }

  protected requireClient(): Client {
    if (!this.client) {
      throw new Error(`${this.clientName} is currently unavailable`);
    }
    return this.client;
  }

  get connected(): boolean {
    return this.client !== null;
  }

  async callTool(name: string, args: Record<string, unknown>): Promise<ReturnType<Client["callTool"]>> {
    return this.requireClient().callTool({ name, arguments: args });
  }

  async listTools(): Promise<ReturnType<Client["listTools"]>> {
    return this.requireClient().listTools();
  }
}
