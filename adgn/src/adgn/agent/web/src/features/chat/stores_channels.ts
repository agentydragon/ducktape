/**
 * Channel-based stores - uses modular WebSocket channels + MCP subscriptions.
 *
 * WebSocket channels:
 * - /ws/session - agent execution state
 * - /ws/policy - policy content
 *
 * MCP subscriptions:
 * - resource://agents/{agentId}/mcp/state - MCP server state (replaces /ws/mcp)
 * - resource://agents/{agentId}/approvals/pending - pending approvals (replaces /ws/approvals)
 * - resource://agents/{agentId}/ui/state - UI state (replaces /ws/ui)
 */

import { writable, derived, type Writable, type Readable } from 'svelte/store'
import { currentAgentId } from '../agents/stores'
import type {
  IncomingPayload,
  UiState,
  ServerEntry,
  ApprovalPolicyInfo,
} from '../../shared/types'
import {
  ChannelManager,
  type ChannelHandlers,
} from './channels'
import {
  getSnapshot as httpGetSnapshot,
  getProposal as httpGetProposal,
  rejectProposal as httpRejectProposal,
  approveCall,
  denyAbortCall,
  denyContinueCall,
  setPolicy as httpSetPolicy,
  sendPrompt as httpSendPrompt,
  abortRun as httpAbortRun,
  attachMcpServer,
  detachMcpServer,
} from '../agents/api'
import { get } from 'svelte/store'
import { agentStatus } from '../agents/stores'
import { getMCPClient } from '../mcp/clientManager'
import { createSubscriptionManager, type SubscriptionManager } from '../mcp/subscriptions'

export type Pending = {
  call_id: string
  tool_key: string
  args_json?: string | null
}

// Connection state per channel
export const channelsConnected: Writable<Set<string>> = writable(new Set())
export const runStatus: Writable<string> = writable('idle')
export const uiStates: Writable<Map<string, UiState>> = writable(new Map())
export const uiState: Readable<UiState | null> = derived(
  [uiStates, currentAgentId],
  ([$uiStates, $current]) => ($current ? $uiStates.get($current) ?? null : null)
)
export const lastError: Writable<string | null> = writable(null)
export const pendingApprovals: Writable<Map<string, Pending>> = writable(new Map())
export const approvalPolicy: Writable<ApprovalPolicyInfo | null> = writable(null)
export const mcpServerEntries: Writable<ServerEntry[]> = writable([])

let manager: ChannelManager | null = null
let subscriptionManager: SubscriptionManager | null = null
let closingIntentional = false

export function clearError() {
  lastError.set(null)
}

function createChannelHandlers(
  channel: string,
  onMessage: (msg: any) => void,
  options: {
    onOpen?: () => void
    onUnexpectedClose?: () => void
    isOptional?: boolean
  } = {}
): ChannelHandlers {
  return {
    onOpen: () => {
      channelsConnected.update((s) => new Set(s).add(channel))
      options.onOpen?.()
    },
    onClose: (ev) => {
      channelsConnected.update((s) => {
        const ns = new Set(s)
        ns.delete(channel)
        return ns
      })

      const isNormalClose = ev.code === 1000 || ev.code === 1001 || ev.code === 1005
      const isOptionalNotFound = options.isOptional && ev.code === 4404

      if (!closingIntentional && !isNormalClose && !isOptionalNotFound) {
        if (options.isOptional) {
          console.warn(`${channel} channel closed: ${ev.code}`)
        } else {
          lastError.set(`${channel} channel closed: ${ev.code}`)
        }
        options.onUnexpectedClose?.()
      }
    },
    onError: () => {
      if (options.isOptional) {
        console.warn(`${channel} channel error (optional)`)
      } else {
        lastError.set(`${channel} channel error`)
      }
    },
    onMessage,
  }
}

