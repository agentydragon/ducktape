import { z } from "zod";

// Zod schemas for MCP server specs (frontend validation)
export const StdioSpecZ = z
  .object({
    transport: z.literal("stdio"),
    command: z.string().min(1, "command required"),
    args: z.array(z.string()).default([]),
    env: z.record(z.string(), z.string()).default({}),
  })
  .strict();

export const SseSpecZ = z
  .object({
    transport: z.literal("sse"),
    url: z.string().min(1, "url required"),
    headers: z.record(z.string(), z.string()).optional().default({}),
    timeout_secs: z.number().int().positive().default(5),
    sse_read_timeout_secs: z.number().int().positive().default(300),
  })
  .strict();

export const InprocSpecZ = z
  .object({
    transport: z.literal("inproc"),
    factory: z.string().min(1, "factory required"),
    args: z.array(z.any()).default([]),
    kwargs: z.record(z.string(), z.any()).default({}),
  })
  .strict();

export const HttpSpecZ = z
  .object({
    transport: z.literal("http"),
    url: z.string().min(1, "url required"),
    headers: z.record(z.string(), z.string()).optional().default({}),
    auth: z.string().optional(),
    timeout_secs: z.number().int().positive().default(30),
    sse_read_timeout_secs: z.number().int().positive().default(300),
  })
  .strict();

export const TransportSpecZ = z.discriminatedUnion("transport", [StdioSpecZ, SseSpecZ, InprocSpecZ, HttpSpecZ]);

export type TransportSpec = z.infer<typeof TransportSpecZ>;

function safeParseJson<T>(text: string, fallback: T): T {
  try {
    return JSON.parse(text);
  } catch {
    return fallback;
  }
}

export type FieldErrors = Record<string, string[]>;

function addFieldErr(map: FieldErrors, key: string, msg: string) {
  if (!map[key]) map[key] = [];
  map[key].push(msg);
}

export function buildSpecFromForm(input: {
  transport: "stdio" | "sse" | "inproc" | "http";
  stdioCommand?: string;
  stdioArgs?: string;
  stdioEnv?: string;
  sseUrl?: string;
  sseHeaders?: string;
  sseTimeout?: number | string;
  sseReadTimeout?: number | string;
  httpUrl?: string;
  httpHeaders?: string;
  httpAuth?: string;
  httpTimeout?: number | string;
  httpReadTimeout?: number | string;
  inprocFactory?: string;
  inprocArgs?: string;
  inprocKwargs?: string;
}): { spec?: TransportSpec; errors: string[]; fieldErrors: FieldErrors } {
  const errs: string[] = [];
  const fieldErrors: FieldErrors = {};
  let candidate: unknown;
  switch (input.transport) {
    case "stdio": {
      const args = safeParseJson(input.stdioArgs || "[]", null);
      const env = safeParseJson(input.stdioEnv || "{}", null);
      if (!Array.isArray(args)) addFieldErr(fieldErrors, "stdioArgs", "args must be JSON array");
      if (!(env === null || typeof env === "object")) addFieldErr(fieldErrors, "stdioEnv", "env must be JSON object");
      candidate = {
        transport: "stdio",
        command: input.stdioCommand || "",
        args,
        env,
      };
      const res = StdioSpecZ.safeParse(candidate);
      if (!res.success) {
        // Map zod issues to form field keys
        for (const issue of res.error.issues) {
          const p0 = issue.path[0];
          const key =
            p0 === "command"
              ? "stdioCommand"
              : p0 === "args"
                ? "stdioArgs"
                : p0 === "env"
                  ? "stdioEnv"
                  : String(p0 || "stdio");
          addFieldErr(fieldErrors, key, issue.message);
        }
        errs.push("Invalid stdio spec");
        return { errors: errs, fieldErrors };
      }
      return { spec: res.data, errors: [], fieldErrors };
    }
    case "sse": {
      const headers = safeParseJson(input.sseHeaders || "{}", null);
      if (!(headers === null || typeof headers === "object"))
        addFieldErr(fieldErrors, "sseHeaders", "headers must be JSON object");
      const timeout = Number(input.sseTimeout ?? 5);
      const rto = Number(input.sseReadTimeout ?? 300);
      candidate = {
        transport: "sse",
        url: input.sseUrl || "",
        headers,
        timeout_secs: timeout,
        sse_read_timeout_secs: rto,
      };
      const res = SseSpecZ.safeParse(candidate);
      if (!res.success) {
        for (const issue of res.error.issues) {
          const p0 = issue.path[0];
          let key = "sse";
          if (p0 === "url") key = "sseUrl";
          else if (p0 === "headers") key = "sseHeaders";
          else if (p0 === "timeout_secs" || p0 === "timeout") key = "sseTimeout";
          else if (p0 === "sse_read_timeout_secs" || p0 === "sse_read_timeout") key = "sseReadTimeout";
          addFieldErr(fieldErrors, key, issue.message);
        }
        errs.push("Invalid sse spec");
        return { errors: errs, fieldErrors };
      }
      return { spec: res.data, errors: [], fieldErrors };
    }
    case "http": {
      const headers = safeParseJson(input.httpHeaders || "{}", null);
      if (!(headers === null || typeof headers === "object"))
        addFieldErr(fieldErrors, "httpHeaders", "headers must be JSON object");
      const timeout = Number(input.httpTimeout ?? 30);
      const rto = Number(input.httpReadTimeout ?? 300);
      candidate = {
        transport: "http",
        url: input.httpUrl || "",
        headers,
        auth: input.httpAuth || undefined,
        timeout_secs: timeout,
        sse_read_timeout_secs: rto,
      };
      const res = HttpSpecZ.safeParse(candidate);
      if (!res.success) {
        for (const issue of res.error.issues) {
          const p0 = issue.path[0];
          let key = "http";
          if (p0 === "url") key = "httpUrl";
          else if (p0 === "headers") key = "httpHeaders";
          else if (p0 === "auth") key = "httpAuth";
          else if (p0 === "timeout_secs" || p0 === "timeout") key = "httpTimeout";
          else if (p0 === "sse_read_timeout_secs" || p0 === "sse_read_timeout") key = "httpReadTimeout";
          addFieldErr(fieldErrors, key, issue.message);
        }
        errs.push("Invalid http spec");
        return { errors: errs, fieldErrors };
      }
      return { spec: res.data, errors: [], fieldErrors };
    }
    case "inproc": {
      const args = safeParseJson(input.inprocArgs || "[]", null);
      const kwargs = safeParseJson(input.inprocKwargs || "{}", null);
      if (!Array.isArray(args)) addFieldErr(fieldErrors, "inprocArgs", "args must be JSON array");
      if (!(kwargs === null || typeof kwargs === "object"))
        addFieldErr(fieldErrors, "inprocKwargs", "kwargs must be JSON object");
      candidate = {
        transport: "inproc",
        factory: input.inprocFactory || "",
        args,
        kwargs,
      };
      const res = InprocSpecZ.safeParse(candidate);
      if (!res.success) {
        for (const issue of res.error.issues) {
          const p0 = issue.path[0];
          const key =
            p0 === "factory"
              ? "inprocFactory"
              : p0 === "args"
                ? "inprocArgs"
                : p0 === "kwargs"
                  ? "inprocKwargs"
                  : String(p0 || "inproc");
          addFieldErr(fieldErrors, key, issue.message);
        }
        errs.push("Invalid inproc spec");
        return { errors: errs, fieldErrors };
      }
      return { spec: res.data, errors: [], fieldErrors };
    }
  }
}
