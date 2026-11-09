"use strict";

(() => {
  const PROVIDERS = [
    { id: "monsieur_cuisine", name: "Monsieur Cuisine", icon: "pot", className: "cuisine-icon", description: "Rezepte und Einkäufe aus deiner Küche." },
    { id: "bring", name: "Bring!", icon: "shopping", className: "bring-icon", description: "Gemeinsam planen. Gemeinsam einkaufen." },
    { id: "alexa", name: "Amazon Alexa", icon: "alexa", className: "alexa-icon", description: "Deine Einkaufsliste per Sprache ergänzen." },
    { id: "homeassistant", name: "Home Assistant", icon: "home", className: "ha-icon", description: "Deine Einkaufsliste im smarten Zuhause." },
  ];
  const VIEWS = { overview: "Übersicht", connections: "Verbindungen", lists: "Listen & Abgleich", events: "Protokoll" };
  const state = {
    session: null,
    status: null,
    settings: null,
    events: [],
    authenticated: false,
    refreshing: false,
    syncing: false,
    settingsDirty: false,
    toastTimer: null,
    alexaAuth: { running: false, complete: false, error: null },
    alexaSubmitting: false,
    alexaPollBusy: false,
    alexaPollTimer: null,
    alexaAuthGeneration: 0,
  };
  const $ = (id) => document.getElementById(id);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const setText = (id, value) => { $(id).textContent = value; };
  const providerById = (id) => state.status?.providers?.find((provider) => provider.id === id);
  const icon = (name) => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#icon-${name}`);
    svg.append(use);
    svg.setAttribute("aria-hidden", "true");
    return svg;
  };

  function errorMessage(detail) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map((entry) => typeof entry === "string" ? entry : entry?.msg || "Bitte prüfe deine Eingaben.").join(" ");
    }
    return "Die Anfrage konnte nicht abgeschlossen werden. Bitte versuche es erneut.";
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.timeout || 60000);
    try {
      const response = await fetch(`api/${path}`, {
        method: options.method || "GET",
        credentials: "same-origin",
        headers: options.body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: controller.signal,
      });
      let payload = {};
      if (response.status !== 204) {
        try { payload = await response.json(); }
        catch { throw new Error("Der Server hat keine gültige Antwort geliefert. Bitte prüfe, ob Listenbrücke läuft."); }
      }
      if (!response.ok) {
        if (response.status === 401 && state.authenticated) {
          state.authenticated = false;
          showAuth({ authenticated: false, setup_required: false, ingress: false });
        }
        throw new Error(errorMessage(payload.detail || payload.message));
      }
      return payload;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Der Server antwortet gerade nicht. Bitte prüfe die Verbindung und versuche es erneut.");
      if (error instanceof TypeError) throw new Error("Listenbrücke ist nicht erreichbar. Bitte prüfe deine Netzwerkverbindung.");
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  function toast(message, isError = false) {
    clearTimeout(state.toastTimer);
    setText("toast-message", message);
    $("toast").classList.toggle("error", isError);
    $("toast").querySelector("use").setAttribute("href", isError ? "#icon-info" : "#icon-check");
    $("toast").hidden = false;
    state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, isError ? 10000 : 6500);
  }

  async function busy(button, text, operation) {
    if (button.disabled) return;
    const label = button.querySelector("[data-label]");
    const previousText = label?.textContent;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    if (label) label.textContent = text;
    try { return await operation(); }
    catch (error) { toast(error.message, true); }
    finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      if (label) label.textContent = previousText;
      if (button.matches("[data-sync-button]")) renderOverview();
    }
  }

  function dateTime(value, fallback = "Noch keiner") {
    if (!value) return fallback;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "Zeitpunkt nicht verfügbar";
    return new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
  }

  function connectionState(provider) {
    if (!state.status) return { label: "Wird geladen", className: "pending" };
    if (provider?.connected) return { label: "Verbunden", className: "success" };
    if (provider?.error) return { label: "Prüfung nötig", className: "error" };
    if (provider?.configured) return { label: "Gespeichert", className: "pending" };
    return { label: "Nicht eingerichtet", className: "neutral" };
  }

  function setPill(element, info) {
    element.textContent = info.label;
    element.className = `pill ${info.className}`;
  }

  function createProviderCards() {
    for (const provider of PROVIDERS) {
      const card = $("provider-card-template").content.firstElementChild.cloneNode(true);
      card.dataset.providerCard = provider.id;
      card.querySelector(".provider-icon").classList.add(provider.className);
      card.querySelector(".provider-icon use").setAttribute("href", `#icon-${provider.icon}`);
      card.querySelector("h3").textContent = provider.name;
      card.querySelector(".provider-card-description").textContent = provider.description;
      card.querySelector(".provider-card-link").addEventListener("click", () => {
        setTimeout(() => $("connection-" + provider.id).scrollIntoView({ block: "start", behavior: "smooth" }), 30);
      });
      $("provider-cards").append(card);
      const row = $("list-selector-template").content.firstElementChild.cloneNode(true);
      row.querySelector(".provider-icon").classList.add(provider.className);
      row.querySelector(".provider-icon use").setAttribute("href", `#icon-${provider.icon}`);
      row.querySelector(".list-provider-name").textContent = provider.name;
      const selectId = `list-${provider.id}`;
      const label = row.querySelector("label");
      label.setAttribute("for", selectId);
      label.textContent = "Einkaufsliste";
      const select = row.querySelector("select");
      select.id = selectId;
      select.name = provider.id;
      select.setAttribute("aria-label", `Einkaufsliste für ${provider.name}`);
      $("list-selectors").append(row);
    }
  }

  function renderProviders() {
    for (const meta of PROVIDERS) {
      const provider = providerById(meta.id);
      const info = connectionState(provider);
      const card = document.querySelector(`[data-provider-card="${meta.id}"]`);
      setPill(card.querySelector(".pill"), info);
      card.querySelector(".provider-card-link span").textContent = provider?.connected ? "Verbindung ansehen" : provider?.configured ? "Verbindung prüfen" : "Jetzt verbinden";
      setPill(document.querySelector(`[data-provider-pill="${meta.id}"]`), info);
      const error = document.querySelector(`[data-provider-error="${meta.id}"]`);
      error.textContent = typeof provider?.error === "string" ? provider.error : "";
      error.hidden = !error.textContent;
      const testButton = document.querySelector(`[data-test="${meta.id}"]`);
      if (!testButton.hasAttribute("aria-busy")) testButton.disabled = !provider?.configured;
      document.querySelector(`[data-disconnect="${meta.id}"]`).hidden = !provider?.configured;
      document.querySelector(`[data-last-test="${meta.id}"]`).textContent = provider?.last_test ? `Zuletzt geprüft: ${dateTime(provider.last_test)}` : "Noch nicht geprüft";
    }
    const addon = Boolean(state.session?.ingress || state.status?.mode === "addon" || state.status?.mode === "add-on");
    $("ha-addon").hidden = !addon;
    $("ha-form").hidden = addon;
    if (addon) {
      document.querySelector('[data-disconnect="homeassistant"]').hidden = true;
      const testButton = document.querySelector('[data-test="homeassistant"]');
      if (!testButton.hasAttribute("aria-busy")) testButton.disabled = false;
    }
    $$("input", $("ha-form")).forEach((input) => { input.disabled = addon; });
    $("logout").hidden = Boolean(state.session?.ingress);
    renderAlexaAuth();
  }

  function renderAlexaAuth() {
    const auth = state.alexaAuth;
    const active = state.alexaSubmitting || auth.running;
    const button = $("alexa-auth-start");
    $$("input", $("alexa-form")).forEach((input) => { input.disabled = active; });
    button.disabled = active;
    button.querySelector("[data-label]").textContent = active ? "Anmeldung läuft …" : "Anmelden & verbinden";
    if (active) button.setAttribute("aria-busy", "true");
    else button.removeAttribute("aria-busy");
    const notice = $("alexa-auth-notice");
    notice.hidden = !active && !auth.complete && !auth.error;
    notice.className = `notice alexa-auth-notice${auth.error ? " error" : auth.complete && !active ? " success" : ""}`;
    notice.querySelector("use").setAttribute("href", auth.error ? "#icon-info" : auth.complete && !active ? "#icon-check" : "#icon-clock");
    setText("alexa-auth-message", auth.error || (active ? "Die Amazon-Anmeldung läuft. Du kannst diese Seite neu laden; der Fortschritt wird wieder angezeigt." : auth.complete ? providerById("alexa")?.connected ? "Amazon Alexa ist verbunden. Du kannst deine Einkaufsliste auswählen." : "Amazon-Anmeldung abgeschlossen. Prüfe jetzt die Verbindung." : ""));
    for (const control of $$('[data-test="alexa"], [data-disconnect="alexa"]')) {
      if (!control.hasAttribute("aria-busy")) control.disabled = active || !providerById("alexa")?.configured;
    }
    const provider = providerById("alexa");
    const info = active ? { label: "Anmeldung läuft", className: "pending" } : auth.error && !provider?.connected ? { label: "Anmeldung prüfen", className: "error" } : connectionState(provider);
    setPill(document.querySelector('[data-provider-pill="alexa"]'), info);
    const card = document.querySelector('[data-provider-card="alexa"]');
    setPill(card.querySelector(".pill"), info);
    card.querySelector(".provider-card-link span").textContent = active ? "Anmeldung ansehen" : provider?.connected ? "Verbindung ansehen" : provider?.configured ? "Verbindung prüfen" : "Jetzt verbinden";
  }

  function resetAlexaAuth() {
    state.alexaAuthGeneration += 1;
    clearTimeout(state.alexaPollTimer);
    state.alexaPollTimer = null;
    state.alexaSubmitting = false;
    state.alexaAuth = { running: false, complete: false, error: null };
  }

  async function pollAlexaAuth({ quiet = false } = {}) {
    if (!state.authenticated || state.alexaPollBusy || state.alexaSubmitting) return;
    clearTimeout(state.alexaPollTimer);
    state.alexaPollTimer = null;
    state.alexaPollBusy = true;
    const generation = state.alexaAuthGeneration;
    const wasRunning = state.alexaAuth.running;
    try {
      const result = await api("connections/alexa/auth/status", { timeout: 15000 });
      if (!state.authenticated || generation !== state.alexaAuthGeneration) return;
      if (typeof result.running !== "boolean" || typeof result.complete !== "boolean" || !(result.error === null || typeof result.error === "string")) throw new Error("Der Anmeldestatus ist momentan nicht verfügbar. Bitte versuche es erneut.");
      state.alexaAuth = { running: result.running, complete: result.complete, error: result.error };
      renderAlexaAuth();
      if (!result.running && (result.complete || result.error)) {
        await refresh();
        if (wasRunning && !quiet) toast(result.error || "Amazon-Anmeldung abgeschlossen.", Boolean(result.error));
      }
    } catch (error) {
      if (state.authenticated && generation === state.alexaAuthGeneration && wasRunning) {
        state.alexaAuth.error = "Der Anmeldestatus ist momentan nicht erreichbar. Listenbrücke prüft ihn erneut.";
        renderAlexaAuth();
      }
    } finally {
      state.alexaPollBusy = false;
      if (state.authenticated && state.alexaAuth.running && !state.alexaSubmitting) state.alexaPollTimer = setTimeout(() => pollAlexaAuth(), 2000);
    }
  }

  function selectedTargets() {
    return state.settings?.targets || [];
  }

  function hasTargets() {
    return selectedTargets().filter((target) => target.list_id).length >= 2;
  }

  function connectionsReady() {
    return PROVIDERS.filter((meta) => providerById(meta.id)?.connected).length >= 2
      && selectedTargets().every((target) => !target.list_id || providerById(target.provider)?.connected);
  }

  function renderOverview() {
    const connectedCount = PROVIDERS.filter((meta) => providerById(meta.id)?.connected).length;
    const sync = state.status?.sync || {};
    const enabled = Boolean(sync.enabled);
    const running = Boolean(sync.running);
    const ready = connectionsReady();
    setText("nav-connection-count", `${connectedCount}/${PROVIDERS.length}`);
    setText("setup-progress", `${connectedCount} von ${PROVIDERS.length} verbunden`);
    setText("app-version", state.status?.version ? `Version ${state.status.version}` : "Listenbrücke");
    setText("app-mode", state.status?.mode === "addon" || state.session?.ingress ? "HA-Add-on" : "Selbst gehostet");
    $("global-dot").className = `status-dot ${running ? "live" : sync.last_error ? "error" : enabled && ready ? "live" : ""}`;
    setText("global-status", running ? "Abgleich läuft" : enabled ? !ready ? "Verbindung fehlt" : sync.last_error ? "Abgleich prüfen" : "Abgleich aktiv" : "Abgleich pausiert");
    setText("global-description", running ? "Deine Listen werden abgeglichen" : enabled ? `Prüfung alle ${sync.interval_seconds || 60} Sekunden` : `${connectedCount} von ${PROVIDERS.length} Konten verbunden`);
    setPill($("sync-pill"), { label: running ? "Läuft" : enabled ? sync.last_error ? "Hinweis" : "Aktiv" : "Pausiert", className: running ? "pending" : enabled ? sync.last_error ? "pending" : "success" : "neutral" });
    setText("last-run", dateTime(sync.last_run, "Noch kein Durchlauf"));
    setText("last-success", dateTime(sync.last_success));
    setText("current-interval", sync.interval_seconds ? `${sync.interval_seconds} Sekunden` : "Noch nicht festgelegt");
    setText("items-synced", sync.last_run && Number.isFinite(sync.items_synced) ? String(sync.items_synced) : "–");
    $("sync-last-error").textContent = typeof sync.last_error === "string" ? sync.last_error : "";
    $("sync-last-error").hidden = !$("sync-last-error").textContent;
    $("step-connect").classList.toggle("done", ready);
    $("step-lists").classList.toggle("done", hasTargets());
    $("step-enable").classList.toggle("done", enabled);
    const button = $("welcome-action");
    if (!ready) {
      setText("welcome-kicker", "DEINE LISTEN ZUSAMMENBRINGEN");
      $("welcome-title").replaceChildren(document.createTextNode("Einmal verbinden."), document.createElement("br"), document.createTextNode("Gemeinsam einkaufen."));
      setText("welcome-description", "Verbinde mindestens zwei Konten, wähle deine Einkaufslisten und starte den automatischen Abgleich.");
      button.replaceChildren(document.createTextNode("Verbindungen einrichten"), icon("arrow"));
      button.href = "#connections";
    } else if (!enabled) {
      setText("welcome-kicker", "DEINE KONTEN SIND BEREIT");
      $("welcome-title").replaceChildren(document.createTextNode("Die Verbindung steht."), document.createElement("br"), document.createTextNode("Dein Einkauf kann losgehen."));
      setText("welcome-description", "Wähle deine Einkaufslisten und schalte den regelmäßigen Abgleich ein, sobald alles passt.");
      button.replaceChildren(document.createTextNode("Listen & Abgleich einstellen"), icon("arrow"));
      button.href = "#lists";
    } else {
      setText("welcome-kicker", "DEIN AUTOMATISCHER ABGLEICH IST AKTIV");
      $("welcome-title").replaceChildren(document.createTextNode("Deine Listen."), document.createElement("br"), document.createTextNode("Ein gemeinsamer Einkauf."));
      setText("welcome-description", sync.last_error ? "Beim letzten Abgleich gab es einen Hinweis. Prüfe das Protokoll, um mehr zu erfahren." : "Listenbrücke prüft regelmäßig deine ausgewählten Listen. Den letzten Stand findest du unten.");
      button.replaceChildren(document.createTextNode("Protokoll ansehen"), icon("arrow"));
      button.href = "#events";
    }
    $$('[data-sync-button]').forEach((syncButton) => {
      syncButton.disabled = state.syncing || running || !ready || !hasTargets();
      syncButton.title = running ? "Der Abgleich läuft bereits." : !ready ? "Verbinde mindestens zwei Konten und prüfe die ausgewählten Verbindungen." : !hasTargets() ? "Wähle mindestens zwei Einkaufslisten aus und speichere die Einstellungen." : "Ausgewählte Listen jetzt abgleichen";
    });
  }

  function renderLists(applySettings = false) {
    for (const meta of PROVIDERS) {
      const select = $("list-" + meta.id);
      const provider = providerById(meta.id);
      const lists = Array.isArray(provider?.lists) ? provider.lists : [];
      const configuredValue = selectedTargets().find((target) => target.provider === meta.id)?.list_id || "";
      const chosenValue = applySettings ? configuredValue : state.settingsDirty ? select.value : select.value || configuredValue;
      const placeholder = document.createElement("option");
      placeholder.value = "";
      select.required = false;
      placeholder.textContent = meta.id === "homeassistant" ? "Keine zusätzliche HA-Liste" : "Nicht abgleichen";
      const options = [placeholder];
      for (const list of lists) {
        if (list.id === undefined || list.id === null) continue;
        const option = document.createElement("option");
        option.value = String(list.id);
        option.textContent = typeof list.name === "string" ? list.name : String(list.id);
        option.disabled = !provider?.connected;
        options.push(option);
      }
      if (chosenValue && !options.some((option) => option.value === String(chosenValue))) {
        const missingOption = document.createElement("option");
        missingOption.value = String(chosenValue);
        missingOption.textContent = "Gespeicherte Liste ist momentan nicht verfügbar";
        missingOption.disabled = true;
        options.push(missingOption);
      }
      select.replaceChildren(...options);
      select.value = String(chosenValue);
      select.disabled = !provider?.connected && !chosenValue;
    }
    const ready = PROVIDERS.filter((meta) => providerById(meta.id)?.connected && providerById(meta.id)?.lists?.length).length >= 2;
    setText("list-readiness", ready ? "Wähle mindestens zwei Listen aus. Die übrigen Dienste kannst du bei Bedarf ergänzen." : "Verbinde mindestens zwei Konten und prüfe die Verbindungen, damit ihre verfügbaren Listen hier erscheinen.");
    if (applySettings && state.settings) {
      $("sync-enabled").checked = Boolean(state.settings.enabled);
      $("sync-interval").value = state.settings.interval_seconds || 60;
      $("sync-deletions").checked = Boolean(state.settings.propagate_deletions);
      state.settingsDirty = false;
    }
  }

  function renderEvents() {
    const container = $("event-list");
    if (!state.events.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      const heading = document.createElement("h3");
      heading.textContent = "Hier beginnt deine Verbindung.";
      const description = document.createElement("p");
      description.textContent = "Sobald du ein Konto prüfst oder Listen abgleichst, erscheinen die Ereignisse hier.";
      empty.append(icon("activity"), heading, description);
      container.replaceChildren(empty);
      return;
    }
    const rows = state.events.slice().sort((left, right) => {
      const difference = new Date(right.time).getTime() - new Date(left.time).getTime();
      return Number.isFinite(difference) ? difference : 0;
    }).map((event) => {
      const row = document.createElement("div");
      row.className = "event-row";
      const time = document.createElement("time");
      time.className = "event-time";
      time.textContent = dateTime(event.time, "–");
      if (event.time) time.dateTime = String(event.time);
      const level = document.createElement("span");
      const normalizedLevel = String(event.level || "info").toLowerCase();
      const labels = { error: "Fehler", warning: "Hinweis", warn: "Hinweis", success: "Erfolgreich", info: "Info", debug: "Detail" };
      setPill(level, { label: labels[normalizedLevel] || "Info", className: normalizedLevel === "error" ? "error" : normalizedLevel === "success" ? "success" : ["warning", "warn"].includes(normalizedLevel) ? "pending" : "neutral" });
      const message = document.createElement("span");
      message.className = "event-message";
      message.textContent = typeof event.message === "string" ? event.message : "Ereignis ohne Nachricht";
      row.append(time, level, message);
      return row;
    });
    container.replaceChildren(...rows);
  }

  function route() {
    const requested = window.location.hash.slice(1) || "overview";
    const view = Object.hasOwn(VIEWS, requested) ? requested : "overview";
    for (const id of Object.keys(VIEWS)) $("view-" + id).hidden = id !== view;
    for (const item of $$("[data-nav]")) {
      if (item.dataset.nav === view) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    }
    setText("breadcrumb-current", VIEWS[view]);
    document.title = `${VIEWS[view]} · Listenbrücke`;
  }

  function showAuth(session) {
    resetAlexaAuth();
    state.session = session;
    state.authenticated = false;
    $("boot").hidden = true;
    $("app").hidden = true;
    $("auth-screen").hidden = false;
    $("auth-form").reset();
    ["bring-form", "ha-form", "mc-auth-form", "alexa-form"].forEach((id) => $(id).reset());
    $("mc-auth-fallback").hidden = true;
    $("mc-auth-fallback").removeAttribute("href");
    $("auth-error").hidden = true;
    const setup = Boolean(session.setup_required);
    setText("auth-title", setup ? "Willkommen bei Listenbrücke." : "Willkommen zurück.");
    setText("auth-description", setup ? "Lege ein Passwort fest, um deine Konten und Einstellungen zu schützen." : "Melde dich mit deinem Listenbrücke-Passwort an.");
    $("auth-confirm-group").hidden = !setup;
    $("auth-confirm").required = setup;
    $("auth-confirm").disabled = !setup;
    $("auth-password").autocomplete = setup ? "new-password" : "current-password";
    $("auth-password").minLength = setup ? 12 : 1;
    $("auth-password").placeholder = setup ? "Mindestens 12 Zeichen" : "Dein Passwort";
    $("auth-submit").querySelector("[data-label]").textContent = setup ? "Listenbrücke einrichten" : "Anmelden";
    $("auth-password").focus();
  }

  async function showApp(session) {
    state.session = session;
    state.authenticated = true;
    $("boot").hidden = true;
    $("auth-screen").hidden = true;
    $("app").hidden = false;
    route();
    renderProviders();
    await refresh({ includeSettings: true });
    await pollAlexaAuth({ quiet: true });
  }

  async function refresh({ includeSettings = false, quiet = false } = {}) {
    if (state.refreshing || !state.authenticated) return;
    state.refreshing = true;
    $("refresh-status").setAttribute("aria-busy", "true");
    try {
      const [status, settings] = await Promise.all([
        api("status", { timeout: 20000 }),
        includeSettings || !state.settings ? api("settings", { timeout: 20000 }) : Promise.resolve(null),
      ]);
      if (!status || !Array.isArray(status.providers) || typeof status.sync !== "object") throw new Error("Der Status des Servers ist unvollständig. Bitte versuche es erneut.");
      state.status = status;
      if (settings) state.settings = settings;
      if (Array.isArray(status.events)) state.events = status.events;
      $("server-error").hidden = true;
      renderProviders();
      renderLists(Boolean(settings));
      renderOverview();
      renderEvents();
      setText("last-refreshed", `Aktualisiert um ${new Intl.DateTimeFormat("de-DE", { hour: "2-digit", minute: "2-digit" }).format(new Date())} Uhr`);
    } catch (error) {
      if (state.authenticated) {
        $("server-error").textContent = error.message;
        $("server-error").hidden = false;
        if (!quiet) toast(error.message, true);
      }
    } finally {
      state.refreshing = false;
      $("refresh-status").removeAttribute("aria-busy");
    }
  }

  async function testProvider(id) {
    const result = await api(`connections/${id}/test`, { method: "POST", body: {} });
    await refresh();
    const provider = result?.provider || result;
    if (provider?.connected === false || provider?.error) throw new Error(errorMessage(provider.error || "Die Verbindung konnte nicht hergestellt werden."));
    const name = PROVIDERS.find((entry) => entry.id === id)?.name || "Konto";
    toast(`${name}: Verbindung erfolgreich geprüft.`);
    return result;
  }

  async function saveAndTest(id, credentials, form) {
    try {
      await api(`connections/${id}`, { method: "PUT", body: credentials });
      form?.reset();
      await testProvider(id);
    } finally {
      form?.reset();
      await refresh();
    }
  }

  async function runSync(button) {
    if (state.syncing) return;
    await busy(button, "Listen werden abgeglichen …", async () => {
      state.syncing = true;
      renderOverview();
      try {
        const result = await api("sync", { method: "POST", body: {}, timeout: 120000 });
        const counts = [];
        if (!result.accepted) {
          if (Number.isFinite(result.added)) counts.push(`${result.added} hinzugefügt`);
          if (Number.isFinite(result.updated)) counts.push(`${result.updated} aktualisiert`);
          if (Number.isFinite(result.removed)) counts.push(`${result.removed} entfernt`);
          if (Number.isFinite(result.conflicts) && result.conflicts > 0) counts.push(`${result.conflicts} Konflikte`);
        }
        const message = [typeof result.message === "string" ? result.message : result.accepted ? "Der Abgleich wurde gestartet. Den Fortschritt findest du in der Übersicht." : result.ok === false ? "Der Abgleich konnte nicht vollständig abgeschlossen werden." : "Der Abgleich ist abgeschlossen.", counts.join(" · ")].filter(Boolean).join(" ");
        $("sync-result").textContent = message;
        $("sync-result").className = `banner${result.ok === false || result.conflicts > 0 ? " warning" : ""}`;
        $("sync-result").hidden = false;
        toast(message);
      } finally {
        state.syncing = false;
        await refresh();
        renderOverview();
      }
    });
  }

  $("auth-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const setup = Boolean(state.session?.setup_required);
    const password = $("auth-password").value;
    if (setup && password !== $("auth-confirm").value) {
      setText("auth-error", "Die beiden Passwörter stimmen noch nicht überein.");
      $("auth-error").hidden = false;
      $("auth-confirm").focus();
      return;
    }
    await busy($("auth-submit"), setup ? "Wird eingerichtet …" : "Wird angemeldet …", async () => {
      try {
        $("auth-error").hidden = true;
        await api(setup ? "setup" : "login", { method: "POST", body: { password } });
        $("auth-form").reset();
        const session = await api("session");
        if (!session.authenticated) throw new Error("Die Anmeldung wurde noch nicht bestätigt. Bitte versuche es erneut.");
        await showApp(session);
      } catch (error) {
        $("auth-password").value = "";
        $("auth-confirm").value = "";
        $("auth-error").textContent = error.message;
        $("auth-error").hidden = false;
        $("auth-password").focus();
      }
    });
  });

  $("bring-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const credentials = { email: $("bring-email").value.trim(), password: $("bring-password").value };
    await busy(form.querySelector('[type="submit"]'), "Verbindung wird geprüft …", () => saveAndTest("bring", credentials, form));
  });

  $("ha-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const credentials = { url: $("ha-url").value.trim().replace(/\/+$/, ""), token: $("ha-token").value.trim() };
    await busy(form.querySelector('[type="submit"]'), "Verbindung wird geprüft …", () => saveAndTest("homeassistant", credentials, form));
  });

  $("ha-auto-connect").addEventListener("click", (event) => busy(event.currentTarget, "Verbindung wird geprüft …", () => saveAndTest("homeassistant", { auto: true })));

  $("alexa-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.alexaSubmitting || state.alexaAuth.running) return;
    resetAlexaAuth();
    const generation = state.alexaAuthGeneration;
    const credentials = { email: $("alexa-email").value.trim(), password: $("alexa-password").value, code: $("alexa-code").value.trim() };
    state.alexaSubmitting = true;
    state.alexaAuth.running = true;
    renderAlexaAuth();
    const request = api("connections/alexa/auth/start", { method: "POST", body: credentials });
    // The request body has been serialized. Keep no password or one-time code
    // in the visible form while the background login is running.
    $("alexa-password").value = "";
    $("alexa-code").value = "";
    credentials.password = "";
    credentials.code = "";
    try {
      const result = await request;
      if (generation !== state.alexaAuthGeneration || !state.authenticated) return;
      if (result.running !== true) throw new Error("Die Amazon-Anmeldung konnte nicht gestartet werden. Bitte versuche es erneut.");
    } catch (error) {
      if (generation === state.alexaAuthGeneration && state.authenticated) {
        state.alexaAuth = { running: false, complete: false, error: error.message };
        toast(error.message, true);
      }
    } finally {
      if (generation === state.alexaAuthGeneration) {
        $("alexa-password").value = "";
        $("alexa-code").value = "";
        state.alexaSubmitting = false;
        renderAlexaAuth();
        if (state.alexaAuth.running) await pollAlexaAuth();
      }
    }
  });

  $("mc-auth-start").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const newTab = window.open("about:blank", "_blank");
    if (newTab) newTab.opener = null;
    await busy(button, "Anmeldung wird geöffnet …", async () => {
      try {
        const result = await api("connections/monsieur_cuisine/auth/start", { method: "POST", body: {} });
        const url = new URL(result.authorization_url);
        if (!["https:", "http:"].includes(url.protocol)) throw new Error("Die Anmeldeadresse ist ungültig. Bitte versuche es erneut.");
        const fallback = $("mc-auth-fallback");
        fallback.href = url.href;
        fallback.hidden = false;
        if (newTab) newTab.location.href = url.href;
        toast("Die Lidl-Anmeldung ist geöffnet. Übernimm anschließend den Code aus dem Browserhelfer.");
      } catch (error) {
        newTab?.close();
        throw error;
      }
    });
  });

  $("mc-auth-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    await busy(form.querySelector('[type="submit"]'), "Anmeldung wird übernommen …", async () => {
      let credentials;
      try { credentials = JSON.parse($("mc-auth-code").value.trim()); }
      catch { throw new Error("Bitte füge den vollständigen JSON-Code aus dem Browserhelfer ein."); }
      if (typeof credentials?.code !== "string" || typeof credentials?.state !== "string" || !credentials.code || !credentials.state) throw new Error("Der Anmeldecode muss die beiden Felder „code“ und „state“ enthalten.");
      try {
        await api("connections/monsieur_cuisine/auth/complete", { method: "POST", body: { code: credentials.code, state: credentials.state } });
        form.reset();
        await testProvider("monsieur_cuisine");
      } finally {
        form.reset();
        await refresh();
      }
    });
  });

  $$('[data-test]').forEach((button) => button.addEventListener("click", () => busy(button, "Wird geprüft …", () => testProvider(button.dataset.test))));
  $$('[data-disconnect]').forEach((button) => button.addEventListener("click", () => busy(button, "Zugang wird entfernt …", async () => {
    const id = button.dataset.disconnect;
    await api(`connections/${id}`, { method: "DELETE", body: {} });
    if (id === "alexa") resetAlexaAuth();
    await refresh();
    toast("Der gespeicherte Zugang wurde entfernt.");
  })));

  $("settings-form").addEventListener("input", () => { state.settingsDirty = true; });
  $("settings-form").addEventListener("change", () => { state.settingsDirty = true; });
  $("settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    await busy($("settings-save"), "Wird gespeichert …", async () => {
      const targets = PROVIDERS.map((meta) => ({ provider: meta.id, list_id: $("list-" + meta.id).value })).filter((target) => target.list_id);
      const enabled = $("sync-enabled").checked;
      if (enabled && targets.length < 2) throw new Error("Wähle mindestens zwei Einkaufslisten aus, um den automatischen Abgleich einzuschalten.");
      if (!targets.every((target) => providerById(target.provider)?.connected)) throw new Error("Prüfe zuerst die ausgewählten Verbindungen oder nimm deren Listen aus dem Abgleich.");
      const settings = { enabled, interval_seconds: Number($("sync-interval").value), targets, propagate_deletions: $("sync-deletions").checked };
      await api("settings", { method: "PUT", body: settings });
      state.settings = settings;
      state.settingsDirty = false;
      await refresh({ includeSettings: true });
      toast(enabled ? "Einstellungen gespeichert. Der automatische Abgleich ist eingeschaltet." : "Einstellungen gespeichert. Der automatische Abgleich ist pausiert.");
    });
  });

  $$('[data-sync-button]').forEach((button) => button.addEventListener("click", () => runSync(button)));
  $("refresh-status").addEventListener("click", () => busy($("refresh-status"), "", () => refresh()));
  $("refresh-events").addEventListener("click", () => busy($("refresh-events"), "Wird aktualisiert …", async () => {
    const result = await api("events");
    state.events = Array.isArray(result) ? result : Array.isArray(result.events) ? result.events : [];
    renderEvents();
    toast("Das Protokoll ist aktuell.");
  }));
  $("logout").addEventListener("click", () => busy($("logout"), "", async () => {
    await api("logout", { method: "POST", body: {} });
    state.status = null;
    state.settings = null;
    state.events = [];
    showAuth({ authenticated: false, setup_required: false, ingress: false });
  }));
  $("toast-close").addEventListener("click", () => { $("toast").hidden = true; clearTimeout(state.toastTimer); });
  window.addEventListener("hashchange", () => { route(); window.scrollTo({ top: 0 }); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.authenticated) refresh({ quiet: true }); });

  createProviderCards();
  route();
  renderProviders();
  renderLists();
  renderEvents();
  api("session", { timeout: 20000 }).then((session) => session.authenticated ? showApp(session) : showAuth(session)).catch((error) => {
    $("boot").replaceChildren(icon("info"), document.createTextNode(error.message));
  });
  setInterval(() => { if (!document.hidden && state.authenticated) refresh({ quiet: true }); }, 10000);
})();
