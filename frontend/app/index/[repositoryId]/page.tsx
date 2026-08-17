import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Empty, ErrorBanner, IndexRunStatusBadge, Stat, when } from "@/app/components";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ReindexDialog } from "../reindex-dialog";

export const dynamic = "force-dynamic";

export default async function IndexDetailPage({
  params,
}: {
  params: Promise<{ repositoryId: string }>;
}) {
  const { repositoryId } = await params;

  let repo;
  try {
    repo = await api.indexRepository(repositoryId);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    return (
      <>
        <h2 className="text-xl font-semibold">Index</h2>
        <ErrorBanner
          message={
            error instanceof ApiError
              ? `Cannot reach the API (${error.message}).`
              : String(error)
          }
        />
      </>
    );
  }

  const runs = await api.indexHistory(repositoryId).catch(() => []);

  return (
    <>
      <p className="mb-1.5 text-sm">
        <Link
          href="/index"
          className="inline-flex items-center gap-1 text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" />
          Index
        </Link>
      </p>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">
            {repo.repository_name}
          </h2>
          <p className="mt-1 font-mono text-xs text-muted-foreground">
            {repo.project}
          </p>
        </div>
        <div className="pt-1">
          <ReindexDialog
            repositoryId={repo.repository_id}
            repositoryName={repo.repository_name}
          />
        </div>
      </div>

      <div className="mt-5 mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Chunks" value={repo.chunk_count.toLocaleString()} />
        <Stat label="Files" value={repo.file_count.toLocaleString()} />
        <Stat
          label="Last indexed"
          value={repo.last_indexed_at ? when(repo.last_indexed_at) : "never"}
        />
        <Stat
          label="Last run"
          value={repo.last_run ? repo.last_run.status : "—"}
          note={repo.last_run?.error ?? undefined}
        />
      </div>

      <h3 className="mb-3 text-base font-semibold">Run history</h3>
      {runs.length === 0 ? (
        <Empty>No indexing runs yet. Trigger one with Re-index above.</Empty>
      ) : (
        <div className="overflow-hidden rounded-xl border border-border bg-card">
          <div className="max-h-[70vh] overflow-auto">
            <Table>
              <TableHeader className="sticky top-0 z-10 bg-card">
                <TableRow className="hover:bg-transparent">
                  <TableHead>Run</TableHead>
                  <TableHead>Branch</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Files</TableHead>
                  <TableHead>Changes</TableHead>
                  <TableHead>Triggered by</TableHead>
                  <TableHead>Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((run) => (
                  <TableRow key={run.run_id} className="h-12 align-top">
                    <TableCell className="font-mono text-xs">
                      {run.run_id.slice(0, 8)}
                      {run.commit_sha ? (
                        <div className="text-muted-foreground">
                          {run.commit_sha.slice(0, 8)}
                        </div>
                      ) : null}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {run.branch || "—"}
                      <div className="flex gap-1 text-[10px] text-muted-foreground">
                        {run.replace_existing ? <span>replace</span> : null}
                        {run.embed_requested ? (
                          <span>
                            embed{run.embed_effective ? "" : " (skipped)"}
                          </span>
                        ) : null}
                      </div>
                    </TableCell>
                    <TableCell>
                      <IndexRunStatusBadge status={run.status} />
                      {run.error ? (
                        <div className="mt-1 max-w-56 text-xs text-destructive">
                          {run.error}
                        </div>
                      ) : null}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs tabular-nums">
                      {run.files_collected ?? "—"}
                    </TableCell>
                    <TableCell className="font-mono text-xs whitespace-nowrap text-muted-foreground">
                      {run.written != null ? (
                        <>
                          <span className="text-foreground">{run.written}</span> written
                          {" · "}
                          {run.unchanged ?? 0} unchanged
                          {" · "}
                          {run.superseded ?? 0} superseded
                          {" · "}
                          {run.removed_files ?? 0} removed
                        </>
                      ) : (
                        "—"
                      )}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {run.triggered_by || "—"}
                    </TableCell>
                    <TableCell className="text-xs whitespace-nowrap text-muted-foreground">
                      {when(run.created_at)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      )}
    </>
  );
}
