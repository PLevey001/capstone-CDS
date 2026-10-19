import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";

type Fixture = {
  case: { id: string; name: string };
  other: { id: string; name: string };
  source: string;
  run: string;
};
const headers = { "X-CDS-Request": "local-ui" };

// The model server behind these tests is a scripted stub, never a real model.
async function openBrief(page: Page, mode: string): Promise<Fixture> {
  await page.request.post(`/test/model/${mode}`, { headers });
  const response = await page.request.post("/test/seed", { headers });
  expect(response.ok()).toBeTruthy();
  const fixture: Fixture = await response.json();
  await page.goto("/");
  await page
    .getByRole("button", { name: fixture.case.name, exact: true })
    .click();
  await expect(page.getByRole("button", { name: /fixture.img/ })).toBeVisible();
  await page.getByRole("button", { name: "Case brief", exact: true }).click();
  return fixture;
}

test.afterEach(async ({ page }) => {
  await page.request.post("/test/model/ready", { headers });
});

test("fact sheet needs no model and says why wording is unavailable", async ({
  page,
}) => {
  const fixture = await openBrief(page, "missing");
  const facts = page.locator(".brief-facts li");
  await expect(facts).toHaveCount(7);
  await expect(facts.first()).toHaveText(
    `F1Case "${fixture.case.name}" has 1 evidence source; 1 of them has saved analysis results.`,
  );
  await expect(facts.nth(4)).toContainText(
    'The inventory of "fixture.img" lists 205 file entries and 0 directories',
  );
  await expect(page.getByText("Local model unavailable")).toBeVisible();
  await expect(
    page.getByText(
      "The model llama3.2:3b is not installed. Run: ollama pull llama3.2:3b",
    ),
  ).toBeVisible();
  const generate = page.getByRole("button", { name: "Generate brief" });
  await expect(generate).toBeDisabled();
  await page.request.post("/test/model/ready", { headers });
  await page.getByRole("button", { name: "Check again" }).click();
  await expect(page.getByText("Local model ready: llama3.2:3b")).toBeVisible();
  await expect(generate).toBeEnabled();
  await expect(page.getByRole("button", { name: "Check again" })).toHaveCount(
    0,
  );
  // A failed attempt says why, re-checks the model, and is still explained after leaving the tab.
  await page.request.post("/test/model/missing", { headers });
  await generate.click();
  const failure = page.getByRole("alert");
  await expect(failure).toHaveText(
    "The model llama3.2:3b is not installed. Run: ollama pull llama3.2:3b",
  );
  await expect(page.getByText("Local model unavailable")).toBeVisible();
  await page.getByRole("button", { name: "Timeline", exact: true }).click();
  await page.getByRole("button", { name: "Case brief", exact: true }).click();
  await expect(failure).toBeVisible();
  await expect(page.getByRole("list", { name: "Overall summary" })).toHaveCount(
    0,
  );
  // A fact opens the evidence it describes.
  await page
    .getByRole("button", { name: "Open the source of fact F2" })
    .click();
  await expect(page.getByRole("dialog")).toContainText("fixture.img");
});

