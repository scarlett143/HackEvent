"use client";

import Image from "next/image";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { api, safeNext, type Me } from "@/lib/api";
import { Icon } from "../symbol";
import { BrandMark } from "../ui";

function LoginForm() {
  const router = useRouter();
  const next = safeNext(useSearchParams().get("next"));
  const [error, setError] = useState<string | null>(null);
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
    </form>
  );
}

export default function LoginPage() {
  return (
    // Split login: Karpagam campus banner (brand side) + sign-in panel. Phones stack them.
    <main className="grid flex-1 bg-card lg:min-h-dvh lg:grid-cols-[minmax(0,3fr)_minmax(26rem,2fr)]">
      <section className="relative min-h-56 overflow-hidden lg:min-h-full" aria-label="HackEvents">
        <Image
          src="/brand/login-banner.jpg"
          alt="Karpagam Institutions campus"
          fill
          preload
          unoptimized
          sizes="(min-width: 1024px) 60vw, 100vw"
          className="object-cover object-center"
        />
        <div aria-hidden className="absolute inset-0 bg-gradient-to-t from-black/80 via-black/35 to-black/10" />
        <div className="relative flex h-full flex-col justify-between gap-8 p-6 text-white sm:p-10">
          <div className="flex items-center gap-3">
            <BrandMark size={40} />
            <span className="font-display text-2xl font-bold tracking-[0.12em] uppercase">HackEvents</span>
          </div>
          <div className="hidden max-w-xl flex-col gap-3 sm:flex">
            <p className="font-display text-4xl leading-tight font-bold">Never miss a hackathon deadline.</p>
            <p className="text-lg text-white/85">
              Track hackathons from companies and institutions, get alerts and keep your calendar in one place.
            </p>
          </div>
        </div>
      </section>

      <section className="flex items-center justify-center p-6 sm:p-10">
        <div className="flex w-full max-w-md flex-col gap-6">
          <div className="flex items-center gap-4">
            <Image src="/brand/karpagam-emblem.png" alt="" width={64} height={64} unoptimized className="shrink-0" />
            <div className="flex flex-col">
              <p className="font-display text-2xl leading-tight font-bold text-ok-ink">Karpagam Institutions</p>
              <p className="text-muted text-sm">Enable | Enlighten | Enrich</p>
            </div>
          </div>
          <div className="h-px bg-line" aria-hidden />
          <div className="flex flex-col gap-2">
            <h1 className="font-display text-[1.75rem] leading-8 font-bold">Sign in to HackEvents</h1>
            <p className="text-muted">Use your work or university account.</p>
          </div>
          <Suspense fallback={<p className="text-muted">Loading…</p>}>
            <LoginForm />
          </Suspense>
          <p className="text-muted text-center text-sm">Accounts are invite-only. Ask your admin for an invite.</p>
        </div>
      </section>
    </main>
  );
}
