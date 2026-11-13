import type { HTMLAttributes } from "react";

export function Skeleton({ className = "", ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div aria-hidden="true" className={"rounded-md bg-secondary motion-safe:animate-pulse " + className} {...props} />;
}
