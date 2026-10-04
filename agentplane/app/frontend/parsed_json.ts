/** `text` as the JSON it holds, or `undefined` where it is not JSON, as a body still streaming in is not. */
export function parsedJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch (error) {
    if (error instanceof SyntaxError) return undefined;
    throw error;
  }
}
