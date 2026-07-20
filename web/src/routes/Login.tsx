// =============================================================================
// web/src/routes/Login.tsx
// -----------------------------------------------------------------------------
// Responsible for: The full-bleed login. Offers name+password (the interactive
//                  credential since the auth revamp) with an access-key fallback for
//                  accounts that have no password yet. On success re-reads /api/me
//                  and navigates home.
// Role in project: The entry point for a gated relay. Full-bleed (no shell). An
//                  already-authenticated viewer (or an open relay) is bounced home.
// =============================================================================

import { useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import type { Me } from "../api/types";
import { login, loginWithPassword } from "../api/client";
import { ThemeSwitcher } from "../components/ThemeSwitcher";

interface LoginProps {
  me: Me;
  /** Re-read /api/me after a successful login (so the app picks up the new identity). */
  onAuthChange: () => void | Promise<void>;
}

export function Login({ me, onAuthChange }: LoginProps) {
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [key, setKey] = useState("");
  // Password is the default: it is what humans have after the auth revamp. The key mode
  // stays reachable for accounts that have not been given a password yet, so nobody is
  // locked out during the transition.
  const [useKey, setUseKey] = useState(false);
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
      const result = useKey ? await login(key) : await loginWithPassword(name, password);
      if (result.ok) {
        await onAuthChange();
        navigate("/");
      } else {
        // ONE generic message, mirroring the relay's single generic 401. Saying "no such
        // user" or "wrong password" would answer "does this account exist?" for free.
        setError(failureMessage);
      }
    } catch {
      setError(failureMessage);
    } finally {
      setBusy(false);
    }
  };

  const failureMessage = useKey
    ? "Invalid or expired access key."
    : "Invalid credentials, or the account is temporarily locked.";
  const canSubmit = useKey ? Boolean(key) : Boolean(name && password);

  return (
    <div className="login-screen">
      <div className="brand brand-login">
        <span className="brand-dot" aria-hidden="true" />
        <span>Orion</span>
      </div>

      <form className="login-card" onSubmit={submit}>
        <h1>Sign in</h1>
        <p className="login-sub">
          {useKey
            ? "Enter your access key. You'll see only the projects you've been granted."
            : "Sign in with your name and password. You'll see only the projects you've been granted."}
        </p>

        {useKey ? (
          <>
            <label className="field-label" htmlFor="access-key">
              Access key
            </label>
            <input
              id="access-key"
              className="field"
              type="password"
              autoComplete="one-time-code"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              autoFocus
            />
          </>
        ) : (
          <>
            <label className="field-label" htmlFor="account-name">
              Name
            </label>
            <input
              id="account-name"
              className="field"
              type="text"
              autoComplete="username"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
            />

            <label className="field-label" htmlFor="account-password">
              Password
            </label>
            <input
              id="account-password"
              className="field"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </>
        )}

        <button className="btn-primary" type="submit" disabled={busy || !canSubmit}>
          {busy ? "Signing in…" : "Sign in"}
        </button>

        {/* The transition escape hatch: an account that has not been given a password yet
            still signs in with its key. Once it HAS one, the relay stops accepting the key
            here, so this link becomes a dead end for that account by design. */}
        <button
          type="button"
          className="login-mode-toggle"
          onClick={() => {
            setUseKey((v) => !v);
            setError(null);
          }}
        >
          {useKey ? "Sign in with a name and password instead" : "Sign in with an access key instead"}
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
