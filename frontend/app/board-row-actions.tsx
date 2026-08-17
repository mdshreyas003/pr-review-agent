"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { ExternalLink, MoreHorizontal } from "lucide-react";
import type { BoardRow, PipelineState } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cancelReview, rerunReview } from "./reviews/actions";

const CANCELLABLE: readonly PipelineState[] = [
  "open",
  "processing",
  "awaiting_approval",
];

/**
 * Row-level menu for the board: Cancel / Re-review / open in Azure DevOps,
 * behind one kebab button instead of three competing links stacked in the
 * cell.
 */
export function BoardRowActions({ row }: { row: BoardRow }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [confirmOpen, setConfirmOpen] = useState(false);

  const cancellable = row.review_id && CANCELLABLE.includes(row.pipeline_state);
  const rerunnable = row.review_id && !cancellable;

  if (!row.review_id && !row.web_url) return null;

  const doCancel = () => {
    startTransition(async () => {
      const result = await cancelReview(row.review_id as string);
      if (result.ok) {
        toast.success(result.message);
        setConfirmOpen(false);
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

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Row actions"
            className="text-muted-foreground"
          >
            <MoreHorizontal className="size-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          {cancellable ? (
            <DropdownMenuItem
              variant="destructive"
              onSelect={(e) => {
                e.preventDefault();
                setConfirmOpen(true);
              }}
            >
              Cancel review
            </DropdownMenuItem>
          ) : null}
          {rerunnable ? (
            <DropdownMenuItem disabled={pending} onSelect={doRerun}>
              Re-review
            </DropdownMenuItem>
          ) : null}
          {row.web_url ? (
            <DropdownMenuItem asChild>
              <a href={row.web_url} target="_blank" rel="noreferrer">
                Open in Azure DevOps
                <ExternalLink className="ml-auto size-3.5" />
              </a>
            </DropdownMenuItem>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
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
              onClick={() => setConfirmOpen(false)}
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
    </>
  );
}
