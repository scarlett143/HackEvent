export type ProviderStats = {
  id: string;
  name: string;
  model: string;
  role: string;
  is_enabled: boolean;
  daily_cap_usd: string;
  monthly_cap_usd: string;
  spent_day_usd: string;
  spent_month_usd: string;
  tokens_day: number;
  calls_day: number;
  failed_calls_day: number;
  avg_latency_ms_day: number | null;
};
export type PurposeStats = { purpose: string; tokens_day: number };
export type Telemetry = { day_resets_at: string; providers: ProviderStats[]; purposes: PurposeStats[]; total_events: number };

/** Whole percent of a cap used (money arrives as decimal strings). 0 when the cap is 0; may exceed 100. */
export function pctUsed(spent: string, cap: string): number {
  const c = Number(cap);
  return c > 0 ? Math.round((Number(spent) / c) * 100) : 0;
}

/** Status for a usage bar: the label always travels with the colour (design brief §6). */
export function quotaLevel(pct: number): "ok" | "warning" | "urgent" {
  return pct >= 100 ? "urgent" : pct >= 80 ? "warning" : "ok";
}

/** "4h 12m" until `iso`; "now" once passed. */
export function untilReset(iso: string, now: Date): string {
  const mins = Math.max(0, Math.floor((new Date(iso).getTime() - now.getTime()) / 60_000));
  return mins === 0 ? "now" : `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

export const usd = (v: string) => `$${Number(v).toFixed(2)}`;
