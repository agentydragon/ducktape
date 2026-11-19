import * as z from "zod";
export const user = z.object({ name: z.string().min(1), age: z.number().int(), tags: z.array(z.string()) });
export { z };
