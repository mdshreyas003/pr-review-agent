"use server";

import { revalidatePath } from "next/cache";
import { ApiError, api, post } from "@/lib/api";

export interface TaskResult {
  ok: boolean;
  message: string;
}

/**
 * Stop an in-flight review.
 *
 * Only offered for non-terminal pipeline states (open / processing /
 * awaiting_approval) — the board hides the button otherwise so this never
 * fires against a review that already finished.
 */
export async function cancelReview(reviewId: string): Promise<TaskResult> {
  try {
    await api.cancelReview(reviewId);
    revalidatePath("/");
    revalidatePath(`/reviews/${reviewId}`);
    return { ok: true, message: "Review cancelled." };
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    return { ok: false, message: String(error) };
  }
}

/** Queue a brand-new review for a pull request that already has a terminal one. */
export async function rerunReview(
  repositoryId: string,
  pullRequestId: number,
  reviewId?: string,
): Promise<TaskResult> {
  try {
    await api.rerunReview(repositoryId, pullRequestId);
    revalidatePath("/");
    if (reviewId) revalidatePath(`/reviews/${reviewId}`);
    return { ok: true, message: "Re-review queued." };
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    return { ok: false, message: String(error) };
  }
}

/** Post a free-text comment straight to the Azure DevOps pull request. */
export async function postComment(
  reviewId: string,
  author: string,
  content: string,
): Promise<TaskResult> {
  if (!author.trim()) return { ok: false, message: "Enter your name first." };
  if (!content.trim()) return { ok: false, message: "Write something to post." };

  try {
    await api.postComment(reviewId, author.trim(), content.trim());
    revalidatePath(`/reviews/${reviewId}`);
    return { ok: true, message: "Comment posted to the pull request." };
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    return { ok: false, message: String(error) };
  }
}

export interface DecisionResult {
  ok: boolean;
  message: string;
}

/**
 * Approve or reject a review the autonomy gate held back.
 *
 * Approval posts to Azure DevOps synchronously in the backend, so a failure
 * there must surface here rather than being reported as success - a reviewer
 * who is told "posted" and finds nothing on the pull request has been misled
 * about an irreversible action.
 */
export async function decide(
  reviewId: string,
  hitlId: string,
  decision: "approved" | "rejected",
  decidedBy: string,
  note: string,
): Promise<DecisionResult> {
  if (!decidedBy.trim()) {
    return { ok: false, message: "Enter who is making this decision." };
  }

  try {
    const result = await post<{ status: string; threads?: number[] }>(
      `/api/hitl/${hitlId}/decision`,
      { decision, decided_by: decidedBy.trim(), note: note.trim() },
    );
    revalidatePath(`/reviews/${reviewId}`);
    revalidatePath("/");
    return {
      ok: true,
      message:
        result.status === "posted"
          ? `Posted ${result.threads?.length ?? 0} comment thread(s) to the pull request.`
          : "Review rejected. Nothing was posted.",
    };
  } catch (error) {
    if (error instanceof ApiError) {
      if (error.status === 409) {
        revalidatePath(`/reviews/${reviewId}`);
        return { ok: false, message: "Someone else already decided this one." };
      }
      return { ok: false, message: error.message };
    }
    return { ok: false, message: String(error) };
  }
}
