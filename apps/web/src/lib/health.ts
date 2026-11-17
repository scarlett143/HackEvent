export type HealthStatus = "red" | "amber" | "green";
type Row = { url: string; kind: string; adapter: string; status: HealthStatus };

/** Admin source-health filter: status chip + free-text search over url/kind/adapter. */
export function filterHealth<T extends Row>(rows: T[], status: "all" | HealthStatus, query: string): T[] {
  const q = query.trim().toLowerCase();
  return rows.filter(
    (h) =>
      (status === "all" || h.status === status) &&
      (!q || `${h.url} ${h.kind} ${h.adapter}`.toLowerCase().includes(q)),
  );
}
