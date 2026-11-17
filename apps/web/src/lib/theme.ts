export type Theme = "light" | "dark";
export const THEME_KEY = "he-theme";

/** A stored choice wins; otherwise follow the OS. Anything unrecognised is ignored. */
export function resolveTheme(stored: string | null, prefersDark: boolean): Theme {
  if (stored === "light" || stored === "dark") return stored;
  return prefersDark ? "dark" : "light";
}

/** Inline script run before first paint so a dark-mode user never sees a light flash. */
export const THEME_INIT_SCRIPT = `try{var s=localStorage.getItem("${THEME_KEY}");var t=s==="light"||s==="dark"?s:matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";document.documentElement.dataset.theme=t}catch(e){}`;
