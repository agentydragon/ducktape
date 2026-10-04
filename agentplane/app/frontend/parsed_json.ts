/** `text` as the JSON it holds, or `undefined` where it is not JSON, as a body still streaming in is not. */
// TODO: parse JSON that is still streaming in, recording which keys and values are complete and which are
// cut off partway, and render from that. Today a call whose arguments have not fully arrived parses to
// `undefined`, so its folded line and its opened view show the raw text until the last chunk lands. With a
// partial parse, a `command` still being written could show as it arrives, marked unfinished, and a
// `description` could wait until it is complete.
export function parsedJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch (error) {
    if (error instanceof SyntaxError) return undefined;
    throw error;
  }
}
