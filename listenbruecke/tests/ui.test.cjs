// Browser regression check. Install Playwright in a development environment first.
// From listenbruecke/: npm ci && npm run test:ui
// PLAYWRIGHT_MODULE may point to an existing Playwright installation.
const assert = require("node:assert/strict");
const { readFile } = require("node:fs/promises");
const http = require("node:http");
const path = require("node:path");
const { test } = require("node:test");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

test("local setup, provider connections, optional HA list, sync, and ingress layout", { timeout: 60000 }, async () => {
  const prefix = "/ingress/browser-test/";
  const server = http.createServer(async (request, response) => {
    const filename = new URL(request.url, "http://localhost").pathname.slice(prefix.length) || "index.html";
    if (!["index.html", "styles.css", "app.js"].includes(filename)) {
      response.writeHead(404).end();
      return;
    }
    const content = await readFile(path.join(__dirname, "..", "web", filename));
    response.writeHead(200, { "Content-Type": filename.endsWith("html") ? "text/html" : filename.endsWith("css") ? "text/css" : "application/javascript" }).end(content);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  server.unref();
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  const calls = [];
  let acceptNextSync = false;
  let session = { authenticated: false, setup_required: true, ingress: false };
  let settings = { enabled: false, interval_seconds: 60, targets: [], propagate_deletions: false };
  const providers = [
    { id: "bring", name: "Bring!", configured: false, connected: false, error: null, lists: [], last_test: null },
    { id: "monsieur_cuisine", name: "Monsieur Cuisine", configured: false, connected: false, error: null, lists: [], last_test: null },
    { id: "homeassistant", name: "Home Assistant", configured: false, connected: false, error: null, lists: [], last_test: null },
    { id: "alexa", name: "Amazon Alexa", configured: false, connected: false, error: null, lists: [], last_test: null },
  ];
  const status = { version: "test", mode: "standalone", providers, sync: { enabled: false, interval_seconds: 60, last_run: null, last_success: null, last_error: null, items_synced: 0 }, events: [] };
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const endpoint = new URL(request.url()).pathname.slice((prefix + "api/").length);
    const method = request.method();
    const body = request.postDataJSON();
    calls.push({ endpoint, method, body, contentType: request.headers()["content-type"] });
    const respond = (json, code = 200) => route.fulfill({ status: code, contentType: "application/json", body: JSON.stringify(json) });
    if (endpoint === "session") return respond(session);
    if (endpoint === "setup" || endpoint === "login") {
      session = { authenticated: true, setup_required: false, ingress: false };
      return respond({ ok: true });
    }
    if (endpoint === "logout") { session.authenticated = false; return respond({ ok: true }); }
    if (endpoint === "status") return respond(status);
    if (endpoint === "events") return respond(status.events);
    if (endpoint === "connections/alexa/auth/status") return respond({ running: false, complete: false, error: null });
    if (endpoint === "settings") {
      if (method === "PUT") { settings = body; status.sync.enabled = body.enabled; status.sync.interval_seconds = body.interval_seconds; }
      return respond(settings);
    }
    if (endpoint === "sync") {
      if (acceptNextSync) {
        status.sync.running = true;
        acceptNextSync = false;
        return respond({ ok: true, accepted: true, added: 0, updated: 0, removed: 0, conflicts: 0, message: "Der Abgleich wurde gestartet. Den Fortschritt findest du in der Übersicht." }, 202);
      }
      status.sync.last_run = "2026-11-09T20:00:00Z";
      status.sync.last_success = "2026-11-09T20:00:00Z";
      status.events.push({ time: "2026-11-09T20:00:00Z", level: "success", message: "Testabgleich abgeschlossen." });
      return respond({ ok: true, added: 2, updated: 1, removed: 0, conflicts: 0, message: "Testabgleich abgeschlossen." });
    }
    if (endpoint === "connections/monsieur_cuisine/auth/start") return respond({ authorization_url: origin + "/test-lidl-login" });
    const match = endpoint.match(/^connections\/(bring|monsieur_cuisine|homeassistant)(?:\/(test|auth\/complete))?$/);
    if (match) {
      const provider = providers.find((entry) => entry.id === match[1]);
      if (method === "DELETE") { provider.configured = false; provider.connected = false; provider.lists = []; return respond({ ok: true }); }
      if (method === "PUT" || match[2] === "auth/complete") { provider.configured = true; return respond({ ok: true }); }
      if (match[2] === "test") {
        provider.connected = true;
        provider.last_test = "2026-11-09T19:00:00Z";
        provider.lists = provider.id === "homeassistant" ? [] : [{ id: provider.id + "-test-list", name: "Test-Einkaufsliste" }];
        return respond(provider);
      }
    }
    return respond({ detail: "Unbekannte Testanfrage: " + endpoint }, 404);
  });

  try {
    await page.goto(origin + prefix);
    await page.locator("#auth-title").filter({ hasText: "Willkommen bei Listenbrücke" }).waitFor();
    await page.locator("#auth-password").fill("synthetic-password-123");
    await page.locator("#auth-confirm").fill("different-password-123");
    await page.locator("#auth-submit").click();
    assert.match(await page.locator("#auth-error").textContent(), /stimmen/);
    assert.equal(calls.filter((call) => call.endpoint === "setup").length, 0);
    await page.locator("#auth-confirm").fill("synthetic-password-123");
    await page.locator("#auth-submit").click();
    await page.locator("#app").waitFor({ state: "visible" });
    await page.locator("#last-refreshed").filter({ hasText: "Aktualisiert" }).waitFor();
    assert.match(await page.locator("#last-run").textContent(), /Noch kein/);
    assert.equal(await page.locator("[data-provider-card]").count(), 4);
    assert.equal(await page.locator("#nav-connection-count").textContent(), "0/4");
    assert.equal(await page.locator("[data-sync-button]").first().isDisabled(), true);
    await page.screenshot({ path: process.env.UI_SCREENSHOT || "/tmp/listenbruecke-overview.png", fullPage: true });

    await page.locator('[data-nav="connections"]').click();
    await page.locator("#bring-email").fill("synthetic@example.invalid");
    await page.locator("#bring-password").fill("synthetic-bring-password");
    await page.locator('#bring-form button[type="submit"]').click();
    await page.locator('[data-provider-pill="bring"]').filter({ hasText: "Verbunden" }).waitFor();
    assert.equal(await page.locator("#bring-password").inputValue(), "");
    assert.deepEqual(calls.find((call) => call.endpoint === "connections/bring" && call.method === "PUT").body, { email: "synthetic@example.invalid", password: "synthetic-bring-password" });

    const popupPromise = page.waitForEvent("popup");
    await page.locator("#mc-auth-start").click();
    const popup = await popupPromise;
    await page.locator("#mc-auth-fallback").waitFor({ state: "visible" });
    assert.equal(await page.locator("#mc-auth-fallback").getAttribute("href"), origin + "/test-lidl-login");
    await popup.close();
    await page.locator("#mc-auth-code").fill(JSON.stringify({ code: "synthetic-code", state: "synthetic-state" }));
    await page.locator('#mc-auth-form button[type="submit"]').click();
    await page.locator('[data-provider-pill="monsieur_cuisine"]').filter({ hasText: "Verbunden" }).waitFor();
    assert.equal(await page.locator("#mc-auth-code").inputValue(), "");
    await page.locator("#ha-url").fill("http://homeassistant.invalid:8123/");
    await page.locator("#ha-token").fill("synthetic-ha-token");
    await page.locator('#ha-form button[type="submit"]').click();
    await page.locator('[data-provider-pill="homeassistant"]').filter({ hasText: "Verbunden" }).waitFor();
    assert.equal(await page.locator("#ha-token").inputValue(), "");

    await page.locator('[data-nav="lists"]').click();
    await page.locator("#list-monsieur_cuisine").selectOption("monsieur_cuisine-test-list");
    await page.locator("#list-bring").selectOption("bring-test-list");
    assert.equal(await page.locator("#list-homeassistant").inputValue(), "");
    assert.equal(await page.locator("#sync-deletions").isChecked(), false);
    await page.locator("#sync-enabled").check();
    await page.locator("#settings-save").click();
    await page.locator("#toast-message").filter({ hasText: "eingeschaltet" }).waitFor();
    const savedSettings = calls.find((call) => call.endpoint === "settings" && call.method === "PUT").body;
    assert.equal(savedSettings.targets.length, 2);
    assert.equal(savedSettings.propagate_deletions, false);
    await page.locator("#list-monsieur_cuisine").selectOption("");
    await page.locator("#refresh-status").click();
    await page.waitForFunction(() => !document.getElementById("refresh-status").disabled);
    assert.equal(await page.locator("#list-monsieur_cuisine").inputValue(), "", "Polling must preserve an unsaved cleared selection");
    await page.locator("#list-monsieur_cuisine").selectOption("monsieur_cuisine-test-list");
    await page.locator('#view-lists [data-sync-button]').click();
    await page.locator("#sync-result").filter({ hasText: "2 hinzugefügt" }).waitFor();
    acceptNextSync = true;
    await page.locator('#view-lists [data-sync-button]').click();
    await page.locator("#sync-result").filter({ hasText: "wurde gestartet" }).waitFor();
    await page.waitForFunction(() => !document.querySelector('#view-lists [data-sync-button]').hasAttribute("aria-busy"));
    assert.equal((await page.locator("#sync-result").textContent()).includes("0 hinzugefügt"), false);
    assert.equal(await page.locator('#view-lists [data-sync-button]').isDisabled(), true);
    assert.equal(await page.locator("#sync-pill").textContent(), "Läuft");
    status.sync.running = false;
    await page.locator("#refresh-status").click();
    await page.waitForFunction(() => !document.getElementById("refresh-status").disabled);
    assert.equal(await page.locator('#view-lists [data-sync-button]').isDisabled(), false);
    assert.equal(await page.locator("#sync-pill").textContent(), "Aktiv");

    status.events.push({ time: "2026-11-09T20:01:00Z", level: "warning", message: '<img src=x onerror="window.syntheticXss=true">' });
    await page.locator('[data-nav="events"]').click();
    await page.locator("#refresh-events").click();
    await page.locator(".event-message").filter({ hasText: "<img" }).waitFor();
    assert.equal(await page.locator("#event-list img").count(), 0);
    assert.equal(await page.evaluate(() => window.syntheticXss), undefined);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
    await page.locator('[data-nav="overview"]').click();
    await page.screenshot({ path: "/tmp/listenbruecke-mobile.png", fullPage: true });
    for (const view of ["connections", "lists", "events"]) {
      await page.locator(`[data-nav="${view}"]`).click();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, view + " should fit a mobile screen");
    }

    await page.locator("#logout").click();
    await page.locator("#auth-screen").waitFor({ state: "visible" });
    await page.locator("#auth-password").fill("synthetic-password-123");
    await page.locator("#auth-submit").click();
    await page.locator("#app").waitFor({ state: "visible" });
    assert.equal(calls.some((call) => call.endpoint === "login"), true);

    status.mode = "addon";
    session.ingress = true;
    providers.find((provider) => provider.id === "homeassistant").configured = false;
    await page.reload();
    await page.locator('[data-nav="connections"]').click();
    await page.locator("#ha-addon").waitFor({ state: "visible" });
    assert.equal(await page.locator("#ha-form").isVisible(), false);
    assert.equal(await page.locator("#logout").isVisible(), false);
    assert.equal(await page.locator('[data-disconnect="homeassistant"]').isVisible(), false);
    assert.equal(await page.locator('[data-test="homeassistant"]').isDisabled(), false);
    await page.locator("#ha-auto-connect").click();
    await page.locator("#toast-message").filter({ hasText: "Home Assistant: Verbindung erfolgreich" }).waitFor();
    assert.equal(calls.some((call) => call.endpoint === "connections/homeassistant" && call.body?.auto === true), true);
    await page.locator('[data-disconnect="bring"]').click();
    await page.locator('[data-provider-pill="bring"]').filter({ hasText: "Nicht eingerichtet" }).waitFor();
    const disconnect = calls.find((call) => call.endpoint === "connections/bring" && call.method === "DELETE");
    assert.deepEqual(disconnect.body, {});
    assert.equal(disconnect.contentType, "application/json");
    assert.deepEqual(browserErrors, []);
    assert.ok(calls.every((call) => !call.endpoint.includes("ingress/")), "All API URLs must preserve the ingress prefix");
  } finally {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
});

