"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import type { BoardRow, PipelineState } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { cancelReview, rerunReview } from "./actions";

const CANCELLABLE: readonly PipelineState[] = [
  "open",
  "processing",
  "awaiting_approval",
];

/**
 * Row-level Cancel / Re-review control for the board.
 *
 * Cancel is a stop-action on a running job, so it sits behind a confirm
 * dialog rather than firing on a single click. Re-review has no undo either,
 * but it only creates work rather than destroying it, so it stays one click.
 */
export function TaskActions({ row }: { row: BoardRow }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [open, setOpen] = useState(false);

  if (!row.review_id) return null;

  const cancellable = CANCELLABLE.includes(row.pipeline_state);

  const doCancel = () => {
    startTransition(async () => {
      const result = await cancelReview(row.review_id as string);
      if (result.ok) {
        toast.success(result.message);
        setOpen(false);
        router.refresh();
      } else {
        toast.error(result.message);
      }
    });
  };

  const doRerun = () => {
    startTransition(async () => {
      const result = await rerunReview(
        row.repository_id,
        row.pull_request_id,
        row.review_id ?? undefined,
      );
      if (result.ok) {
        toast.success(result.message);
        router.refresh();
      } else {
        toast.error(result.message);
      }
    });
  };

  if (cancellable) {
    return (
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="text-muted-foreground hover:text-destructive"
          >
            Cancel
          </Button>
        </DialogTrigger>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Cancel this review?</DialogTitle>
            <DialogDescription>
              !{row.pull_request_id} in{" "}
              <code className="font-mono">{row.repository_name}</code> will
              stop processing. This can&rsquo;t be undone, but you can trigger
              a fresh review afterwards.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setOpen(false)}
              disabled={pending}
            >
              Keep going
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={doCancel}
              disabled={pending}
            >
              {pending ? "Cancelling…" : "Cancel review"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      onClick={doRerun}
      disabled={pending}
      className="text-muted-foreground hover:text-foreground"
    >
      {pending ? "Queuing…" : "Re-review"}
    </Button>
  );
}
