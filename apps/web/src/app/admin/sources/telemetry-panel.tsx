"use client";

// Stitch "LLM Quotas & Scraper Telemetry" panel. Data: GET /api/v1/admin/telemetry (spend vs
// caps, same UTC windows the router enforces). Scraper counts come from the page's health list.
// Provider names/models are rendered as React text, never HTML.

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { pctUsed, quotaLevel, type ProviderStats, type Telemetry, untilReset, usd } from "@/lib/telemetry";
import { Icon } from "../../symbol";
import { useNow } from "../../ui";

const REFRESH_MS = 60_000;
const LEVEL = {
  ok: { bar: "bg-primary", text: "", label: "" },
  warning: { bar: "bg-warning", text: "text-warning-ink", label: "Near cap" },
  urgent: { bar: "bg-urgent", text: "text-urgent", label: "Cap reached" },
} as const;

export type ScraperCounts = { healthy: number; degraded: number; error: number };

export function TelemetryPanel({ scrapers }: { scrapers: ScraperCounts | null }) {
  const now = useNow();
  const [data, setData] = useState<Telemetry | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await api<Telemetry>("/api/v1/admin/telemetry");
    if (r.ok) {
      setData(r.data);
      setError(null);
    } else setError(r.error.message);
  }, []);

  useEffect(() => {
    // Mount-time fetch; setState runs after an await, not synchronously.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => clearInterval(id);
  }, [load]);

  return (
    <section aria-labelledby="telemetry-heading" className="card flex flex-col gap-4 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="telemetry-heading" className="font-display text-xl font-bold">
          LLM quotas and scraper telemetry
        </h2>
        <span className="chip border-ok/30 bg-ok/10 text-ok-ink">Refreshes every minute</span>
      </div>

      {error && (
        <p role="alert" className="text-urgent text-sm">
          Could not load telemetry. {error}
        </p>
      )}
      {!data && !error && <div aria-busy="true" aria-label="Loading telemetry" className="skeleton h-24" />}
      {data && data.providers.length === 0 && (
        <p className="text-muted text-sm">No LLM providers are configured yet.</p>
      )}
      {data && (
        <>
          <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {data.providers.map((p) => (
              <Provider key={p.id} p={p} />
            ))}
            {scrapers && (
              <li className="surface flex flex-col gap-2 rounded-lg p-3">
                <p className="text-muted flex items-center justify-between text-xs font-semibold tracking-wide uppercase">
                  Scraper sources <Icon name="sensors" size={20} />
                </p>
                <p className="font-display text-3xl font-bold tabular-nums">{scrapers.healthy + scrapers.degraded + scrapers.error}</p>
                <ul className="flex flex-col gap-1 text-sm tabular-nums">
                  <li className="flex items-center gap-2"><span aria-hidden className="size-2 rounded-full bg-ok" />{scrapers.healthy} healthy</li>
                  <li className="flex items-center gap-2"><span aria-hidden className="size-2 rounded-full bg-warning" />{scrapers.degraded} degraded</li>
                  <li className="flex items-center gap-2"><span aria-hidden className="size-2 rounded-full bg-urgent" />{scrapers.error} failing</li>
                </ul>
              </li>
            )}
            <li className="surface flex flex-col gap-2 rounded-lg p-3">
              <p className="text-muted flex items-center justify-between text-xs font-semibold tracking-wide uppercase">
                Database index <Icon name="database" size={20} />
              </p>
              <p className="font-display text-3xl font-bold tabular-nums">{data.total_events.toLocaleString("en-IN")}</p>
              <p className="text-muted text-sm">events stored</p>
            </li>
          </ul>
          <QuotaAllocation data={data} now={now} />
          {data.providers.length > 0 && (
            <p className="text-muted text-sm">
              Daily caps reset in <span className="tabular-nums">{untilReset(data.day_resets_at, now)}</span>{" "}
              (00:00 UTC).
            </p>
          )}
        </>
      )}

    </section>
  );
}

