import { Badge, Button, Group, Loader, Paper, SegmentedControl, Text, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";

import { displayableError, fetchSessionFrames, type SessionFrame, type SessionFramePage } from "../client";
import { JsonPreview } from "../json_preview";
import { conversationPath, navigateToConsolePath } from "../routing";
import { useVariant, VariantControl } from "../variant_control";
import { frameSummary, kindsForMode, prependEarlierPage, type FrameMode } from "./frame_log";

// Matches the server's own default page. Held here too so "Load earlier frames" asks for the same
// size as the first read; a frame carries a whole tool result, and each one on screen builds a
// syntax-highlighted editor once it nears the viewport (frontend/code_block.tsx).
const FRAME_PAGE_SIZE = 50;

function FrameRow({ frame }: { frame: SessionFrame }) {
  // Per-row verbosity, as on the history page: compact auto-folds the payload to fill the block,
  // full shows it whole with line numbers. Frames are read by skimming until one looks wrong.
  const [variant, setVariant] = useVariant("compact");
  const summary = frameSummary(frame);
  const outbound = frame.direction === "to_agent";
  return (
    <Paper withBorder p="sm">
      {/* Both groups wrap: at a phone width the badges would otherwise be squeezed to one
          truncated letter each, which is the whole identity of the row. */}
      <Group justify="space-between" align="center" gap="xs" mb={4}>
        <Group gap={6}>
          <Text fw={600} size="sm" ff="monospace">
            #{frame.frame_seq}
          </Text>
          <Badge size="sm" variant="light" color={outbound ? "blue" : "teal"}>
            {outbound ? "to agent" : "from agent"}
          </Badge>
          <Badge size="sm" variant="outline">
            {frame.kind}
          </Badge>
          {/* Not a protocol frame: the console's own reconstruction of an answer still streaming,
              so one left at the end of a log is a turn that never finished. */}
          {frame.partial && (
            <Badge size="sm" variant="light" color="orange">
              partial
            </Badge>
          )}
        </Group>
        <Group gap="xs" wrap="nowrap">
          {/* Wall-clock rather than the relative form the tool-call surfaces use: frames are read
              as a sequence, where "2 hours ago" on every row says nothing about the gaps. */}
          <Text size="xs" c="dimmed" title={frame.created_at}>
            {frame.created_at.slice(11, 19)}
          </Text>
          <VariantControl variant={variant} onChange={setVariant} />
        </Group>
      </Group>
      {summary && (
        <Text size="xs" c="dimmed" lineClamp={1} mb={4}>
          {summary}
        </Text>
      )}
      <JsonPreview value={frame.payload} variant={variant} />
    </Paper>
  );
}

/** The raw protocol frames behind one conversation — what the console's transcript is a lossy
 * projection *of* (haku/plans/chat_runtime_projection.md § stage 4). Reached from the conversation
 * it belongs to and deep-linkable at its own route, so "look at frame 412" is a link.
 *
 * **A long session must not be expensive to open.** The first read is the *tail* of the log, so
 * the frames an operator came for — a cut-off answer, a turn that died — are on the first page
 * rather than a hundred pages in; earlier ones arrive on demand, above what is loaded. Within a
 * page the frames stay in wire order and the view opens at the top of it, because reading a
 * protocol log backwards is not reading it — so this is a window on the end, read downwards.
 * Each row's payload builds its editor only once it nears the viewport, which is what keeps a
 * page of fifty JSON blocks off the main thread. */
export function SessionFramesPage({ sessionId }: { sessionId: string }) {
  const [mode, setMode] = useState<FrameMode>("frames");
  const [reloads, setReloads] = useState(0);
  const [loaded, setLoaded] = useState<SessionFramePage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingEarlier, setLoadingEarlier] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoaded(null);
    setError(null);
    fetchSessionFrames(sessionId, FRAME_PAGE_SIZE, undefined, kindsForMode(mode))
      .then((page) => {
        if (alive) setLoaded(page);
      })
      .catch((reason: unknown) => {
        if (alive) setError(displayableError(reason));
      });
    return () => {
      alive = false;
    };
  }, [sessionId, mode, reloads]);

  const loadEarlier = useCallback(async () => {
    const cursor = loaded?.next_before_seq;
    if (cursor == null || loadingEarlier) return;
    setLoadingEarlier(true);
    try {
      const page = await fetchSessionFrames(sessionId, FRAME_PAGE_SIZE, cursor, kindsForMode(mode));
      setLoaded((previous) => prependEarlierPage(page, previous));
      setError(null);
    } catch (reason: unknown) {
      setError(displayableError(reason));
    } finally {
      setLoadingEarlier(false);
    }
  }, [loaded?.next_before_seq, loadingEarlier, mode, sessionId]);

  const frames = loaded?.frames;
  return (
    <section className="haku-page" aria-label="Raw frames">
      <header className="haku-page-header">
        <div className="haku-page-bar haku-conversation-detail-header">
          <div>
            <Button
              variant="subtle"
              size="compact-sm"
              onClick={() => navigateToConsolePath(conversationPath(sessionId))}
            >
              ← Conversation
            </Button>
            <Title order={1}>Raw frames</Title>
            <Text c="dimmed" size="sm">
              The agent protocol as it crossed the wire — what the transcript is a projection of.
            </Text>
          </div>
          <Group gap="sm" wrap="nowrap" align="center">
            <SegmentedControl
              size="xs"
              value={mode}
              onChange={(next) => setMode(next as FrameMode)}
              aria-label="Frame view"
              data={[
                { value: "frames", label: "Frames" },
                { value: "deltas", label: "Stream deltas" },
              ]}
            />
            <Button size="xs" variant="light" loading={!frames && !error} onClick={() => setReloads((n) => n + 1)}>
              Refresh
            </Button>
          </Group>
        </div>
      </header>
      <div className="haku-page-scroll">
        <div className="haku-page-list">
          {error && (
            <Text c="red" size="sm">
              Failed to load frames: {error}
            </Text>
          )}
          {!frames && !error && (
            <Group justify="center" p="xl">
              <Loader size="sm" />
            </Group>
          )}
          {frames && frames.length === 0 && (
            <Text c="dimmed" size="sm">
              {mode === "deltas"
                ? "No stream deltas recorded — only the SPA chat surface streams them."
                : "No frames recorded for this session."}
            </Text>
          )}
          {loaded?.next_before_seq != null && (
            <Group justify="center">
              <Button size="xs" variant="light" loading={loadingEarlier} onClick={() => void loadEarlier()}>
                Load earlier frames
              </Button>
            </Group>
          )}
          {frames?.map((frame) => (
            <FrameRow key={frame.frame_seq} frame={frame} />
          ))}
        </div>
      </div>
    </section>
  );
}
