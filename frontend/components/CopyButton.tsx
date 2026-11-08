"use client";

import { useState } from "react";

/** Copies text to the clipboard and says so. Fails quietly where the clipboard is unavailable. */
export default function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard blocked: nothing to do
    }
  }
  return (
    <button type="button" className="button button--small" onClick={copy}>
      {copied ? "Copied" : label}
    </button>
  );
}