export function connectAgentChannels(agentId: string) {
  if (manager) {
    try {
      closingIntentional = true
      manager.close()
    } catch {}
  }

  manager = new ChannelManager(agentId)

  manager.on('session', createChannelHandlers('session', handleSessionMessage))

  manager.on('policy', createChannelHandlers('policy', handlePolicyMessage))

  closingIntentional = false
  manager.connect()

  // Subscribe to MCP state via MCP resource (replaces /ws/mcp)
  subscribeToMcpState(agentId).then(() => {
    agentStatus.set({ id: agentId, live: true })
  }).catch((error) => {
    console.error('MCP state subscription failed:', error)
    agentStatus.set({ id: agentId, live: false })
  })

  // Subscribe to approvals via MCP resource (replaces /ws/approvals)
  subscribeToApprovals(agentId).catch((error) => {
    console.error('Approvals subscription failed:', error)
  })

  // Subscribe to UI state via MCP (replaces /ws/ui)
  subscribeToUiState(agentId).catch((error) => {
    console.warn('UI state subscription unavailable (optional):', error)
  })
}

export function disconnectAgentChannels() {
  if (manager) {
    closingIntentional = true
    manager.close()
    manager = null
  }
  if (subscriptionManager) {
    subscriptionManager.cleanup().catch((error) => {
      console.warn('Error cleaning up UI state subscription:', error)
    })
    subscriptionManager = null
  }
  channelsConnected.set(new Set())
}

// Message handlers per channel

function handleSessionMessage(msg: any) {
  console.log('[CHANNEL:session]', msg.type)

  switch (msg.type) {
    case 'session_snapshot':
      if (msg.run_state) {
        runStatus.set(msg.run_state.status || 'idle')
      }
      break

    case 'run_status':
      if (msg.run_state?.status) {
        runStatus.set(msg.run_state.status)
      }
      break

    case 'turn_done':
      // Turn completed
      break

    default:
      // Transcript items (user_text, assistant_text, tool_call, etc.)
      // These could be handled by a transcript store
      break
  }
}


function handleApprovalsMessage(msg: any) {
  console.log('[CHANNEL:approvals]', msg.type)

  switch (msg.type) {
    case 'approvals_snapshot':
      const map = new Map<string, Pending>()
      for (const p of msg.pending) {
        map.set(p.call_id, {
          call_id: p.call_id,
          tool_key: p.tool_key,
          args_json: p.args ? JSON.stringify(p.args) : null,
        })
      }
      pendingApprovals.set(map)
      break

    case 'approval_pending':
      pendingApprovals.update((m) => {
        const mm = new Map(m)
        mm.set(msg.call_id, {
          call_id: msg.call_id,
          tool_key: msg.tool_key,
          args_json: msg.args_json ?? null,
        })
        return mm
      })
      break

    case 'approval_decision':
      pendingApprovals.update((m) => {
        const mm = new Map(m)
        mm.delete(msg.call_id)
        return mm
      })
      break
  }
}

function handlePolicyMessage(msg: any) {
  console.log('[CHANNEL:policy]', msg.type)

  switch (msg.type) {
    case 'policy_snapshot':
      approvalPolicy.set(msg.policy)
      break

    case 'policy_updated':
      // Could fetch updated policy via HTTP or wait for next snapshot
      break

    case 'policy_proposal':
      // New proposal, could update proposals list
      break
  }
}

/**
 * Subscribe to MCP state via MCP resource subscription
 * Replaces WebSocket /ws/mcp channel with resource://agents/{agentId}/mcp/state
 */
async function subscribeToMcpState(agentId: string): Promise<void> {
  try {
    // Get or create subscription manager
    const client = await getMCPClient()
    if (!subscriptionManager) {
      subscriptionManager = createSubscriptionManager(client)
    }

    // Subscribe to MCP state resource
    const uri = `resource://agents/${agentId}/mcp/state`
    await subscriptionManager.subscribe(uri, (data) => {
      handleMcpStateUpdate(data)
    })

    console.log('[MCP:mcp]', `Subscribed to ${uri}`)
  } catch (error) {
    console.error('[MCP:mcp]', 'Subscription failed:', error)
    throw error
  }
}

/**
 * Handle MCP state updates from MCP resource subscription
 */
