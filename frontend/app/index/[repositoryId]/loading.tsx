import { Skeleton } from "@/components/ui/skeleton";
import { Card, CardContent } from "@/components/ui/card";

export default function IndexDetailLoading() {
  return (
    <>
      <Skeleton className="h-4 w-16" />
      <Skeleton className="mt-2 h-6 w-56" />
      <Skeleton className="mt-2 mb-6 h-3 w-40" />

      <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Card key={i} className="gap-1 py-4">
            <CardContent className="px-4">
              <Skeleton className="h-3 w-16" />
              <Skeleton className="mt-2 h-6 w-20" />
            </CardContent>
          </Card>
        ))}
      </div>

      <Skeleton className="mb-3 h-5 w-28" />
      <div className="overflow-hidden rounded-xl border border-border bg-card">
        <div className="h-10 border-b border-border bg-muted/30" />
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="flex h-12 items-center gap-4 border-b border-border px-3.5 last:border-0">
            <Skeleton className="h-4 w-16" />
            <Skeleton className="h-4 w-20" />
            <Skeleton className="h-5 w-20" />
            <Skeleton className="h-4 w-10" />
            <Skeleton className="h-4 w-10" />
          </div>
        ))}
      </div>
    </>
  );
}