test("Alexa login clears secrets, resumes polling, reports failures, and syncs with Bring alone", { timeout: 60000 }, async () => {
  const prefix = "/ingress/alexa-test/";
  const server = http.createServer(async (request, response) => {
    const filename = new URL(request.url, "http://localhost").pathname.slice(prefix.length) || "index.html";
    if (!["index.html", "styles.css", "app.js"].includes(filename)) return response.writeHead(404).end();
    const content = await readFile(path.join(__dirname, "..", "web", filename));
    response.writeHead(200, { "Content-Type": filename.endsWith("html") ? "text/html" : filename.endsWith("css") ? "text/css" : "application/javascript" }).end(content);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  server.unref();
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  const providers = ["monsieur_cuisine", "bring", "alexa", "homeassistant"].map((id) => ({ id, name: id, configured: id === "bring", connected: id === "bring", error: null, lists: id === "bring" ? [{ id: "bring-list", name: "Test-Einkauf" }] : [], last_test: null }));
  const alexa = providers.find((provider) => provider.id === "alexa");
  const status = { version: "0.2.0", mode: "standalone", providers, sync: { enabled: false, interval_seconds: 60, running: false, last_run: null, last_success: null, last_error: null, items_synced: 0 }, events: [] };
  let settings = { enabled: false, interval_seconds: 60, targets: [], propagate_deletions: false };
  let auth = { running: false, complete: false, error: null };
  const starts = [];
  const calls = [];
  let releaseStart;
  let receiveStart;
  let startError = null;
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const endpoint = new URL(request.url()).pathname.slice((prefix + "api/").length);
    const body = request.postDataJSON();
    calls.push({ endpoint, method: request.method() });
    const respond = (data, code = 200) => route.fulfill({ status: code, contentType: "application/json", body: JSON.stringify(data) });
    if (endpoint === "session") return respond({ authenticated: true, setup_required: false, ingress: false });
    if (endpoint === "status") return respond(status);
    if (endpoint === "events") return respond([]);
    if (endpoint === "settings") {
      if (request.method() === "PUT") { settings = body; status.sync.enabled = body.enabled; status.sync.interval_seconds = body.interval_seconds; }
      return respond(settings);
    }
    if (endpoint === "connections/alexa/auth/status") return respond(auth);
    if (endpoint === "connections/alexa/auth/start") {
      starts.push(body);
      receiveStart?.();
      if (startError) return respond({ detail: startError }, 400);
      auth = { running: true, complete: false, error: null };
      await new Promise((resolve) => { releaseStart = resolve; });
      return respond({ running: true }, 202);
    }
    if (endpoint === "connections/alexa/test") return respond(alexa);
    if (endpoint === "connections/alexa" && request.method() === "DELETE") {
      assert.deepEqual(body, {});
      alexa.connected = false; alexa.configured = false; alexa.lists = [];
      auth = { running: false, complete: false, error: null };
      status.sync.enabled = false;
      return respond(alexa);
    }
    if (endpoint === "sync") return respond({ ok: true, added: 1, updated: 0, removed: 0, conflicts: 0, message: "Testabgleich abgeschlossen." });
    return respond({ detail: "Unbekannte Testanfrage." }, 404);
  });

  async function fillAlexa(code = "123456") {
    await page.locator("#alexa-email").fill("synthetic@example.invalid");
    await page.locator("#alexa-password").fill("synthetic-alexa-password");
    await page.locator("#alexa-code").fill(code);
  }

  try {
    await page.goto(origin + prefix);
    await page.locator("#last-refreshed").filter({ hasText: "Aktualisiert" }).waitFor();
    assert.equal(await page.locator("#nav-connection-count").textContent(), "1/4");
    const cards = await page.locator("[data-provider-card]").evaluateAll((elements) => elements.map((element) => ({ x: element.getBoundingClientRect().x, y: element.getBoundingClientRect().y })));
    assert.equal(cards[0].y, cards[1].y);
    assert.equal(cards[2].y, cards[3].y);
    assert.ok(cards[2].y > cards[0].y, "Four provider cards should form two desktop rows");
    await page.locator('[data-nav="connections"]').click();
    assert.equal(await page.locator("#alexa-code").getAttribute("autocomplete"), "one-time-code");
    assert.equal(await page.locator("#alexa-code").getAttribute("inputmode"), "numeric");
    await fillAlexa("12345");
    await page.locator("#alexa-auth-start").click();
    assert.equal(starts.length, 0, "An incomplete Authenticator code must not be sent");
    await page.locator("#alexa-code").fill("123456");
    const firstStart = new Promise((resolve) => { receiveStart = resolve; });
    await page.locator("#alexa-auth-start").click();
    await firstStart;
    assert.deepEqual(starts[0], { email: "synthetic@example.invalid", password: "synthetic-alexa-password", code: "123456" });
    assert.equal(await page.locator("#alexa-password").inputValue(), "", "Password must clear before the POST response");
    assert.equal(await page.locator("#alexa-code").inputValue(), "", "Code must clear before the POST response");
    assert.equal(await page.locator("#alexa-auth-start").isDisabled(), true);
    releaseStart();
    await page.locator("#alexa-auth-message").filter({ hasText: "Anmeldung läuft" }).waitFor();
    await page.reload();
    await page.locator('[data-nav="connections"]').click();
    await page.locator("#alexa-auth-message").filter({ hasText: "Anmeldung läuft" }).waitFor();
    assert.equal(await page.locator("#alexa-auth-start").isDisabled(), true);
    assert.equal(await page.locator("#alexa-password").inputValue(), "");
    assert.equal(await page.locator("#alexa-code").inputValue(), "");
    assert.deepEqual(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length })), { local: 0, session: 0 });

    await page.locator('[data-nav="lists"]').click();
    await page.locator("#sync-interval").fill("90");
    await page.locator("#list-bring").selectOption("bring-list");
    alexa.configured = true; alexa.connected = true; alexa.lists = [{ id: "alexa-list", name: "Test-Alexa-Einkauf" }];
    auth = { running: false, complete: true, error: null };
    await page.locator('#list-alexa option[value="alexa-list"]').waitFor({ state: "attached" });
    assert.equal(await page.locator("#sync-interval").inputValue(), "90", "Auth polling must preserve unsaved settings");
    assert.equal(await page.locator("#list-bring").inputValue(), "bring-list");
    assert.equal(await page.locator("#nav-connection-count").textContent(), "2/4");
    await page.locator("#list-alexa").selectOption("alexa-list");
    await page.locator("#sync-enabled").check();
    await page.locator("#settings-save").click();
    await page.locator("#toast-message").filter({ hasText: "eingeschaltet" }).waitFor();
    assert.deepEqual(settings.targets, [{ provider: "bring", list_id: "bring-list" }, { provider: "alexa", list_id: "alexa-list" }]);
    assert.equal(settings.interval_seconds, 90);
    assert.equal(await page.locator('#view-lists [data-sync-button]').isDisabled(), false);
    await page.locator('#view-lists [data-sync-button]').click();
    await page.locator("#sync-result").filter({ hasText: "1 hinzugefügt" }).waitFor();
    await page.locator('[data-nav="overview"]').click();
    await page.locator("#toast-close").click();
    await page.mouse.move(1430, 1090);
    await page.screenshot({ path: "/tmp/listenbruecke-alexa-overview.png", fullPage: true, animations: "disabled" });

    await page.locator('[data-nav="connections"]').click();
    await page.locator('[data-test="alexa"]').click();
    await page.locator("#toast-message").filter({ hasText: "Amazon Alexa: Verbindung erfolgreich" }).waitFor();
    await fillAlexa();
    const failedStart = new Promise((resolve) => { receiveStart = resolve; });
    await page.locator("#alexa-auth-start").click();
    await failedStart;
    assert.equal(await page.locator("#alexa-password").inputValue(), "");
    assert.equal(await page.locator("#alexa-code").inputValue(), "");
    releaseStart();
    auth = { running: false, complete: false, error: "Die Amazon-Anmeldung ist fehlgeschlagen. Bitte prüfe den Einmalcode." };
    await page.locator("#alexa-auth-message").filter({ hasText: "fehlgeschlagen" }).waitFor();
    assert.equal(await page.locator("#alexa-auth-start").isDisabled(), false);
    assert.equal(await page.locator("#alexa-auth-notice").evaluate((element) => element.classList.contains("error")), true);

    startError = "Die Anmeldung konnte nicht gestartet werden. Bitte erneut versuchen.";
    await fillAlexa();
    await page.locator("#alexa-auth-start").click();
    await page.locator("#alexa-auth-message").filter({ hasText: "nicht gestartet" }).waitFor();
    assert.equal(await page.locator("#alexa-password").inputValue(), "", "Password must also clear after a rejected start");
    assert.equal(await page.locator("#alexa-code").inputValue(), "");
    assert.equal(await page.locator("#alexa-auth-start").isDisabled(), false);
    assert.notEqual(await page.locator('[data-provider-pill="alexa"]').textContent(), "Anmeldung läuft");
    await page.locator('[data-disconnect="alexa"]').click();
    await page.locator('[data-provider-pill="alexa"]').filter({ hasText: "Nicht eingerichtet" }).waitFor();
    assert.equal(await page.locator("#alexa-auth-notice").isVisible(), false);
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      for (const view of ["overview", "connections", "lists"]) {
        await page.locator(`[data-nav="${view}"]`).click();
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${view} should fit ${width}px`);
      }
    }
    assert.equal(calls.some((call) => call.endpoint === "connections/alexa/test"), true);
    assert.deepEqual(browserErrors, []);
  } finally {
    releaseStart?.();
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
});
