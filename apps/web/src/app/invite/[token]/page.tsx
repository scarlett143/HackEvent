"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { api, type Me } from "@/lib/api";

// App Flow F1 step 3: the invitee sets name + password; the accept call starts a session.
// ponytail: onboarding (F1 step 4) and "Sign in with Google" come with their tickets.

const MIN_PASSWORD = 12;
type Info = { email: string; role: string };
type State = { kind: "loading" } | { kind: "invalid" } | { kind: "form"; info: Info };

function InviteForm() {
  const router = useRouter();
  const { token } = useParams<{ token: string }>();
  const path = `/api/v1/invites/${encodeURIComponent(token)}`;
  const [state, setState] = useState<State>({ kind: "loading" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<Info>(path).then((r) => {
      if (r.ok) setState({ kind: "form", info: r.data });
      else if (r.status === 429) setError("Too many attempts. Try again later.");
      else setState({ kind: "invalid" });
    });
  }, [path]);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const name = String(f.get("name") ?? "").trim();
    const password = String(f.get("password") ?? "");
    if (!name) return setError("Enter your name.");
    if (password.length < MIN_PASSWORD) return setError(`Use at least ${MIN_PASSWORD} characters.`);
    if (password !== f.get("confirm")) return setError("The passwords do not match.");
    setBusy(true);
    setError(null);
    const r = await api<Me>(`${path}/accept`, "POST", { name, password });
    setBusy(false);
    if (r.ok) return router.replace("/");
    if (r.status === 404) return setState({ kind: "invalid" });
    setError(
      r.status === 429
        ? "Too many attempts. Try again later."
        : r.error.code === "weak_password"
          ? r.error.message
          : "Could not create your account. Check the form and try again.",
    );
  }

  if (state.kind === "invalid")
    return (
      <p role="alert">
        This invite link is invalid, expired or already used. Ask your admin for a new one, or{" "}
        <Link className="link" href="/login">
          sign in
        </Link>{" "}
        if you already have an account.
      </p>
    );

  return (
    <>
      {state.kind === "loading" && !error && <p className="text-muted">Checking your invite…</p>}
      {state.kind === "form" && (
        <form onSubmit={onSubmit} className="flex flex-col gap-4" noValidate>
          <p>
            You are joining as <strong>{state.info.email}</strong>{" "}
            <span className="text-muted">({state.info.role})</span>.
          </p>
          <label className="flex flex-col gap-1 text-sm font-medium">
            Name
            <input name="name" autoComplete="name" required maxLength={200} className="field" />
          </label>
          <label className="flex flex-col gap-1 text-sm font-medium">
            Password
            <input
              name="password"
              type="password"
              autoComplete="new-password"
              required
              minLength={MIN_PASSWORD}
              aria-describedby="password-rule"
              className="field"
            />
          </label>
          <p id="password-rule" className="text-muted -mt-2 text-sm">
            At least {MIN_PASSWORD} characters, and not a common password.
          </p>
          <label className="flex flex-col gap-1 text-sm font-medium">
            Confirm password
            <input
              name="confirm"
              type="password"
              autoComplete="new-password"
              required
              className="field"
            />
          </label>
          <button type="submit" disabled={busy} className="btn-primary">
            {busy ? "Creating account…" : "Create account"}
          </button>
        </form>
      )}
      <p role="alert" aria-live="polite" className="min-h-5 text-sm text-urgent">
        {error}
      </p>
    </>
  );
}

export default function InvitePage() {
  return (
    <main className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center gap-6 p-6">
      <h1 className="text-2xl font-semibold">Join HackEvents</h1>
      {/* cacheComponents: useParams on an unknown dynamic segment needs a Suspense boundary */}
      <Suspense fallback={<p className="text-muted">Loading…</p>}>
        <InviteForm />
      </Suspense>
    </main>
  );
}
