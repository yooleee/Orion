// =============================================================================
// web/src/lib/useApiData.ts
// -----------------------------------------------------------------------------
// Responsible for: A tiny hook that loads data from the API client and tracks
//                  loading / error state — the shared pattern the Project and Report
//                  screens use to fetch their own data.
// Role in project: Keeps each route from re-implementing the same effect + state. The
//                  lightest thing that works (no data library); a 404 is surfaced via
//                  the ApiError.status so a screen can show a clean not-found.
// =============================================================================

import { useEffect, useState } from "react";
import { ApiError } from "../api/client";

export interface Async<T> {
  data: T | null;
  error: ApiError | Error | null;
  loading: boolean;
}

/**
 * Load `loader()` once per change in `deps`, tracking loading/error/data.
 *
 * Args:
 *   loader: an async function returning the data (e.g. () => getProject(name)).
 *   deps: the dependency list that re-triggers the load (e.g. [name]).
 *
 * Returns: {data, error, loading}. `error` carries an ApiError (with .status) on an API
 * failure, so a caller can branch on 404.
 *
 * Why: an `alive` flag guards against a late response setting state after the component
 * unmounted or the deps changed (the classic effect race), so a quick navigation between
 * projects never shows the previous project's data.
 */
export function useApiData<T>(loader: () => Promise<T>, deps: unknown[]): Async<T> {
  const [state, setState] = useState<Async<T>>({ data: null, error: null, loading: true });

  useEffect(() => {
    let alive = true;
    setState({ data: null, error: null, loading: true });
    loader().then(
      (data) => {
        if (alive) setState({ data, error: null, loading: false });
      },
      (error) => {
        if (alive) setState({ data: null, error, loading: false });
      },
    );
    return () => {
      alive = false;
    };
    // The loader closes over deps; we intentionally key the effect on deps, not loader.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return state;
}
