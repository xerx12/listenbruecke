// Adapted from MrVain74/ha-monsieur-cuisine (MIT, Copyright 2026 MrVain74).
// No callback URL is embedded in OAuth state or sent to a third party.
let activeState = null;

function parseRedirect(value) {
  try {
    const url = new URL(value);
    if (url.protocol.toLowerCase() !== "lidl-com.monsieur-cuisine.companion:" ||
        url.hostname !== "login" || url.pathname !== "/prod") return null;
    if (url.searchParams.getAll("code").length !== 1 ||
        url.searchParams.getAll("state").length !== 1) return null;
    const code = url.searchParams.get("code");
    const state = url.searchParams.get("state");
    if (!code || !state || code.length > 8192 || state.length > 512) return null;
    return {code, state};
  } catch (_) {
    return null;
  }
}

async function capture(value) {
  const data = parseRedirect(value);
  if (!data || data.state === activeState) return;
  activeState = data.state; // Both redirect events can fire for the same response.
  try {
    const {pending} = await chrome.storage.session.get("pending");
    if (pending && pending.state === data.state && pending.expires > Date.now()) return;
    await chrome.storage.session.set({pending: {...data, expires: Date.now() + 5 * 60 * 1000}});
    await chrome.tabs.create({url: chrome.runtime.getURL("handoff.html")});
  } catch (_) {
    activeState = null;
    // Never log the code, request URL, state or any login response.
  }
}

chrome.webRequest.onHeadersReceived.addListener((details) => {
  const location = (details.responseHeaders || []).find(
    (header) => header.name.toLowerCase() === "location"
  );
  if (location && location.value) void capture(location.value);
}, {urls: ["https://accounts.lidl.com/*"]}, ["responseHeaders"]);

chrome.webRequest.onBeforeRedirect.addListener((details) => {
  if (details.redirectUrl) void capture(details.redirectUrl);
}, {urls: ["https://accounts.lidl.com/*"]});

// Export only for Node's offline unit tests, never to web pages.
if (typeof module !== "undefined") module.exports = {parseRedirect};
