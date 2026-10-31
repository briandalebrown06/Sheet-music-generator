import { useCallback, useEffect, useRef, useState } from "react";
import { AudioLines, LoaderCircle, LogIn, ShieldCheck } from "lucide-react";
import { request, setApiSession } from "./api";
import type { AuthSession } from "./api";

export default function AuthBoundary({
  children,
}: {
  children: (
    session: AuthSession,
    logout: () => Promise<void>,
  ) => React.ReactNode;
}) {
  const [session, setSession] = useState<AuthSession | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const channel = useRef<BroadcastChannel | null>(null);
  const generation = useRef(0);
  const load = useCallback(async () => {
    const current = ++generation.current;
    try {
      const value = await request<AuthSession>("/auth/session");
      if (current !== generation.current) return;
      setApiSession(value);
      setSession(value);
      setError("");
    } catch (reason) {
      if (current !== generation.current) return;
      // A failed connectivity check is not evidence that a valid session ended.
      // Retain an open draft; an actual 401 still clears the whole workspace.
      setError((reason as Error).message);
    }
  }, []);
  useEffect(() => {
    void load();
    const expired = () => {
      generation.current++;
      setApiSession(null);
      setSession({
        authentication_required: true,
        user: null,
        csrf_token: null,
      });
      setPassword("");
      setError("Your session ended. Sign in again to continue.");
    };
    window.addEventListener("scorecraft-session-expired", expired);
    const check = () => {
      if (document.visibilityState === "visible") void load();
    };
    document.addEventListener("visibilitychange", check);
    const timer = setInterval(check, 60000);
    if (typeof BroadcastChannel !== "undefined") {
      channel.current = new BroadcastChannel("scorecraft-session");
      channel.current.onmessage = (event) => {
        if (event.data === "signed-out") expired();
        else if (event.data === "signed-in") void load();
      };
    }
    return () => {
      generation.current++;
      clearInterval(timer);
      window.removeEventListener("scorecraft-session-expired", expired);
      document.removeEventListener("visibilitychange", check);
      channel.current?.close();
      channel.current = null;
    };
  }, [load]);
  const login = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await request("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      setPassword("");
      await load();
      channel.current?.postMessage("signed-in");
    } catch (reason) {
      setPassword("");
      setError((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const logout = async () => {
    if (busy) return;
    // Unmount all private players/editors while revoking the session. Recording
    // cleanup releases microphone tracks through the component's own effect.
    setBusy(true);
    const previous = session;
    setSession(null);
    generation.current++;
    try {
      await request("/auth/logout", { method: "POST" });
      setApiSession(null);
      setSession({
        authentication_required: true,
        user: null,
        csrf_token: null,
      });
      setError("");
      setPassword("");
      channel.current?.postMessage("signed-out");
    } catch (reason) {
      setSession(previous);
      setError(`Sign out could not be confirmed. ${(reason as Error).message}`);
    } finally {
      setBusy(false);
    }
  };
  if (session && (!session.authentication_required || session.user)) {
    return (
      <>
        {error && (
          <div className="session-alert notice error" role="alert">
            {error}
          </div>
        )}
        {children(session, logout)}
      </>
    );
  }
  return (
    <main className="auth-page">
      <section className="card auth-card" aria-labelledby="auth-heading">
        <div className="brand">
          <span className="brand-mark">
            <AudioLines size={24} />
          </span>
          scorecraft.
        </div>
        <span className="eyebrow">PRIVATE WORKSPACE</span>
        <h1 id="auth-heading">
          {session ? "Sign in to your studio" : "Connecting to your studio"}
        </h1>
        {error && (
          <p className="notice error" role="alert">
            {error}
          </p>
        )}
        {session ? (
          <form onSubmit={login}>
            <label>
              Username
              <input
                name="username"
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                required
                maxLength={80}
                value={username}
                onChange={(event) => setUsername(event.target.value)}
              />
            </label>
            <label>
              Password
              <input
                name="password"
                type="password"
                autoComplete="current-password"
                required
                maxLength={256}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </label>
            <button className="button primary" type="submit" disabled={busy}>
              {busy ? (
                <LoaderCircle className="spin" size={17} />
              ) : (
                <LogIn size={17} />
              )}{" "}
              {busy ? "Signing in…" : "Sign in"}
            </button>
            <p className="input-help">
              Use an account provided by this studio's administrator. Public
              account registration is not enabled.
            </p>
          </form>
        ) : error ? (
          <button className="button secondary" onClick={() => void load()}>
            Retry connection
          </button>
        ) : (
          <p role="status">
            <LoaderCircle className="spin" size={18} />{" "}
            {busy ? "Signing out…" : "Checking access…"}
          </p>
        )}
        <p className="privacy-caption">
          <ShieldCheck size={14} /> Recordings and scores require your account
          to access.
        </p>
      </section>
    </main>
  );
}
