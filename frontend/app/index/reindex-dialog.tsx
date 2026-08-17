"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { RefreshCw } from "lucide-react";
import { toast } from "sonner";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { triggerIndex } from "./actions";

export function ReindexDialog({
  repositoryId,
  repositoryName,
}: {
  repositoryId: string;
  repositoryName: string;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [branch, setBranch] = useState("");
  const [replace, setReplace] = useState(false);
  const [embed, setEmbed] = useState(true);
  const [pending, startTransition] = useTransition();

  const submit = () => {
    startTransition(async () => {
      const result = await triggerIndex(repositoryId, { branch, replace, embed });
      if (result.ok) {
        toast.success(result.message);
        setOpen(false);
        router.refresh();
      } else {
        toast.error(result.message);
      }
    });
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button type="button" variant="outline" size="sm">
          <RefreshCw className="size-3.5" />
          Re-index
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Re-index {repositoryName}</DialogTitle>
          <DialogDescription>
            Runs in the background. This never happens automatically — it
            only starts when you trigger it here.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <div>
            <Label htmlFor="branch" className="mb-1.5">
              Branch
            </Label>
            <Input
              id="branch"
              placeholder="default branch"
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
              disabled={pending}
            />
          </div>

          <div className="flex items-center justify-between gap-3 rounded-lg border border-border px-3 py-2.5">
            <div>
              <div className="text-sm font-medium">Replace existing index</div>
              <div className="text-xs text-muted-foreground">
                Discard prior chunks instead of diffing against them.
              </div>
            </div>
            <Switch checked={replace} onCheckedChange={setReplace} disabled={pending} />
          </div>

          <div className="flex items-center justify-between gap-3 rounded-lg border border-border px-3 py-2.5">
            <div>
              <div className="text-sm font-medium">Generate embeddings</div>
              <div className="text-xs text-muted-foreground">
                Skip to index files only, without embedding vectors.
              </div>
            </div>
            <Switch checked={embed} onCheckedChange={setEmbed} disabled={pending} />
          </div>
        </div>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => setOpen(false)} disabled={pending}>
            Cancel
          </Button>
          <Button type="button" onClick={submit} disabled={pending}>
            {pending ? "Queuing…" : "Start indexing"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
