import { useState } from "react";
import { useSearchParams } from "react-router";

export default function PendingPage() {
  const [params] = useSearchParams();
  const sub = params.get("sub") ?? "";
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    await navigator.clipboard.writeText(sub);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="center-screen">
      <div className="card login-card">
        <div className="logo">⏳</div>
        <h1>Approval pending</h1>
        <p className="muted">
          Your account isn't approved yet. Send the ID below to an admin — once
          they add it to the allow-list, sign in again.
        </p>
        <div className="sub-box">
          <code>{sub || "unknown"}</code>
          <button className="btn btn-secondary" onClick={copy} disabled={!sub}>
            {copied ? "Copied!" : "Copy"}
          </button>
        </div>
        <a className="btn btn-primary btn-block" href="/">
          Back to sign in
        </a>
      </div>
    </div>
  );
}
