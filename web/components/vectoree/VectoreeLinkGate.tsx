"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";
import { fetchVectoreeLinkStatus } from "@/lib/vectoree-link";

/** Send an unlinked install to /link before chat tries to use a model. */
export default function VectoreeLinkGate({
  children,
}: {
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  const router = useRouter();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchVectoreeLinkStatus().then((status) => {
      if (cancelled) return;
      if (status?.linked !== true) {
        router.replace("/link");
        return;
      }
      setReady(true);
    });
    return () => {
      cancelled = true;
    };
  }, [router]);

  if (!ready) {
    return (
      <div className="flex h-full min-h-[40vh] items-center justify-center text-sm text-[var(--muted-foreground)]">
        {t("Checking Vectoree…")}
      </div>
    );
  }

  return children;
}
