// Settings search (Stitch "search" screen): which sections match what the user typed.

export type SectionId =
  | "profile"
  | "password"
  | "timezone"
  | "reminders"
  | "channels"
  | "quiet"
  | "export"
  | "delete";

const INDEX: [SectionId, string][] = [
  ["profile", "profile name email role account"],
  ["password", "password security change sign in login"],
  ["timezone", "timezone time zone region india global ist utc"],
  ["reminders", "reminders offsets before deadline notify days hours"],
  ["channels", "channels push notifications email digest in-app test alerts"],
  ["quiet", "quiet hours mute sleep night"],
  ["export", "export my data xls excel spreadsheet ics download tracked calendar"],
  ["delete", "delete my account remove close account danger"],
];

/** Chips under the search box (Stitch "Quick Suggestions"). */
export const SUGGESTIONS = ["Push alerts", "Timezone", "Quiet hours", "Export .xls"] as const;

/** Sections whose text contains every word typed (case-insensitive). Empty query = all sections. */
export function matchSettings(query: string): Set<SectionId> {
  const words = query.toLowerCase().split(/[^a-z0-9-]+/).filter(Boolean);
  return new Set(INDEX.filter(([, text]) => words.every((w) => text.includes(w))).map(([id]) => id));
}
