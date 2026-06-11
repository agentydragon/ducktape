/**
 * MCP Client wrapper for agent web UI.
 *
 * Provides simplified interface for connecting to the MCP compositor and calling tools/resources.
 * Supports bearer token authentication from URL query param (?token=...).
 */
import { Client } from "@modelcontextprotocol/sdk/client";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import {
  ResourceUpdatedNotificationSchema,
  ResourceListChangedNotificationSchema,
  type ResourceUpdatedNotification,
} from "@modelcontextprotocol/sdk/types.js";

export interface McpClientOptions {
  /** Agent ID for scoping tool calls (used as prefix in compositor hierarchy) */
  agentId?: string;
  /** Bearer token for authentication (defaults to URL query param) */
  token?: string;
}

/** Content item from MCP results */
interface ContentItem {
  type?: string;
  text?: string;
  uri?: string;
  mimeType?: string;
}

/**
 * Parse JSON from content text, or return as-is if not JSON.
 * Handles mimeType hints and URI-based detection.
 */
function parseContent<T>(text: string, mimeType?: string, uri?: string): T {
  const isJson =
    mimeType === "application/json" ||
    uri?.startsWith("approvals://") ||
    uri?.startsWith("agents://") ||
    uri?.startsWith("snapshot://");
  if (isJson) {
    try {
      return JSON.parse(text) as T;
    } catch {
      return text as T;
    }
  }
  return text as T;
}

/**
 * Extract first content item from MCP result and parse it.
 */
function extractContent<T>(contents: ContentItem[] | undefined, uri?: string): T | null {
  if (!contents || contents.length === 0) return null;
  const first = contents[0];
  if (first.text !== undefined) {
    return parseContent<T>(first.text, first.mimeType, first.uri ?? uri);
  }
  return null;
}

/**
 * Get authentication token from URL query params or localStorage.
 */
function getAuthToken(): string | null {
  // Check URL query param first
  const params = new URLSearchParams(window.location.search);
  const urlToken = params.get("token");
  if (urlToken) {
    // Store in localStorage for persistence across page reloads
    localStorage.setItem("adgn_auth_token", urlToken);
    return urlToken;
  }
  // Fall back to localStorage
  return localStorage.getItem("adgn_auth_token");
}

export class AgentMcpClient {
  private client: Client;
  private transport: StreamableHTTPClientTransport;
  private agentId?: string;
  private subscriptions = new Map<string, Set<(data: unknown) => void>>();

  private constructor(client: Client, transport: StreamableHTTPClientTransport, agentId?: string) {
    this.client = client;
    this.transport = transport;
    this.agentId = agentId;
    this.setupNotificationHandler();
  }

  /**
   * Set up MCP notification handlers for resource updates.
   */
  private setupNotificationHandler(): void {
    // Handle resource update notifications (specific resource changed)
    this.client.setNotificationHandler(
      ResourceUpdatedNotificationSchema,
      async (notification: ResourceUpdatedNotification) => {
        const uri = notification.params?.uri;
        if (uri) {
          await this.notifySubscribers(uri);
        }
      }
    );

    // Handle resource list changed notifications (re-fetch all subscribed)
    this.client.setNotificationHandler(ResourceListChangedNotificationSchema, async () => {
      for (const uri of this.subscriptions.keys()) {
        await this.notifySubscribers(uri);
      }
    });
  }

  /**
   * Notify all subscribers of a resource that it has been updated.
   */
  private async notifySubscribers(uri: string): Promise<void> {
    const callbacks = this.subscriptions.get(uri);
    if (!callbacks || callbacks.size === 0) return;

    try {
      const result = await this.client.readResource({ uri });
      const data = extractContent(result.contents as ContentItem[], uri);
      if (data !== null) {
        for (const cb of callbacks) {
          try {
            cb(data);
          } catch (e) {
            console.error(`Subscription callback error for ${uri}:`, e);
          }
        }
      }
    } catch (e) {
      console.error(`Failed to read resource ${uri} for notification:`, e);
    }
  }

  /**
   * Connect to the global MCP compositor endpoint.
   *
   * The compositor exposes tools from all agents via nested sub-compositors.
   * Use agentId option to automatically prefix tool calls for a specific agent.
   *
   * @param options - Optional agent ID for automatic tool name prefixing
   * @returns Connected MCP client instance
   */
  static async connect(options: McpClientOptions = {}): Promise<AgentMcpClient> {
    const url = `${window.location.origin}/mcp`;

    // Get auth token
    const token = options.token ?? getAuthToken();
    if (!token) {
      throw new Error("No authentication token found. Add ?token=... to URL.");
    }

    // Create transport with bearer token auth
    const transport = new StreamableHTTPClientTransport(new URL(url), {
      requestInit: {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      },
    });

    const client = new Client({ name: "adgn-web", version: "1.0.0" }, { capabilities: {} });

    await client.connect(transport);
    return new AgentMcpClient(client, transport, options.agentId);
  }

