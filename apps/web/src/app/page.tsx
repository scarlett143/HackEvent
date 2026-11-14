"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, type Me } from "@/lib/api";

export default function Home() {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);

  useEffect(() => {
    api<Me>("/api/v1/me").then((r) => {
      if (r.ok) setMe(r.data);
      else router.replace("/login?next=/");
    });
  }, [router]);

  async function logout() {
    await api("/api/v1/auth/logout", "POST");
    router.replace("/login");
  }

  if (!me) return <p className="text-muted p-8">Loading…</p>;
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-6 p-6">
      <header className="flex items-center justify-between border-b border-line pb-4">
        <span className="text-xl font-semibold">HackEvents</span>
        <div className="flex items-center gap-3 text-sm">
          <span>
            {me.name} <span className="text-muted">· {me.role}</span>
          </span>
          {me.role === "admin" && (
            <Link className="link" href="/admin/2fa">
              Admin
            </Link>
          )}
          <button className="btn-secondary" onClick={logout}>
            Sign out
          </button>
        </div>
      </header>
      {/* ponytail: placeholder until the feed API (HE-050) and Feed UI (HE-051) land. */}
      <p className="text-muted">The hackathon feed appears here once event discovery is live.</p>
    </main>
  );
}
