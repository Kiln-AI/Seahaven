/**
 * Drives the built UI against the mock environment in a real browser.
 *
 * Start the mocks first:
 *   node mock/server.mjs --port 8000 &
 *   node mock/server.mjs --port 8001 --plain &
 *   npm run build && node e2e/smoke.mjs
 *
 * Screenshots land in e2e/shots/. Any console error or page error fails the run.
 */

import { chromium } from "playwright"
import { mkdirSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const here = dirname(fileURLToPath(import.meta.url))
const shots = join(here, "shots")
mkdirSync(shots, { recursive: true })

const problems = []
let step = 0
const shot = async (page, name) => {
  step += 1
  await page.screenshot({ path: join(shots, `${String(step).padStart(2, "0")}-${name}.png`) })
}

const check = (condition, message) => {
  if (!condition) problems.push(message)
}

// This container ships a Chromium that may not match the version the installed
// playwright package expects, so allow an explicit binary.
const executablePath = process.env.CHROMIUM_PATH || undefined
const browser = await chromium.launch(executablePath ? { executablePath } : {})
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2,
  colorScheme: process.env.SCHEME === "light" ? "light" : "dark",
})
const page = await context.newPage()

// A cross-origin `/schema` fetch is blocked by the browser and logged by it.
// That case is exercised on purpose below, so it is not a failure here.
const EXPECTED = /CORS policy|ERR_FAILED|favicon|status of 404/
page.on("console", (message) => {
  if (message.type() === "error" && !EXPECTED.test(message.text())) {
    problems.push(`console error: ${message.text()}`)
  }
})
page.on("pageerror", (error) => problems.push(`page error: ${error.message}`))

// --- open the page, served by the environment itself ------------------------

await page.goto("http://127.0.0.1:8000/console", { waitUntil: "networkidle" })
await shot(page, "empty")
check(await page.getByText("No environment open").isVisible(), "the empty state did not render")

// --- new environment, through the generated reset form ---------------------
//
// The default mock answers `GET /seahaven/schemas`, so the dialog builds the
// reset message as a form. The frame the browser sends is the proof: the
// startup keywords go back under `startup`, and untouched fields are left out.

const resetFrames = []
page.on("websocket", (socket) => {
  socket.on("framesent", ({ payload }) => {
    const frame = JSON.parse(String(payload))
    if (frame.type === "reset") resetFrames.push(frame.data)
  })
})

await page.getByRole("button", { name: "New environment" }).first().click()
await page.getByPlaceholder("run 1").fill("agency run")
await page.waitForTimeout(700)
check(
  (await page.getByText("Advanced: reset arguments").count()) === 0,
  "the JSON box was shown although the environment publishes a reset schema",
)
await page.getByRole("button", { name: "agency", exact: true }).click()
await page.getByLabel("Seed").fill("7")
await page.getByLabel("startup · User Id").fill("u_dana")
// `team` is untyped, so a plain word is sent as a string without quotes.
await page.getByLabel("startup · Team").fill("ENG")
await shot(page, "new-env-dialog")
check(
  await page.getByText("Issues, sprints and comments").isVisible(),
  "the dialog did not name the environment from /metadata",
)

const EXPECTED_RESET = { seed: 7, fixture: "agency", startup: { user_id: "u_dana", team: "ENG" } }
const canonical = (value) =>
  value && typeof value === "object"
    ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]))
    : value
const sameMessage = (message) =>
  JSON.stringify(canonical(message)) === JSON.stringify(canonical(EXPECTED_RESET))

// A field error from a refused submit describes values the form no longer
// holds once they have been round-tripped through raw JSON, so it is cleared.
await page.getByLabel("Seed").fill("7.5")
await page.getByRole("button", { name: "Open environment" }).click()
check(await page.getByText("must be a whole number").isVisible(), "a bad seed was not flagged on submit")
await page.getByRole("switch", { name: "Edit reset arguments as raw JSON" }).click()
await page.getByRole("switch", { name: "Edit reset arguments as raw JSON" }).click()
check(
  (await page.getByText("must be a whole number").count()) === 0,
  "a stale field error survived the round trip through raw JSON",
)
await page.getByLabel("Seed").fill("7")

await page.getByRole("switch", { name: "Edit reset arguments as raw JSON" }).click()
await shot(page, "new-env-raw")
const rawText = await page.locator("textarea").first().inputValue()
check(sameMessage(JSON.parse(rawText)), `raw mode did not show the nested reset message: ${rawText}`)
await page.getByRole("switch", { name: "Edit reset arguments as raw JSON" }).click()
check(
  (await page.getByLabel("startup · User Id").inputValue()) === "u_dana",
  "switching back from raw JSON lost the form's values",
)

