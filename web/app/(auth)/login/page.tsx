"use client";

import { Suspense, useCallback, useState, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslation } from "react-i18next";
import {
  fetchVectoreeSession,
  login,
  register,
  resendVectoreeCode,
  verifyVectoreeEmail,
} from "@/lib/auth";
import {
  inheritLoginHash,
  normalizeInternalReturnPath,
} from "@/shared/auth/return-url";

function LoginPageContent() {
  const { t } = useTranslation();
  const router = useRouter();
  const searchParams = useSearchParams();
  const next = normalizeInternalReturnPath(searchParams.get("next"));
  const resolvedNext = useCallback(
    () =>
      inheritLoginHash(
        next,
        typeof window === "undefined" ? "" : window.location.hash,
      ),
    [next],
  );

  const registered = searchParams.get("registered") === "1";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [otp, setOtp] = useState("");
  const [needsCode, setNeedsCode] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchVectoreeSession().then((session) => {
      if (cancelled) return;
      if (session && !session.linked) {
        router.replace("/link");
        return;
      }
      if (session?.authenticated) {
        router.replace(resolvedNext());
        return;
      }
      setReady(true);
    });
    return () => {
      cancelled = true;
    };
  }, [router, resolvedNext]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);

    if (needsCode) {
      const verified = await verifyVectoreeEmail(email, otp);
      setLoading(false);
      if (verified.ok) {
        router.replace(resolvedNext());
        return;
      }
      setError(verified.error ?? t("Login failed"));
      return;
    }

    const result = creating
      ? await register(email, password)
      : await login(email, password);

    setLoading(false);
    if (!result.ok) {
      setError(result.error ?? t("Login failed"));
      return;
    }
    if (result.requireEmailVerification) {
      setNeedsCode(true);
      return;
    }
    router.replace(resolvedNext());
  }

  return (
    <div className="w-full max-w-sm">
      {/* Logo / Title */}
      <div className="text-center mb-8">
        <h1 className="font-serif text-2xl font-semibold text-[var(--foreground)] tracking-tight">
          DeepTutor
        </h1>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          {needsCode
            ? t("Enter the 8-digit code sent to your email.")
            : creating
              ? t("Create a Vectoree account")
              : t("Sign in with your Vectoree account")}
        </p>
      </div>

      {/* Registered success notice */}
      {registered && (
        <div className="mb-4 rounded-lg border border-green-500/30 bg-green-500/10 px-4 py-3 text-sm text-green-600 dark:text-green-400">
          {t("Account created! Sign in to continue.")}
        </div>
      )}

      {/* Card */}
      <div className="bg-[var(--card)] border border-[var(--border)] rounded-2xl shadow-sm px-8 py-8">
        {!ready ? (
          <p className="text-center text-sm text-[var(--muted-foreground)]">
            {t("Checking Vectoree…")}
          </p>
        ) : (
        <form onSubmit={handleSubmit} className="space-y-5">
          {/* Email or username */}
          <div>
            <label
              htmlFor="username"
              className="block text-sm font-medium text-[var(--foreground)] mb-1.5"
            >
              {t("Email")}
            </label>
            <input
              id="username"
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full px-3.5 py-2.5 rounded-lg border border-[var(--border)]
                         bg-[var(--background)] text-[var(--foreground)]
                         placeholder:text-[var(--muted-foreground)]
                         focus:outline-none focus:ring-2 focus:ring-[var(--primary)] focus:border-transparent
                         transition-shadow text-sm"
              placeholder="you@example.com"
            />
          </div>

          {/* Password */}
          <div>
            <label
              htmlFor="password"
              className="block text-sm font-medium text-[var(--foreground)] mb-1.5"
            >
              {needsCode ? t("Verification code") : t("Password")}
            </label>
            {needsCode ? (
            <input
              id="otp"
              inputMode="numeric"
              autoComplete="one-time-code"
              required
              minLength={8}
              maxLength={8}
              pattern="\d{8}"
              value={otp}
              onChange={(e) => setOtp(e.target.value.replace(/\D/g, "").slice(0, 8))}
              className="w-full px-3.5 py-2.5 rounded-lg border border-[var(--border)]
                         bg-[var(--background)] text-[var(--foreground)]
                         placeholder:text-[var(--muted-foreground)]
                         focus:outline-none focus:ring-2 focus:ring-[var(--primary)] focus:border-transparent
                         transition-shadow text-sm"
              placeholder="00000000"
            />
            ) : (
            <input
              id="password"
              type="password"
              autoComplete={creating ? "new-password" : "current-password"}
              required
              minLength={creating ? 8 : 1}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full px-3.5 py-2.5 rounded-lg border border-[var(--border)]
                         bg-[var(--background)] text-[var(--foreground)]
                         placeholder:text-[var(--muted-foreground)]
                         focus:outline-none focus:ring-2 focus:ring-[var(--primary)] focus:border-transparent
                         transition-shadow text-sm"
              placeholder="••••••••"
            />
            )}
          </div>

          {/* Error message */}
          {error && (
            <p className="text-sm text-red-500 bg-red-500/10 rounded-lg px-3 py-2">
              {error}
            </p>
          )}

          {/* Submit */}
          <button
            type="submit"
            disabled={loading}
            className="w-full py-2.5 px-4 rounded-lg font-medium text-sm
                       bg-[var(--primary)] text-[var(--primary-foreground)]
                       hover:opacity-90 active:opacity-80
                       disabled:opacity-50 disabled:cursor-not-allowed
                       transition-opacity"
          >
            {loading
              ? t("Signing in…")
              : needsCode
                ? t("Sign in")
                : creating
                  ? t("Create account")
                  : t("Sign in")}
          </button>
          {needsCode && (
            <button
              type="button"
              disabled={loading}
              onClick={() => {
                setError("");
                void resendVectoreeCode(email).then((resent) => {
                  if (!resent.ok) setError(resent.error ?? t("Login failed"));
                });
              }}
              className="w-full py-2.5 px-4 rounded-lg font-medium text-sm border border-[var(--border)] text-[var(--foreground)] hover:bg-[var(--muted)] disabled:opacity-50"
            >
              {t("Resend code")}
            </button>
          )}
        </form>
        )}
      </div>

      {ready && !needsCode && (
      <p className="mt-6 text-center text-sm text-[var(--muted-foreground)]">
        {creating ? t("Already have an account?") : t("Don't have an account?")}{" "}
        <button
          type="button"
          onClick={() => {
            setCreating((value) => !value);
            setError("");
          }}
          className="text-[var(--primary)] hover:underline font-medium"
        >
          {creating ? t("Sign in") : t("Create one")}
        </button>
      </p>
      )}

      <p className="mt-3 text-center text-xs text-[var(--muted-foreground)]">
        {t("DeepTutor · Agent-Native Learning")}
      </p>
    </div>
  );
}

export default function LoginPage() {
  const { t } = useTranslation();
  return (
    <Suspense
      fallback={
        <div className="w-full max-w-sm text-center text-sm text-[var(--muted-foreground)]">
          {t("Loading sign in...")}
        </div>
      }
    >
      <LoginPageContent />
    </Suspense>
  );
}
