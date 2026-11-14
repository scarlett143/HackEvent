"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { api, safeNext, type Me } from "@/lib/api";

function LoginForm() {
  const router = useRouter();
  const next = safeNext(useSearchParams().get("next"));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const totp = String(f.get("totp") ?? "").trim();
    setBusy(true);
    setError(null);
    const r = await api<Me>("/api/v1/auth/login", "POST", {
      email: f.get("email"),
      password: f.get("password"),
      ...(totp ? { totp } : {}),
    });
    setBusy(false);
    if (r.ok) return router.replace(next);
    setError(
      r.status === 429
        ? "Too many attempts. Try again in 15 minutes."
        : r.error.code === "invalid_totp"
          ? "That authenticator code is not valid."
          : "Email or password is not correct.",
    );
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-4" noValidate>
      <label className="flex flex-col gap-1 text-sm font-medium">
        Email
        <input name="email" type="email" autoComplete="username" required className="field" />
      </label>
      <label className="flex flex-col gap-1 text-sm font-medium">
        Password
        <input
          name="password"
          type="password"
          autoComplete="current-password"
          required
          className="field"
        />
      </label>
      <label className="flex flex-col gap-1 text-sm font-medium">
        <span>
          Authenticator code <span className="text-muted font-normal">(admins only)</span>
        </span>
        <input
          name="totp"
          inputMode="numeric"
          autoComplete="one-time-code"
          pattern="[0-9]{6}"
          maxLength={6}
          className="field"
        />
      </label>
      <p role="alert" aria-live="polite" className="min-h-5 text-sm text-urgent">
        {error}
      </p>
      <button type="submit" disabled={busy} className="btn-primary">
        {busy ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}

export default function LoginPage() {
  return (
    <main className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center gap-6 p-6">
      <h1 className="text-2xl font-semibold">Sign in to HackEvents</h1>
      <Suspense fallback={<p className="text-muted">Loading…</p>}>
        <LoginForm />
      </Suspense>
      <p className="text-muted text-sm">Accounts are invite-only. Ask your admin for an invite.</p>
    </main>
  );
}
