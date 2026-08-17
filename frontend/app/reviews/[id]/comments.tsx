"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import type { Comment } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent } from "@/components/ui/card";
import { Empty, when } from "@/app/components";
import { postComment } from "@/app/reviews/actions";

/**
 * Free-text comments on a review, posted straight to the real Azure DevOps
 * pull request as a new thread — not just kept inside the dashboard.
 */
export function Comments({
  reviewId,
  comments,
}: {
  reviewId: string;
  comments: Comment[];
}) {
  const router = useRouter();
  const [author, setAuthor] = useState("");
  const [content, setContent] = useState("");
  const [pending, startTransition] = useTransition();

  const submit = () => {
    startTransition(async () => {
      const result = await postComment(reviewId, author, content);
      if (result.ok) {
        toast.success(result.message);
        setContent("");
        router.refresh();
      } else {
        toast.error(result.message);
      }
    });
  };

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-wrap items-end gap-2">
            <div className="min-w-48 flex-1">
              <Label htmlFor="comment-author" className="mb-1 text-xs text-muted-foreground">
                Your name
              </Label>
              <Input
                id="comment-author"
                value={author}
                onChange={(e) => setAuthor(e.target.value)}
                placeholder="your name or email"
                disabled={pending}
              />
            </div>
          </div>
          <div>
            <Label htmlFor="comment-content" className="mb-1 text-xs text-muted-foreground">
              Comment
            </Label>
            <textarea
              id="comment-content"
              value={content}
              onChange={(e) => setContent(e.target.value)}
              placeholder="Posts as a new thread on the pull request in Azure DevOps…"
              disabled={pending}
              rows={3}
              className="w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-xs outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
            />
          </div>
          <div>
            <Button
              type="button"
              size="sm"
              onClick={submit}
              disabled={pending || !author.trim() || !content.trim()}
            >
              {pending ? "Posting…" : "Post to pull request"}
            </Button>
          </div>
        </CardContent>
      </Card>

      {comments.length === 0 ? (
        <Empty>No comments posted from the dashboard yet.</Empty>
      ) : (
        <Card>
          <CardContent className="flex flex-col gap-3 divide-y divide-border">
            {comments.map((comment) => (
              <div key={comment.comment_id} className="pt-3 first:pt-0">
                <div className="flex items-baseline gap-2 text-xs text-muted-foreground">
                  <span className="font-medium text-foreground">
                    {comment.author}
                  </span>
                  <span>{when(comment.created_at)}</span>
                  {comment.thread_id ? (
                    <span className="font-mono">thread {comment.thread_id}</span>
                  ) : null}
                </div>
                <p className="mt-1 text-sm whitespace-pre-wrap">{comment.content}</p>
              </div>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
