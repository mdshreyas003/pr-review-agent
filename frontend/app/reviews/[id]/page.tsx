import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { api, ApiError, type BoardRow } from "@/lib/api";
import {
  AuthorChip,
  Empty,
  FindingCard,
  Stat,
  StatusBadge,
  duration,
} from "@/app/components";
import { Card, CardContent } from "@/components/ui/card";
import { TaskActions } from "@/app/reviews/task-actions";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { LogViewer } from "./logs";
import { Comments } from "./comments";
import { DecisionForm } from "./decision-form";
import { ReviewSwitcher } from "./review-switcher";

export const dynamic = "force-dynamic";

export default async function ReviewDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  let data;
  try {
    data = await api.getReview(id);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }

  const { review, findings } = data;
  const logs = await api.getLogs(id).catch(() => []);
  const comments = await api.listComments(id).catch(() => []);

  // Every review this pull request has ever had, most recent first — the
  // switcher below lets a reviewer jump between them without leaving the page.
  const history = await api
    .reviewHistory(review.repository_id, review.pull_request_id)
    .catch(() => [review]);

  const hitl =
    review.status === "awaiting_approval"
      ? await api
          .listHitl()
          .then((queue) => queue.find((item) => item.review_id === id))
          .catch(() => undefined)
      : undefined;

  // TaskActions expects a BoardRow shape; a review detail page has enough of
  // one to drive Cancel / Re-review without a second fetch.
  const asBoardRow: BoardRow = {
    repository_id: review.repository_id,
    pull_request_id: review.pull_request_id,
    project: review.project,
    repository_name: review.repository_name,
    title: review.title,
    description: "",
    author: review.author,
    source_branch: review.source_branch,
    target_branch: review.target_branch,
    is_draft: false,
    web_url: "",
    closed_at: null,
    first_seen_at: review.created_at,
    last_seen_at: review.updated_at,
    last_commit_at: review.updated_at,
    review_id: review.review_id,
    review_status: review.status,
    overall_confidence: review.overall_confidence,
    escalated: review.escalated,
    summary: review.summary,
    reviewed_at: review.updated_at,
    pipeline_state: statusToPipelineState(review.status),
    finding_count: findings.length,
  };

  return (
    <>
      <p className="mb-1.5 text-sm">
        <Link
          href="/"
          className="inline-flex items-center gap-1 text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" />
          Pull requests
        </Link>
      </p>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">
            {review.repository_name} !{review.pull_request_id}
          </h2>
          <div className="mt-1.5 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <span>{review.title}</span>
            <span>&middot;</span>
            <AuthorChip name={review.author} />
            <span>&middot;</span>
            <code className="font-mono text-xs">
              {review.source_branch} &rarr; {review.target_branch}
            </code>
          </div>
        </div>
        <div className="flex items-center gap-2 pt-1">
          {history.length > 1 ? (
            <ReviewSwitcher history={history} currentId={id} />
          ) : null}
          <TaskActions row={asBoardRow} />
        </div>
      </div>

      <div className="mt-5 mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Status" value={review.status.replace(/_/g, " ")} />
        <Stat
          label="Confidence"
          value={review.overall_confidence.toFixed(2)}
          note={review.requires_human ? "held for a human" : "auto-posted"}
        />
        <Stat label="Findings" value={findings.length} />
        <Stat label="Duration" value={duration(review.duration_ms)} />
      </div>

      {review.error ? (
        <div className="mb-4 rounded-lg border border-destructive/40 bg-destructive/10 px-3.5 py-3 text-sm text-destructive">
          <strong>Failed:</strong> {review.error}
        </div>
      ) : null}

      <Tabs defaultValue="overview">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="logs">Logs</TabsTrigger>
          <TabsTrigger value="comments">
            Comments{comments.length > 0 ? ` (${comments.length})` : ""}
          </TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="mt-4">
          {hitl ? (
            <DecisionForm
              reviewId={review.review_id}
              hitlId={hitl.hitl_id}
              reason={hitl.reason}
            />
          ) : null}

          <Card className="mb-6">
            <CardContent className="flex flex-col gap-2">
              <div className="flex items-center gap-2.5">
                <StatusBadge status={review.status} />
                <strong className="text-sm font-semibold">Summary</strong>
              </div>
              <p className="text-sm">{review.summary || "No summary recorded."}</p>
            </CardContent>
          </Card>

          <h3 className="mb-3 text-base font-semibold">Findings</h3>
          {findings.length === 0 ? (
            <Empty>
              No findings. The changed lines looked clean to all five specialists.
            </Empty>
          ) : (
            <Card>
              <CardContent>
                {findings.map((finding) => (
                  <FindingCard key={finding.finding_id} finding={finding} />
                ))}
              </CardContent>
            </Card>
          )}
        </TabsContent>

        <TabsContent value="logs" className="mt-4">
          <LogViewer events={logs} />
        </TabsContent>

        <TabsContent value="comments" className="mt-4">
          <Comments reviewId={review.review_id} comments={comments} />
        </TabsContent>
      </Tabs>
    </>
  );
}

function statusToPipelineState(status: string): BoardRow["pipeline_state"] {
  switch (status) {
    case "queued":
    case "running":
      return "processing";
    case "awaiting_approval":
      return "awaiting_approval";
    case "posted":
      return "posted";
    case "rejected":
      return "rejected";
    case "cancelled":
      return "cancelled";
    default:
      return "failed";
  }
}
