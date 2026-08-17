"use client";

import { Fragment, useMemo, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { ReviewLogEvent } from "@/lib/api";
import { duration } from "@/app/components";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Empty } from "@/app/components";

/**
 * The log viewer.
 *
 * Spans nest via parent_span_id, so indentation is computed from the chain
 * rather than assumed from ordering - a parallel fan-out emits five sibling
 * spans whose completion order says nothing about their structure.
 */
export function LogViewer({ events }: { events: ReviewLogEvent[] }) {
  const [expanded, setExpanded] = useState<string | null>(null);

  const depths = useMemo(() => {
    const parents = new Map<string, string>();
    for (const event of events) {
      if (event.span_id) parents.set(event.span_id, event.parent_span_id);
    }
    const depthOf = (spanId: string): number => {
      let depth = 0;
      let cursor = parents.get(spanId);
      // The guard stops a malformed parent chain from hanging the render.
      while (cursor && depth < 12) {
        depth += 1;
        cursor = parents.get(cursor);
      }
      return depth;
    };
    return new Map(events.map((e) => [e.event_id, depthOf(e.span_id)]));
  }, [events]);

  if (events.length === 0) {
    return <Empty>No log events recorded.</Empty>;
  }

  const started = new Date(events[0].ts).getTime();

  return (
    <div className="overflow-hidden rounded-xl border border-border bg-card">
      <div className="max-h-[70vh] overflow-auto">
        <Table>
          <TableHeader className="sticky top-0 z-10 bg-card">
            <TableRow className="hover:bg-transparent">
              <TableHead className="w-16">+t</TableHead>
              <TableHead>Event</TableHead>
              <TableHead>Agent</TableHead>
              <TableHead>Model</TableHead>
              <TableHead>Duration</TableHead>
              <TableHead>Tokens</TableHead>
              <TableHead className="w-8" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {events.map((event) => {
              const offset = new Date(event.ts).getTime() - started;
              const depth = depths.get(event.event_id) ?? 0;
              const isOpen = expanded === event.event_id;
              const hasPayload = Object.keys(event.payload ?? {}).length > 0;

              return (
                // The key belongs on the Fragment: it, not the <tr>, is what
                // the map returns.
                <Fragment key={event.event_id}>
                  <TableRow className="h-10">
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {offset}ms
                    </TableCell>
                    <TableCell>
                      <span className="text-muted-foreground">
                        {depth > 0 ? "│ ".repeat(depth - 1) + "└ " : ""}
                      </span>
                      <span
                        className={
                          event.status === "error" ? "text-destructive" : ""
                        }
                      >
                        {event.event_type}
                      </span>
                    </TableCell>
                    <TableCell>{event.agent_type || "—"}</TableCell>
                    <TableCell className="font-mono text-xs">
                      {event.model || "—"}
                    </TableCell>
                    <TableCell className="text-xs">
                      {event.duration_ms ? duration(event.duration_ms) : "—"}
                    </TableCell>
                    <TableCell className="font-mono text-xs tabular-nums text-muted-foreground">
                      {event.input_tokens || event.output_tokens
                        ? `${event.input_tokens}/${event.output_tokens}`
                        : "—"}
                    </TableCell>
                    <TableCell>
                      {hasPayload ? (
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-xs"
                          aria-label={isOpen ? "Hide payload" : "Show payload"}
                          onClick={() =>
                            setExpanded(isOpen ? null : event.event_id)
                          }
                        >
                          {isOpen ? (
                            <ChevronDown className="size-3.5" />
                          ) : (
                            <ChevronRight className="size-3.5" />
                          )}
                        </Button>
                      ) : null}
                    </TableCell>
                  </TableRow>
                  {isOpen ? (
                    <TableRow className="hover:bg-transparent">
                      <TableCell colSpan={7} className="whitespace-normal bg-muted/40 py-2">
                        <pre className="overflow-x-auto rounded-md border border-border bg-muted p-3 font-mono text-xs">
                          <code>{JSON.stringify(event.payload, null, 2)}</code>
                        </pre>
                      </TableCell>
                    </TableRow>
                  ) : null}
                </Fragment>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
