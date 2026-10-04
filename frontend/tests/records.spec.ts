import { expect, test, chromium, firefox, type Page } from "@playwright/test";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const headers = { "X-CDS-Request": "local-ui" };

async function importHistory(
  page: Page,
  buffer?: Buffer,
  filename = "History",
) {
  const name = `${filename} ${Date.now()}`;
  const created = await page.request.post("/api/cases", {
    headers,
    data: { name },
  });
  const caseId = (await created.json()).id;
  const bytes =
    buffer ??
    (await (
      await page.request.get(
        filename === "places.sqlite"
          ? "/test/firefox-file"
          : "/test/chrome-file",
      )
    ).body());
  await page.goto("/");
  await page.getByRole("button", { name, exact: true }).click();
  await page.getByRole("button", { name: "Add evidence", exact: true }).click();
  await page.locator("input[type=file]").setInputFiles({
    name: filename,
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
  await page
    .getByRole("button", {
      name: new RegExp(`^${filename.replaceAll(".", "\\.")} Logical file`),
    })
    .click();
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

for (const browserType of [chromium, firefox]) {
  test(`a fresh ${browserType.name()} profile supplies independently recorded visits`, async ({
    page,
  }, testInfo) => {
    const isFirefox = browserType === firefox;
    const filename = isFirefox ? "places.sqlite" : "History";
    const profile = await mkdtemp(
      join(tmpdir(), `cds-${browserType.name()}-fixture-`),
    );
    try {
      const context = await browserType.launchPersistentContext(
        profile,
        isFirefox
          ? {
              headless: true,
              firefoxUserPrefs: {
                "places.history.enabled": true,
                "browser.privatebrowsing.autostart": false,
              },
            }
          : { channel: "chromium", headless: true },
      );
      testInfo.annotations.push({
        type: "fixture-browser",
        description: context.browser()?.version() ?? browserType.name(),
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
      const path = isFirefox
        ? join(profile, filename)
        : join(profile, "Default", filename);
      const fixture = await importHistory(page, await readFile(path), filename);
      const data = await (
        await page.request.get(`/api/evidence/${fixture.source}/records`)
      ).json();
      expect(data.items).toHaveLength(3);
      expect(
        data.items.map(
          (record: { details: { url: string } }) => record.details.url,
        ),
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
      const selected = page.getByRole("region", {
        name: "Selected record",
        exact: true,
      });
      await expect(selected).toContainText("Browser calibration page");
      await expect(selected).toContainText(
        isFirefox ? "Firefox" : "Chrome/Chromium",
      );
    } finally {
      await rm(profile, { recursive: true, force: true });
    }
  });
}

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

test("Firefox uses the same paging, export, and mixed-browser timeline navigation", async ({
  page,
}) => {
  const fixture = await importHistory(page, undefined, "places.sqlite");
  await expect(page.getByText("1–50 of 120 records")).toBeVisible();
  await page.getByRole("button", { name: "Next record page" }).click();
  await expect(page.getByText("51–100 of 120 records")).toBeVisible();
  await page.locator(".record-row").first().click();
  const selected = page.getByRole("region", {
    name: "Selected record",
    exact: true,
  });
  await expect(selected).toContainText("Firefox");
  await expect(selected).toContainText("2023-11-14 23:03:20.123456 UTC");
  await page.getByLabel("Search records").fill("%_");
  await expect(page.getByText("1–1 of 1 records")).toBeVisible();
  const jsonLink = page.getByRole("link", {
    name: "Export matching records JSON",
  });
  const exported = await (
    await page.request.get((await jsonLink.getAttribute("href"))!)
  ).json();
  expect(exported.items).toHaveLength(1);
  expect(exported.items[0].details.browser).toBe("Firefox");
  expect(exported.items[0].details.visit_type).toBe(1);
  const chromeFile = await (await page.request.get("/test/chrome-file")).body();
  expect(
    (
      await page.request.post(
        `/api/cases/${fixture.caseId}/evidence?filename=History`,
        { headers, data: chromeFile },
      )
    ).ok(),
  ).toBeTruthy();
  expect(
    (await page.request.post("/test/process", { headers })).ok(),
  ).toBeTruthy();
  await page.getByRole("button", { name: "Close evidence details" }).click();
  await page.getByRole("button", { name: "Timeline", exact: true }).click();
  await expect(page.getByText("1–100 of 240 timestamps")).toBeVisible();
  await page
    .locator(".timeline-event")
    .filter({ hasText: "places.sqlite" })
    .first()
    .click();
  await expect(selected).toContainText("Firefox");
  await expect(selected).toContainText(fixture.run);
  await page
    .getByRole("button", { name: "Open source artifact: places.sqlite" })
    .click();
  await expect(page.locator(".timeline-artifact")).toContainText(
    "places.sqlite",
  );
  await expect(page.locator(".timeline-artifact")).toContainText(fixture.run);
});
