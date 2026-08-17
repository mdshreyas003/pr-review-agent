import { Skeleton } from "@/components/ui/skeleton";
import { Card, CardContent } from "@/components/ui/card";

export default function ReviewDetailLoading() {
  return (
    <>
      <Skeleton className="h-4 w-20" />
      <Skeleton className="mt-2 h-6 w-64" />
      <Skeleton className="mt-2 mb-6 h-4 w-96 max-w-full" />

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

      <Card className="mb-6">
        <CardContent className="space-y-2">
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-4 w-full" />
        </CardContent>
      </Card>

      <Skeleton className="mb-3 h-5 w-24" />
      <Card>
        <CardContent className="space-y-3">
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
        </CardContent>
      </Card>
    </>
  );
}
