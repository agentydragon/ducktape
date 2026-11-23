import { create, fromJson, toJson } from "@bufbuild/protobuf";

import {
  AcknowledgeCommandNoticeRequestSchema,
  BatchGetCommandNoticeAcknowledgementsRequestSchema,
  CommandNoticeAcknowledgementsSchema,
  type CommandNoticeAcknowledgement,
  type CommandNoticeAcknowledgements,
  type CommandNoticeRevision,
} from "../../app_service_pb";
import { fetchJson } from "../transport/client";
import { facadeBody } from "./facade_body";

const PAGE_SIZE = 25;
const MAX_BATCH = 100;

function path(threadId: string): string {
  return `/upstream/app/v1/threads/${encodeURIComponent(threadId)}/notice-acknowledgements`;
}

function post(body: string, signal?: AbortSignal): RequestInit {
  return { method: "POST", body, signal, headers: { "Content-Type": "application/json", Accept: "application/json" } };
}

export async function acknowledgeCommandNotice(threadId: string, notice: CommandNoticeRevision): Promise<void> {
  const request = create(AcknowledgeCommandNoticeRequestSchema, { threadId, notice });
  await fetchJson(path(threadId), post(facadeBody(toJson(AcknowledgeCommandNoticeRequestSchema, request), "threadId")));
}

export async function getCommandNoticeAcknowledgements(
  threadId: string,
  notices: CommandNoticeRevision[],
  signal: AbortSignal
): Promise<CommandNoticeAcknowledgement[]> {
  const batches = [];
  for (let from = 0; from < notices.length; from += MAX_BATCH) {
    const request = create(BatchGetCommandNoticeAcknowledgementsRequestSchema, {
      threadId,
      notices: notices.slice(from, from + MAX_BATCH),
    });
    batches.push(
      fetchJson(
        `${path(threadId)}:batchGet`,
        post(facadeBody(toJson(BatchGetCommandNoticeAcknowledgementsRequestSchema, request), "threadId"), signal)
      ).then(
        (payload) =>
          fromJson(CommandNoticeAcknowledgementsSchema, payload, { ignoreUnknownFields: true }).acknowledgements
      )
    );
  }
  return (await Promise.all(batches)).flat();
}

export async function listCommandNoticeAcknowledgements(
  threadId: string,
  pageToken: string,
  signal: AbortSignal
): Promise<CommandNoticeAcknowledgements> {
  const query = new URLSearchParams({ pageSize: String(PAGE_SIZE), ...(pageToken && { pageToken }) });
  const payload = await fetchJson(`${path(threadId)}?${query}`, { signal, headers: { Accept: "application/json" } });
  return fromJson(CommandNoticeAcknowledgementsSchema, payload, { ignoreUnknownFields: true });
}
