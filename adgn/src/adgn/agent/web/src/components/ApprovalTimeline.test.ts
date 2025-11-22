import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte'
import ApprovalTimeline from './ApprovalTimeline.svelte'
import type { ApprovalHistoryEntry, ApprovalOutcome } from '../generated/types'

// Mock the agents API
const mockGetApprovalHistory = vi.fn()
vi.mock('../features/agents/api', () => ({
  getApprovalHistory: mockGetApprovalHistory
}))

// Mock WebSocket
class MockWebSocket {
  url: string
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null
  readyState = WebSocket.OPEN

  constructor(url: string) {
    this.url = url
  }

  close() {
    if (this.onclose) {
      this.onclose()
    }
  }

  // Helper to simulate receiving messages
  _simulateMessage(data: any) {
    if (this.onmessage) {
      this.onmessage(new MessageEvent('message', { data: JSON.stringify(data) }))
    }
  }
}

global.WebSocket = MockWebSocket as any

describe('ApprovalTimeline', () => {
  const mockAgentId = 'test-agent-123'

  const createMockEntry = (
    callId: string,
    tool: string,
    outcome: ApprovalOutcome,
    timestamp: string = '2025-01-01T00:00:00Z',
    reason?: string
  ): ApprovalHistoryEntry => ({
    tool_call: {
      name: tool,
      call_id: callId,
      args_json: '{"test": "value"}'
    },
    outcome,
    reason: reason || null,
    timestamp,
  })

  beforeEach(() => {
    vi.clearAllMocks()
    mockGetApprovalHistory.mockResolvedValue({ timeline: [] })
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('should render loading state initially', () => {
    mockGetApprovalHistory.mockImplementation(() => new Promise(() => {})) // Never resolves

    render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    expect(screen.getByText('Loading timeline...')).toBeTruthy()
  })

  it('should render empty state when no entries', async () => {
    mockGetApprovalHistory.mockResolvedValue({ timeline: [] })

    render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      expect(screen.getByText('No approval history yet')).toBeTruthy()
    })
  })

  it('should display error message when fetch fails', async () => {
    mockGetApprovalHistory.mockRejectedValue(new Error('getApprovalHistory MCP error: Failed to read resource'))

    render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      expect(screen.getByText(/getApprovalHistory MCP error/)).toBeTruthy()
    })
  })

  it('should render timeline entries', async () => {
    const timeline = [
      createMockEntry('call-1', 'read_file', 'user_approve'),
      createMockEntry('call-2', 'write_file', 'user_deny_continue', '2025-01-01T00:01:00Z'),
      createMockEntry('call-3', 'exec', 'policy_allow', '2025-01-01T00:02:00Z'),
    ]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      expect(screen.getByText('read_file')).toBeTruthy()
      expect(screen.getByText('write_file')).toBeTruthy()
      expect(screen.getByText('exec')).toBeTruthy()
    })
  })

  it('should apply correct color coding for user_approve', async () => {
    const timeline = [createMockEntry('call-1', 'test_tool', 'user_approve')]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    const { container } = render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      const entry = container.querySelector('.timeline-entry')
      expect(entry?.classList.contains('user-approved')).toBe(true)
    })
  })

  it('should apply correct color coding for policy_allow', async () => {
    const timeline = [createMockEntry('call-1', 'test_tool', 'policy_allow')]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    const { container } = render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      const entry = container.querySelector('.timeline-entry')
      expect(entry?.classList.contains('auto-approved')).toBe(true)
    })
  })

  it('should apply correct color coding for rejected outcomes', async () => {
    const timeline = [createMockEntry('call-1', 'test_tool', 'user_deny_continue')]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    const { container } = render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      const entry = container.querySelector('.timeline-entry')
      expect(entry?.classList.contains('rejected')).toBe(true)
    })
  })

  it('should filter by approved decisions only', async () => {
    const timeline = [
      createMockEntry('call-1', 'approved_tool', 'user_approve'),
      createMockEntry('call-2', 'rejected_tool', 'user_deny_continue'),
      createMockEntry('call-3', 'auto_tool', 'policy_allow'),
    ]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    const { container } = render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      expect(screen.getByText('approved_tool')).toBeTruthy()
      expect(screen.getByText('rejected_tool')).toBeTruthy()
      expect(screen.getByText('auto_tool')).toBeTruthy()
    })

    // Change filter to approved only
    const filterSelect = container.querySelector('select[bind\\:value]') as HTMLSelectElement
    expect(filterSelect).toBeTruthy()

    if (filterSelect) {
      await fireEvent.change(filterSelect, { target: { value: 'approved' } })
    }

    await waitFor(() => {
      expect(screen.getByText('approved_tool')).toBeTruthy()
      expect(screen.getByText('auto_tool')).toBeTruthy()
      expect(screen.queryByText('rejected_tool')).toBeNull()
    })
  })

  it('should filter by rejected decisions only', async () => {
    const timeline = [
      createMockEntry('call-1', 'approved_tool', 'user_approve'),
      createMockEntry('call-2', 'rejected_tool', 'user_deny_continue'),
      createMockEntry('call-3', 'auto_tool', 'policy_allow'),
    ]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    const { container } = render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      expect(screen.getByText('approved_tool')).toBeTruthy()
    })

    // Change filter to rejected only
    const selects = container.querySelectorAll('select')
    const filterSelect = selects[0]

    if (filterSelect) {
      await fireEvent.change(filterSelect, { target: { value: 'rejected' } })
    }

    await waitFor(() => {
      expect(screen.getByText('rejected_tool')).toBeTruthy()
      expect(screen.queryByText('approved_tool')).toBeNull()
      expect(screen.queryByText('auto_tool')).toBeNull()
    })
  })

  it('should filter by policy decisions', async () => {
    const timeline = [
      createMockEntry('call-1', 'user_tool', 'user_approve'),
      createMockEntry('call-2', 'policy_allow_tool', 'policy_allow'),
      createMockEntry('call-3', 'policy_deny_tool', 'policy_deny_continue'),
    ]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    const { container } = render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      expect(screen.getByText('user_tool')).toBeTruthy()
    })

    // Change filter to policy only
    const selects = container.querySelectorAll('select')
    const filterSelect = selects[0]

    if (filterSelect) {
      await fireEvent.change(filterSelect, { target: { value: 'policy' } })
    }

    await waitFor(() => {
      expect(screen.getByText('policy_allow_tool')).toBeTruthy()
      expect(screen.getByText('policy_deny_tool')).toBeTruthy()
      expect(screen.queryByText('user_tool')).toBeNull()
    })
  })

  it('should filter by tool name search', async () => {
    const timeline = [
      createMockEntry('call-1', 'read_file', 'user_approve'),
      createMockEntry('call-2', 'write_file', 'user_approve'),
      createMockEntry('call-3', 'exec_command', 'user_approve'),
    ]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    const { container } = render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      expect(screen.getByText('read_file')).toBeTruthy()
      expect(screen.getByText('write_file')).toBeTruthy()
      expect(screen.getByText('exec_command')).toBeTruthy()
    })

    // Enter search term
    const searchInput = container.querySelector('input[type="text"]') as HTMLInputElement
    expect(searchInput).toBeTruthy()

    if (searchInput) {
      await fireEvent.input(searchInput, { target: { value: 'file' } })
    }

    await waitFor(() => {
      expect(screen.getByText('read_file')).toBeTruthy()
      expect(screen.getByText('write_file')).toBeTruthy()
      expect(screen.queryByText('exec_command')).toBeNull()
    })
  })

  it('should search case-insensitively', async () => {
    const timeline = [
      createMockEntry('call-1', 'ReadFile', 'user_approve'),
      createMockEntry('call-2', 'WriteFile', 'user_approve'),
    ]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    const { container } = render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      expect(screen.getByText('ReadFile')).toBeTruthy()
    })

    // Enter lowercase search term
    const searchInput = container.querySelector('input[type="text"]') as HTMLInputElement

    if (searchInput) {
      await fireEvent.input(searchInput, { target: { value: 'read' } })
    }

    await waitFor(() => {
      expect(screen.getByText('ReadFile')).toBeTruthy()
      expect(screen.queryByText('WriteFile')).toBeNull()
    })
  })

  it('should sort by newest first (default)', async () => {
    const timeline = [
      createMockEntry('call-1', 'tool_1', 'user_approve', '2025-01-01T00:00:00Z'),
      createMockEntry('call-2', 'tool_2', 'user_approve', '2025-01-01T00:02:00Z'),
      createMockEntry('call-3', 'tool_3', 'user_approve', '2025-01-01T00:01:00Z'),
    ]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    const { container } = render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      const entries = container.querySelectorAll('.tool-name')
      expect(entries[0]?.textContent).toBe('tool_2') // Newest (00:02)
      expect(entries[1]?.textContent).toBe('tool_3') // Middle (00:01)
      expect(entries[2]?.textContent).toBe('tool_1') // Oldest (00:00)
    })
  })

  it('should sort by oldest first when toggled', async () => {
    const timeline = [
      createMockEntry('call-1', 'tool_1', 'user_approve', '2025-01-01T00:00:00Z'),
      createMockEntry('call-2', 'tool_2', 'user_approve', '2025-01-01T00:02:00Z'),
      createMockEntry('call-3', 'tool_3', 'user_approve', '2025-01-01T00:01:00Z'),
    ]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    const { container } = render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      expect(screen.getByText('tool_1')).toBeTruthy()
    })

    // Change sort order to oldest first
    const selects = container.querySelectorAll('select')
    const sortSelect = selects[1] // Second select is sort order

    if (sortSelect) {
      await fireEvent.change(sortSelect, { target: { value: 'oldest' } })
    }

    await waitFor(() => {
      const entries = container.querySelectorAll('.tool-name')
      expect(entries[0]?.textContent).toBe('tool_1') // Oldest (00:00)
      expect(entries[1]?.textContent).toBe('tool_3') // Middle (00:01)
      expect(entries[2]?.textContent).toBe('tool_2') // Newest (00:02)
    })
  })

  it('should toggle args expansion when button is clicked', async () => {
    const timeline = [
      createMockEntry('call-1', 'test_tool', 'user_approve', '2025-01-01T00:00:00Z'),
    ]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    const { container } = render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      expect(screen.getByText('test_tool')).toBeTruthy()
    })

    // Find expand toggle
    const expandToggle = container.querySelector('.expand-toggle')
    expect(expandToggle).toBeTruthy()
    expect(expandToggle?.textContent).toContain('▶')

    // Initially args should not be visible
    expect(container.querySelector('.args-content')).toBeNull()

    // Click to expand
    if (expandToggle) {
      await fireEvent.click(expandToggle)
    }

    await waitFor(() => {
      expect(expandToggle?.textContent).toContain('▼')
      expect(container.querySelector('.args-content')).toBeTruthy()
    })

    // Click to collapse
    if (expandToggle) {
      await fireEvent.click(expandToggle)
    }

    await waitFor(() => {
      expect(expandToggle?.textContent).toContain('▶')
      expect(container.querySelector('.args-content')).toBeNull()
    })
  })

  it('should display reason when present', async () => {
    const timeline = [
      createMockEntry('call-1', 'test_tool', 'user_deny_continue', '2025-01-01T00:00:00Z', 'Security risk'),
    ]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      expect(screen.getByText('Security risk')).toBeTruthy()
    })
  })

  it('should show correct count of filtered entries', async () => {
    const timeline = [
      createMockEntry('call-1', 'approved_tool', 'user_approve'),
      createMockEntry('call-2', 'rejected_tool', 'user_deny_continue'),
      createMockEntry('call-3', 'another_approved', 'user_approve'),
    ]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    const { container } = render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      expect(screen.getByText(/Showing 3 of 3 entries/)).toBeTruthy()
    })

    // Apply filter
    const selects = container.querySelectorAll('select')
    const filterSelect = selects[0]

    if (filterSelect) {
      await fireEvent.change(filterSelect, { target: { value: 'approved' } })
    }

    await waitFor(() => {
      expect(screen.getByText(/Showing 2 of 3 entries/)).toBeTruthy()
    })
  })

  it('should display decision badges with correct labels', async () => {
    const timeline = [
      createMockEntry('call-1', 'tool_1', 'policy_allow'),
      createMockEntry('call-2', 'tool_2', 'user_approve'),
      createMockEntry('call-3', 'tool_3', 'user_deny_continue'),
      createMockEntry('call-4', 'tool_4', 'policy_deny_abort'),
    ]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      expect(screen.getByText('AUTO APPROVED')).toBeTruthy()
      expect(screen.getByText('USER APPROVED')).toBeTruthy()
      expect(screen.getByText('USER DENIED (CONTINUE)')).toBeTruthy()
      expect(screen.getByText('POLICY DENIED (ABORT)')).toBeTruthy()
    })
  })

  it('should display method badges (AUTO/USER)', async () => {
    const timeline = [
      createMockEntry('call-1', 'tool_1', 'policy_allow'),
      createMockEntry('call-2', 'tool_2', 'user_approve'),
    ]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    const { container } = render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      const methodBadges = container.querySelectorAll('.method-badge')
      expect(methodBadges[0]?.textContent).toBe('AUTO')
      expect(methodBadges[1]?.textContent).toBe('USER')
    })
  })

  it('should show message when filters match no entries', async () => {
    const timeline = [
      createMockEntry('call-1', 'tool_1', 'user_approve'),
    ]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    const { container } = render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      expect(screen.getByText('tool_1')).toBeTruthy()
    })

    // Apply search that doesn't match anything
    const searchInput = container.querySelector('input[type="text"]') as HTMLInputElement

    if (searchInput) {
      await fireEvent.input(searchInput, { target: { value: 'nonexistent' } })
    }

    await waitFor(() => {
      expect(screen.getByText('No entries match the current filters')).toBeTruthy()
    })
  })

  it('should refetch and resubscribe when agentId prop changes', async () => {
    mockGetApprovalHistory.mockResolvedValue({ timeline: [] })

    const { rerender } = render(ApprovalTimeline, { props: { agentId: 'agent-1' } })

    await waitFor(() => {
      expect(mockGetApprovalHistory).toHaveBeenCalledWith('agent-1')
    })

    // Change agentId
    rerender({ agentId: 'agent-2' })

    await waitFor(() => {
      expect(mockGetApprovalHistory).toHaveBeenCalledWith('agent-2')
    })
  })

  it('should format timestamps correctly', async () => {
    const timeline = [
      createMockEntry('call-1', 'tool_1', 'user_approve', '2025-01-15T14:30:00Z'),
    ]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    const { container } = render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      const timestamp = container.querySelector('.timestamp')
      expect(timestamp?.textContent).toBeTruthy()
      // Should be a formatted locale string (exact format depends on locale)
      expect(timestamp?.textContent).toMatch(/\d/)
    })
  })

  it('should combine multiple filters correctly', async () => {
    const timeline = [
      createMockEntry('call-1', 'read_file', 'user_approve'),
      createMockEntry('call-2', 'write_file', 'user_approve'),
      createMockEntry('call-3', 'exec_command', 'user_deny_continue'),
      createMockEntry('call-4', 'read_data', 'policy_allow'),
    ]

    mockGetApprovalHistory.mockResolvedValue({ timeline })

    const { container } = render(ApprovalTimeline, { props: { agentId: mockAgentId } })

    await waitFor(() => {
      expect(screen.getByText('read_file')).toBeTruthy()
    })

    // Apply decision filter (approved only)
    const selects = container.querySelectorAll('select')
    const filterSelect = selects[0]

    if (filterSelect) {
      await fireEvent.change(filterSelect, { target: { value: 'approved' } })
    }

    // Apply search filter (read)
    const searchInput = container.querySelector('input[type="text"]') as HTMLInputElement

    if (searchInput) {
      await fireEvent.input(searchInput, { target: { value: 'read' } })
    }

    await waitFor(() => {
      // Should show read_file (user_approve + contains "read")
      expect(screen.getByText('read_file')).toBeTruthy()
      // Should show read_data (policy_allow + contains "read")
      expect(screen.getByText('read_data')).toBeTruthy()
      // Should NOT show write_file (doesn't contain "read")
      expect(screen.queryByText('write_file')).toBeNull()
      // Should NOT show exec_command (rejected + doesn't contain "read")
      expect(screen.queryByText('exec_command')).toBeNull()
    })
  })
})
