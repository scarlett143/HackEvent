/** Material Symbols Outlined glyph (the Stitch designs' icon set), from the self-hosted subset font
 * (app/fonts/material-symbols.woff2: only the icons the Stitch screens use). Decorative: always
 * pair it with visible text or an aria-label on the control. Not named icon.tsx: that filename is a
 * reserved Next.js favicon route. */
export function Icon({
  name,
  fill = false,
  size = 20,
  className = "",
}: {
  name: string;
  fill?: boolean;
  size?: 16 | 18 | 20 | 22 | 24;
  className?: string;
}) {
  return (
    <span aria-hidden className={`icon ${fill ? "icon-fill" : ""} ${className}`} style={{ fontSize: size }}>
      {name}
    </span>
  );
}
