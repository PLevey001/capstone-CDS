import { expect, test, type Page } from "@playwright/test";

type Fixture = {
  case: { id: string; name: string };
  other: { id: string; name: string };
  source: string;
  run: string;
};
const headers = { "X-CDS-Request": "local-ui" };

async function openCase(page: Page): Promise<Fixture> {
  const response = await page.request.post("/test/seed", { headers });
  expect(response.ok()).toBeTruthy();
  const fixture: Fixture = await response.json();
  await page.goto("/");
  await page
    .getByRole("button", { name: fixture.case.name, exact: true })
    .click();
  await expect(page.getByRole("button", { name: /fixture.img/ })).toBeVisible();
  return fixture;
}

async function openTimeline(page: Page) {
  await page.getByRole("button", { name: "Timeline", exact: true }).click();
  await expect(page.getByText("1–100 of 205 timestamps")).toBeVisible();
}

test("UTC range is inclusive, validates order, and clears without fetching while hidden", async ({
  page,
}) => {
  let timelineRequests = 0;
  page.on("request", (request) => {
    if (request.url().includes("/timeline?")) timelineRequests++;
  });
  const fixture = await openCase(page);
  // Wait for a complete evidence polling cycle while the timeline is hidden.
  await page.waitForResponse((response) =>
    response.url().endsWith(`/cases/${fixture.case.id}/evidence`),
  );
  expect(timelineRequests).toBe(0);
  await openTimeline(page);
  await page.getByLabel("From (UTC)").fill("2023-11-14T22:13:20");
  await page.getByLabel("Through (UTC)").fill("2023-11-14T22:13:20");
  await page.getByRole("button", { name: "Apply", exact: true }).click();
  await expect(page.getByText("1–1 of 1 timestamps")).toBeVisible();
  await expect(page.locator(".timeline-events time")).toHaveText(
    "2023-11-14 22:13:20 UTC",
  );
  await page.getByLabel("From (UTC)").fill("2023-11-15T00:00");
  await page.getByRole("button", { name: "Apply", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText(
    "From must be earlier than or equal to Through.",
  );
  await page.getByRole("button", { name: "Clear", exact: true }).click();
  await expect(page.getByText("1–100 of 205 timestamps")).toBeVisible();
  await expect(page.getByLabel("From (UTC)")).toBeEmpty();
  await expect(page.getByLabel("Through (UTC)")).toBeEmpty();
  await page.getByLabel("From (UTC)").fill("2030-01-01T00:00");
  await page.getByRole("button", { name: "Apply", exact: true }).click();
  await expect(page.getByText("No timestamps in this range")).toBeVisible();
});

test("pages through all timestamps and opens an artifact beyond the first artifact page", async ({
  page,
}) => {
  const fixture = await openCase(page);
  await openTimeline(page);
  await page.getByRole("button", { name: "Next timeline page" }).click();
  await expect(page.getByText("101–200 of 205 timestamps")).toBeVisible();
  await page.getByRole("button", { name: "Next timeline page" }).click();
  await expect(page.getByText("201–205 of 205 timestamps")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Next timeline page" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Previous timeline page" }).click();
  await page.getByRole("button", { name: "Previous timeline page" }).click();
  await page
    .locator(".timeline-event")
    .filter({ hasText: "/file-000.txt" })
    .click();
  const selected = page.getByRole("region", {
    name: "Selected timeline artifact",
  });
  await expect(selected).toContainText("/file-000.txt");
  await expect(selected).toContainText(fixture.run);
  await expect(
    page.getByRole("combobox", { name: /Analysis history/ }),
  ).toHaveValue(fixture.run);
  await expect(
    page.locator(".artifact-item").filter({ hasText: "/file-000.txt" }),
  ).toHaveCount(0);
});

test("a delayed response cannot replace the new case's timeline", async ({
  page,
}) => {
  const fixture = await openCase(page);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let started!: () => void;
  const requested = new Promise<void>((resolve) => {
    started = resolve;
  });
  await page.route(
    `**/api/cases/${fixture.case.id}/timeline?*`,
    async (route) => {
      const response = await route.fetch();
      started();
      await gate;
      await route.fulfill({ response });
    },
  );
  await page.getByRole("button", { name: "Timeline", exact: true }).click();
  await requested;
  await page
    .getByRole("button", { name: fixture.other.name, exact: true })
    .click();
  await expect(page.getByText("No timestamps in this range")).toBeVisible();
  release();
  await page.unrouteAll({ behavior: "wait" });
  await expect(page.locator(".timeline-event")).toHaveCount(0);
  await expect(page.getByText("0–0 of 0 timestamps")).toBeVisible();
});

test("an older visible event pins its run when reanalysis finishes, then resets pagination", async ({
  page,
}) => {
  const fixture = await openCase(page);
  const evidence = await (
    await page.request.get(`/api/cases/${fixture.case.id}/evidence`)
  ).json();
  let refresh = false;
  await page.route(`**/api/cases/${fixture.case.id}/evidence`, (route) =>
    refresh ? route.continue() : route.fulfill({ json: evidence }),
  );
  await openTimeline(page);
  await page.getByRole("button", { name: "Next timeline page" }).click();
  await expect(page.getByText("101–200 of 205 timestamps")).toBeVisible();
  await page.request.post(`/test/finish/${fixture.source}`, { headers });
  await page
    .locator(".timeline-event")
    .filter({ hasText: "/file-100.txt" })
    .click();
  const selected = page.getByRole("region", {
    name: "Selected timeline artifact",
  });
  await expect(selected).toContainText("/file-100.txt");
  await expect(selected).toContainText(fixture.run);
  await expect(selected).toContainText("current saved run only");
  await expect(
    page.getByRole("combobox", { name: /Analysis history/ }),
  ).toHaveValue(fixture.run);
  refresh = true;
  await page.waitForResponse((response) =>
    response.url().endsWith(`/cases/${fixture.case.id}/evidence`),
  );
  await expect(selected).toContainText(fixture.run);
  await page.getByRole("button", { name: "Close evidence details" }).click();
  await expect(page.getByText("1–1 of 1 timestamps")).toBeVisible();
  await expect(page.locator(".timeline-event")).toContainText(
    "/new-result.txt",
  );
});

test("timeline failures leave evidence polling active and can be retried", async ({
  page,
}) => {
  const fixture = await openCase(page);
  await page.route(`**/api/cases/${fixture.case.id}/timeline?*`, (route) =>
    route.fulfill({
      status: 503,
      json: { detail: "Timeline temporarily unavailable" },
    }),
  );
  await page.getByRole("button", { name: "Timeline", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText(
    "Timeline temporarily unavailable",
  );
  const polling = page.waitForResponse((response) =>
    response.url().endsWith(`/cases/${fixture.case.id}/evidence`),
  );
  await page.request.post(`/test/finish/${fixture.source}`, { headers });
  await polling;
  await page.getByRole("button", { name: /^Evidence \d/ }).click();
  await page.getByRole("button", { name: /fixture.img/ }).click();
  await expect(page.getByText("Viewing run 2", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Close evidence details" }).click();
  await page.getByRole("button", { name: "Timeline", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText(
    "Timeline temporarily unavailable",
  );
  await page.unrouteAll({ behavior: "wait" });
  await page.getByRole("button", { name: "Retry timeline" }).click();
  await expect(page.getByText("1–1 of 1 timestamps")).toBeVisible();
});

test("a delayed response cannot overwrite a newly applied range", async ({
  page,
}) => {
  const fixture = await openCase(page);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let started!: () => void;
  const requested = new Promise<void>((resolve) => {
    started = resolve;
  });
  await page.route(
    `**/api/cases/${fixture.case.id}/timeline?*`,
    async (route) => {
      if (new URL(route.request().url()).searchParams.get("start")) {
        await route.continue();
        return;
      }
      const response = await route.fetch();
      started();
      await gate;
      await route.fulfill({ response });
    },
  );
  await page.getByRole("button", { name: "Timeline", exact: true }).click();
  await requested;
  await page.getByLabel("From (UTC)").fill("2030-01-01T00:00");
  await page.getByRole("button", { name: "Apply", exact: true }).click();
  await expect(page.getByText("No timestamps in this range")).toBeVisible();
  release();
  await page.unrouteAll({ behavior: "wait" });
  await expect(page.locator(".timeline-event")).toHaveCount(0);
  await expect(page.getByText("0–0 of 0 timestamps")).toBeVisible();
});