test("invalid assessment is rejected whole without saving a partial version", async ({
  page,
}) => {
  const fixture = await openBrief(page, "flagged");
  await page.getByRole("button", { name: "Generate brief" }).click();
  await expect(page.getByRole("alert")).toContainText("No brief was saved");
  await expect(page.getByRole("link", { name: "Download JSON" })).toHaveCount(
    0,
  );
  expect(
    await (
      await page.request.get(`/api/cases/${fixture.case.id}/briefs`)
    ).json(),
  ).toEqual([]);
  await page.request.post("/test/model/ready", { headers });
  await page.getByRole("button", { name: "Generate brief" }).click();
  await expect(
    page.getByRole("heading", { name: "Saved version 1", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("note")).toContainText(
    "Inferences are hypotheses, not evidence",
  );
  await expect(
    page.getByRole("list", { name: "Inferences / hypotheses", exact: true }),
  ).toContainText("Alternative explanations");
  await expect(
    page.getByRole("list", { name: "Inferences / hypotheses", exact: true }),
  ).toContainText("Uncertainty and limitations");
});

test("saved JSON survives reload, offline reading, reanalysis and version selection", async ({
  page,
}) => {
  const fixture = await openBrief(page, "ready");
  await page.getByRole("button", { name: "Generate brief" }).click();
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download JSON" }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toMatch(
    /^case-brief-[a-f0-9-]+-v1\.json$/,
  );
  const bytes = await readFile((await download.path())!, "utf-8");
  const document = JSON.parse(bytes);
  expect(document.schema_version).toBe("cds.case-assessment.v1");
  expect(document.version).toBe(1);
  expect(
    document.facts.some(
      (fact: { run_id: string }) => fact.run_id === fixture.run,
    ),
  ).toBeTruthy();
  await page.request.post("/test/model/missing", { headers });
  await page.reload();
  // Reload initially selects the newest case (the empty fixture), not the previously open one.
  await page
    .getByRole("button", { name: fixture.case.name, exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: fixture.case.name, exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Case brief", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Saved version 1", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Local model unavailable", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Download JSON" })).toBeVisible();
  const stored = await page.request.get(
    `/api/cases/${fixture.case.id}/briefs/${document.id}/download`,
  );
  expect(await stored.text()).toBe(bytes);
  await page.request.post(`/test/finish/${fixture.source}`, { headers });
  await expect(
    page.getByText(/Stale brief: case name, sources or results changed/),
  ).toBeVisible();
  const cited = page
    .getByRole("list", { name: "Overall summary", exact: true })
    .getByRole("button", { name: "Show fact F1" });
  await cited.click();
  await expect(page.locator("#brief-fact-F1")).toHaveClass(/focused/);
  expect(
    await (
      await page.request.get(
        `/api/cases/${fixture.case.id}/briefs/${document.id}/download`,
      )
    ).text(),
  ).toBe(bytes);
  await page.request.post("/test/model/ready", { headers });
  await page.getByRole("button", { name: "Check again" }).click();
  await page.getByRole("button", { name: "Generate again" }).click();
  await expect(
    page.getByRole("heading", { name: "Saved version 2", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByLabel("Saved brief version").locator("option"),
  ).toHaveCount(2);
  await page.getByLabel("Saved brief version").selectOption(document.id);
  await expect(
    page.getByRole("heading", { name: "Saved version 1", exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/Stale brief:/)).toBeVisible();
  await page
    .getByRole("button", { name: fixture.other.name, exact: true })
    .click();
  await expect(page.getByRole("link", { name: "Download JSON" })).toHaveCount(
    0,
  );
});

test("case chat explicitly selects a saved brief and distinguishes prior assessment references", async ({
  page,
}) => {
  const fixture = await openBrief(page, "ready");
  await page.getByRole("button", { name: "Generate brief" }).click();
  await expect(
    page.getByRole("heading", { name: "Saved version 1", exact: true }),
  ).toBeVisible();
  const [version] = await (
    await page.request.get(`/api/cases/${fixture.case.id}/briefs`)
  ).json();
  await page
    .getByRole("button", { name: "Learn with AI", exact: true })
    .click();
  await page.getByLabel("Learning mode").selectOption("case");
  await expect(page.getByLabel("Assessment for this conversation")).toHaveValue(
    "",
  );
  await page
    .getByLabel("Assessment for this conversation")
    .selectOption(version.id);
  await page
    .getByLabel("Your question", { exact: true })
    .fill("Why was this inference suggested?");
  const request = page.waitForRequest((r) => r.url().endsWith("/chat"));
  await page.getByRole("button", { name: "Send question" }).click();
  expect((await request).postDataJSON()).toMatchObject({
    mode: "case",
    brief_id: version.id,
    history: [],
  });
  await expect(page.locator(".learn-turn")).toContainText(
    "Prior unverified assessment",
  );
  await expect(page.locator(".learn-turn")).toContainText("Evidence fact · F1");
  await page.locator(".learn-assessment-reference summary").click();
  await expect(page.locator(".learn-assessment-reference")).toContainText(
    "Saved brief version 1",
  );
  await page.getByLabel("Learning mode").selectOption("concepts");
  await expect(page.locator(".learn-turn")).toHaveCount(0);
  await expect(page.getByLabel("Assessment for this conversation")).toHaveCount(
    0,
  );
  await page.request.post(`/test/finish/${fixture.source}`, { headers });
  // Wait for the app's evidence poll to consume the new run before selecting a chat mode.
  // Otherwise its normal source-revision reset can remount LearnPanel during this assertion.
  await expect(
    page
      .locator(".stat-card")
      .filter({ hasText: "File artifacts indexed" })
      .locator(".stat-number"),
  ).toHaveText("1");
  await page.getByRole("button", { name: "Case brief", exact: true }).click();
  await expect(page.getByText(/Stale brief:/)).toBeVisible();
  await page
    .getByRole("button", { name: "Learn with AI", exact: true })
    .click();
  await page.getByLabel("Learning mode").selectOption("case");
  const selection = page.getByLabel("Assessment for this conversation");
  const staleOption = selection.locator(`option[value="${version.id}"]`);
  // Option disabled is a DOM property; Playwright's disabled matcher can retarget the enclosing label's select.
  await expect(staleOption).toHaveJSProperty("disabled", true);
  await selection.focus();
  await selection.press("ArrowDown");
  await selection.press("Enter");
  await expect(selection).toHaveValue("");
  await expect(page.getByLabel("Learning mode")).toHaveValue("case");
  const forced = await page.request.post(`/api/cases/${fixture.case.id}/chat`, {
    headers,
    data: {
      mode: "case",
      message: "Explain the old brief",
      brief_id: version.id,
    },
  });
  expect(forced.status()).toBe(409);
  expect((await forced.json()).detail).toContain("stale");
});

test("failed current-fact refresh keeps historical references and download usable", async ({
  page,
}) => {
  const fixture = await openBrief(page, "ready");
  await page.getByRole("button", { name: "Generate brief" }).click();
  const citation = page.getByRole("button", {
    name: "Show fact F1",
    exact: true,
  });
  await expect(citation).toBeEnabled();
  await page.route(`**/api/cases/${fixture.case.id}/brief`, async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    await route.fulfill({
      status: 503,
      json: { detail: "Synthetic fact refresh failed" },
    });
  });
  await page.getByRole("button", { name: "Rename case", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("textbox")
    .fill("Renamed learning case");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Save name" })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Synthetic fact refresh failed",
  );
  await expect(citation).toBeEnabled();
  await expect(page.getByRole("link", { name: "Download JSON" })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Generate again" }),
  ).toBeDisabled();
});
