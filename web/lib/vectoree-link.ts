import { apiFetch, apiUrl } from "@/lib/api";

export interface VectoreeLinkStatus {
  linked: boolean;
  projectName?: string;
  apiUrl?: string;
}

export interface VectoreeLinkPoll {
  status: "idle" | "pending" | "linked" | "error";
  authorizeUrl?: string;
  message?: string;
  apiUrl?: string;
  projectId?: string;
  projectName?: string;
}

export async function fetchVectoreeLinkStatus(): Promise<VectoreeLinkStatus | null> {
  try {
    const response = await apiFetch(apiUrl("/api/vectoree/link-status"));
    if (!response.ok) return null;
    const body = (await response.json()) as VectoreeLinkStatus;
    if (!body || typeof body.linked !== "boolean") return null;
    return body;
  } catch {
    return null;
  }
}

export async function fetchVectoreeLinkPoll(): Promise<VectoreeLinkPoll | null> {
  try {
    const response = await apiFetch(apiUrl("/api/vectoree/link"));
    if (!response.ok) return null;
    return (await response.json()) as VectoreeLinkPoll;
  } catch {
    return null;
  }
}

export async function startVectoreeLink(input: {
  apiUrl: string;
  projectId: string;
}): Promise<{ ok: true; poll: VectoreeLinkPoll } | { ok: false; message: string }> {
  try {
    const response = await apiFetch(apiUrl("/api/vectoree/link/start"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    });
    const body = (await response.json().catch(() => null)) as
      | VectoreeLinkPoll
      | { detail?: string }
      | null;
    if (!response.ok) {
      const detail =
        body && "detail" in body && typeof body.detail === "string"
          ? body.detail
          : "";
      return { ok: false, message: detail };
    }
    return { ok: true, poll: body as VectoreeLinkPoll };
  } catch {
    return { ok: false, message: "" };
  }
}
