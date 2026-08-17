import { Skeleton } from "@/components/ui/skeleton";

export default function BoardLoading() {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-4 flex shrink-0 items-center justify-between gap-3">
        <Skeleton className="h-6 w-40" />
        <div className="flex gap-2">
          <Skeleton className="h-8 w-28" />
          <Skeleton className="h-8 w-24" />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-hidden rounded-xl border border-border bg-card">
        <div className="h-10 border-b border-border bg-muted/30" />
        {Array.from({ length: 8 }).map((_, i) => (
          <div
            key={i}
            className="flex h-[52px] items-center gap-4 border-b border-border px-3.5 last:border-0"
          >
            <div className="flex-1 space-y-2">
              <Skeleton className="h-4 w-64 max-w-full" />
            </div>
            <Skeleton className="h-5 w-24" />
            <Skeleton className="hidden h-4 w-12 sm:block" />
            <Skeleton className="hidden h-4 w-16 sm:block" />
          </div>
        ))}
      </div>
    </div>
  );
}
