"use client";

import Link from "next/link";
import { Icon } from "../symbol";

// Admin sub-navigation shared by the sources, users and review pages. Health / Sources
// jump to sections on /admin/sources; Users, Review and Audit are their own pages (the ones that can be "current").
const TABS = [
  { label: "Health", icon: "sensors", href: "/admin/sources#health-heading", page: "sources" },
  { label: "Sources", icon: "rss_feed", href: "/admin/sources#sources-heading", page: "sources" },
  { label: "Users", icon: "group", href: "/admin/users", page: "users" },
  { label: "Review", icon: "warning", href: "/admin/review", page: "review" },
  { label: "Changes", icon: "history", href: "/admin/changes", page: "changes" },
  { label: "Audit", icon: "verified_user", href: "/admin/audit", page: "audit" },
] as const;

export function AdminNav({ current }: { current: "sources" | "users" | "review" | "changes" | "audit" }) {
  return (
    <nav aria-label="Admin sections" className="flex flex-wrap gap-2">
      {TABS.map((t) => (
        <Link
          key={t.label}
          href={t.href}
          aria-current={t.page !== "sources" && current === t.page ? "page" : undefined}
          className="chip chip-tab focus-ring min-h-11 gap-2 px-4 text-sm font-medium"
        >
          <Icon name={t.icon} size={18} />
          {t.label}
        </Link>
      ))}
    </nav>
  );
}
