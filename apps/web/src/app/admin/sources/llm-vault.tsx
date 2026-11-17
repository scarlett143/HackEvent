"use client";

// Stitch "AI Model API Keys & Credential Vault" (HE-094). Keys are write-only: they are typed here,
// sent once to the API (encrypted at rest there) and never shown again — the API only says has_key.

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Icon } from "../../symbol";

type Provider = {
  id: string;
  name: string;
  type: "gemini" | "anthropic" | "openai_compatible";
  base_url: string | null;
  model: string;
  role: "extract" | "verify" | "fallback";
  priority: number;
  input_price_per_mtok: string;
  output_price_per_mtok: string;
  daily_cap_usd: string;
  monthly_cap_usd: string;
  is_enabled: boolean;
  has_key: boolean;
};

const TYPE_LABEL = { gemini: "Google AI (Gemini)", anthropic: "Anthropic Claude", openai_compatible: "OpenAI-compatible" } as const;
const TYPE_ICON = { gemini: "neurology", anthropic: "smart_toy", openai_compatible: "apps" } as const;

/** A key field with show/hide; empty means "keep the stored key". */
function KeyField({ id, value, onChange, placeholder }: { id: string; value: string; onChange: (v: string) => void; placeholder: string }) {
  const [show, setShow] = useState(false);
  return (
    <span className="relative block">
      <input
        id={id}
        type={show ? "text" : "password"}
        autoComplete="off"
        spellCheck={false}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="field w-full pr-12 font-mono"
      />
      <button
        type="button"
        className="focus-ring text-muted absolute top-1/2 right-1 flex size-10 -translate-y-1/2 items-center justify-center rounded-lg hover:bg-card"
        aria-label={show ? "Hide key" : "Show key"}
        aria-pressed={show}
        onClick={() => setShow((v) => !v)}
      >
        <Icon name={show ? "visibility_off" : "visibility"} />
      </button>
    </span>
  );
}

function ProviderCard({ p, onSaved }: { p: Provider; onSaved: (p: Provider) => void }) {
  const [key, setKey] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function patch(body: Record<string, unknown>, done: string) {
    setBusy(true);
    const r = await api<Provider>(`/api/v1/admin/llm-providers/${p.id}`, "PATCH", body);
    setBusy(false);
    if (!r.ok) return setMsg({ ok: false, text: r.error.message });
    setKey("");
    setMsg({ ok: true, text: done });
    onSaved(r.data);
  }

  return (
    <li className="surface flex flex-col gap-3 rounded-lg p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <Icon name={TYPE_ICON[p.type]} />
          <div className="min-w-0">
            <h4 className="font-display truncate text-lg font-semibold">{p.name}</h4>
            <p className="text-muted truncate text-xs">
              {TYPE_LABEL[p.type]} · {p.model}
            </p>
          </div>
        </div>
        <span className={`chip shrink-0 gap-1.5 ${p.is_enabled ? "border-ok/30 bg-ok/10 text-ok-ink" : ""}`}>
          <span aria-hidden className={`size-2 rounded-full ${p.is_enabled ? "bg-ok" : "bg-muted"}`} />
          {p.is_enabled ? (p.role === "fallback" ? "Standby" : "Active") : "Disabled"}
        </span>
      </div>
      <label className="flex flex-col gap-1 text-sm font-medium" htmlFor={`key-${p.id}`}>
        API key
        <KeyField
          id={`key-${p.id}`}
          value={key}
          onChange={setKey}
          placeholder={p.has_key ? "•••••••••••••••• (saved — type to replace)" : "Paste the provider API key"}
        />
      </label>
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
        <span className="text-muted">
          {p.role} · priority #{p.priority} · cap ${Number(p.daily_cap_usd).toFixed(2)}/day
        </span>
        <div className="flex gap-2">
          <button className="btn-secondary" disabled={busy} onClick={() => patch({ is_enabled: !p.is_enabled }, p.is_enabled ? "Disabled." : "Enabled.")}>
            {p.is_enabled ? "Disable" : "Enable"}
          </button>
          <button className="btn-primary" disabled={busy || key.trim().length < 8} onClick={() => patch({ api_key: key.trim() }, "Key saved and encrypted.")}>
            Save key
          </button>
        </div>
      </div>
      {msg && (
        <p role={msg.ok ? "status" : "alert"} className={`text-sm ${msg.ok ? "text-ok-ink" : "text-urgent"}`}>
          {msg.text}
        </p>
      )}
    </li>
  );
}

const EMPTY = {
  name: "", type: "gemini", model: "", base_url: "", role: "extract", priority: "1",
  input_price_per_mtok: "", output_price_per_mtok: "", daily_cap_usd: "", monthly_cap_usd: "", api_key: "",
};

