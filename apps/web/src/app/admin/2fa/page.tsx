"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

// Admin routes answer 403 totp_enrolment_required / totp_required until this session
// has passed a code (HE-014). This page walks the admin through whichever applies.
type State = "loading" | "enrol" | "verify" | "done" | "forbidden";
type Enrol = { secret: string; otpauth_uri: string };

export default function TwoFactorPage() {
  const router = useRouter();
  const [state, setState] = useState<State>("loading");
  const [enrol, setEnrol] = useState<Enrol | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api("/api/v1/admin/ping").then((r) => {
      if (r.ok) return setState("done");
      if (r.status === 401) return router.replace("/login?next=/admin/2fa");
      if (r.error.code === "totp_enrolment_required") return setState("enrol");
      if (r.error.code === "totp_required") return setState("verify");
      setState("forbidden");
    });
  }, [router]);

  async function startEnrol() {
    const r = await api<Enrol>("/api/v1/auth/2fa/enrol", "POST");
    if (r.ok) setEnrol(r.data);
    else setError(r.error.message);
  }

  async function submitCode(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const code = String(new FormData(e.currentTarget).get("code") ?? "").trim();
    const path = state === "enrol" ? "/api/v1/auth/2fa/confirm" : "/api/v1/auth/2fa/verify";
    const r = await api(path, "POST", { code });
    if (r.ok) return setState("done");
    setError(r.status === 429 ? "Too many attempts. Try again in 15 minutes." : "Code not valid.");
  }

  return (
    <main className="mx-auto flex w-full max-w-md flex-1 flex-col justify-center gap-5 p-6">
      <h1 className="text-2xl font-semibold">Two-factor sign-in</h1>
      {state === "loading" && <p className="text-muted">Checking…</p>}
      {state === "forbidden" && <p>This area is for admins only.</p>}
      {state === "done" && (
        <p>
          Verified for this session. <Link className="link" href="/">
            Continue
          </Link>
        </p>
      )}
      {state === "enrol" && !enrol && (
        <>
          <p>Admin accounts need an authenticator app (Google Authenticator, Aegis, 1Password…).</p>
          <button className="btn-primary" onClick={startEnrol}>
            Set up authenticator
          </button>
        </>
      )}
      {state === "enrol" && enrol && (
        <div className="flex flex-col gap-2">
          <p>Add this key to your authenticator app, then enter the 6-digit code it shows.</p>
          <code className="surface break-all rounded-xl p-3 text-sm">{enrol.secret}</code>
          <a className="link text-sm" href={enrol.otpauth_uri}>
            Open in authenticator app
          </a>
        </div>
      )}
      {(state === "verify" || (state === "enrol" && enrol)) && (
        <form onSubmit={submitCode} className="flex flex-col gap-3">
          <label className="flex flex-col gap-1 text-sm font-medium">
            6-digit code
            <input
              name="code"
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="[0-9]{6}"
              maxLength={6}
              required
              className="field"
            />
          </label>
          <button type="submit" className="btn-primary">
            Verify
          </button>
        </form>
      )}
      <p role="alert" aria-live="polite" className="min-h-5 text-sm text-urgent">
        {error}
      </p>
    </main>
  );
}
