// Pure helpers for /settings (HE-077). Server rules mirrored here: offsets are whole days
// 0–30, deduped, sorted descending; quiet hours are "HH:MM" pairs that may cross midnight.

export const MAX_OFFSET_DAYS = 30;
export const PRESET_OFFSETS = [7, 3, 2, 1, 0]; // App Flow F8 chips

export type Channel = "email" | "in_app";
export type Prefs = {
  deadline_offsets_days: number[];
  event_offsets_days: number[];
  channels: Partial<Record<string, boolean>>;
};

/** "5", "5d", "5 days" → 5; anything else (or out of range) → null. */
export function parseOffset(input: string): number | null {
  const m = /^\s*(\d{1,2})\s*(d|day|days)?\s*$/i.exec(input);
  if (!m) return null;
  const n = Number(m[1]);
  return n <= MAX_OFFSET_DAYS ? n : null;
}

export function normaliseOffsets(days: number[]): number[] {
  return [...new Set(days)].sort((a, b) => b - a);
}

export function toggleOffset(days: number[], d: number): number[] {
  return days.includes(d) ? days.filter((x) => x !== d) : normaliseOffsets([...days, d]);
}

export function offsetLabel(d: number): string {
  return d === 0 ? "Day of" : d === 1 ? "1 day" : `${d} days`;
}

/** Chips to show: the presets plus any custom offsets the user has saved. */
export function offsetChips(days: number[]): number[] {
  return normaliseOffsets([...PRESET_OFFSETS, ...days]);
}

/** "Off", "13:00–14:00" or "22:00–07:00 (overnight)". */
export function formatQuietHours(start: string | null, end: string | null): string {
  if (!start || !end) return "Off";
  return `${start}–${end}${end < start ? " (overnight)" : ""}`;
}

/** Browser's IANA zones, always including `current` and the default (some engines list
 *  legacy names, e.g. Asia/Calcutta, and omit the other). */
export function timezoneOptions(current: string, all?: string[]): string[] {
  const list = all ?? (typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : []);
  return [...new Set([...list, current, "Asia/Kolkata", "UTC"])].sort();
}
