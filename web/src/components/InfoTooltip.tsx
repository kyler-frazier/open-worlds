import type { ReactNode } from "react";

interface Props {
  children: ReactNode;
  label?: string;
}

/** A small info icon that reveals a tooltip on hover/focus (keyboard accessible). */
export default function InfoTooltip({
  children,
  label = "More information",
}: Props) {
  return (
    <span className="info-tip" tabIndex={0} role="button" aria-label={label}>
      <span className="info-tip-icon" aria-hidden="true">
        i
      </span>
      <span className="info-tip-bubble" role="tooltip">
        {children}
      </span>
    </span>
  );
}
