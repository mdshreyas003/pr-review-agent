/**
 * The one place the dashboard talks to the backend.
 *
 * Every call runs on the server (React Server Components), so the API base URL
 * is an internal address the browser never sees and never needs reachable.
 * That also means no API credentials are ever shipped to the client.
 */

const API_URL = process.env.API_URL ?? "http://localhost:8000";

export type ReviewStatus =
  | "queued"
  | "running"
  | "awaiting_approval"
  | "posted"
  | "rejected"
  | "failed"
  | "skipped"
  | "cancelled";

export type Severity = "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | "INFO";

export interface Review {
  review_id: string;
  project: string;
  repository_id: string;
  repository_name: string;
  pull_request_id: number;
  title: string;
  author: string;
  source_branch: string;
  target_branch: string;
  status: ReviewStatus;
  overall_confidence: number;
  requires_human: boolean;
  escalated: boolean;
  summary: string;
  duration_ms: number;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface Finding {
  finding_id: string;
  review_id: string;
  agent_type: string;
  severity: Severity;
  category: string;
  file_path: string;
  line_start: number;
  line_end: number;
  title: string;
  rationale: string;
  suggestion: string | null;
  confidence: number;
  citations: string[];
  merged_from: string[];
  posted: boolean;
  thread_id: number | null;
}

export interface ReviewLogEvent {
  ts: string;
  event_id: string;
  span_id: string;
  parent_span_id: string;
  agent_type: string;
  event_type: string;
  status: string;
  duration_ms: number;
  model: string;
  input_tokens: number;
  output_tokens: number;
  payload: Record<string, unknown>;
}

export interface HitlItem {
  hitl_id: string;
  review_id: string;
  reason: string;
  decision: string;
  created_at: string;
  project: string;
  repository_name: string;
  pull_request_id: number;
  title: string;
  author: string;
  overall_confidence: number;
  escalated: boolean;
  finding_count: number;
}

export type PipelineState =
  | "open"
  | "processing"
  | "awaiting_approval"
  | "posted"
  | "rejected"
  | "cancelled"
  | "failed";

export interface BoardRow {
  repository_id: string;
  pull_request_id: number;
  project: string;
  repository_name: string;
  title: string;
  description: string;
  author: string;
  source_branch: string;
  target_branch: string;
  is_draft: boolean;
  web_url: string;
  closed_at: string | null;
  first_seen_at: string;
  last_seen_at: string;
  last_commit_at: string;
  review_id: string | null;
  review_status: ReviewStatus | null;
  overall_confidence: number;
  escalated: boolean | null;
  summary: string | null;
  reviewed_at: string | null;
  pipeline_state: PipelineState;
  finding_count: number;
}

export interface Board {
  pull_requests: BoardRow[];
  total: number;
}

export interface BoardFilters {
  repositories: { repository_id: string; repository_name: string }[];
  authors: string[];
}

export interface Comment {
  comment_id: string;
  review_id: string;
  author: string;
  content: string;
  thread_id: number | null;
  created_at: string;
}

/** Manual, per-repository code-memory indexing. Never triggered automatically. */
export type IndexRunStatus = "queued" | "running" | "completed" | "failed";

export interface IndexLastRun {
  run_id: string;
  status: IndexRunStatus;
  branch: string;
  created_at: string;
  finished_at: string | null;
  error: string | null;
}

export interface IndexRepository {
  repository_id: string;
  project: string;
  repository_name: string;
  chunk_count: number;
  file_count: number;
  last_indexed_at: string | null;
  last_run: IndexLastRun | null;
}

export interface IndexRun {
  run_id: string;
  branch: string;
  commit_sha: string | null;
  replace_existing: boolean;
  embed_requested: boolean;
  embed_effective: boolean;
  status: IndexRunStatus;
  files_collected: number | null;
  written: number | null;
  unchanged: number | null;
  superseded: number | null;
  removed_files: number | null;
  error: string | null;
  triggered_by: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface TriggerIndexBody {
  project?: string;
  branch?: string;
  commit_sha?: string;
  replace?: boolean;
  embed?: boolean;
  triggered_by?: string;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function get<T>(path: string): Promise<T> {
  // no-store: this is an operations dashboard. A cached view of a queue that
  // someone is actively working is worse than a slightly slower page.
  const response = await fetch(`${API_URL}${path}`, { cache: "no-store" });
  if (!response.ok) {
    throw new ApiError(
      `GET ${path} failed: ${response.status} ${response.statusText}`,
      response.status,
    );
  }
  return response.json() as Promise<T>;
}

export const api = {
  board: (repositoryIds: string[] = [], authors: string[] = []) => {
    const params = new URLSearchParams({ limit: "200" });
    for (const id of repositoryIds) params.append("repo", id);
    for (const author of authors) params.append("author", author);
    return get<Board>(`/api/board?${params.toString()}`);
  },

  boardFilters: () => get<BoardFilters>("/api/board/filters"),

  listReviews: (status?: string) =>
    get<{ reviews: Review[] }>(
      `/api/reviews?limit=100${status ? `&status=${encodeURIComponent(status)}` : ""}`,
    ).then((r) => r.reviews),

  getReview: (id: string) =>
    get<{ review: Review; findings: Finding[] }>(`/api/reviews/${id}`),

  getLogs: (id: string) =>
    get<{ logs: ReviewLogEvent[] }>(`/api/reviews/${id}/logs`).then((r) => r.logs),

  // 200 is the backend's hard ceiling (Query(..., le=200)) - the queue has no
  // pagination yet, so this is the most this call can ever surface in one
  // page. A silent default of 50 here is what hid a real pending review
  // behind older, non-escalated ones once the queue passed 50 deep.
  listHitl: () =>
    get<{ queue: HitlItem[] }>("/api/hitl?limit=200").then((r) => r.queue),

  reviewHistory: (repositoryId: string, pullRequestId: number) =>
    get<{ reviews: Review[] }>(
      `/api/pull-requests/${repositoryId}/${pullRequestId}/reviews`,
    ).then((r) => r.reviews),

  /**
   * Stop an in-flight review. Only meaningful while a review is still
   * queued, running, or awaiting approval — terminal reviews use
   * rerunReview instead.
   */
  cancelReview: (reviewId: string) =>
    post<{ status: string }>(`/api/reviews/${reviewId}/cancel`, {}),

  /** Queue a fresh review for a pull request that already has a terminal one. */
  rerunReview: (repositoryId: string, pullRequestId: number) =>
    post<{ review_id: string; status: string }>(
      `/api/pull-requests/${repositoryId}/${pullRequestId}/review`,
      {},
    ),

  indexStatus: () =>
    get<{ repositories: IndexRepository[] }>("/api/index").then(
      (r) => r.repositories,
    ),

  indexRepository: (repositoryId: string) =>
    get<IndexRepository>(`/api/index/${repositoryId}`),

  indexHistory: (repositoryId: string) =>
    get<{ runs: IndexRun[] }>(`/api/index/${repositoryId}/runs`).then(
      (r) => r.runs,
    ),

  triggerIndex: (repositoryId: string, body: TriggerIndexBody) =>
    post<{ run_id: string; status: string }>(
      `/api/index/${repositoryId}`,
      body,
    ),

  listComments: (reviewId: string) =>
    get<{ comments: Comment[] }>(`/api/reviews/${reviewId}/comments`).then(
      (r) => r.comments,
    ),

  postComment: (reviewId: string, author: string, content: string) =>
    post<{ comment: Comment }>(`/api/reviews/${reviewId}/comments`, {
      author,
      content,
    }),
};

/** Server Actions post through here so the browser still never sees API_URL. */
export async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  const text = await response.text();
  if (!response.ok) {
    let detail = text;
    try {
      detail = (JSON.parse(text) as { detail?: string }).detail ?? text;
    } catch {
      /* not JSON; use the raw body */
    }
    throw new ApiError(detail, response.status);
  }
  return (text ? JSON.parse(text) : {}) as T;
}