  /**
   * Call an MCP tool.
   *
   * Tool names are automatically prefixed with agent ID if configured.
   * Example: 'approve_call' becomes '{agentId}_approve_call'
   *
   * FastMCP compositor prefixes tools as: {server}_{tool}
   *
   * @param name - Tool name (without agent prefix)
   * @param args - Tool arguments
   * @returns Tool result from structuredContent
   */
  async callTool<T = unknown>(name: string, args: Record<string, unknown> = {}): Promise<T> {
    const toolName = this.agentId ? `${this.agentId}_${name}` : name;
    const result = await this.client.callTool({ name: toolName, arguments: args });
    return result.structuredContent as T;
  }

  /**
   * Read an MCP resource.
   *
   * Resource URIs are automatically prefixed with agent ID if configured.
   * FastMCP default resource_prefix_format is "path", which transforms:
   *   approvals://pending → approvals://agent123/pending
   *
   * @param uri - Resource URI (without agent prefix)
   * @returns Resource contents (parsed from first content item)
   */
  async readResource<T = unknown>(uri: string): Promise<T> {
    const resourceUri = this.agentId ? this.prefixResourceUri(uri, this.agentId) : uri;
    const result = await this.client.readResource({ uri: resourceUri });
    const data = extractContent<T>(result.contents as ContentItem[], resourceUri);
    if (data === null) {
      throw new Error(`No content in resource: ${uri}`);
    }
    return data;
  }

  /**
   * Subscribe to an MCP resource for real-time updates.
   *
   * Resource URIs are automatically prefixed with agent ID if configured.
   * FastMCP default resource_prefix_format is "path", which transforms:
   *   approvals://pending → approvals://agent123/pending
   *
   * Uses MCP notifications (notifications/resources/updated) to receive updates.
   * Falls back to initial read if notification is missed.
   *
   * @param uri - Resource URI to subscribe to (without agent prefix)
   * @param callback - Called with resource data on updates
   * @returns Unsubscribe function
   */
  async subscribeResource<T>(uri: string, callback: (data: T) => void): Promise<() => void> {
    const resourceUri = this.agentId ? this.prefixResourceUri(uri, this.agentId) : uri;

    // Register callback
    if (!this.subscriptions.has(resourceUri)) {
      this.subscriptions.set(resourceUri, new Set());
    }
    this.subscriptions.get(resourceUri)!.add(callback as (data: unknown) => void);

    // Subscribe to resource updates via MCP
    await this.client.subscribeResource({ uri: resourceUri });

    // Initial read to populate callback immediately
    try {
      const result = await this.client.readResource({ uri: resourceUri });
      const data = extractContent<T>(result.contents as ContentItem[], resourceUri);
      if (data !== null) {
        callback(data);
      }
    } catch (e) {
      console.error(`Initial resource read failed: ${uri}`, e);
    }

    return () => {
      const callbacks = this.subscriptions.get(resourceUri);
      if (callbacks) {
        callbacks.delete(callback as (data: unknown) => void);
        // Only unsubscribe when no more callbacks
        if (callbacks.size === 0) {
          this.subscriptions.delete(resourceUri);
          this.client.unsubscribeResource({ uri: resourceUri }).catch((e: unknown) => {
            console.warn(`Failed to unsubscribe from ${resourceUri}:`, e);
          });
        }
      }
    };
  }

  /**
   * Apply FastMCP "path" format resource prefix.
   * Transforms: protocol://path → protocol://prefix/path
   *
   * @param uri - Original resource URI
   * @param prefix - Prefix to add (agent ID)
   * @returns Prefixed resource URI
   */
  private prefixResourceUri(uri: string, prefix: string): string {
    const match = uri.match(/^([^:]+:\/\/)(.*)$/);
    if (!match) {
      throw new Error(`Invalid resource URI format: ${uri}`);
    }
    const [, protocol, path] = match;
    return `${protocol}${prefix}/${path}`;
  }

  /**
   * List available tools.
   *
   * @returns List of available MCP tools
   */
  async listTools() {
    return await this.client.listTools();
  }

  /**
   * List available resources.
   *
   * @returns List of available MCP resources
   */
  async listResources() {
    return await this.client.listResources();
  }

  /**
   * Close the MCP client connection.
   */
  async close(): Promise<void> {
    await this.client.close();
  }
}
