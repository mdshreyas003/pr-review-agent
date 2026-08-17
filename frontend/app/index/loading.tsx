import { Skeleton } from "@/components/ui/skeleton";

export default function IndexLoading() {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <Skeleton className="mb-4 h-6 w-24 shrink-0" />

      <div className="min-h-0 flex-1 overflow-hidden rounded-xl border border-border bg-card">
        <div className="h-10 border-b border-border bg-muted/30" />
        {Array.from({ length: 6 }).map((_, i) => (
          <div
            key={i}
            className="flex h-14 items-center gap-4 border-b border-border px-3.5 last:border-0"
          >
            <div className="flex-1 space-y-1.5">
              <Skeleton className="h-4 w-48" />
              <Skeleton className="h-3 w-28" />
            </div>
            <Skeleton className="h-4 w-12" />
            <Skeleton className="h-4 w-12" />
            <Skeleton className="h-4 w-20" />
            <Skeleton className="h-5 w-20" />
            <Skeleton className="h-7 w-20" />
          </div>
        ))}
      </div>
    </div>
  );
}
