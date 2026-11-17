import { api } from "./api";
import { type EventItem, eventsQuery, type Page } from "./events";

const MAX_PAGES = 5;

/** Every event the feed would show, up to 5 pages (250). Null if any page fails.
 * ponytail: client-side paging for the calendar and alerts; add server-side tracked/new filters past 250. */
export async function loadEvents(): Promise<EventItem[] | null> {
  const out: EventItem[] = [];
  let cursor: string | null = null;
  for (let i = 0; i < MAX_PAGES; i++) {
    const r: Awaited<ReturnType<typeof api<Page<EventItem>>>> = await api<Page<EventItem>>(
      `/api/v1/events?${eventsQuery({}, new Date(), cursor)}`,
    );
    if (!r.ok) return null;
    out.push(...r.data.items);
    if (!r.data.next_cursor) break;
    cursor = r.data.next_cursor;
  }
  return out;
}