function Provider({ p }: { p: ProviderStats }) {
  const dayPct = pctUsed(p.spent_day_usd, p.daily_cap_usd);
  const lvl = LEVEL[quotaLevel(dayPct)];
  const monthPct = pctUsed(p.spent_month_usd, p.monthly_cap_usd);
  const mlvl = LEVEL[quotaLevel(monthPct)];
  return (
    <li className="surface flex flex-col gap-2 rounded-lg p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="font-display text-lg font-semibold break-words">{p.name}</h3>
          <p className="text-muted text-sm break-all">
            {p.model} · {p.role}
          </p>
        </div>
        {!p.is_enabled && <span className="chip shrink-0">Disabled</span>}
      </div>
      <div
        role="progressbar"
        aria-label={`${p.name} daily spend`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.min(dayPct, 100)}
        aria-valuetext={`${usd(p.spent_day_usd)} of ${usd(p.daily_cap_usd)} daily cap`}
        className="h-2 overflow-hidden rounded-full bg-line"
      >
        <div className={`h-full ${lvl.bar}`} style={{ width: `${Math.min(dayPct, 100)}%` }} />
      </div>
      <p className="flex flex-wrap justify-between gap-x-3 text-sm tabular-nums">
        <span>
          {usd(p.spent_day_usd)} / {usd(p.daily_cap_usd)} today ({dayPct}%)
        </span>
        {lvl.label && <span className={`font-semibold ${lvl.text}`}>{lvl.label}</span>}
      </p>
      <p className="flex flex-wrap justify-between gap-x-3 text-sm tabular-nums">
        <span className="text-muted">
          {usd(p.spent_month_usd)} / {usd(p.monthly_cap_usd)} this month ({monthPct}%)
        </span>
        {mlvl.label && <span className={`font-semibold ${mlvl.text}`}>Monthly: {mlvl.label.toLowerCase()}</span>}
      </p>
      <dl className="grid grid-cols-3 gap-2 text-center text-sm">
        <div>
          <dt className="text-muted text-xs">Tokens today</dt>
          <dd className="font-medium tabular-nums">{p.tokens_day.toLocaleString("en-IN")}</dd>
        </div>
        <div>
          <dt className="text-muted text-xs">Calls (failed)</dt>
          <dd className="font-medium tabular-nums">
            {p.calls_day} ({p.failed_calls_day})
          </dd>
        </div>
        <div>
          <dt className="text-muted text-xs">Avg latency</dt>
          <dd className="font-medium tabular-nums">
            {p.avg_latency_ms_day === null ? "—" : `${p.avg_latency_ms_day} ms`}
          </dd>
        </div>
      </dl>
    </li>
  );
}

const PURPOSE_LABEL: Record<string, string> = { extract: "Event extraction", verify: "Verification pass", other: "Other calls" };

/** Stitch "LLM Quota Allocation": today's settled tokens by pipeline step, plus the tier status. */
function QuotaAllocation({ data, now }: { data: Telemetry; now: Date }) {
  const total = data.purposes.reduce((s, p) => s + p.tokens_day, 0);
  const enabled = data.providers.filter((p) => p.is_enabled);
  const cap = enabled.reduce((s, p) => s + Number(p.daily_cap_usd), 0);
  return (
    <section aria-labelledby="quota-h" className="flex flex-col gap-3 border-t border-line pt-4">
      <div>
        <h3 id="quota-h" className="font-display text-lg font-semibold">LLM quota allocation</h3>
        <p className="text-muted text-sm">Today&apos;s tokens by pipeline step.</p>
      </div>
      <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {data.purposes.length === 0 && <li className="text-muted text-sm">No LLM calls yet today.</li>}
        {data.purposes.map((p) => {
          const pct = total ? Math.round((p.tokens_day / total) * 100) : 0;
          const label = PURPOSE_LABEL[p.purpose] ?? p.purpose;
          return (
            <li key={p.purpose} className="surface flex flex-col gap-2 rounded-lg p-3">
              <p className="flex items-center justify-between gap-2 text-sm font-medium">
                {label} <span className="chip tabular-nums">{pct}%</span>
              </p>
              <p className="font-mono text-lg font-semibold tabular-nums">{p.tokens_day.toLocaleString("en-IN")} tokens</p>
              <div
                role="progressbar"
                aria-label={`${label} share of today's tokens`}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={pct}
                className="h-2 overflow-hidden rounded-full bg-line"
              >
                <div className="h-full bg-link" style={{ width: `${pct}%` }} />
              </div>
            </li>
          );
        })}
        <li className="surface flex flex-col gap-1 rounded-lg p-3">
          <p className="text-muted text-xs font-semibold tracking-wide uppercase">Tier status</p>
          <p className="font-display text-lg font-semibold">
            {enabled.length} active {enabled.length === 1 ? "provider" : "providers"}
          </p>
          <p className="text-sm tabular-nums">Total cap: ${cap.toFixed(2)} / day</p>
          <p className="text-muted flex justify-between text-sm">
            Cycle reset <span className="font-mono tabular-nums">{untilReset(data.day_resets_at, now)}</span>
          </p>
        </li>
      </ul>
    </section>
  );
}