function AddProvider({ onAdded, onCancel }: { onAdded: (p: Provider) => void; onCancel: () => void }) {
  const [f, setF] = useState(EMPTY);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof EMPTY) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    const { base_url, priority, ...rest } = f;
    const r = await api<Provider>("/api/v1/admin/llm-providers", "POST", {
      ...rest,
      priority: Number(priority),
      ...(f.type === "openai_compatible" ? { base_url } : {}),
    });
    setBusy(false);
    if (!r.ok) return setErr(r.error.message);
    onAdded(r.data);
  }

  const field = (k: keyof typeof EMPTY, label: string, props: React.InputHTMLAttributes<HTMLInputElement> = {}) => (
    <label className="flex flex-col gap-1 text-sm font-medium">
      {label}
      <input value={f[k]} onChange={set(k)} required className="field" {...props} />
    </label>
  );

  return (
    <form onSubmit={submit} className="surface grid gap-3 rounded-lg p-4 sm:grid-cols-2" aria-label="Add provider">
      {field("name", "Name", { maxLength: 80 })}
      <label className="flex flex-col gap-1 text-sm font-medium">
        Provider
        <select value={f.type} onChange={set("type")} className="field">
          {Object.entries(TYPE_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </label>
      {field("model", "Model id", { placeholder: "gemini-1.5-flash", maxLength: 120 })}
      {f.type === "openai_compatible" && field("base_url", "Base URL (https)", { type: "url", placeholder: "https://api.example.com/v1" })}
      <label className="flex flex-col gap-1 text-sm font-medium">
        Role
        <select value={f.role} onChange={set("role")} className="field">
          <option value="extract">extract</option>
          <option value="verify">verify</option>
          <option value="fallback">fallback</option>
        </select>
      </label>
      {field("priority", "Priority (1 = first)", { type: "number", min: 1, max: 99 })}
      {field("input_price_per_mtok", "Input price / 1M tokens (USD)", { type: "number", min: 0, step: "0.0001" })}
      {field("output_price_per_mtok", "Output price / 1M tokens (USD)", { type: "number", min: 0, step: "0.0001" })}
      {field("daily_cap_usd", "Daily cap (USD)", { type: "number", min: 0, step: "0.01" })}
      {field("monthly_cap_usd", "Monthly cap (USD)", { type: "number", min: 0, step: "0.01" })}
      <label className="flex flex-col gap-1 text-sm font-medium sm:col-span-2" htmlFor="new-key">
        API key
        <KeyField id="new-key" value={f.api_key} onChange={(v) => setF({ ...f, api_key: v })} placeholder="Paste the provider API key" />
      </label>
      {err && <p role="alert" className="text-urgent text-sm sm:col-span-2">{err}</p>}
      <div className="flex gap-2 sm:col-span-2">
        <button type="submit" className="btn-primary" disabled={busy}>{busy ? "Saving…" : "Save provider"}</button>
        <button type="button" className="btn-secondary" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

export function LlmVault() {
  const [list, setList] = useState<Provider[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const load = useCallback(async () => {
    const r = await api<Provider[]>("/api/v1/admin/llm-providers");
    if (r.ok) setList(r.data);
    else setError(r.error.message);
  }, []);
  useEffect(() => {
    // Mount-time fetch; state is set after the await.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
  }, [load]);

  const replace = (p: Provider) => setList((l) => l?.map((x) => (x.id === p.id ? p : x)) ?? null);

  return (
    <section aria-labelledby="vault-h" className="card flex flex-col gap-4 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="vault-h" className="font-display flex flex-wrap items-center gap-2 text-xl font-semibold">
            <Icon name="key" /> AI model API keys &amp; credential vault
            <span className="chip gap-1 font-mono"><Icon name="lock" size={16} /> AES-256 encrypted at rest</span>
          </h2>
          <p className="text-muted text-sm">Keys are write-only: saved encrypted, never shown again. Used for extraction with fallback in priority order.</p>
        </div>
        <button className="btn-primary inline-flex items-center gap-2" onClick={() => setAdding((v) => !v)} aria-expanded={adding}>
          <Icon name="add" /> Add provider key
        </button>
      </div>
      {adding && <AddProvider onAdded={(p) => { setList((l) => [...(l ?? []), p].sort((a, b) => a.priority - b.priority)); setAdding(false); }} onCancel={() => setAdding(false)} />}
      {error && <p role="alert" className="text-urgent text-sm">Could not load providers. {error}</p>}
      {!list && !error && <div aria-busy="true" aria-label="Loading providers" className="skeleton h-32" />}
      {list && list.length === 0 && !adding && <p className="text-muted text-sm">No providers yet. Add one to start extraction.</p>}
      {list && list.length > 0 && (
        <ul className="grid gap-3 lg:grid-cols-3">
          {list.map((p) => <ProviderCard key={p.id} p={p} onSaved={replace} />)}
        </ul>
      )}
      <p className="surface flex flex-wrap items-center justify-between gap-2 rounded-lg px-4 py-3 text-sm">
        <span className="flex items-center gap-2">
          <Icon name="verified_user" size={18} /> Rate-limit guard: on HTTP 429 the router pauses that provider and moves to the next in priority.
        </span>
        <span className="text-ok-ink font-semibold">Always on</span>
      </p>
    </section>
  );
}
