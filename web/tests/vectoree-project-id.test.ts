import test from "node:test";
import assert from "node:assert/strict";
import { extractProjectId } from "../lib/vectoree-project-id";

const PROJECT_ID = "11111111-2222-4333-8444-555555555555";

test("extractProjectId reads a bare UUID and a console URL", () => {
  assert.equal(extractProjectId(PROJECT_ID), PROJECT_ID);
  assert.equal(
    extractProjectId(`https://vectoree.ai/dashboard/projects/${PROJECT_ID}`),
    PROJECT_ID,
  );
  assert.equal(
    extractProjectId(`Dashboard project: Demo (${PROJECT_ID})`),
    PROJECT_ID,
  );
  assert.equal(extractProjectId("not a project"), null);
});
