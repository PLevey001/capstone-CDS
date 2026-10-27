import { expect, test, type Page } from "@playwright/test";

const headers = { "X-CDS-Request": "local-ui" };
type Fixture = {
  case: { id: string; name: string };
  other: { id: string; name: string };
  source: string;
  run: string;
};

async function openLearning(page: Page, mode = "ready"): Promise<Fixture> {
  await page.request.post(`/test/model/${mode}`, { headers });
  const fixture: Fixture = await (
    await page.request.post("/test/seed", { headers })
  ).json();
  await page.goto("/");
  await page
    .getByRole("button", { name: fixture.case.name, exact: true })
    .click();
  await expect(page.getByRole("button", { name: /fixture.img/ })).toBeVisible();
  await page
    .getByRole("button", { name: "Learn with AI", exact: true })
    .click();
  return fixture;
}

test.afterEach(async ({ page }) => {
  await page.request.post("/test/model/ready", { headers });
});

test("concepts support follow-ups, static references, keyboard submission and reset", async ({
  page,
}) => {
  // The answer's reference snapshot must be shown, even if the separately
  // fetched catalogue differs. Neither is model-generated reference text.
  await page.route("**/api/ai/glossary", async (route) => {
    const response = await route.fetch();
    const entries = await response.json();
    entries[0].text = "Different fetched catalogue entry.";
    await route.fulfill({ response, json: entries });
  });
  const fixture = await openLearning(page);
  const input = page.getByLabel("Your question", { exact: true });
  await expect(page.getByRole("note")).toContainText("Unverified AI wording");
  await page
    .getByRole("button", { name: "What does SHA-256 tell me?", exact: true })
    .click();
  await expect(input).toHaveValue("What does SHA-256 tell me?");
  const firstRequest = page.waitForRequest((request) =>
    request.url().endsWith(`/cases/${fixture.case.id}/chat`),
  );
  await page.getByRole("button", { name: "Send question" }).focus();
  await page.keyboard.press("Enter");
  expect((await firstRequest).postDataJSON()).toMatchObject({
    mode: "concepts",
    history: [],
    facts_digest: null,
  });
  await expect(page.locator(".learn-turn")).toHaveCount(1);
  await expect(page.locator(".learn-turn")).toContainText("No case facts sent");
  await expect(page.locator(".learn-turn .learn-reference")).toContainText(
    "Matching a reference digest supports byte integrity relative to that reference.",
  );
  await expect(page.locator(".learn-turn .learn-reference")).toBeVisible();
  await expect(page.locator(".learn-turn")).not.toContainText(
    "Different fetched catalogue entry.",
  );
  await expect(page.locator("#learn-limits")).toContainText(
    "These limits apply together",
  );
  await expect(
    page.getByRole("link", { name: "SHA-256 · reference" }),
  ).toHaveAttribute("href", "https://csrc.nist.gov/pubs/fips/180-4/upd1/final");
  await input.fill("Can that establish who used a file?");
  const followup = page.waitForRequest((request) =>
    request.url().endsWith("/chat"),
  );
  await page.getByRole("button", { name: "Send question" }).click();
  expect((await followup).postDataJSON().history).toEqual([
    { role: "user", content: "What does SHA-256 tell me?" },
    { role: "assistant", content: "Scripted general explanation." },
  ]);
  await expect(page.locator(".learn-turn")).toHaveCount(2);
  await page.getByRole("button", { name: "Clear conversation" }).click();
  await expect(page.locator(".learn-turn")).toHaveCount(0);
  await expect(input).toHaveValue("");
  await page.reload();
  await page
    .getByRole("button", { name: "Learn with AI", exact: true })
    .click();
  await expect(page.locator(".learn-turn")).toHaveCount(0);
});

