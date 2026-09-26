import { CapabilityAccessProvider } from "@/components/access/CapabilityAccessContext";
import CapabilityGate from "@/components/access/CapabilityGate";
import VectoreeLinkGate from "@/components/vectoree/VectoreeLinkGate";

/** Settings owns its navigation, independently of the conversation sidebar. */
export default function SettingsRouteLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <CapabilityAccessProvider>
      <VectoreeLinkGate>
        <CapabilityGate>{children}</CapabilityGate>
      </VectoreeLinkGate>
    </CapabilityAccessProvider>
  );
}
