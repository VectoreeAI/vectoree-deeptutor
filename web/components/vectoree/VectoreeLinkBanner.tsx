"use client";

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { fetchVectoreeLinkStatus } from "@/lib/vectoree-link";

/** Soft settings note once a project key is already stored. */
export default function VectoreeLinkBanner() {
  const { t } = useTranslation();
  const [name, setName] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchVectoreeLinkStatus().then((status) => {
      if (cancelled || !status?.linked) return;
      setName(status.projectName?.trim() || "");
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (name === null) return null;

  return (
    <p className="mb-6 rounded-lg border border-[var(--border)] bg-[var(--card)] px-4 py-3 text-sm text-[var(--muted-foreground)]">
      {name
        ? t("Vectoree is linked to {{name}}.", { name })
        : t("Vectoree is linked.")}
    </p>
  );
}
