import type { ComponentType, SVGProps } from "react";

export type IconProps = SVGProps<SVGSVGElement> & {
  size?: number | string;
  stroke?: number | string;
};

export type Icon = ComponentType<IconProps>;
