import UtilitySidebar from "@/components/sidebar/UtilitySidebar";
import AppShell from "@/components/layout/AppShell";
import { CapabilityAccessProvider } from "@/components/access/CapabilityAccessContext";
import CapabilityGate from "@/components/access/CapabilityGate";
import VectoreeLinkGate from "@/components/vectoree/VectoreeLinkGate";

export default function UtilityLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <CapabilityAccessProvider>
      <AppShell sidebar={<UtilitySidebar />}>
        <VectoreeLinkGate>
          <CapabilityGate>{children}</CapabilityGate>
        </VectoreeLinkGate>
      </AppShell>
    </CapabilityAccessProvider>
  );
}
