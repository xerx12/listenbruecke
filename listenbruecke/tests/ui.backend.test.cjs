// Real FastAPI + browser smoke with a temporary SQLite store and fake providers.
// npm run test:ui:backend
// LISTENBRUECKE_TEST_PYTHON selects an environment with the app dependencies.
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const { mkdir, writeFile } = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const readline = require("node:readline");
const { test } = require("node:test");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

test("actual API: setup/login, fake providers, synchronization, disconnect and CSP", { timeout: 60000 }, async () => {
  const child = spawn(process.env.LISTENBRUECKE_TEST_PYTHON || "python3", [path.join(__dirname, "ui_backend_fixture.py")], {
    env: { ...process.env, PYTHONPATH: path.join(__dirname, "..", "src"), SUPERVISOR_TOKEN: "", LISTENBRUECKE_SECURE_COOKIE: "0" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let backendErrors = "";
  child.stderr.on("data", (data) => { backendErrors += data.toString(); });
  let browser;
  let page;
  const network = [];
  try {
    const info = await new Promise((resolve, reject) => {
      const lines = readline.createInterface({ input: child.stdout });
      const timeout = setTimeout(() => reject(new Error("UI test backend did not start: " + backendErrors)), 10000);
      child.once("error", (error) => { clearTimeout(timeout); reject(error); });
      child.once("exit", () => { clearTimeout(timeout); reject(new Error("UI test backend stopped: " + backendErrors)); });
      lines.once("line", (line) => { clearTimeout(timeout); resolve(JSON.parse(line)); });
    });
    const origin = `http://127.0.0.1:${info.port}`;
    browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
    page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    if (process.env.UI_CPU_THROTTLE) {
      const cdp = await page.context().newCDPSession(page);
      await cdp.send("Emulation.setCPUThrottlingRate", { rate: Number(process.env.UI_CPU_THROTTLE) });
    }
    page.on("response", (response) => network.push({ path: new URL(response.url()).pathname, method: response.request().method(), status: response.status() }));
    await page.addInitScript(() => {
      window.__authTestEvents = [];
      for (const type of ["focusin", "input", "invalid", "submit"]) {
        document.addEventListener(type, (event) => {
          if (!event.target.closest?.("#auth-form")) return;
          window.__authTestEvents.push({ type, target: event.target.id, at: performance.now(), passwordLength: document.getElementById("auth-password").value.length, confirmLength: document.getElementById("auth-confirm").value.length });
        }, true);
      }
      // A user or password manager may choose a field immediately after the
      // form appears. A later application autofocus must not override that.
      new MutationObserver(() => {
        const screen = document.getElementById("auth-screen");
        const confirmation = document.getElementById("auth-confirm");
        if (!screen || screen.hidden || !confirmation || confirmation.disabled || window.__authTestFocus) return;
        window.__authTestFocus = { before: null, after: null, done: false };
        confirmation.focus();
        window.__authTestFocus.before = document.activeElement.id;
        setTimeout(() => {
          window.__authTestFocus.after = document.activeElement.id;
          window.__authTestFocus.done = true;
        }, 100);
      }).observe(document, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden"] });
    });
    const failures = [];
    page.on("pageerror", (error) => failures.push(error.message));
    page.on("console", (message) => { if (message.type() === "error" && /Content Security Policy|Refused to/.test(message.text())) failures.push(message.text()); });
    await page.route("**/*", async (route) => {
      if (new URL(route.request().url()).origin !== origin) {
        failures.push("Unexpected external browser request");
        await route.abort();
      } else await route.continue();
    });
    for (let attempt = 0; attempt < 100; attempt++) {
      try { if ((await page.request.get(origin + "/healthz")).ok()) break; }
      catch { /* Uvicorn is still starting. */ }
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    const response = await page.goto(origin + "/");
    assert.match(response.headers()["content-security-policy"], /script-src 'self'/);
    await page.locator("#auth-screen").waitFor({ state: "visible" });
    await page.waitForFunction(() => window.__authTestFocus?.done);
    if (await page.evaluate(() => window.__authTestFocus.after !== "auth-confirm")) {
      // Preserve evidence of the user-visible consequence before the assertion:
      // pasted text reaches the wrong field and native validation blocks POST.
      await page.keyboard.insertText("synthetic-local-password-123");
      await page.locator("#auth-submit").click();
    }
    assert.equal(await page.evaluate(() => window.__authTestFocus.after), "auth-confirm", "Autofocus must preserve a field selected after the form appears");
    await page.locator("#auth-password").fill("synthetic-local-password-123");
    await page.locator("#auth-confirm").fill("synthetic-local-password-123");
    const setupResponse = page.waitForResponse((result) => result.url() === origin + "/api/setup" && result.request().method() === "POST");
    await page.locator("#auth-submit").click();
    const setup = await setupResponse;
    assert.equal(setup.status(), 200, "Setup should succeed: " + await setup.text());
    await page.locator("#app").waitFor({ state: "visible" });
    await page.locator("#last-refreshed").filter({ hasText: "Aktualisiert" }).waitFor();
    const cookies = await page.context().cookies();
    assert.equal(cookies.find((cookie) => cookie.name === "listenbruecke_session").httpOnly, true);
    assert.equal((await page.evaluate(() => document.cookie)).includes("listenbruecke_session"), false);
    if (process.env.UI_BACKEND_SCREENSHOT) await page.screenshot({ path: process.env.UI_BACKEND_SCREENSHOT, fullPage: true });

    await page.locator('[data-nav="connections"]').click();
    await page.locator('[data-test="monsieur_cuisine"]').click();
    await page.locator('[data-provider-pill="monsieur_cuisine"]').filter({ hasText: "Verbunden" }).waitFor();
    await page.locator("#bring-email").fill("synthetic@example.invalid");
    await page.locator("#bring-password").fill("synthetic-bring-password");
    await page.locator('#bring-form [type="submit"]').click();
    await page.locator('[data-provider-pill="bring"]').filter({ hasText: "Verbunden" }).waitFor();
    assert.equal(await page.locator("#bring-password").inputValue(), "");
    assert.equal((await (await page.request.get(origin + "/api/status")).text()).includes("synthetic-bring-password"), false);

    await page.locator("#alexa-email").fill("synthetic@example.invalid");
    await page.locator("#alexa-password").fill("synthetic-alexa-password");
    await page.locator("#alexa-code").fill("123456");
    const alexaStarted = page.waitForResponse((result) => result.url() === origin + "/api/connections/alexa/auth/start" && result.request().method() === "POST");
    await page.locator("#alexa-auth-start").click();
    assert.equal((await alexaStarted).status(), 202);
    assert.equal(await page.locator("#alexa-password").inputValue(), "");
    assert.equal(await page.locator("#alexa-code").inputValue(), "");
    await page.locator("#alexa-auth-message").filter({ hasText: "Anmeldung läuft" }).waitFor();
    await page.reload();
    await page.locator("#alexa-auth-message").filter({ hasText: "Anmeldung läuft" }).waitFor();
    await page.locator('[data-provider-pill="alexa"]').filter({ hasText: "Verbunden" }).waitFor();
    assert.equal(await page.locator("#alexa-password").inputValue(), "");
    assert.equal(await page.locator("#alexa-code").inputValue(), "");
    const alexaStatus = await (await page.request.get(origin + "/api/connections/alexa/auth/status")).json();
    assert.deepEqual(alexaStatus, { running: false, complete: true, error: null });
    await page.locator('[data-disconnect="monsieur_cuisine"]').click();
    await page.locator('[data-provider-pill="monsieur_cuisine"]').filter({ hasText: "Nicht eingerichtet" }).waitFor();

    await page.locator('[data-nav="lists"]').click();
    await page.locator("#list-bring").selectOption("bring-synthetic-list");
    await page.locator("#list-alexa").selectOption("alexa-synthetic-list");
    await page.locator("#sync-enabled").check();
    await page.locator("#settings-save").click();
    await page.locator("#toast-message").filter({ hasText: "eingeschaltet" }).waitFor();
    const settings = await (await page.request.get(origin + "/api/settings")).json();
    assert.equal(settings.targets.length, 2);
    assert.deepEqual(settings.targets.map((target) => target.provider), ["bring", "alexa"]);
    assert.equal(settings.enabled, true);
    assert.equal(settings.propagate_deletions, false);
    const syncResponse = page.waitForResponse((result) => result.url() === origin + "/api/sync" && result.request().method() === "POST");
    await page.locator('#view-lists [data-sync-button]').click();
    const sync = await (await syncResponse).json();
    assert.equal(sync.ok, true);
    let status;
    for (let attempt = 0; attempt < 100; attempt++) {
      status = await (await page.request.get(origin + "/api/status")).json();
      if (!status.sync.running && status.sync.last_success) break;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    assert.ok(status.sync.last_success);
    assert.equal(status.sync.items_synced, 2);
    await page.locator("#refresh-status").click();
    await page.waitForFunction(() => !document.getElementById("refresh-status").disabled);
    await page.locator('[data-nav="events"]').click();
    await page.locator("#refresh-events").click();
    await page.locator(".event-message").filter({ hasText: "<img" }).waitFor();
    assert.equal(await page.locator("#event-list img").count(), 0);
    assert.equal(await page.evaluate(() => window.syntheticXss), undefined);
    await page.setViewportSize({ width: 390, height: 844 });
    for (const view of ["overview", "connections", "lists", "events"]) {
      await page.locator(`[data-nav="${view}"]`).click();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, view + " should fit a mobile screen");
    }

    await page.locator('[data-nav="connections"]').click();
    await page.locator("#alexa-email").fill("synthetic@example.invalid");
    await page.locator("#alexa-password").fill("synthetic-rejected-password");
    await page.locator("#alexa-code").fill("654321");
    await page.locator("#alexa-auth-start").click();
    await page.locator("#alexa-auth-message").filter({ hasText: "fehlgeschlagen" }).waitFor();
    assert.equal(await page.locator("#alexa-password").inputValue(), "");
    assert.equal(await page.locator("#alexa-code").inputValue(), "");
    assert.equal(await page.locator("#alexa-auth-start").isDisabled(), false);
    const disconnected = page.waitForResponse((result) => result.url() === origin + "/api/connections/alexa" && result.request().method() === "DELETE");
    await page.locator('[data-disconnect="alexa"]').click();
    assert.equal((await disconnected).status(), 200);
    await page.locator('[data-provider-pill="alexa"]').filter({ hasText: "Nicht eingerichtet" }).waitFor();
    status = await (await page.request.get(origin + "/api/status")).json();
    assert.equal(status.providers.find((provider) => provider.id === "alexa").configured, false);
    assert.equal(status.sync.enabled, false);

    await page.locator("#logout").click();
    await page.locator("#auth-screen").waitFor({ state: "visible" });
    await page.locator("#auth-password").fill("synthetic-local-password-123");
    await page.locator("#auth-submit").click();
    await page.locator("#app").waitFor({ state: "visible" });
    assert.equal((await (await page.request.get(origin + "/api/session")).json()).authenticated, true);
    assert.equal((await page.request.get(origin + "/tests/ui.test.cjs")).status(), 404);
    assert.deepEqual(failures, []);
  } catch (error) {
    if (page && !page.isClosed()) {
      const directory = process.env.UI_FAILURE_DIR || path.join(os.tmpdir(), "listenbruecke-ui-browser-failure");
      await mkdir(directory, { recursive: true });
      const diagnostics = await page.evaluate(() => ({
        activeElement: document.activeElement?.id,
        authVisible: !document.getElementById("auth-screen").hidden,
        error: document.getElementById("auth-error").textContent,
        formValid: document.getElementById("auth-form").checkValidity(),
        fields: ["auth-password", "auth-confirm"].map((id) => {
          const field = document.getElementById(id);
          return { id, value: field.value, length: field.value.length, required: field.required, disabled: field.disabled, minLength: field.minLength, valid: field.validity.valid, missing: field.validity.valueMissing, tooShort: field.validity.tooShort, validationMessage: field.validationMessage };
        }),
        events: window.__authTestEvents,
        focusCheck: window.__authTestFocus,
      })).catch(() => ({ error: "Browser diagnostics unavailable" }));
      await writeFile(path.join(directory, "diagnostics.json"), JSON.stringify({ network, diagnostics }, null, 2));
      await page.screenshot({ path: path.join(directory, "failure.png"), fullPage: true }).catch(() => {});
      error.message += "\nSynthetic browser diagnostics: " + directory;
    }
    throw error;
  } finally {
    if (browser) await browser.close();
    if (child.pid && child.exitCode === null) {
      const exited = once(child, "exit");
      child.kill("SIGTERM");
      const timeout = setTimeout(() => child.kill("SIGKILL"), 5000);
      timeout.unref();
      await exited;
      clearTimeout(timeout);
    }
  }
});
