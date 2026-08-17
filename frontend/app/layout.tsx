import type { Metadata } from "next";
import "./globals.css";
import { Toaster } from "@/components/ui/sonner";
import { AppNav } from "@/components/app-nav";
import { ShieldCheck } from "lucide-react";

export const metadata: Metadata = {
  title: "PR Review Agent",
  description: "Multi-agent pull-request review for Azure DevOps",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="font-sans antialiased">
        {/*
         * A fixed-height app shell, not a scrolling page: the app bar never
         * moves, and `main` is the one place that scrolls when a page's own
         * content doesn't manage a tighter scroll region itself (the board
         * does - see app/page.tsx - so only its table scrolls there).
         */}
        <div className="flex h-dvh flex-col overflow-hidden">
          <header className="z-20 flex h-14 shrink-0 items-center gap-6 border-b border-border bg-card px-6 shadow-sm">
            <div className="flex items-center gap-2">
              <span className="flex size-7 items-center justify-center rounded-lg bg-primary text-primary-foreground">
                <ShieldCheck className="size-4" />
              </span>
              <span className="text-sm font-semibold tracking-tight">
                PR Review Agent
              </span>
            </div>
            <AppNav />
          </header>
          <main className="min-h-0 flex-1 overflow-y-auto">
            <div className="mx-auto flex h-full max-w-[1800px] flex-col px-6 py-6 lg:px-10 xl:px-12">
              {children}
            </div>
          </main>
        </div>
        <Toaster position="bottom-right" />
      </body>
    </html>
  );
}
