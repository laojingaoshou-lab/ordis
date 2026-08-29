import { defineConfig, devices } from "playwright/test";

export default defineConfig({
  testDir: "./playwright-tests",
  timeout: 30_000,
  fullyParallel: false,
  reporter: "line",
  use: {
    baseURL: "https://github.com",
    ...devices["Desktop Chrome"],
    trace: "retain-on-failure",
  },
});
