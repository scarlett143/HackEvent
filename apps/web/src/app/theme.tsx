"use client";

import { useEffect, useId, useState } from "react";
import { THEME_KEY, type Theme } from "@/lib/theme";

/** Light/dark switch. The <html data-theme> attribute is set before paint (layout script). */
export function ThemeSwitch({ compact = false }: { compact?: boolean }) {
  const labelId = useId(); // rendered in the sidebar and the phone menu: ids must be unique
  const [theme, setTheme] = useState<Theme | null>(null);

  useEffect(() => {
    // Read what the pre-paint script chose; this is a mount-time sync with the DOM.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setTheme(document.documentElement.dataset.theme === "dark" ? "dark" : "light");
  }, []);

  function flip() {
    const next: Theme = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      // Private mode: the choice just won't persist.
    }
    setTheme(next);
  }

  const on = theme === "dark";
  return (
    <div className="flex items-center justify-between gap-4">
      <div>
        <p className="font-medium" id={labelId}>
          Dark mode
        </p>
        {!compact && <p className="text-muted text-sm">Switch between light and dark visual themes.</p>}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-labelledby={labelId}
        disabled={theme === null}
        onClick={flip}
        className={`focus-ring relative inline-flex h-8 w-14 shrink-0 items-center rounded-full border border-line transition-colors ${
          on ? "bg-primary" : "bg-surface"
        }`}
      >
        <span
          aria-hidden
          className={`inline-block size-6 rounded-full bg-card shadow transition-transform ${
            on ? "translate-x-7" : "translate-x-1"
          }`}
        />
      </button>
    </div>
  );
}
