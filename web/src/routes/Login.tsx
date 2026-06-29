// =============================================================================
// web/src/routes/Login.tsx
// -----------------------------------------------------------------------------
// Responsible for: The full-bleed access-key login. Posts the key to /api/login,
//                  and on success re-reads /api/me and navigates home.
// Role in project: The entry point for a gated relay. Full-bleed (no shell). An
//                  already-authenticated viewer (or an open relay) is bounced home.
// =============================================================================

import { useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import type { Me } from "../api/types";
import { login } from "../api/client";
import { ThemeSwitcher } from "../components/ThemeSwitcher";

interface LoginProps {
  me: Me;
  /** Re-read /api/me after a successful login (so the app picks up the new identity). */
  onAuthChange: () => void | Promise<void>;
}

export function Login({ me, onAuthChange }: LoginProps) {
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  // Nothing to log into when already signed in, or on an open (ungated) relay.
  if (me.authenticated || !me.gated) {
    return <Navigate to="/" replace />;
  }

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await login(key);
      if (result.ok) {
        await onAuthChange();
        navigate("/");
      } else {
        // Generic message — the server never reveals which part of the key failed.
        setError("Invalid or expired access key.");
      }
    } catch {
      setError("Invalid or expired access key.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-screen">
      <div className="brand brand-login">
        <span className="brand-dot" aria-hidden="true" />
        <span>Orion</span>
      </div>

      <form className="login-card" onSubmit={submit}>
        <h1>Sign in</h1>
        <p className="login-sub">
          Enter your personal access key. You'll see only the projects you've been granted.
        </p>

        <label className="field-label" htmlFor="access-key">
          Access key
        </label>
        <input
          id="access-key"
          className="field"
          type="password"
          autoComplete="current-password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          autoFocus
        />

        <button className="btn-primary" type="submit" disabled={busy || !key}>
          {busy ? "Signing in…" : "Sign in"}
        </button>

        {error && <div className="login-error">{error}</div>}
      </form>

      {/* A way out of the login wall for someone who arrived without the showcase link.
          Shown ONLY when the relay actually exposes a public showcase (else it'd dead-end). */}
      {me.showcase_enabled && (
        <Link to="/showcase" className="login-showcase-link">
          View the public showcase →
        </Link>
      )}

      <ThemeSwitcher />
    </div>
  );
}
