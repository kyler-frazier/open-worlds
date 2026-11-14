import { useState } from "react";

// Google blocks OAuth inside embedded/in-app webviews ("disallowed_useragent").
// Detect the common ones so we can nudge users into a real browser.
function isEmbeddedWebview(): boolean {
  if (typeof navigator === "undefined") return false;
  const ua = navigator.userAgent || "";
  const rules = [
    "(iPhone|iPod|iPad)(?!.*Safari)", // iOS WKWebView lacks "Safari"
    "Android.*; wv", // Android System WebView
    "FBAN",
    "FBAV",
    "FB_IAB",
    "FBIOS",
    "Instagram",
    "Line/",
    "Twitter",
    "TikTok",
    "musical_ly",
    "BytedanceWebview",
    "Snapchat",
    "Discord",
    "MicroMessenger", // WeChat
    "GSA/", // Google Search App in-app browser
  ];
  return new RegExp(`(${rules.join("|")})`, "i").test(ua);
}

export default function WebviewNotice() {
  const [copied, setCopied] = useState(false);
  if (!isEmbeddedWebview()) return null;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard blocked; user can copy from the address bar */
    }
  };

  return (
    <div className="webview-warning">
      <strong>Open this page in your browser</strong>
      <p>
        Google sign-in won't work inside in-app browsers (Instagram, Discord,
        Messenger, etc.). Please open this link in Safari or Chrome to continue.
      </p>
      <button
        type="button"
        className="btn btn-secondary btn-block"
        onClick={copy}
      >
        {copied ? "Link copied — paste in your browser" : "Copy link"}
      </button>
    </div>
  );
}
