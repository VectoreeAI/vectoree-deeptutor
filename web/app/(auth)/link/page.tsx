"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";
import { extractProjectId } from "@/lib/vectoree-project-id";
import {
  fetchVectoreeLinkPoll,
  fetchVectoreeLinkStatus,
  startVectoreeLink,
  type VectoreeLinkPoll,
} from "@/lib/vectoree-link";

const inputClass =
  "w-full px-3.5 py-2.5 rounded-lg border border-[var(--border)] bg-[var(--background)] text-[var(--foreground)] placeholder:text-[var(--muted-foreground)] focus:outline-none focus:ring-2 focus:ring-[var(--primary)] focus:border-transparent transition-shadow text-sm";

export default function VectoreeLinkPage() {
  const { t } = useTranslation();
  const router = useRouter();
  const [apiUrl, setApiUrl] = useState("https://vectoree.ai");
  const [paste, setPaste] = useState("");
  const [phase, setPhase] = useState<VectoreeLinkPoll["status"] | "starting">("idle");
  const [authorizeUrl, setAuthorizeUrl] = useState("");
  const [projectName, setProjectName] = useState("");
  const [error, setError] = useState("");

  const projectId = extractProjectId(paste);

  useEffect(() => {
    let cancelled = false;
    fetchVectoreeLinkStatus().then((status) => {
      if (cancelled || !status?.linked) return;
      setProjectName(status.projectName ?? "");
      setPhase("linked");
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (phase !== "pending") return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      fetchVectoreeLinkPoll().then((poll) => {
        if (cancelled || !poll) return;
        if (poll.authorizeUrl) setAuthorizeUrl(poll.authorizeUrl);
        if (poll.projectName) setProjectName(poll.projectName);
        if (poll.status === "linked" || poll.status === "error") {
          setPhase(poll.status);
          if (poll.status === "error") setError(poll.message || t("Could not start Vectoree link"));
        }
      });
    }, 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [phase, t]);

  async function connect(event: React.FormEvent) {
    event.preventDefault();
    if (!projectId) {
      setError(t("Paste a Vectoree project ID or a Console URL that contains one."));
      return;
    }
    setError("");
    setPhase("starting");
    const result = await startVectoreeLink({ apiUrl, projectId });
    if (!result.ok) {
      setPhase("error");
      setError(result.message || t("Could not start Vectoree link"));
      return;
    }
    setAuthorizeUrl(result.poll.authorizeUrl ?? "");
    setPhase(result.poll.status === "linked" ? "linked" : "pending");
    if (result.poll.projectName) setProjectName(result.poll.projectName);
  }

  const busy = phase === "starting" || phase === "pending";

  return (
    <div className="w-full max-w-sm">
      <div className="text-center mb-8">
        <h1 className="font-serif text-2xl font-semibold text-[var(--foreground)] tracking-tight">
          {t("Link Vectoree")}
        </h1>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          {t("Connect this install to a Vectoree project before chat can use its models.")}
        </p>
      </div>

      <div className="bg-[var(--card)] border border-[var(--border)] rounded-2xl shadow-sm px-8 py-8">
        {phase === "linked" ? (
          <div className="space-y-5">
            <p className="text-sm text-[var(--foreground)]">
              {projectName
                ? t("Linked to {{name}}", { name: projectName })
                : t("Vectoree is linked.")}
            </p>
            <p className="text-sm text-[var(--muted-foreground)]">
              {t(
                "If chat still uses the previous model, restart DeepTutor so it reloads the catalog.",
              )}
            </p>
            <button
              type="button"
              onClick={() => router.push("/chat")}
              className="w-full py-2.5 px-4 rounded-lg font-medium text-sm bg-[var(--primary)] text-[var(--primary-foreground)] hover:opacity-90"
            >
              {t("Continue to chat")}
            </button>
          </div>
        ) : (
          <form onSubmit={connect} className="space-y-5">
            <ol className="list-decimal space-y-1 pl-4 text-sm text-[var(--muted-foreground)]">
              <li>{t("Paste a project ID or Console URL.")}</li>
              <li>{t("Sign in on Vectoree.")}</li>
              <li>{t("DeepTutor saves a project key and points chat at that gateway.")}</li>
            </ol>

            <div>
              <label htmlFor="vectoree-api-url" className="block text-sm font-medium text-[var(--foreground)] mb-1.5">
                {t("API origin")}
              </label>
              <input
                id="vectoree-api-url"
                type="url"
                required
                value={apiUrl}
                onChange={(event) => setApiUrl(event.target.value)}
                className={inputClass}
                disabled={busy}
              />
            </div>

            <div>
              <label htmlFor="vectoree-project" className="block text-sm font-medium text-[var(--foreground)] mb-1.5">
                {t("Project ID or Console URL")}
              </label>
              <textarea
                id="vectoree-project"
                required
                rows={3}
                value={paste}
                onChange={(event) => setPaste(event.target.value)}
                className={inputClass}
                disabled={busy}
              />
              {projectId && (
                <p className="mt-1.5 text-xs text-[var(--muted-foreground)]">
                  {t("Recognized project")}{" "}
                  <span className="font-mono text-[var(--foreground)]">{projectId}</span>
                </p>
              )}
            </div>

            {error && (
              <p className="text-sm text-red-500 bg-red-500/10 rounded-lg px-3 py-2" role="alert">
                {error}
              </p>
            )}

            {phase === "pending" && (
              <div className="space-y-2 text-sm text-[var(--muted-foreground)]">
                <p>{t("Waiting for browser login…")}</p>
                {authorizeUrl && (
                  <a
                    href={authorizeUrl}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-block font-medium text-[var(--primary)] hover:underline"
                  >
                    {t("Open login")}
                  </a>
                )}
              </div>
            )}

            <button
              type="submit"
              disabled={busy || !projectId}
              className="w-full py-2.5 px-4 rounded-lg font-medium text-sm bg-[var(--primary)] text-[var(--primary-foreground)] hover:opacity-90 active:opacity-80 disabled:opacity-50 disabled:cursor-not-allowed transition-opacity"
            >
              {busy ? t("Connecting…") : t("Connect")}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
