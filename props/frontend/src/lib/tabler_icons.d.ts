declare module "@tabler/icons-react/dist/esm/icons/*.mjs" {
  import type { FC, SVGProps } from "react";

  const Icon: FC<SVGProps<SVGSVGElement> & { size?: number | string; stroke?: number | string }>;
  export default Icon;
}
