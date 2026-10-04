import { existsSync } from "node:fs";
import { defineConfig, devices } from "@playwright/test";

const python = existsSync("../.venv/bin/python")
  ? ".venv/bin/python"
  : "python3";

export default defineConfig({
  testDir: "./tests",
  workers: 1,
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:8765",
    // Prove the controls use UTC even when the browser does not.
    timezoneId: "America/New_York",
    trace: "retain-on-failure",
  },
  webServer: {
    command: `${python} -m tests.browser_server`,
    cwd: "..",
    url: "http://127.0.0.1:8765/api/health",
    reuseExistingServer: false,
    gracefulShutdown: { signal: "SIGTERM", timeout: 5000 },
  },
});
