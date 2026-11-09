const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const events = {};
let pending;
let tabs = 0;
global.chrome = {
  webRequest: {
    onHeadersReceived: {addListener(callback) {events.headers = callback;}},
    onBeforeRedirect: {addListener(callback) {events.redirect = callback;}}
  },
  storage: {session: {
    async get() {return {pending};},
    async set(value) {pending = value.pending;}
  }},
  runtime: {getURL(value) {return "chrome-extension://test/" + value;}},
  tabs: {async create() {tabs += 1;}}
};
const helper = path.join(__dirname, "../browser-helper");
const {parseRedirect} = require(path.join(helper, "background.js"));
const uri = "lidl-com.monsieur-cuisine.Companion://login/prod?code=one%26time&state=opaque";

test("only exact manufacturer redirect with one code/state is accepted", () => {
  assert.deepEqual(parseRedirect(uri), {code: "one&time", state: "opaque"});
  for (const value of ["https://evil.example/?code=x&state=x", uri + "&code=duplicate",
                       uri.replace("/prod?", "/prod-other?"), uri.replace("login/", "evil/")]) {
    assert.equal(parseRedirect(value), null);
  }
});

test("duplicate browser events store only temporary code/state and open one page", async () => {
  events.headers({responseHeaders: [{name: "Location", value: uri}]});
  events.redirect({redirectUrl: uri});
  await new Promise(setImmediate);
  assert.equal(tabs, 1);
  assert.deepEqual(Object.keys(pending).sort(), ["code", "expires", "state"]);
  assert.equal(pending.state, "opaque");
  assert.ok(pending.expires > Date.now());
  assert.ok(pending.expires <= Date.now() + 5 * 60 * 1000);
});

test("manifest has only narrowly scoped permissions and no callback receiver", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(helper, "manifest.json")));
  assert.deepEqual(manifest.permissions, ["webRequest", "storage"]);
  assert.deepEqual(manifest.host_permissions, ["https://accounts.lidl.com/*"]);
  assert.equal(manifest.externally_connectable, undefined);
  const source = fs.readFileSync(path.join(helper, "background.js"), "utf8");
  assert.equal(source.includes("console."), false);
  assert.equal(source.includes("fetch("), false);
});
