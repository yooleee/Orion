// =============================================================================
// web/src/theme/ThemeProvider.tsx
// -----------------------------------------------------------------------------
// Responsible for: Holding the active theme (dark | sepia | light), persisting it,
//                  and reflecting it onto <html data-theme> so tokens.css applies.
// Role in project: The runtime half of the theming system. A theme switch is purely
//                  setting data-theme — no component re-render of markup, just the CSS
//                  variables changing (with the 0.25s transition from base.css).
// Why a context: the theme is global UI state read by the switcher (to show the active
//                segment) and set from multiple places (sidebar, login). A tiny context
//                is the lightest way to share it without prop-drilling — no state library.
// =============================================================================

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

export type Theme = "dark" | "sepia" | "light";

export const THEMES: Theme[] = ["dark", "sepia", "light"];

// Persisted under this key so a viewer's choice survives reloads. Sepia is the default
// (the user's preferred scheme), matching tokens.css's :root fallback.
const STORAGE_KEY = "orion-theme";
const DEFAULT_THEME: Theme = "sepia";

interface ThemeContextValue {
  theme: Theme;
  setTheme: (theme: Theme) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function readStoredTheme(): Theme {
  // localStorage can be unavailable (private mode, SSR) — fall back to the default rather
  // than throwing. A stored value outside the known set is ignored (defensive).
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && (THEMES as string[]).includes(stored)) {
      return stored as Theme;
    }
  } catch {
    /* ignore — use the default */
  }
  return DEFAULT_THEME;
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(readStoredTheme);

  // Reflect the theme onto the document root so tokens.css's [data-theme] blocks apply,
  // and persist it. Runs on mount (sets the initial attribute) and on every change.
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      /* ignore — persistence is best-effort */
    }
  }, [theme]);

  const setTheme = useCallback((next: Theme) => setThemeState(next), []);

  return (
    <ThemeContext.Provider value={{ theme, setTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

/**
 * Read the active theme and a setter.
 *
 * Why: components that show or change the theme (the switcher) need both; a hook keeps
 * the context access in one typed place and throws a clear error if used outside the
 * provider (a wiring mistake, caught early).
 */
export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (ctx === null) {
    throw new Error("useTheme must be used within a ThemeProvider");
  }
  return ctx;
}
