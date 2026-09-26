const UUID =
  /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

/** Pull a project UUID out of a raw id or a Console URL / dashboard paste. */
export function extractProjectId(text: string): string | null {
  const dashboard = text.match(
    /Dashboard project:[^\n]*\(([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\)/i,
  );
  if (dashboard?.[1]) return dashboard[1];
  const any = text.match(UUID);
  return any ? any[0] : null;
}
