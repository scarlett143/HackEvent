"use client";

import Link from "next/link";
import { Icon } from "../symbol";

// Admin sub-navigation shared by the sources and users pages. Health / Sources / Organisations
// jump to sections on /admin/sources; Users is its own page (the only one that can be "current").
const TABS = [
  { label: "Health", icon: "sensors", href: "/admin/sources#health-heading", page: "sources" },
  { label: "Sources", icon: "rss_feed", href: "/admin/sources#sources-heading", page: "sources" },
  { label: "Organisations", icon: "category", href: "/admin/sources#orgs-heading", page: "sources" },
  { label: "Users", icon: "group", href: "/admin/users", page: "users" },
] as const;

export function AdminNav({ current }: { current: "sources" | "users" }) {
  return (
    <nav aria-label="Admin sections" className="flex flex-wrap gap-2">
      {TABS.map((t) => (
        <Link
          key={t.label}
          href={t.href}
          aria-current={t.page === "users" && current === "users" ? "page" : undefined}
          className="chip chip-tab focus-ring min-h-11 gap-2 px-4 text-sm font-medium"
        >
          <Icon name={t.icon} size={18} />
          {t.label}
        </Link>
      ))}
    </nav>
  );
}
