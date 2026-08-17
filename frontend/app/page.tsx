import { api, ApiError } from "@/lib/api";
import { ErrorBanner } from "./components";
import { BoardExplorer } from "./board-explorer";

export const dynamic = "force-dynamic";

function toList(value: string | string[] | undefined): string[] {
  if (!value) return [];
  return Array.isArray(value) ? value : [value];
}

export default async function BoardPage({
  searchParams,
}: {
  searchParams: Promise<{ repo?: string | string[]; author?: string | string[] }>;
}) {
  const params = await searchParams;
  const selectedRepos = toList(params.repo);
  const selectedAuthors = toList(params.author);

  let board;
  let filters;
  try {
    [board, filters] = await Promise.all([
      api.board(selectedRepos, selectedAuthors),
      api.boardFilters(),
    ]);
  } catch (error) {
    return (
      <ErrorBanner
        message={
          error instanceof ApiError
            ? `Cannot reach the API (${error.message}). Is the backend running?`
            : String(error)
        }
      />
    );
  }

  return (
    <BoardExplorer
      rows={board.pull_requests}
      total={board.total}
      repositories={filters.repositories}
      authors={filters.authors}
      selectedRepos={selectedRepos}
      selectedAuthors={selectedAuthors}
    />
  );
}
