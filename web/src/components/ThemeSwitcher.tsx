// =============================================================================
// web/src/components/ThemeSwitcher.tsx
// -----------------------------------------------------------------------------
// Responsible for: The Dark/Sepia/Light segmented control. Shared by the sidebar, the
//                  full-bleed login screen, and the Showcase top bar so the switch UI
//                  lives in one place.
// Role in project: Reads/sets the theme via useTheme; the active segment is the
//                  accent fill (design). Switching is just setTheme — tokens.css does
//                  the rest with the 0.25s transition. The `compact` variant renders
//                  glyph-only (the Showcase top bar), staying accessible via aria-label.
// =============================================================================

import { useTheme, THEMES, type Theme } from "../theme/ThemeProvider";

const THEME_LABEL: Record<Theme, string> = { dark: "Dark", sepia: "Sepia", light: "Light" };
const THEME_GLYPH: Record<Theme, string> = { dark: "☾", sepia: "◐", light: "☀" };

/**
 * The theme segmented control.
 *
 * Args:
 *   compact: when true, show only the glyph (no text label) — the icon-only top-bar
 *     variant the Showcase uses. The label still rides as the button's aria-label/title,
 *     so the control stays legible to assistive tech and on hover.
 */
export function ThemeSwitcher({ compact = false }: { compact?: boolean }) {
  const { theme, setTheme } = useTheme();
  return (
    <div
      className={`theme-switch${compact ? " theme-switch-compact" : ""}`}
      role="group"
      aria-label="Theme"
    >
      {THEMES.map((t) => (
        <button
          key={t}
          type="button"
          className={`seg${theme === t ? " active" : ""}`}
          aria-pressed={theme === t}
          aria-label={compact ? THEME_LABEL[t] : undefined}
          title={compact ? THEME_LABEL[t] : undefined}
          onClick={() => setTheme(t)}
        >
          <span aria-hidden="true">{THEME_GLYPH[t]}</span>
          {!compact && THEME_LABEL[t]}
        </button>
      ))}
    </div>
  );
}
