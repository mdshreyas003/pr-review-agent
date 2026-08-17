import { api, ApiError } from "@/lib/api";
import { ErrorBanner } from "@/app/components";
import { IndexExplorer } from "./index-explorer";

export const dynamic = "force-dynamic";

export default async function IndexPage() {
  let repositories;
  try {
    repositories = await api.indexStatus();
  } catch (error) {
    return (
      <ErrorBanner
        message={
          error instanceof ApiError
            ? `Cannot reach the API (${error.message}).`
            : String(error)
        }
      />
    );
  }

  return <IndexExplorer repositories={repositories} />;
}
