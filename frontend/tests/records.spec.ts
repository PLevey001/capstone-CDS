import { expect, test, chromium, type Page } from "@playwright/test";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const headers = { "X-CDS-Request": "local-ui" };

async function importHistory(page: Page, buffer?: Buffer) {
  const name = `Chrome ${Date.now()}`;
  const created = await page.request.post("/api/cases", {
    headers,
    data: { name },
  });
  const caseId = (await created.json()).id;
  const bytes =
    buffer ?? (await (await page.request.get("/test/chrome-file")).body());
  await page.goto("/");
  await page.getByRole("button", { name, exact: true }).click();
  await page.getByRole("button", { name: "Add evidence", exact: true }).click();
  await page.locator("input[type=file]").setInputFiles({
    name: "History",
    mimeType: "application/octet-stream",
    buffer: bytes,
  });
  await page
    .getByRole("button", { name: "Import 1 source", exact: true })
    .click();
  await page.getByRole("button", { name: "View analysis" }).click();
  const processed = await page.request.post("/test/process", { headers });
  expect(processed.ok()).toBeTruthy();
  const run = (await processed.json()).run;
  const source = (
    await (await page.request.get(`/api/cases/${caseId}/evidence`)).json()
  )[0].id;
  await page.getByRole("button", { name: /^History Logical file/ }).click();
  await expect(page.getByText("Viewing run 1", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /^Records \d/ }).click();
  return { caseId, source, run };
}

test("import, page, search, and export visits with exact timestamps", async ({
  page,
}) => {
  const fixture = await importHistory(page);
  await expect(page.getByText("1–50 of 120 records")).toBeVisible();
  await page.getByRole("button", { name: "Next record page" }).click();
  await expect(page.getByText("51–100 of 120 records")).toBeVisible();
  await page.locator(".record-row").first().click();
  const selected = page.getByRole("region", {
    name: "Selected record",
    exact: true,
  });
  await expect(selected).toContainText("2023-11-14 23:03:20.123456 UTC");
  await expect(selected).toContainText(fixture.run);
  await expect(selected.locator("a")).toHaveCount(0);
  const jsonLink = page.getByRole("link", {
    name: "Export matching records JSON",
  });
  const all = await (
    await page.request.get((await jsonLink.getAttribute("href"))!)
  ).json();
  expect(all.items).toHaveLength(120);
  await page.getByLabel("Search records").fill("%_");
  await expect(page.getByText("1–1 of 1 records")).toBeVisible();
  await page.locator(".record-row").click();
  await expect(selected).toContainText("=2+2");
  const filtered = await (
    await page.request.get((await jsonLink.getAttribute("href"))!)
  ).json();
  expect(filtered.items).toHaveLength(1);
  expect(filtered.items[0].summary).toBe("=2+2");
  const csvLink = page.getByRole("link", {
    name: "Export matching records CSV",
  });
  const csv = await (
    await page.request.get((await csvLink.getAttribute("href"))!)
  ).text();
  expect(csv).toContain("'=2+2");
});

