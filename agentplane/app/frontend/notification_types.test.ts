import { expect, it } from "vitest";

import { decodeSandboxNotificationStatus, decodeSessionNoticeStates } from "./notification_types";

it("decodes notification snapshots as ProtoJSON and materializes implicit defaults", () => {
  const status = decodeSandboxNotificationStatus(
    JSON.stringify({
      observedAt: "2026-10-27T18:00:00Z",
      inboxes: [
        {
          inbox: {
            id: "inbox-test",
            sessionId: "session-test",
            lastCursor: "9007199254740993",
            acknowledged: "9007199254740990",
            covered: "9007199254740992",
            expiredThrough: "0",
          },
          pendingEntries: [
            {
              cursor: "9007199254740993",
              createdAt: "2026-10-27T17:59:00Z",
              provider: "actions",
              summary: "A completed Action request",
            },
          ],
          subscriptions: [
            {
              id: "subscription-test",
              source: { actions: { requestId: "request-test" } },
              expiresAt: "2026-11-03T18:00:00Z",
            },
          ],
          noticeWaitReason: "NOTICE_WAIT_REASON_DEBOUNCING",
        },
      ],
    })
  );
  expect(status.observedAt).toBe("2026-10-27T18:00:00Z");
  expect(status.inboxes?.[0]?.inbox?.lastCursor).toBe("9007199254740993");
  expect(status.inboxes?.[0]?.pendingEntries?.[0]?.cursor).toBe("9007199254740993");
  expect(status.inboxes[0]?.subscriptions[0]?.expiresAt).toBe("2026-11-03T18:00:00Z");
  expect(status.inboxes[0]?.unannouncedCount).toBe(0);
  expect(status.inboxes[0]?.pendingAcknowledgementCount).toBe(0);
  expect(status.inboxes[0]?.pendingEntriesMore).toBe(false);

  const states = decodeSessionNoticeStates(
    JSON.stringify({
      observedAt: "2026-10-27T18:00:00Z",
      inboxes: [{ inboxId: "inbox-test", sessionId: "session-test", queued: 2 }],
    })
  );
  expect(states.inboxes?.[0]).toMatchObject({
    inboxId: "inbox-test",
    sessionId: "session-test",
    queued: 2,
    unacknowledged: 0,
  });
  expect(states.inboxes[0]?.notice).toBeUndefined();
});

it("rejects malformed ProtoJSON, unknown delivery states, and snapshots without observation time", () => {
  expect(() => decodeSessionNoticeStates("{")).toThrow();
  expect(() => decodeSessionNoticeStates('{"observedAt":42}')).toThrow();
  expect(() =>
    decodeSessionNoticeStates('{"observedAt":"2026-10-27T18:00:00Z","inboxes":[{"queued":"two"}]}')
  ).toThrow();
  expect(() =>
    decodeSessionNoticeStates(
      '{"observedAt":"2026-10-27T18:00:00Z","inboxes":[{"notice":"NOTICE_PROGRESS_FUTURE_VALUE"}]}'
    )
  ).toThrow();
  expect(() => decodeSessionNoticeStates('{"inboxes":[]}')).toThrow(TypeError);
  expect(() => decodeSandboxNotificationStatus('{"inboxes":[]}')).toThrow(TypeError);
  expect(() => decodeSandboxNotificationStatus('{"observedAt":"2026-10-27T18:00:00Z","inboxes":[{}]}')).toThrow(
    TypeError
  );
  expect(() =>
    decodeSandboxNotificationStatus(
      JSON.stringify({
        observedAt: "2026-10-27T18:00:00Z",
        inboxes: [
          {
            inbox: {},
            subscriptions: [
              {
                id: "subscription-test",
                expiresAt: "2026-11-03T18:00:00Z",
                source: {
                  actions: { requestId: "request-test" },
                  github: { repository: "owner/repository", subject: { branch: { name: "main" } } },
                },
              },
            ],
          },
        ],
      })
    )
  ).toThrow();
});

it("preserves numeric enum values from newer writers as explicit unknown variants", () => {
  const states = decodeSessionNoticeStates(
    JSON.stringify({
      observedAt: "2026-10-27T18:00:00Z",
      inboxes: [{ inboxId: "inbox-test", sessionId: "session-test", queued: 1, notice: 999, noticeWaitReason: 999 }],
    })
  );
  expect(states.inboxes[0]?.notice).toEqual({ unknown: 999 });
  expect(states.inboxes[0]?.noticeWaitReason).toEqual({ unknown: 999 });

  const status = decodeSandboxNotificationStatus(
    JSON.stringify({
      observedAt: "2026-10-27T18:00:00Z",
      inboxes: [
        {
          inbox: { id: "inbox-test", sessionId: "session-test" },
          subscriptions: [
            {
              id: "subscription-test",
              source: {
                github: {
                  repository: "owner/repository",
                  subject: { branch: { name: "main" } },
                  events: [{ event: 999 }],
                },
              },
              expiresAt: "2026-11-03T18:00:00Z",
              errorKind: 999,
            },
          ],
        },
      ],
    })
  );
  expect(status.inboxes[0]?.subscriptions[0]).toMatchObject({
    errorKind: { unknown: 999 },
    source: { github: { events: [{ event: { unknown: 999 } }] } },
  });
});
