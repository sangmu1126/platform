import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "@playwright/test";

const directory = mkdtempSync(join(tmpdir(), "sova-demo-browser-"));
export default defineConfig({
  testDir: ".",
  testMatch: "workflow.spec.mjs",
  workers: 1,
  timeout: 60_000,
  metadata: { demoDirectory: directory },
  use: {
    baseURL: "http://127.0.0.1:18132",
    viewport: { width: 1440, height: 1000 },
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  webServer: {
    cwd: fileURLToPath(new URL("../", import.meta.url)),
    command: `.venv/bin/python -m demo_workflow.server --port 18132 --new '${directory}'`,
    url: "http://127.0.0.1:18132/__demo/status",
    // A forbidden unauthenticated response proves the local server is listening.
    ignoreHTTPSErrors: false,
    reuseExistingServer: false,
    timeout: 20_000,
  },
});
