import { Fragment } from "react";

/** A strategy id that can wrap at its underscores (edge_finder_tight_v2), where the
 *  part that tells variants apart usually is, rather than being cut off at the end. */
export function Id({ name }: { name: string }) {
  const parts = name.split("_");
  return <>{parts.map((p, i) => <Fragment key={i}>{p}{i < parts.length - 1 && <>_<wbr /></>}</Fragment>)}</>;
}
