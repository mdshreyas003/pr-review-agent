"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Card, CardContent } from "@/components/ui/card";
import { decide } from "@/app/reviews/actions";

/**
 * Approve / reject a review the autonomy gate held back, inline on its own
 * detail page — no separate approvals queue to jump to.
 */
export function DecisionForm({
  reviewId,
  hitlId,
  reason,
}: {
  reviewId: string;
  hitlId: string;
  reason: string;
}) {
  const router = useRouter();
  const [decidedBy, setDecidedBy] = useState("");
  const [note, setNote] = useState("");
  const [pending, startTransition] = useTransition();

  const submit = (decision: "approved" | "rejected") => {
    startTransition(async () => {
      const result = await decide(reviewId, hitlId, decision, decidedBy, note);
      if (result.ok) {
        toast.success(result.message);
        router.refresh();
      } else {
        toast.error(result.message);
      }
    });
  };

  return (
    <Card className="mb-4 border-[color-mix(in_oklch,var(--state-awaiting-approval)_35%,transparent)]">
      <CardContent className="flex flex-col gap-3">
        <div>
          <div className="text-sm font-semibold">Held for approval</div>
          <p className="mt-0.5 text-[13px] text-muted-foreground">
            {reason}
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <div className="min-w-48 flex-1">
            <Label htmlFor={`by-${hitlId}`} className="mb-1 text-xs text-muted-foreground">
              Decided by
            </Label>
            <Input
              id={`by-${hitlId}`}
              type="text"
              placeholder="your name or email"
              value={decidedBy}
              onChange={(e) => setDecidedBy(e.target.value)}
              disabled={pending}
            />
          </div>
          <div className="min-w-48 flex-1">
            <Label htmlFor={`note-${hitlId}`} className="mb-1 text-xs text-muted-foreground">
              Note (optional)
            </Label>
            <Input
              id={`note-${hitlId}`}
              type="text"
              placeholder="note"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              disabled={pending}
            />
          </div>
          <Button
            type="button"
            onClick={() => submit("approved")}
            disabled={pending || !decidedBy.trim()}
          >
            {pending ? "Working…" : "Approve & post"}
          </Button>
          <Button
            type="button"
            variant="destructive"
            onClick={() => submit("rejected")}
            disabled={pending || !decidedBy.trim()}
          >
            Reject
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