test("timeline opens the visit and source, and record selection stays on its saved run", async ({
  page,
}) => {
  const fixture = await importHistory(page);
  await page.locator(".record-row").first().click();
  await expect(
    page.getByRole("region", { name: "Selected record", exact: true }),
  ).toContainText(fixture.run);
  expect(
    (
      await page.request.post(`/api/evidence/${fixture.source}/retry`, {
        headers,
      })
    ).ok(),
  ).toBeTruthy();
  expect(
    (await page.request.post("/test/process", { headers })).ok(),
  ).toBeTruthy();
  await page.waitForResponse((response) =>
    response.url().endsWith(`/cases/${fixture.caseId}/evidence`),
  );
  await expect(
    page.getByRole("combobox", { name: /Analysis history/ }),
  ).toHaveValue(fixture.run);
  await expect(
    page.getByRole("region", { name: "Selected record", exact: true }),
  ).toContainText(fixture.run);
  await page
    .getByRole("combobox", { name: /Analysis history/ })
    .selectOption("");
  await expect(page.getByText("Viewing run 2", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Selected record", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Close evidence details" }).click();
  await page.getByRole("button", { name: "Timeline", exact: true }).click();
  await expect(page.getByText("1–100 of 120 timestamps")).toBeVisible();
  await page.locator(".timeline-event").first().click();
  await expect(
    page.getByRole("region", { name: "Selected record", exact: true }),
  ).toContainText("2023-11-14 22:13:20.123456 UTC");
  await page
    .getByRole("button", { name: "Open source artifact: History" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Source artifact", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".timeline-artifact")).toContainText("sha256");
});

test("an earlier run without parsed records remains understandable", async ({
  page,
}) => {
  const fixture = await (
    await page.request.post("/test/seed", { headers })
  ).json();
  await page.goto("/");
  await page
    .getByRole("button", { name: fixture.case.name, exact: true })
    .click();
  await page.getByRole("button", { name: /fixture.img/ }).click();
  await page.getByRole("button", { name: "Records 0", exact: true }).click();
  await expect(
    page.getByText(
      "No parsed records saved for this run. Check its coverage for what was examined.",
    ),
  ).toBeVisible();
  await expect(page.getByText("Viewing run 1", { exact: true })).toBeVisible();
});

test("a fresh Chromium profile supplies independently recorded visits", async ({
  page,
}) => {
  const profile = await mkdtemp(join(tmpdir(), "cds-chromium-fixture-"));
  try {
    const context = await chromium.launchPersistentContext(profile, {
      channel: "chromium",
      headless: true,
    });
    const started = Date.now();
    try {
      const tab = await context.newPage();
      await tab.goto("http://127.0.0.1:8765/test/visited/first");
      await tab.goto("http://127.0.0.1:8765/test/visited/second");
      await tab.goto("http://127.0.0.1:8765/test/visited/first");
    } finally {
      await context.close();
    }
    const finished = Date.now();
    const fixture = await importHistory(
      page,
      await readFile(join(profile, "Default", "History")),
    );
    const data = await (
      await page.request.get(`/api/evidence/${fixture.source}/records`)
    ).json();
    expect(data.items).toHaveLength(3);
    expect(
      data.items.map((r: { details: { url: string } }) => r.details.url),
    ).toEqual([
      "http://127.0.0.1:8765/test/visited/first",
      "http://127.0.0.1:8765/test/visited/second",
      "http://127.0.0.1:8765/test/visited/first",
    ]);
    for (const record of data.items) {
      expect(Date.parse(record.at)).toBeGreaterThanOrEqual(started);
      expect(Date.parse(record.at)).toBeLessThanOrEqual(finished);
    }
    await expect(page.getByText("1–3 of 3 records")).toBeVisible();
    await page.locator(".record-row").first().click();
    await expect(
      page.getByRole("region", { name: "Selected record", exact: true }),
    ).toContainText("Browser calibration page");
  } finally {
    await rm(profile, { recursive: true, force: true });
  }
});

test("record failures can be retried and delayed searches cannot replace newer results", async ({
  page,
}) => {
  const fixture = await importHistory(page);
  const route = `**/api/evidence/${fixture.source}/records?*`;
  await page.route(route, (handler) =>
    handler.fulfill({
      status: 503,
      json: { detail: "Records temporarily unavailable" },
    }),
  );
  await page.getByLabel("Search records").fill("unavailable");
  await expect(page.getByRole("alert")).toHaveText(
    "Records temporarily unavailable",
  );
  await page.unrouteAll({ behavior: "wait" });
  await page.getByRole("button", { name: "Retry records" }).click();
  await expect(
    page.getByText("No matching records in this run."),
  ).toBeVisible();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let started!: () => void;
  const requested = new Promise<void>((resolve) => {
    started = resolve;
  });
  await page.route(route, async (handler) => {
    if (new URL(handler.request().url()).searchParams.get("q") !== "research") {
      await handler.continue();
      return;
    }
    const response = await handler.fetch();
    started();
    await gate;
    await handler.fulfill({ response });
  });
  await page.getByLabel("Search records").fill("research");
  await requested;
  await page.getByLabel("Search records").fill("%_");
  await expect(page.getByText("1–1 of 1 records")).toBeVisible();
  release();
  await page.unrouteAll({ behavior: "wait" });
  await expect(page.locator(".record-row")).toHaveCount(1);
  await expect(page.locator(".record-row")).toContainText("=2+2");
});
