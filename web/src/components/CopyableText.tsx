import { useEffect, useRef, useState } from "react";

interface Props {
  /** The text shown and copied. */
  value: string;
  /** Announced to screen readers and used as the tooltip. */
  label?: string;
  className?: string;
}

/**
 * Text that copies itself when clicked. A connect address exists to be pasted,
 * and selecting text on a phone is fiddly, so the whole element is the target,
 * not just the icon.
 */
export default function CopyableText({
  value,
  label = "Copy",
  className,
}: Props) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  // Clearing on unmount: the card re-renders on every poll, and a pending
  // timer firing into an unmounted component sets state on nothing.
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const copy = async () => {
    window.clearTimeout(timer.current);
    try {
      // Requires a secure context. Everything is served over HTTPS, but a
      // failure here is silent otherwise, and silently doing nothing on click
      // is worse than saying so.
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setFailed(false);
    } catch {
      setFailed(true);
      setCopied(false);
    }
    timer.current = window.setTimeout(() => {
      setCopied(false);
      setFailed(false);
    }, 1600);
  };

  return (
    <button
      type="button"
      className={`copyable ${className ?? ""}`}
      onClick={() => void copy()}
      title={failed ? "Could not copy" : copied ? "Copied" : label}
      aria-label={`${label}: ${value}`}
    >
      <span className="copyable-text mono">{value}</span>
      <span className="copyable-icon" aria-hidden="true">
        {copied ? "✓" : failed ? "✕" : "⧉"}
      </span>
      {/* Announced on change so the copy is confirmed without sight of the icon. */}
      <span className="sr-only" role="status">
        {copied ? "Copied to clipboard" : failed ? "Copy failed" : ""}
      </span>
    </button>
  );
}
