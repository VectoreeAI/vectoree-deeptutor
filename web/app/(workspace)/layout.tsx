import WorkspaceSidebar from "@/components/sidebar/WorkspaceSidebar";
import AppShell from "@/components/layout/AppShell";
import { CapabilityAccessProvider } from "@/components/access/CapabilityAccessContext";
import CapabilityGate from "@/components/access/CapabilityGate";
import { ChatRuntimeProvider } from "@/features/chat";
import { ReadingProvider } from "@/context/ReadingContext";
import { WatchingProvider } from "@/context/WatchingContext";
import { Suspense } from "react";
import { WorkspaceRuntimeBoundary } from "@/components/workspaces/WorkspaceRuntimeBoundary";
import VectoreeLinkGate from "@/components/vectoree/VectoreeLinkGate";

export default function WorkspaceLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <CapabilityAccessProvider>
      <Suspense>
      <WorkspaceRuntimeBoundary>
      <ChatRuntimeProvider>
        {/* Above the page on purpose: sending the first message navigates
            /chat → /chat/<id>, which remounts the page. The open document
            must not die with it. */}
        <ReadingProvider>
          <WatchingProvider>
            <AppShell sidebar={<WorkspaceSidebar />}>
              <VectoreeLinkGate>
                <CapabilityGate>{children}</CapabilityGate>
              </VectoreeLinkGate>
            </AppShell>
          </WatchingProvider>
        </ReadingProvider>
      </ChatRuntimeProvider>
      </WorkspaceRuntimeBoundary>
      </Suspense>
    </CapabilityAccessProvider>
  );
}