function handleMcpStateUpdate(data: any): void {
  try {
    // Check for error indicator from subscription manager
    if (data.error) {
      console.warn('[MCP:mcp]', 'Resource read error:', data.message)
      return
    }

    // Parse MCP resource contents
    // Expected format: array of content items, first item is text with JSON
    if (!Array.isArray(data) || data.length === 0) {
      console.warn('[MCP:mcp]', 'Unexpected resource format:', data)
      return
    }

    const firstContent = data[0]
    if (firstContent.type !== 'text' || !firstContent.text) {
      console.warn('[MCP:mcp]', 'Expected text content, got:', firstContent.type)
      return
    }

    // Parse JSON: expected shape is {"sampling": {"servers": [...]}}
    const mcpState = JSON.parse(firstContent.text)
    if (mcpState.sampling?.servers) {
      console.log('[MCP:mcp]', `MCP state updated (${mcpState.sampling.servers.length} servers)`)
      mcpServerEntries.set(mcpState.sampling.servers)
    } else {
      console.warn('[MCP:mcp]', 'No sampling.servers in MCP state:', mcpState)
    }
  } catch (error) {
    console.error('[MCP:mcp]', 'Failed to process MCP state update:', error)
  }
}

/**
 * Subscribe to approvals via MCP resource subscription
 * Replaces WebSocket /ws/approvals channel with resource://agents/{agentId}/approvals/pending
 */
async function subscribeToApprovals(agentId: string): Promise<void> {
  try {
    // Get or create subscription manager
    const client = await getMCPClient()
    if (!subscriptionManager) {
      subscriptionManager = createSubscriptionManager(client)
    }

    // Subscribe to approvals resource
    const uri = `resource://agents/${agentId}/approvals/pending`
    await subscriptionManager.subscribe(uri, (data) => {
      handleApprovalsUpdate(data)
    })

    console.log('[MCP:approvals]', `Subscribed to ${uri}`)
  } catch (error) {
    console.error('[MCP:approvals]', 'Subscription failed:', error)
    throw error
  }
}

/**
 * Handle approvals updates from MCP resource subscription
 */
function handleApprovalsUpdate(data: any): void {
  try {
    // Check for error indicator from subscription manager
    if (data.error) {
      console.warn('[MCP:approvals]', 'Resource read error:', data.message)
      return
    }

    // Parse MCP resource contents
    // Expected format: array of content items, first item is text with JSON
    if (!Array.isArray(data) || data.length === 0) {
      console.warn('[MCP:approvals]', 'Unexpected resource format:', data)
      return
    }

    const firstContent = data[0]
    if (firstContent.type !== 'text' || !firstContent.text) {
      console.warn('[MCP:approvals]', 'Expected text content, got:', firstContent.type)
      return
    }

    // Parse JSON: expected shape is {"agent_id": "...", "pending": [...]}
    const approvalsData = JSON.parse(firstContent.text)
    if (!approvalsData.pending || !Array.isArray(approvalsData.pending)) {
      console.warn('[MCP:approvals]', 'No pending array in approvals data:', approvalsData)
      return
    }

    // Convert to Pending format expected by UI
    const map = new Map<string, Pending>()
    for (const approval of approvalsData.pending) {
      map.set(approval.call_id, {
        call_id: approval.call_id,
        tool_key: approval.tool,  // MCP uses 'tool', UI expects 'tool_key'
        args_json: approval.args ? JSON.stringify(approval.args) : null,
      })
    }

    console.log('[MCP:approvals]', `Approvals updated (${map.size} pending)`)
    pendingApprovals.set(map)
  } catch (error) {
    console.error('[MCP:approvals]', 'Failed to process approvals update:', error)
  }
}

/**
 * Subscribe to UI state via MCP resource subscription
 * Replaces WebSocket /ws/ui channel with resource://agents/{agentId}/ui/state
 */
