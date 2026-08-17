"use server";

import { revalidatePath } from "next/cache";
import { ApiError, api } from "@/lib/api";

export interface TriggerResult {
  ok: boolean;
  message: string;
  runId?: string;
}

/**
 * Manually trigger a code-index run for one repository.
 *
 * Indexing always runs in the background — this only queues it. A failure
 * shows up later on the repository's status (status: "failed", error set),
 * not here, so a 201 just means "accepted", not "done".
 */
export async function triggerIndex(
  repositoryId: string,
  body: { branch?: string; replace: boolean; embed: boolean },
): Promise<TriggerResult> {
  try {
    const result = await api.triggerIndex(repositoryId, {
      branch: body.branch?.trim() || undefined,
      replace: body.replace,
      embed: body.embed,
    });
    revalidatePath("/index");
    revalidatePath(`/index/${repositoryId}`);
    return {
      ok: true,
      message: `Indexing queued (run ${result.run_id.slice(0, 8)}…).`,
      runId: result.run_id,
    };
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    return { ok: false, message: String(error) };
  }
}
