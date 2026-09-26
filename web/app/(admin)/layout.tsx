import VectoreeLinkGate from "@/components/vectoree/VectoreeLinkGate";

export default function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <VectoreeLinkGate>
      <div className="min-h-screen bg-[var(--background)]">{children}</div>
    </VectoreeLinkGate>
  );
}
