/** Small presentational pieces shared across pages. */

import type { Finding, IndexRunStatus, PipelineState, Severity } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";

const SEVERITY_VAR: Record<Severity, string> = {
  CRITICAL: "--severity-critical",
  HIGH: "--severity-high",
  MEDIUM: "--severity-medium",
  LOW: "--severity-low",
  INFO: "--severity-info",
};

const STATE_VAR: Record<PipelineState, string> = {
  open: "--state-open",
  processing: "--state-processing",
  awaiting_approval: "--state-awaiting-approval",
  posted: "--state-posted",
  rejected: "--state-rejected",
  cancelled: "--state-cancelled",
  failed: "--state-failed",
};

/** A badge tinted from a CSS custom property rather than a Tailwind palette
 * color — severity and pipeline-state colors are a separate design-token
 * ramp from the accent, so neither dilutes the other. */
function TintedBadge({
  cssVar,
  children,
}: {
  cssVar: string;
  children: React.ReactNode;
}) {
  return (
    <Badge
      variant="outline"
      className="border-[color-mix(in_oklch,var(--v)_35%,transparent)] font-semibold uppercase tracking-wide text-[var(--v)]"
      style={
        {
          "--v": `var(${cssVar})`,
          backgroundColor: "color-mix(in oklch, var(--v) 12%, transparent)",
        } as React.CSSProperties
      }
    >
      {children}
    </Badge>
  );
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  return <TintedBadge cssVar={SEVERITY_VAR[severity]}>{severity}</TintedBadge>;
}

export const STATE_LABEL: Record<PipelineState, string> = {
  open: "Open",
  processing: "Processing",
  awaiting_approval: "Needs approval",
  posted: "Posted",
  rejected: "Rejected",
  cancelled: "Cancelled",
  failed: "Failed",
};

export function PipelineStateBadge({ state }: { state: PipelineState }) {
  return (
    <TintedBadge cssVar={STATE_VAR[state]}>{STATE_LABEL[state]}</TintedBadge>
  );
}

const RUN_STATUS_VAR: Record<IndexRunStatus, string> = {
  queued: "--state-open",
  running: "--state-processing",
  completed: "--state-posted",
  failed: "--state-failed",
};

export function IndexRunStatusBadge({ status }: { status: IndexRunStatus }) {
  return <TintedBadge cssVar={RUN_STATUS_VAR[status]}>{status}</TintedBadge>;
}

export function StatusBadge({ status }: { status: string }) {
  const normalized = status.replace(/_/g, " ");
  const asState = (
    ["open", "processing", "awaiting_approval", "posted", "rejected", "cancelled", "failed"] as const
  ).includes(status as PipelineState)
    ? (status as PipelineState)
    : null;
  if (asState) return <PipelineStateBadge state={asState} />;
  return (
    <Badge variant="secondary" className="uppercase tracking-wide">
      {normalized}
    </Badge>
  );
}

export function Stat({
  label,
  value,
  note,
}: {
  label: string;
  value: string | number;
  note?: string;
}) {
  return (
    <Card className="gap-1 py-4">
      <CardContent className="px-4">
        <div className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
          {label}
        </div>
        <div className="mt-1 font-mono text-2xl font-semibold tabular-nums">
          {value}
        </div>
        {note ? (
          <div className="mt-0.5 text-xs text-muted-foreground">{note}</div>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function Empty({
  children,
  icon: Icon,
}: {
  children: React.ReactNode;
  icon?: React.ComponentType<{ className?: string }>;
}) {
  return (
    <Card>
      <CardContent className="flex flex-col items-center gap-2 py-14 text-center text-sm text-muted-foreground">
        {Icon ? <Icon className="size-6 text-muted-foreground/60" /> : null}
        {children}
      </CardContent>
    </Card>
  );
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

/** Small initials-in-circle + name, so an author is a person, not just a
 * string in a column — used sparingly, only where an author is a first-class
 * detail (the board's author cell, a review's header). */
export function AuthorChip({ name }: { name: string }) {
  if (!name) return <span className="text-sm text-muted-foreground">—</span>;
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-secondary text-[10px] font-semibold text-secondary-foreground">
        {initials(name)}
      </span>
      <span className="truncate text-sm">{name}</span>
    </span>
  );
}

export function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="mb-4 rounded-lg border border-destructive/40 bg-destructive/10 px-3.5 py-3 text-sm text-destructive">
      {message}
    </div>
  );
}

export function duration(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

export function when(iso: string): string {
  const then = new Date(iso).getTime();
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function FindingCard({ finding }: { finding: Finding }) {
  const agents = [finding.agent_type, ...finding.merged_from].sort();
  const location = finding.file_path
    ? `${finding.file_path}:${finding.line_start}`
    : "pull request";

  return (
    <div
      className="border-l-2 py-1 pl-3.5 not-last:mb-4"
      style={{ borderColor: `var(${SEVERITY_VAR[finding.severity]})` }}
    >
      <div className="flex flex-wrap items-baseline gap-2.5">
        <SeverityBadge severity={finding.severity} />
        <strong className="text-sm font-semibold">{finding.title}</strong>
        <code className="font-mono text-xs text-muted-foreground">
          {location}
        </code>
      </div>
      <div className="mt-1.5 text-sm">{finding.rationale}</div>
      {finding.suggestion ? (
        <pre className="mt-2 overflow-x-auto rounded-md border border-border bg-muted px-3 py-2.5 font-mono text-xs">
          <code>{finding.suggestion}</code>
        </pre>
      ) : null}
      <div className="mt-1.5 text-xs text-muted-foreground">
        {finding.category} &middot; agent {agents.join(" + ")} &middot;
        confidence {finding.confidence.toFixed(2)}
        {finding.citations.length > 0
          ? ` · grounded in ${finding.citations.length} chunk(s)`
          : " · ungrounded"}
        {finding.posted
          ? ` · posted${finding.thread_id ? ` (thread ${finding.thread_id})` : ""}`
          : " · not posted"}
      </div>
    </div>
  );
}

export function cardClass(...classes: (string | undefined | false)[]) {
  return cn(...classes);
}
