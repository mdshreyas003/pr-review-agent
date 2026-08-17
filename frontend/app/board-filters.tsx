"use client";

import { useTransition } from "react";
import { useRouter } from "next/navigation";
import { ChevronDown, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

/**
 * The board's only two filters: repository and author, each multi-select.
 * State lives in the URL (repeated `repo=` / `author=` params) so a filtered
 * view survives a reload and can be shared.
 */
export function BoardFilters({
  repositories,
  authors,
  selectedRepos,
  selectedAuthors,
}: {
  repositories: { repository_id: string; repository_name: string }[];
  authors: string[];
  selectedRepos: string[];
  selectedAuthors: string[];
}) {
  const router = useRouter();
  const [, startTransition] = useTransition();

  const apply = (repos: string[], authorList: string[]) => {
    const params = new URLSearchParams();
    for (const id of repos) params.append("repo", id);
    for (const author of authorList) params.append("author", author);
    const qs = params.toString();
    startTransition(() => router.replace(qs ? `/?${qs}` : "/"));
  };

  const toggle = (value: string, active: string[], other: string[], repoFirst: boolean) => {
    const next = active.includes(value)
      ? active.filter((v) => v !== value)
      : [...active, value];
    apply(repoFirst ? next : other, repoFirst ? other : next);
  };

  const hasFilters = selectedRepos.length > 0 || selectedAuthors.length > 0;

  return (
    <div className="flex flex-wrap items-center gap-2">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button type="button" variant="outline" size="sm" className="gap-1.5">
            Repository{selectedRepos.length > 0 ? ` (${selectedRepos.length})` : ""}
            <ChevronDown className="size-3.5 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="max-h-80">
          <DropdownMenuLabel>Filter by repository</DropdownMenuLabel>
          <DropdownMenuSeparator />
          {repositories.length === 0 ? (
            <div className="px-1.5 py-1 text-sm text-muted-foreground">None yet</div>
          ) : (
            repositories.map((r) => (
              <DropdownMenuCheckboxItem
                key={r.repository_id}
                checked={selectedRepos.includes(r.repository_id)}
                onCheckedChange={() =>
                  toggle(r.repository_id, selectedRepos, selectedAuthors, true)
                }
                onSelect={(e) => e.preventDefault()}
              >
                {r.repository_name}
              </DropdownMenuCheckboxItem>
            ))
          )}
        </DropdownMenuContent>
      </DropdownMenu>

      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button type="button" variant="outline" size="sm" className="gap-1.5">
            Author{selectedAuthors.length > 0 ? ` (${selectedAuthors.length})` : ""}
            <ChevronDown className="size-3.5 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="max-h-80">
          <DropdownMenuLabel>Filter by author</DropdownMenuLabel>
          <DropdownMenuSeparator />
          {authors.length === 0 ? (
            <div className="px-1.5 py-1 text-sm text-muted-foreground">None yet</div>
          ) : (
            authors.map((author) => (
              <DropdownMenuCheckboxItem
                key={author}
                checked={selectedAuthors.includes(author)}
                onCheckedChange={() =>
                  toggle(author, selectedAuthors, selectedRepos, false)
                }
                onSelect={(e) => e.preventDefault()}
              >
                {author}
              </DropdownMenuCheckboxItem>
            ))
          )}
        </DropdownMenuContent>
      </DropdownMenu>

      {hasFilters ? (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="gap-1 text-muted-foreground"
          onClick={() => apply([], [])}
        >
          <X className="size-3.5" />
          Clear
        </Button>
      ) : null}
    </div>
  );
}
