// =============================================================================
// web/src/components/ThemeSwitcher.tsx
// -----------------------------------------------------------------------------
// Responsible for: The Dark/Sepia/Light segmented control. Shared by the sidebar and
//                  the full-bleed login screen so the switch UI lives in one place.
// Role in project: Reads/sets the theme via useTheme; the active segment is the
//                  accent fill (design). Switching is just setTheme — tokens.css does
//                  the rest with the 0.25s transition.
// =============================================================================

import { useTheme, THEMES, type Theme } from "../theme/ThemeProvider";

const THEME_LABEL: Record<Theme, string> = { dark: "Dark", sepia: "Sepia", light: "Light" };
const THEME_GLYPH: Record<Theme, string> = { dark: "☾", sepia: "◐", light: "☀" };

export function ThemeSwitcher() {
  const { theme, setTheme } = useTheme();
  return (
    <div className="theme-switch" role="group" aria-label="Theme">
      {THEMES.map((t) => (
        <button
          key={t}
          type="button"
          className={`seg${theme === t ? " active" : ""}`}
          aria-pressed={theme === t}
          onClick={() => setTheme(t)}
        >
          <span aria-hidden="true">{THEME_GLYPH[t]}</span>
          {THEME_LABEL[t]}
        </button>
      ))}
    </div>
  );
}