test("case answers link exact saved runs and reset when results change", async ({
  page,
}) => {
  const fixture = await openLearning(page);
  await page.getByLabel("Learning mode").selectOption("case");
  // Use the real endpoint, then choose a source fact for the scripted answer.
  await page.route(`**/api/cases/${fixture.case.id}/chat`, async (route) => {
    const response = await route.fetch();
    const answer = await response.json();
    answer.answer[0].facts = ["F2"];
    await route.fulfill({ response, json: answer });
  });
  await page
    .getByLabel("Your question", { exact: true })
    .fill("What saved run is available?");
  await page.getByRole("button", { name: "Send question" }).click();
  await expect(page.locator(".learn-turn")).toHaveCount(1);
  await page.locator(".learn-turn summary").click();
  const runRequest = page.waitForRequest(
    (request) =>
      new URL(request.url()).searchParams.get("run_id") === fixture.run,
  );
  await page.getByRole("button", { name: "Open source for F2" }).click();
  await runRequest;
  await expect(page.getByRole("dialog")).toContainText("fixture.img");
  await page
    .getByRole("button", { name: "Close evidence details", exact: true })
    .click();
  await page.request.post(`/test/finish/${fixture.source}`, { headers });
  await expect(page.locator(".learn-turn")).toHaveCount(0);
  await expect(page.getByLabel("Learning mode")).toHaveValue("concepts");
});

test("late replies cannot appear in another case or a cleared conversation", async ({
  page,
}) => {
  const fixture = await openLearning(page);
  await page.getByLabel("Learning mode").selectOption("case");
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  let received!: () => void;
  const ready = new Promise<void>((resolve) => {
    received = resolve;
  });
  await page.route(`**/api/cases/${fixture.case.id}/chat`, async (route) => {
    const response = await route.fetch();
    received();
    await held;
    await route.fulfill({ response }).catch(() => {});
  });
  await page
    .getByLabel("Your question", { exact: true })
    .fill("First case question");
  await page.getByRole("button", { name: "Send question" }).click();
  await ready;
  await expect(
    page.getByText("Generating on this machine…", { exact: false }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: fixture.other.name, exact: true })
    .click();
  release();
  await expect(page.locator(".learn-turn")).toHaveCount(0);
  await expect(page.getByLabel("Your question", { exact: true })).toHaveValue(
    "",
  );
  await page
    .getByLabel("Your question", { exact: true })
    .fill("Second case question");
  const next = page.waitForRequest((request) =>
    request.url().endsWith(`/cases/${fixture.other.id}/chat`),
  );
  await page.getByRole("button", { name: "Send question" }).click();
  expect((await next).postDataJSON().history).toEqual([]);
  await expect(page.locator(".learn-turn")).toHaveCount(1);
  await expect(page.locator(".learn-turn")).not.toContainText(
    "First case question",
  );
});

test("availability, retry and hostile text remain understandable and literal", async ({
  page,
}) => {
  const fixture = await openLearning(page, "missing");
  await expect(
    page.getByText("Local model unavailable", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Send question" }),
  ).toBeDisabled();
  await page.request.post("/test/model/hostile", { headers });
  await page.getByRole("button", { name: "Check again" }).click();
  await expect(page.getByText("Local model ready: llama3.2:3b")).toBeVisible();
  await page.route(
    `**/api/cases/${fixture.case.id}/chat`,
    async (route) => {
      await route.fulfill({
        status: 503,
        json: { detail: "Synthetic model unavailable. Retry." },
      });
    },
    { times: 1 },
  );
  await page
    .getByLabel("Your question", { exact: true })
    .fill("<script>window.injected=true</script>");
  await page.getByRole("button", { name: "Send question" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Synthetic model unavailable",
  );
  await page.getByRole("button", { name: "Retry question" }).click();
  await expect(page.locator(".learn-turn")).toContainText(
    "<img src=x onerror=alert('x')>",
  );
  await expect(page.locator(".learn-turn img, .learn-turn script")).toHaveCount(
    0,
  );
  await expect(page.locator(".learn-turn")).toContainText(
    "<script>window.injected=true</script>",
  );
  await page.getByLabel("Learning mode").selectOption("case");
  await expect(page.locator(".learn-turn")).toHaveCount(0);
});
