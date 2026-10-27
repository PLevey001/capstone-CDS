import { expect, test, type Page } from "@playwright/test";

const headers = { "X-CDS-Request": "local-ui" };
async function setup(page: Page) {
  await page.request.post("/test/model/ready", { headers });
  const fixture = await (
    await page.request.post("/test/seed", { headers })
  ).json();
  await page.goto("/");
  await page
    .getByRole("button", { name: fixture.case.name, exact: true })
    .click();
  await page.getByRole("button", { name: "Case brief", exact: true }).click();
  await page
    .getByRole("button", { name: "Generate brief", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Saved version 1", exact: true }),
  ).toBeVisible();
  return fixture;
}

test("generation completion survives leaving and remounting the brief tab", async ({
  page,
}) => {
  const fixture = await setup(page);
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  let arrived!: () => void;
  const ready = new Promise<void>((resolve) => {
    arrived = resolve;
  });
  await page.route(`**/api/cases/${fixture.case.id}/brief`, async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const response = await route.fetch();
    arrived();
    await held;
    await route.fulfill({ response });
  });
  await page
    .getByRole("button", { name: "Generate again", exact: true })
    .click();
  await ready;
  await page.getByRole("button", { name: "Timeline", exact: true }).click();
  await page.getByRole("button", { name: "Case brief", exact: true }).click();
  release();
  await expect(
    page.getByRole("heading", { name: "Saved version 2", exact: true }),
  ).toBeVisible();
  const history = await (
    await page.request.get(`/api/cases/${fixture.case.id}/briefs`)
  ).json();
  await expect(page.getByLabel("Saved brief version")).toHaveValue(
    history[0].id,
  );
});

test("failed version switch identifies the document actually displayed and exported", async ({
  page,
}) => {
  const fixture = await setup(page);
  await page
    .getByRole("button", { name: "Generate again", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Saved version 2", exact: true }),
  ).toBeVisible();
  const history = await (
    await page.request.get(`/api/cases/${fixture.case.id}/briefs`)
  ).json();
  await page.route(
    `**/api/cases/${fixture.case.id}/briefs/${history[1].id}`,
    (route) =>
      route.fulfill({
        status: 503,
        json: { detail: "Synthetic history read failure" },
      }),
  );
  await page.getByLabel("Saved brief version").selectOption(history[1].id);
  await expect(page.getByRole("alert")).toContainText(
    "Synthetic history read failure",
  );
  await expect(
    page.getByText(/Still displaying saved version 2/),
  ).toBeVisible();
  await expect(
    page.getByText(/Freshness unknown until current facts/),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Download JSON", exact: true }),
  ).toHaveAttribute(
    "href",
    `/api/cases/${fixture.case.id}/briefs/${history[0].id}/download`,
  );
});

test("lost save response reconciles history without silently creating another version", async ({
  page,
}) => {
  const fixture = await setup(page);
  await page.route(`**/api/cases/${fixture.case.id}/brief`, async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    await route.fetch();
    await route.abort("failed");
  });
  await page
    .getByRole("button", { name: "Generate again", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("Save outcome unknown");
  await expect(
    page.getByRole("heading", { name: "Saved version 2", exact: true }),
  ).toBeVisible();
  const history = await (
    await page.request.get(`/api/cases/${fixture.case.id}/briefs`)
  ).json();
  expect(history).toHaveLength(2);
});
