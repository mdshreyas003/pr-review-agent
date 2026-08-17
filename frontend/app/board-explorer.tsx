"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { ChevronDown, Inbox, Search, SearchX, X } from "lucide-react";
import type { BoardRow, PipelineState } from "@/lib/api";
import {
  AuthorChip,
  Empty,
  PipelineStateBadge,
  STATE_LABEL,
  when,
} from "./components";
import { BoardFilters } from "./board-filters";
import { BoardRowActions } from "./board-row-actions";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

const ALL_STATES = Object.keys(STATE_LABEL) as PipelineState[];

function StateCell({ row }: { row: BoardRow }) {
  return (
    <div className="flex flex-col items-start gap-1">
      <PipelineStateBadge state={row.pipeline_state} />
      {row.escalated ? (
        <span className="text-[11px] font-medium text-destructive">
          escalated
        </span>
      ) : row.review_status && row.pipeline_state !== "open" ? (
        <span className="text-[11px] text-muted-foreground">
          {row.finding_count} finding{row.finding_count === 1 ? "" : "s"}
        </span>
      ) : null}
    </div>
  );
}

function matches(row: BoardRow, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  const haystack = [
    row.title,
    row.author,
    row.repository_name,
    row.source_branch,
    row.target_branch,
    `!${row.pull_request_id}`,
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(q);
}

/**
 * Board toolbar + table. Repository/author filters stay URL-driven (server
 * refetch, shareable links) — this component layers free-text search and a
 * status filter on top, both instant since the board already fetches its
 * full (200-row) page up front.
 */
export function BoardExplorer({
  rows,
  total,
  repositories,
  authors,
  selectedRepos,
  selectedAuthors,
}: {
  rows: BoardRow[];
  total: number;
  repositories: { repository_id: string; repository_name: string }[];
  authors: string[];
  selectedRepos: string[];
  selectedAuthors: string[];
}) {
  const [query, setQuery] = useState("");
  const [states, setStates] = useState<PipelineState[]>([]);

  const filtered = useMemo(
    () =>
      rows.filter(
        (row) =>
          matches(row, query) &&
          (states.length === 0 || states.includes(row.pipeline_state)),
      ),
    [rows, query, states],
  );

  const hasLocalFilters = query.trim().length > 0 || states.length > 0;
  const hasAnyFilters =
    hasLocalFilters || selectedRepos.length > 0 || selectedAuthors.length > 0;

  const toggleState = (state: PipelineState) => {
    setStates((prev) =>
      prev.includes(state) ? prev.filter((s) => s !== state) : [...prev, state],
    );
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-4 flex shrink-0 flex-wrap items-center justify-between gap-3">
        <h2 className="text-xl font-semibold tracking-tight">
          Pull requests
          <span className="ml-2 text-sm font-normal text-muted-foreground">
            {hasLocalFilters ? `${filtered.length} of ${total}` : total}
          </span>
        </h2>
      </div>

      <div className="mb-4 flex shrink-0 flex-wrap items-center gap-2 rounded-xl border border-border bg-card px-3 py-2.5">
        <div className="relative min-w-56 flex-1">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search by title, author, repository, branch…"
            className="pl-8"
            aria-label="Search pull requests"
          />
        </div>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button type="button" variant="outline" size="sm" className="gap-1.5">
              State{states.length > 0 ? ` (${states.length})` : ""}
              <ChevronDown className="size-3.5 text-muted-foreground" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            <DropdownMenuLabel>Filter by state</DropdownMenuLabel>
            <DropdownMenuSeparator />
            {ALL_STATES.map((state) => (
              <DropdownMenuCheckboxItem
                key={state}
                checked={states.includes(state)}
                onCheckedChange={() => toggleState(state)}
                onSelect={(e) => e.preventDefault()}
              >
                {STATE_LABEL[state]}
              </DropdownMenuCheckboxItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>

        <BoardFilters
          repositories={repositories}
          authors={authors}
          selectedRepos={selectedRepos}
          selectedAuthors={selectedAuthors}
        />

        {hasAnyFilters ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="gap-1 text-muted-foreground"
            onClick={() => {
              setQuery("");
              setStates([]);
            }}
          >
            <X className="size-3.5" />
            Clear search
          </Button>
        ) : null}
      </div>

      {rows.length === 0 ? (
        <Empty icon={Inbox}>
          {selectedRepos.length || selectedAuthors.length
            ? "Nothing matches these filters."
            : "Nothing needs attention right now."}
        </Empty>
      ) : filtered.length === 0 ? (
        <Empty icon={SearchX}>
          No pull requests match your search.
        </Empty>
      ) : (
        <div className="min-h-0 flex-1 overflow-hidden rounded-xl border border-border bg-card">
          <div className="h-full overflow-auto">
            <Table>
              <TableHeader className="sticky top-0 z-10 bg-card">
                <TableRow className="hover:bg-transparent">
                  <TableHead className="min-w-72">Pull request</TableHead>
                  <TableHead>Repository</TableHead>
                  <TableHead>Author</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead className="text-right">Conf.</TableHead>
                  <TableHead>Last commit</TableHead>
                  <TableHead className="w-10" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {filtered.map((row) => (
                  <TableRow key={`${row.repository_id}-${row.pull_request_id}`}>
                    <TableCell className="max-w-md py-2.5">
                      <div className="flex min-w-0 items-baseline gap-2">
                        <span className="shrink-0 font-mono text-xs text-muted-foreground">
                          !{row.pull_request_id}
                        </span>
                        {row.review_id ? (
                          <Link
                            href={`/reviews/${row.review_id}`}
                            className="truncate font-medium text-primary hover:underline"
                          >
                            {row.title}
                          </Link>
                        ) : (
                          <span className="truncate font-medium">{row.title}</span>
                        )}
                        {row.is_draft ? (
                          <Badge
                            variant="outline"
                            className="shrink-0 text-[10px] uppercase"
                          >
                            draft
                          </Badge>
                        ) : null}
                      </div>
                    </TableCell>
                    <TableCell className="py-2.5 font-mono text-xs text-muted-foreground">
                      {row.repository_name}
                    </TableCell>
                    <TableCell className="py-2.5">
                      <AuthorChip name={row.author} />
                    </TableCell>
                    <TableCell className="py-2.5">
                      <StateCell row={row} />
                    </TableCell>
                    <TableCell className="py-2.5 text-right font-mono text-sm tabular-nums">
                      {row.review_id ? row.overall_confidence.toFixed(2) : "—"}
                    </TableCell>
                    <TableCell className="py-2.5 text-xs whitespace-nowrap text-muted-foreground">
                      {when(row.last_commit_at)}
                    </TableCell>
                    <TableCell className="py-2.5 text-right">
                      <BoardRowActions row={row} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      )}
    </div>
  );
}