// A key the form has no field for keeps the dialog in raw JSON, rather than
// being dropped on the way back to the form.
const rawSwitch = page.getByRole("switch", { name: "Edit reset arguments as raw JSON" })
await rawSwitch.click()
const withExtra = { ...JSON.parse(await page.locator("textarea").first().inputValue()), region: "eu" }
await page.locator("textarea").first().fill(JSON.stringify(withExtra))
await rawSwitch.click()
check((await rawSwitch.getAttribute("aria-checked")) === "true", "raw JSON with an unknown key was left")
check(
  await page.getByText("The form has no field for some of these keys").isVisible(),
  "leaving raw JSON was refused without saying why",
)
check(
  (await page.locator("textarea").first().inputValue()).includes('"region"'),
  "the raw JSON was not kept",
)
await page.locator("textarea").first().fill(rawText)
await rawSwitch.click()

// The same server, spelled differently, keeps the form and what is in it.
await page.getByRole("textbox").first().fill("http://127.0.0.1:8000/")
await page.waitForTimeout(700)
check(
  (await page.getByLabel("startup · User Id").inputValue()) === "u_dana",
  "editing the URL to the same server reset the form",
)

await page.getByRole("button", { name: "Open environment" }).click()
await page.waitForTimeout(600)
await shot(page, "tools-empty")
check(await page.getByText("5 tools").first().isVisible(), "the tool list did not load")
check(
  resetFrames.length === 1 && sameMessage(resetFrames[0]),
  `the reset frame was not the form's message: ${JSON.stringify(resetFrames)}`,
)

// --- an optional argument keeps the hints its wrapper carries ---------------
//
// `search_issues.limit` is spelled `int | None = 20`, so pydantic puts the type
// on the `anyOf` branch and the title, description and default on the wrapper.
// Reading only the branch loses all three.

check(
  (await page.getByLabel("Limit").inputValue()) === "20",
  "an optional argument lost the default its wrapper carries",
)
check(
  await page.getByText("How many issues to return.").isVisible(),
  "an optional argument lost the description its wrapper carries",
)

// --- a tool call, through the generated form --------------------------------

await page.getByRole("button", { name: /Choose a tool|search_issues/ }).first().click()
await page.getByPlaceholder("Filter tools…").fill("create")
await shot(page, "tool-dropdown")
await page.getByRole("button", { name: /create_issue/ }).click()
await page.waitForTimeout(200)

await page.getByLabel("Title").fill("Stack trace in the session cookie")
await page.getByLabel("Body").fill("Reproduced on staging with a stale session.")
await page.getByRole("button", { name: "DESIGN", exact: true }).click()
await shot(page, "tool-form")

await page.getByRole("button", { name: "Call tool" }).click()
await page.waitForTimeout(400)
await shot(page, "tool-result")
check(await page.getByText("result").first().isVisible(), "the result card did not render")
check(await page.getByText("DESIGN-").first().isVisible(), "the new issue key is not in the result")

// --- changing tool empties the result panel and the raw JSON box ------------

// Raw mode holds the arguments of the tool it was opened for, so it cannot
// survive into the next tool's form.
await page.getByRole("switch", { name: "Edit arguments as raw JSON" }).click()
check(
  (await page.locator("textarea").first().inputValue()).includes("Stack trace"),
  "raw mode did not open on the current tool's arguments",
)

await page.getByRole("button", { name: /create_issue/ }).first().click()
await page.getByPlaceholder("Filter tools…").fill("get_issue")
await page.getByRole("button", { name: /get_issue/ }).first().click()
await page.waitForTimeout(200)
check(
  await page.getByText("Nothing called yet.").isVisible(),
  "the previous tool's result was still shown after changing tool",
)
check(
  (await page.getByRole("switch", { name: "Edit arguments as raw JSON" }).getAttribute(
    "aria-checked",
  )) === "false",
  "the raw JSON box survived a tool change",
)

// --- a tool that refuses ----------------------------------------------------

await page.getByRole("button", { name: /get_issue/ }).first().click()
await page.getByPlaceholder("Filter tools…").fill("transition")
await page.getByRole("button", { name: /transition_issue/ }).click()
await page.getByLabel("Issue id").fill("i_8f21")
await page.getByRole("button", { name: "done", exact: true }).click()
await page.getByRole("button", { name: "Call tool" }).click()
await page.waitForTimeout(400)
await shot(page, "tool-error")
check(
  await page.getByText("WORKFLOW_REFUSED").isVisible(),
  "a tool error did not render as an error card",
)

// --- required argument, caught before the frame goes out --------------------

await page.getByLabel("Issue id").fill("")
await page.getByRole("button", { name: "Call tool" }).click()
await page.waitForTimeout(150)
check(await page.getByText("required").first().isVisible(), "an empty required field was not caught")

// --- state ------------------------------------------------------------------

