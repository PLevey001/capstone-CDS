import { expect, test, type Page } from "@playwright/test";

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
  await expect(facts).toHaveCount(6);
  await expect(facts.first()).toHaveText(
    `F1Case "${fixture.case.name}" has 1 evidence source; 1 of them has saved analysis results.`,
  );
  await expect(facts.nth(3)).toContainText(
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
  await expect(page.getByRole("list", { name: "Overview" })).toHaveCount(0);
  // A fact opens the evidence it describes.
  await page
    .getByRole("button", { name: "Open the source of fact F2" })
    .click();
  await expect(page.getByRole("dialog")).toContainText("fixture.img");
});

test("wording cites facts, flags unsupported numbers, and goes stale when results change", async ({
  page,
}) => {
  const fixture = await openBrief(page, "flagged");
  await page.getByRole("button", { name: "Generate brief" }).click();
  const overview = page.getByRole("list", { name: "Overview" });
  await expect(overview.getByRole("listitem")).toHaveCount(2);
  await expect(overview).not.toContainText("on purpose");
  const flagged = overview.getByRole("listitem").nth(1);
  await expect(flagged).toContainText("The image lists 999 file entries.");
  await expect(flagged).toContainText(
    "Contains a number that is not in its cited facts.",
  );
  await expect(overview.getByRole("listitem").first()).not.toContainText(
    "Contains a number",
  );
  await expect(
    page.getByRole("list", { name: "Look at first" }).getByRole("listitem"),
  ).toHaveText(/Coverage was not recorded, so check what was examined\./);
  await expect(page.locator(".brief-provenance")).toContainText(
    "3 statements shown, 1 withheld because they cited no fact",
  );
  // A citation points at the fact it was built from.
  await flagged.getByRole("button", { name: "Show fact F4" }).click();
  await expect(page.locator("#brief-fact-F4")).toHaveClass(/focused/);
  await expect(page.locator(".brief-facts li.focused")).toHaveCount(1);
  // The wording survives leaving the tab, and generating is recorded in the activity log.
  await page.getByRole("button", { name: "Activity log", exact: true }).click();
  await expect(page.getByText("case brief generated")).toBeVisible();
  await page.getByRole("button", { name: "Case brief", exact: true }).click();
  await expect(overview.getByRole("listitem")).toHaveCount(2);
  await expect(
    page.getByRole("button", { name: "Generate again" }),
  ).toBeEnabled();
  // It is not shown for another case.
  await page
    .getByRole("button", { name: fixture.other.name, exact: true })
    .click();
  await page.getByRole("button", { name: "Case brief", exact: true }).click();
  await expect(overview).toHaveCount(0);
  await expect(
    page.getByText("Available once a source has saved analysis results."),
  ).toBeVisible();
  await page
    .getByRole("button", { name: fixture.case.name, exact: true })
    .click();
  await page.getByRole("button", { name: "Case brief", exact: true }).click();
  await expect(overview.getByRole("listitem")).toHaveCount(2);
  // New saved results renumber the fact sheet, so the old wording is marked and its citations stop.
  await page.request.post(`/test/finish/${fixture.source}`, { headers });
  await expect(
    page.getByText("Saved results changed after this brief was worded."),
  ).toBeVisible();
  await expect(
    flagged.getByRole("button", { name: "Show fact F4" }),
  ).toBeDisabled();
  await expect(page.locator(".brief-facts li.focused")).toHaveCount(0);
});
