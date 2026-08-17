import Link from "next/link";
import { ChevronDown } from "lucide-react";
import type { Review } from "@/lib/api";
import { StatusBadge, when } from "@/app/components";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

/**
 * Every review this pull request has had, behind one dropdown rather than a
 * row of chips — a pull request re-reviewed a dozen times doesn't grow the
 * header sideways, it just grows the (scrollable) list.
 */
export function ReviewSwitcher({
  history,
  currentId,
}: {
  history: Review[];
  currentId: string;
}) {
  const current = history.find((r) => r.review_id === currentId) ?? history[0];

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="outline" size="sm" className="gap-1.5">
          {when(current.created_at)}
          <StatusBadge status={current.status} />
          {history.length > 1 ? (
            <span className="text-muted-foreground">{history.length} reviews</span>
          ) : null}
          <ChevronDown className="size-3.5 text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="max-h-80 min-w-64">
        <DropdownMenuLabel>Reviews for this pull request</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {history.map((r) => (
          <DropdownMenuItem key={r.review_id} asChild>
            <Link
              href={`/reviews/${r.review_id}`}
              className={cn(
                "flex items-center justify-between gap-3",
                r.review_id === currentId && "bg-accent",
              )}
            >
              <span className="text-muted-foreground">{when(r.created_at)}</span>
              <StatusBadge status={r.status} />
            </Link>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