async function subscribeToUiState(agentId: string): Promise<void> {
  try {
    // Get or create subscription manager
    const client = await getMCPClient()
    if (!subscriptionManager) {
      subscriptionManager = createSubscriptionManager(client)
    }

    // Subscribe to UI state resource
    const uri = `resource://agents/${agentId}/ui/state`
    await subscriptionManager.subscribe(uri, (data) => {
      handleUiStateUpdate(agentId, data)
    })

    console.log('[MCP:ui]', `Subscribed to ${uri}`)
  } catch (error) {
    // Graceful degradation: UI state is optional
    console.warn('[MCP:ui]', 'Subscription failed (UI server may not be attached):', error)
    throw error
  }
}

/**
 * Handle UI state updates from MCP resource subscription
 */
function handleUiStateUpdate(agentId: string, data: any): void {
  try {
    // Check for error indicator from subscription manager
    if (data.error) {
      console.warn('[MCP:ui]', 'Resource read error:', data.message)
      return
    }

    // Parse MCP resource contents
    // Expected format: array of content items, first item is text with JSON
    if (!Array.isArray(data) || data.length === 0) {
      console.warn('[MCP:ui]', 'Unexpected resource format:', data)
      return
    }

    const firstContent = data[0]
    if (firstContent.type !== 'text' || !firstContent.text) {
      console.warn('[MCP:ui]', 'Expected text content, got:', firstContent.type)
      return
    }

    // Parse JSON and update store
    const uiState = JSON.parse(firstContent.text) as UiState
    console.log('[MCP:ui]', `UI state updated (seq: ${uiState.seq})`)

    uiStates.update((m) => {
      const mm = new Map(m)
      mm.set(agentId, uiState)
      return mm
    })
  } catch (error) {
    console.error('[MCP:ui]', 'Failed to process UI state update:', error)
  }
}

// HTTP API wrappers (same as before)

export async function sendPrompt(text: string) {
  runStatus.set('starting')
  const id = get(currentAgentId)
  if (!id) return
  try {
    await httpSendPrompt(id, text)
  } catch (e) {
    console.warn('prompt failed', e)
  }
}

export async function approve(call_id: string) {
  const id = get(currentAgentId)
  if (!id) return
  try {
    await approveCall(id, call_id)
  } catch (e) {
    console.warn('approve failed', e)
  }
}

export async function denyContinue(call_id: string) {
  const id = get(currentAgentId)
  if (!id) return
  try {
    await denyContinueCall(id, call_id)
  } catch (e) {
    console.warn('deny_continue failed', e)
  }
}

export async function deny(call_id: string) {
  const id = get(currentAgentId)
  if (!id) return
  try {
    await denyAbortCall(id, call_id)
  } catch (e) {
    console.warn('deny_abort failed', e)
  }
}

export async function setPolicy(content: string, proposal_id?: string) {
  const id = get(currentAgentId)
  if (!id) return
  try {
    await httpSetPolicy(id, content, proposal_id)
  } catch (e) {
    console.warn('setPolicy failed', e)
  }
}

export async function approveProposal(proposal_id: string) {
  const id = get(currentAgentId)
  if (!id) return
  try {
    const p = await httpGetProposal(id, proposal_id)
    await httpSetPolicy(id, p.content, proposal_id)
  } catch (e) {
    console.warn('approveProposal failed', e)
  }
}

export async function withdrawProposal(proposal_id: string) {
  const id = get(currentAgentId)
  if (!id) return
  try {
    await httpRejectProposal(id, proposal_id)
  } catch (e) {
    console.warn('rejectProposal failed', e)
  }
}

export async function abortRun() {
  const id = get(currentAgentId)
  if (!id) return
  try {
    await httpAbortRun(id)
  } catch (e) {
    console.warn('abort failed', e)
  }
}

export async function reconfigureMcp(attach?: Record<string, any>, detach?: string[]) {
  const id = get(currentAgentId)
  if (!id) return
  try {
    if (attach && Object.keys(attach).length) {
      for (const [name, spec] of Object.entries(attach)) {
        await attachMcpServer(id, name, spec)
      }
    }
    if (detach && detach.length) {
      for (const name of detach) await detachMcpServer(id, name)
    }
  } catch (e) {
    console.warn('reconfigureMcp failed', e)
  }
}