await page.getByRole("button", { name: "State", exact: true }).click()
await page.waitForTimeout(300)
await page.getByRole("button", { name: "Refresh" }).click()
await page.waitForTimeout(300)
await shot(page, "state")
check(await page.getByText("episode_id").isVisible(), "the state document did not render")

// --- transcript -------------------------------------------------------------

await page.getByRole("button", { name: "Transcript", exact: true }).click()
await page.waitForTimeout(200)
await shot(page, "transcript")
check(await page.getByText("3 calls").isVisible(), "the transcript did not count the calls")

// --- environment info, schema present because this page is same-origin ------

await page.getByRole("button", { name: "Environment", exact: true }).click()
await page.waitForTimeout(200)
await shot(page, "env-info")
check(await page.getByText("Action schema").isVisible(), "the schema panel is missing")

// --- a second environment, cross-origin -------------------------------------
//
// This page is served from :8000, so `/schema` and `/metadata` on :8001 are
// unreadable: no CORS middleware exists on an OpenEnv server. The socket is
// unaffected, which is the degradation the UI is built around.

await page.getByRole("button", { name: "New environment" }).first().click()
await page.getByRole("textbox").first().fill("http://127.0.0.1:8001")
await page.waitForTimeout(700)
await page.getByRole("button", { name: "Open environment" }).click()
await page.waitForTimeout(700)
await shot(page, "cross-origin")
check(
  await page.getByText(/rejected a list_tools action/).isVisible(),
  "the non-tool environment did not fall back to the action form",
)

// --- both environments in the list ------------------------------------------

await page.getByText("agency run").click()
await page.waitForTimeout(200)
await shot(page, "two-envs")
check((await page.getByText(/live$/).count()) >= 1, "no environment is shown as live")

// --- the same non-tool environment, same-origin, so the form generates ------

const plain = await context.newPage()
plain.on("pageerror", (error) => problems.push(`page error: ${error.message}`))
await plain.goto("http://127.0.0.1:8001/console", { waitUntil: "networkidle" })
await plain.getByRole("button", { name: "New environment" }).first().click()
await plain.waitForTimeout(700)
// No `/seahaven/schemas` here, so the reset arguments are the JSON box.
await plain.getByText("Advanced: reset arguments").click()
await shot(plain, "plain-new-env")
check(
  await plain.getByPlaceholder(/"fixture": "small_startup"/).isVisible(),
  "an environment with no reset schema did not get the JSON box",
)
await plain.getByRole("button", { name: "Open environment" }).click()
await plain.waitForTimeout(700)
await plain.getByRole("button", { name: "north", exact: true }).click()
await plain.getByRole("button", { name: "Step" }).click()
await plain.waitForTimeout(400)
await shot(plain, "plain-step")
check(
  await plain.getByText("Which way to move the agent on the grid.").isVisible(),
  "the action schema form did not render for a non-tool environment",
)
check(await plain.getByText("0.5").first().isVisible(), "the observation did not render")

// --- a reset schema that is slow to answer ----------------------------------
//
// The dialog does not wait on it for ever: the JSON box appears, and when the
// schema does arrive, the form replaces the box and keeps what was typed.

const slow = await context.newPage()
slow.on("pageerror", (error) => problems.push(`page error: ${error.message}`))
let release
const held = new Promise((resolve) => (release = resolve))
await slow.route("**/seahaven/schemas", async (route) => {
  await held
  await route.continue()
})
await slow.goto("http://127.0.0.1:8000/console", { waitUntil: "domcontentloaded" })
await slow.getByRole("button", { name: "New environment" }).first().click()
// The context remembers the last URL used, which was the plain mock's.
await slow.getByRole("textbox").first().fill("http://127.0.0.1:8000")
await slow.waitForTimeout(2600)
check(
  await slow.getByText("Advanced: reset arguments").isVisible(),
  "a reset schema that never answered left the dialog with no reset arguments",
)
if (!(await slow.getByPlaceholder(/"fixture": "small_startup"/).isVisible())) {
  await slow.getByText("Advanced: reset arguments").click()
}
await slow.getByPlaceholder(/"fixture": "small_startup"/).fill('{"fixture": "big_co", "startup": {"user_id": "u_sam"}}')
release()
await slow.waitForTimeout(600)
await shot(slow, "slow-schema-form")
check(
  (await slow.getByLabel("startup · User Id").inputValue()) === "u_sam",
  "a late reset schema dropped what was typed into the JSON box",
)
check(
  (await slow.getByRole("button", { name: "big_co", exact: true }).getAttribute("class")).includes("border-accent"),
  "a late reset schema did not carry the typed fixture into the form",
)

await browser.close()

if (problems.length > 0) {
  console.error(`${problems.length} problem(s):`)
  for (const problem of problems) console.error(`  - ${problem}`)
  process.exit(1)
}
console.log(`ok: ${step} screenshots in e2e/shots/`)
