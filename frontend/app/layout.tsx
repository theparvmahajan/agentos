import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "AgentOS",
  description: "Watch AI agents plan, run and finish missions in real time",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="topbar">
          <div className="container topbar__inner">
            <Link href="/" className="brand">
              <span className="brand__mark" aria-hidden="true" />
              AgentOS
            </Link>
            <span className="muted small">live multi-agent runtime</span>
          </div>
        </header>
        <main className="container">{children}</main>
      </body>
    </html>
  );
}
