"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { api, safeNext, type Me } from "@/lib/api";
import { Icon } from "../symbol";

// HE-012: the API's callback redirects back here with ?error=google_<reason>.
const GOOGLE_ERRORS: Record<string, string> = {
  google_no_account:
    "No HackEvents account uses that Google email. Sign in with your password, or ask your admin for an invite.",
  google_state: "That Google sign-in expired. Try again.",
  google_cancelled: "Google sign-in was cancelled.",
  google_failed: "Google sign-in failed. Try again.",
};

function LoginForm() {
  const router = useRouter();
  const sp = useSearchParams();
  const next = safeNext(sp.get("next"));
  const [error, setError] = useState<string | null>(GOOGLE_ERRORS[sp.get("error") ?? ""] ?? null);
  const [google, setGoogle] = useState(false);
  useEffect(() => {
    api<{ google: boolean }>("/api/v1/auth/providers").then((r) => r.ok && setGoogle(r.data.google));
  }, []);
  const [busy, setBusy] = useState(false);
  const [show, setShow] = useState(false);

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
      <label className="flex flex-col gap-1.5 text-sm font-medium">
        Work / university email
        <span className="relative">
          <span className="text-muted pointer-events-none absolute top-1/2 left-3 -translate-y-1/2">
            <Icon name="mail" />
          </span>
          <input name="email" type="email" autoComplete="username" required className="field w-full pl-10" />
        </span>
      </label>
      <label className="flex flex-col gap-1.5 text-sm font-medium">
        Password
        <span className="relative">
          <span className="text-muted pointer-events-none absolute top-1/2 left-3 -translate-y-1/2">
            <Icon name="lock" />
          </span>
          <input
            name="password"
            type={show ? "text" : "password"}
            autoComplete="current-password"
            required
            className="field w-full pr-12 pl-10"
          />
          <button
            type="button"
            className="focus-ring text-muted absolute top-1/2 right-1 flex size-10 -translate-y-1/2 items-center justify-center rounded-lg hover:bg-card"
            aria-label={show ? "Hide password" : "Show password"}
            aria-pressed={show}
            onClick={() => setShow((v) => !v)}
          >
            <Icon name={show ? "visibility_off" : "visibility"} />
          </button>
        </span>
      </label>
      <label className="flex flex-col gap-1.5 text-sm font-medium">
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
      <button type="submit" disabled={busy} className="btn-primary inline-flex items-center justify-center gap-2">
        {busy ? "Signing in…" : "Sign in to workspace"}
        {!busy && <Icon name="arrow_outward" />}
      </button>
      {google && (
        <>
          <p className="text-muted flex items-center gap-3 text-sm" aria-hidden>
            <span className="h-px flex-1 bg-line" />
            or
            <span className="h-px flex-1 bg-line" />
          </p>
          {/* A top-level navigation: the API sets the state cookie and redirects to Google. */}
          <a
            className="btn-secondary inline-flex items-center justify-center gap-2"
            href={`/api/v1/auth/google/start?next=${encodeURIComponent(next)}`}
          >
            Continue with Google
          </a>
          <p className="text-muted text-center text-xs">Uses the Google account with your invited email.</p>
        </>
      )}
    </form>
  );
}

export default function LoginPage() {
  return (
    // Stitch login: one card on a soft lime glow.
    <main className="flex flex-1 items-center justify-center bg-[radial-gradient(ellipse_at_center,var(--color-primary-soft)_0%,var(--color-bg)_70%)] p-4">
      <div className="card flex w-full max-w-xl flex-col gap-6 p-6 shadow-[0_8px_24px_rgba(20,23,17,0.06)] sm:p-10">
        <div className="flex flex-col gap-2">
          <h1 className="font-display flex items-center gap-2 text-[1.75rem] leading-8 font-bold">
            Sign in to HackEvents <span aria-hidden className="size-2 rounded-full bg-primary" />
          </h1>
          <p className="text-muted">Never miss a hackathon deadline. Track events, get alerts and keep your calendar in one place.</p>
        </div>
        <Suspense fallback={<p className="text-muted">Loading…</p>}>
          <LoginForm />
        </Suspense>
        <p className="text-muted text-center text-sm">Accounts are invite-only. Ask your admin for an invite.</p>
      </div>
    </main>
  );
}
