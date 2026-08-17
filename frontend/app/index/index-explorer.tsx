"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { ChevronRight, DatabaseZap, Search, SearchX, X } from "lucide-react";
import type { IndexRepository } from "@/lib/api";
import { Empty, IndexRunStatusBadge, when } from "@/app/components";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ReindexDialog } from "./reindex-dialog";

/** Index toolbar + table — same shell as the board's explorer, so both list
 * pages read as one system. */
export function IndexExplorer({
  repositories,
}: {
  repositories: IndexRepository[];
}) {
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return repositories;
    return repositories.filter((repo) =>
      `${repo.repository_name} ${repo.project}`.toLowerCase().includes(q),
    );
  }, [repositories, query]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-4 flex shrink-0 flex-wrap items-center justify-between gap-3">
        <h2 className="text-xl font-semibold tracking-tight">
          Index
          <span className="ml-2 text-sm font-normal text-muted-foreground">
            {query.trim() ? `${filtered.length} of ${repositories.length}` : repositories.length}
          </span>
        </h2>
      </div>

      {repositories.length === 0 ? (
        <Empty icon={DatabaseZap}>
          No repositories known yet. They appear here once the poller has
          seen a pull request against them.
        </Empty>
      ) : (
        <>
          <div className="mb-4 flex shrink-0 flex-wrap items-center gap-2 rounded-xl border border-border bg-card px-3 py-2.5">
            <div className="relative min-w-56 flex-1">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search repositories…"
                className="pl-8"
                aria-label="Search repositories"
              />
            </div>
            {query.trim() ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="gap-1 text-muted-foreground"
                onClick={() => setQuery("")}
              >
                <X className="size-3.5" />
                Clear search
              </Button>
            ) : null}
          </div>

          {filtered.length === 0 ? (
            <Empty icon={SearchX}>No repositories match your search.</Empty>
          ) : (
            <div className="min-h-0 flex-1 overflow-hidden rounded-xl border border-border bg-card">
              <div className="h-full overflow-auto">
                <Table>
                  <TableHeader className="sticky top-0 z-10 bg-card">
                    <TableRow className="hover:bg-transparent">
                      <TableHead className="min-w-56">Repository</TableHead>
                      <TableHead className="text-right">Chunks</TableHead>
                      <TableHead className="text-right">Files</TableHead>
                      <TableHead>Last indexed</TableHead>
                      <TableHead>Last run</TableHead>
                      <TableHead />
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {filtered.map((repo) => (
                      <TableRow key={repo.repository_id} className="h-14">
                        <TableCell>
                          <Link
                            href={`/index/${repo.repository_id}`}
                            className="flex items-center gap-1 font-medium hover:text-primary hover:underline"
                          >
                            {repo.repository_name}
                            <ChevronRight className="size-3.5 text-muted-foreground" />
                          </Link>
                          <div className="font-mono text-xs text-muted-foreground">
                            {repo.project}
                          </div>
                        </TableCell>
                        <TableCell className="text-right font-mono text-sm tabular-nums">
                          {repo.chunk_count.toLocaleString()}
                        </TableCell>
                        <TableCell className="text-right font-mono text-sm tabular-nums">
                          {repo.file_count.toLocaleString()}
                        </TableCell>
                        <TableCell className="text-xs whitespace-nowrap text-muted-foreground">
                          {repo.last_indexed_at ? when(repo.last_indexed_at) : "never"}
                        </TableCell>
                        <TableCell>
                          {repo.last_run ? (
                            <IndexRunStatusBadge status={repo.last_run.status} />
                          ) : (
                            <span className="text-xs text-muted-foreground">—</span>
                          )}
                        </TableCell>
                        <TableCell className="text-right">
                          <ReindexDialog
                            repositoryId={repo.repository_id}
                            repositoryName={repo.repository_name}
                          />
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
