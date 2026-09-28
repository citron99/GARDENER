const state = {
  token: sessionStorage.getItem("aiGardenToken"),
  profile: null,
  partnerAccount: null,
  partnerCommerceEnabled: true,
  b2bInvoicingEnabled: true,
  gardens: [],
  plants: [],
  selectedGardenId: null,
  selectedPlant: null,
  editingGardenId: null,
  editingPlantId: null,
  pendingPlantAfterGarden: false,
  objectUrls: [],
  dashboardWidgets: {},
};
let refreshPromise = null;

const DASHBOARD_WIDGETS = ["weather", "diagnosis", "result", "care", "reminders", "history"];

const byId = (id) => document.getElementById(id);
const t = (value) => window.I18N ? window.I18N.t(value) : value;
const uiLocale = () => ({ ru: "ru-RU", lv: "lv-LV", en: "en-GB" }[window.I18N?.getLanguage()] || "ru-RU");
const money = (cents, currency = "eur") => new Intl.NumberFormat(uiLocale(), {
  style: "currency", currency: String(currency).toUpperCase(),
}).format(Number(cents || 0) / 100);
const formatProductPrice = (product) => money(product.price_cents, product.currency);
const authView = byId("authView");
const appView = byId("appView");
const authError = byId("authError");
const toast = byId("toast");

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = t(text);
  return node;
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.remove("hidden");
  window.setTimeout(() => toast.classList.add("hidden"), 3500);
}

function resetObjectUrls() {
  for (const url of state.objectUrls) URL.revokeObjectURL(url);
  state.objectUrls = [];
}

function dashboardStorageKey() {
  return `aiGardenDashboardWidgets:${state.profile?.id || "guest"}`;
}

function loadDashboardPreferences() {
  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem(dashboardStorageKey()) || "{}") || {};
  } catch (_) { /* invalid or unavailable local storage falls back to defaults */ }
  state.dashboardWidgets = Object.fromEntries(
    DASHBOARD_WIDGETS.map((name) => [name, saved[name] !== false]),
  );
  applyDashboardPreferences();
}

function saveDashboardPreferences() {
  try {
    localStorage.setItem(dashboardStorageKey(), JSON.stringify(state.dashboardWidgets));
  } catch (_) { /* visibility still applies for the current session */ }
}

function applyDashboardPreferences() {
  document.querySelectorAll("[data-dashboard-widget]").forEach((panel) => {
    panel.classList.toggle("hidden", state.dashboardWidgets[panel.dataset.dashboardWidget] === false);
  });
  const diagnosisVisible = state.dashboardWidgets.diagnosis !== false;
  const resultVisible = state.dashboardWidgets.result !== false;
  const grid = byId("diagnosisGrid");
  grid.classList.toggle("hidden", !diagnosisVisible && !resultVisible);
  grid.classList.toggle("single-widget", diagnosisVisible !== resultVisible);
}

function openDashboardSettings() {
  const modal = byId("dashboardSettingsModal");
  byId("dashboardSettingsForm").querySelectorAll("input[type='checkbox']").forEach((checkbox) => {
    checkbox.checked = state.dashboardWidgets[checkbox.value] !== false;
  });
  modal.classList.remove("hidden");
  byId("closeDashboardSettings").focus();
}

function closeDashboardSettings() {
  byId("dashboardSettingsModal").classList.add("hidden");
  byId("dashboardSettingsButton").focus();
}

const wait = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));

async function waitForDiagnosisJob(initialJob, statusNode) {
  let job = initialJob;
  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (job.status === "succeeded" && job.diagnosis_id) {
      return api(`/api/v1/diagnoses/${job.diagnosis_id}`);
    }
    if (job.status === "failed") {
      throw new Error(job.error_message || "Не удалось завершить фоновый анализ");
    }
    statusNode.textContent = job.status === "running"
      ? "AI анализирует фотографии…"
      : "Диагностика ожидает свободный worker…";
    await wait(2000);
    job = await api(`/api/v1/diagnosis-jobs/${job.id}`);
  }
  throw new Error("Диагностика выполняется дольше ожидаемого. Результат останется в истории.");
}

async function refreshAccessToken() {
  if (!refreshPromise) {
    refreshPromise = fetch("/api/v1/auth/refresh", {
      method: "POST",
      headers: { "Accept-Language": window.I18N?.getLanguage() || "ru" },
      credentials: "same-origin",
    }).then(async (response) => {
      if (!response.ok) throw new Error("Сессия истекла");
      const pair = await response.json();
      setSession(pair.access_token);
      return pair.access_token;
    }).finally(() => { refreshPromise = null; });
  }
  return refreshPromise;
}

async function api(path, options = {}, allowRefresh = true) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  headers.set("Accept-Language", window.I18N?.getLanguage() || "ru");
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  if (
    response.status === 401
    && allowRefresh
    && state.token
    && !path.startsWith("/api/v1/auth/")
  ) {
    try {
      await refreshAccessToken();
      return api(path, options, false);
    } catch (_) {
      clearSession();
    }
  }
  if (!response.ok) {
    let detail = `Ошибка ${response.status}`;
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch (_) { /* response is not JSON */ }
    const error = new Error(detail);
    error.status = response.status;
    error.requestId = response.headers.get("x-request-id");
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

async function downloadAuthenticatedFile(path, fallbackName, allowRefresh = true) {
  const headers = new Headers();
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  headers.set("Accept-Language", window.I18N?.getLanguage() || "ru");
  const response = await fetch(path, { headers, credentials: "same-origin" });
  if (response.status === 401 && allowRefresh && state.token) {
    await refreshAccessToken();
    return downloadAuthenticatedFile(path, fallbackName, false);
  }
  if (!response.ok) {
    let detail = `Ошибка ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") || "";
  const matched = disposition.match(/filename=\x22([^\x22]+)\x22/i);
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = matched?.[1] || fallbackName;
  link.click();
  URL.revokeObjectURL(url);
}

function setSession(token) {
  state.token = token;
  sessionStorage.setItem("aiGardenToken", token);
}

function clearSession() {
  state.token = null;
  state.profile = null;
  state.partnerAccount = null;
  sessionStorage.removeItem("aiGardenToken");
  resetObjectUrls();
  appView.classList.add("hidden");
  authView.classList.remove("hidden");
  byId("logoutButton").classList.add("hidden");
  byId("adminButton").classList.add("hidden");
  byId("partnerButton").classList.add("hidden");
  byId("billingButton").classList.add("hidden");
  byId("telegramButton").classList.add("hidden");
  byId("dashboardSettingsButton").classList.add("hidden");
  byId("dashboardSettingsModal").classList.add("hidden");
}

function switchAuth(mode) {
  const login = mode === "login";
  byId("passwordResetRequestForm").classList.add("hidden");
  byId("passwordResetConfirmForm").classList.add("hidden");
  document.querySelector(".segmented").classList.remove("hidden");
  byId("loginTab").classList.toggle("active", login);
  byId("registerTab").classList.toggle("active", !login);
  byId("loginTab").setAttribute("aria-selected", String(login));
  byId("registerTab").setAttribute("aria-selected", String(!login));
  byId("loginTab").tabIndex = login ? 0 : -1;
  byId("registerTab").tabIndex = login ? -1 : 0;
  byId("loginForm").classList.toggle("hidden", !login);
  byId("registerForm").classList.toggle("hidden", login);
  authError.classList.add("hidden");
}

function formJson(form) {
  return Object.fromEntries(new FormData(form).entries());
}

async function authenticate(path, payload) {
  authError.classList.add("hidden");
  try {
    const result = await api(path, { method: "POST", body: JSON.stringify(payload) });
    if (result.email_verification_required) {
      await fetch("/api/v1/auth/logout", { method: "POST", credentials: "same-origin" });
      clearSession();
      authError.textContent = "Проверьте почту и подтвердите email перед входом.";
      authError.className = "form-message";
      return;
    }
    setSession(result.access_token);
    await enterApplication();
  } catch (error) {
    authError.textContent = error.message;
    authError.classList.remove("hidden");
  }
}

async function enterApplication() {
  try {
    state.profile = await api("/api/v1/users/me");
    window.I18N?.setLanguage(state.profile.language);
    byId("languageSelect").value = state.profile.language;
    state.partnerAccount = state.partnerCommerceEnabled
      ? await api("/api/v1/partner/me")
      : null;
    authView.classList.add("hidden");
    appView.classList.remove("hidden");
    byId("logoutButton").classList.remove("hidden");
    byId("adminButton").classList.toggle("hidden", !state.profile.is_admin);
    byId("partnerButton").classList.toggle("hidden", !state.partnerAccount);
    byId("billingButton").classList.remove("hidden");
    byId("telegramButton").classList.remove("hidden");
    byId("dashboardSettingsButton").classList.remove("hidden");
    byId("profileName").textContent = state.profile.name;
    byId("profileMeta").textContent = [state.profile.region, state.profile.email].filter(Boolean).join(" · ");
    loadDashboardPreferences();
    await loadWorkspaceData();
    const billingResult = new URLSearchParams(window.location.search).get("billing");
    if (billingResult) {
      await showBillingView();
      window.history.replaceState({}, "", window.location.pathname);
      showToast(billingResult === "success" ? "Платёж обрабатывается; статус обновится после подтверждения Stripe" : "Оплата отменена");
    }
  } catch (error) {
    if (error.status === 401) clearSession();
    else showToast(error.message);
  }
}

async function loadWorkspaceData() {
  [state.gardens, state.plants] = await Promise.all([api("/api/v1/gardens"), api("/api/v1/plants")]);
  if (!state.selectedGardenId && state.gardens.length) state.selectedGardenId = state.gardens[0].id;
  renderGardens();
  renderPlants();
}

function renderGardens() {
  const list = byId("gardenList");
  const actions = byId("gardenActions");
  list.replaceChildren();
  if (!state.gardens.length) {
    state.selectedGardenId = null;
    actions.classList.add("hidden");
    list.append(element("p", "muted", "Добавьте первый сад"));
    return;
  }
  actions.classList.toggle("hidden", !state.gardens.some((garden) => garden.id === state.selectedGardenId));
  state.gardens.forEach((garden) => {
    const button = element("button", `nav-item${garden.id === state.selectedGardenId ? " active" : ""}`);
    button.type = "button";
    button.append(element("strong", "", garden.name), element("small", "", garden.location || garden.kind));
    button.addEventListener("click", () => {
      state.selectedGardenId = garden.id;
      state.selectedPlant = null;
      renderGardens();
      renderPlants();
      showEmptyState();
    });
    list.append(button);
  });
}

function renderPlants() {
  const list = byId("plantList");
  list.replaceChildren();
  const plants = state.plants.filter((plant) => plant.garden_id === state.selectedGardenId);
  if (!plants.length) {
    list.append(element("p", "muted", state.selectedGardenId ? "В этом саду пока нет растений" : "Сначала добавьте сад"));
    return;
  }
  plants.forEach((plant) => {
    const button = element("button", `nav-item${state.selectedPlant?.id === plant.id ? " active" : ""}`);
    button.type = "button";
    button.append(element("strong", "", plant.name), element("small", "", plant.species || plant.growing_place));
    button.addEventListener("click", () => selectPlant(plant));
    list.append(button);
  });
}

function showEmptyState() {
  byId("adminWorkspace").classList.add("hidden");
  byId("partnerWorkspace").classList.add("hidden");
  byId("billingWorkspace").classList.add("hidden");
  byId("telegramWorkspace").classList.add("hidden");
  byId("calendarWorkspace").classList.add("hidden");
  byId("emptyState").classList.remove("hidden");
  byId("plantWorkspace").classList.add("hidden");
}

function openGardenForm(garden = null) {
  const form = byId("gardenForm");
  form.reset();
  state.editingGardenId = garden?.id || null;
  if (garden) {
    form.elements.name.value = garden.name;
    form.elements.kind.value = garden.kind;
    form.elements.location.value = garden.location || "";
  }
  form.querySelector("button[type='submit']").textContent = garden ? "Обновить" : "Сохранить";
  form.classList.remove("hidden");
}

function openPlantForm(plant = null) {
  const form = byId("plantForm");
  form.reset();
  state.editingPlantId = plant?.id || null;
  if (plant) {
    form.elements.name.value = plant.name;
    form.elements.species.value = plant.species || "";
    form.elements.taxon_id.value = plant.taxon_id || "";
    form.elements.growing_place.value = plant.growing_place;
  }
  form.querySelector("button[type='submit']").textContent = plant ? "Обновить" : "Сохранить";
  form.classList.remove("hidden");
}

async function selectPlant(plant) {
  state.selectedPlant = plant;
  renderPlants();
  byId("calendarWorkspace").classList.add("hidden");
  byId("adminWorkspace").classList.add("hidden");
  byId("partnerWorkspace").classList.add("hidden");
  byId("billingWorkspace").classList.add("hidden");
  byId("telegramWorkspace").classList.add("hidden");
  byId("emptyState").classList.add("hidden");
  byId("plantWorkspace").classList.remove("hidden");
  byId("plantTitle").textContent = plant.name;
  byId("plantDescription").textContent = [plant.species, plant.region, plant.growing_place].filter(Boolean).join(" · ");
  if (!byId("careOccurredAt").value) byId("careOccurredAt").value = localDateTimeValue(new Date());
  if (!byId("reminderDueAt").value) byId("reminderDueAt").value = defaultReminderDate();
  await Promise.all([loadHistory(), loadCareEvents(), loadReminders(), loadWeather()]);
}

function localDateTimeValue(date) {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function localDateValue(date) {
  return localDateTimeValue(date).slice(0, 10);
}

function ensureCalendarRange() {
  if (byId("calendarStart").value && byId("calendarEnd").value) return;
  const start = new Date();
  const end = new Date();
  end.setDate(end.getDate() + 30);
  byId("calendarStart").value = localDateValue(start);
  byId("calendarEnd").value = localDateValue(end);
}

function calendarTypeLabel(item) {
  if (item.item_type === "weather_warning") return "Погодное предупреждение";
  if (item.item_type === "seasonal_task") return t("Сезонная работа");
  if (item.item_type === "reminder") return reminderKindLabel(item.event_type);
  return careEventLabel(item.event_type);
}

function renderCalendar(items) {
  const list = byId("calendarList");
  list.replaceChildren();
  if (!items.length) {
    list.append(element("p", "muted", "В выбранном периоде событий нет."));
    return;
  }
  const groups = new Map();
  items.forEach((item) => {
    const date = new Date(item.starts_at);
    const key = localDateValue(date);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  });
  groups.forEach((dayItems, key) => {
    const day = element("section", "calendar-day");
    const date = new Date(`${key}T12:00:00`);
    day.append(element("time", "calendar-date", date.toLocaleDateString(uiLocale(), {
      weekday: "short", day: "2-digit", month: "long", year: "numeric",
    })));
    const itemList = element("div", "calendar-day-items");
    dayItems.forEach((item) => {
      const card = element("article", `calendar-item ${item.item_type}${item.severity ? ` ${item.severity}` : ""}`);
      const content = element("div");
      content.append(element("div", "calendar-item-title", item.title));
      const place = item.plant_name || item.garden_name;
      const completed = item.completed && item.item_type === "reminder" ? " · выполнено" : "";
      content.append(element("div", "calendar-item-meta", `${calendarTypeLabel(item)}${place ? ` · ${place}` : ""}${completed}`));
      if (item.description) content.append(element("div", "calendar-item-meta", item.description));
      card.append(content);
      itemList.append(card);
    });
    day.append(itemList);
    list.append(day);
  });
}

async function loadCalendar() {
  ensureCalendarRange();
  const status = byId("calendarStatus");
  const start = new Date(`${byId("calendarStart").value}T00:00:00`);
  const end = new Date(`${byId("calendarEnd").value}T00:00:00`);
  end.setDate(end.getDate() + 1);
  status.textContent = "Загружаем календарь…";
  status.className = "muted";
  const params = new URLSearchParams({ start: start.toISOString(), end: end.toISOString(), include_weather: "true" });
  try {
    const items = await api(`/api/v1/calendar?${params}`);
    renderCalendar(items);
    status.textContent = "Уход, сезонные работы, напоминания и погодные предупреждения";
  } catch (error) {
    if (error.status === 503) {
      params.set("include_weather", "false");
      try {
        const items = await api(`/api/v1/calendar?${params}`);
        renderCalendar(items);
        status.textContent = "Погода временно недоступна; показаны уход и напоминания.";
        status.className = "form-message";
        return;
      } catch (fallbackError) {
        error = fallbackError;
      }
    }
    status.textContent = error.message;
    status.className = "form-message error";
  }
}

async function showCalendarView() {
  state.selectedPlant = null;
  renderPlants();
  byId("emptyState").classList.add("hidden");
  byId("plantWorkspace").classList.add("hidden");
  byId("adminWorkspace").classList.add("hidden");
  byId("partnerWorkspace").classList.add("hidden");
  byId("billingWorkspace").classList.add("hidden");
  byId("telegramWorkspace").classList.add("hidden");
  byId("calendarWorkspace").classList.remove("hidden");
  await loadCalendar();
}

function appendCell(row, value) {
  row.append(element("td", "", value === null || value === undefined ? "—" : t(String(value))));
}

function renderAdminOverview(overview) {
  const metrics = byId("adminMetrics");
  metrics.replaceChildren();
  const items = [
    ["Пользователи", overview.users],
    ["Сады", overview.gardens],
    ["Растения", overview.plants],
    ["Диагностики", overview.diagnoses],
    ["Задания в очереди", overview.jobs_queued],
    ["Задания выполняются", overview.jobs_running],
    ["Ошибки заданий", overview.jobs_failed],
    ["Успешные AI-запросы", overview.ai_requests_successful],
    ["Ошибки AI", overview.ai_requests_failed],
    ["Партнёры", overview.partners],
    ["Активные товары", overview.active_products],
    ["Лиды партнёров", overview.product_leads],
    ["Конверсии на проверке", overview.pending_partner_conversions],
    ["Подтверждённые конверсии", overview.confirmed_partner_conversions],
    ["Счета партнёрам", money(overview.partner_invoices_issued_cents)],
    ["Входные токены", overview.input_tokens],
    ["Выходные токены", overview.output_tokens],
    ["Оценочная стоимость", Number(overview.estimated_cost).toFixed(4)],
    ["Оценка пользователей", overview.average_feedback_rating?.toFixed(2) || "—"],
  ];
  items.forEach(([label, value]) => {
    const card = element("article", "card admin-metric");
    card.append(element("strong", "", value), element("span", "", label));
    metrics.append(card);
  });
}

function renderAdminUsers(users) {
  const body = byId("adminUsers");
  body.replaceChildren();
  users.forEach((user) => {
    const row = element("tr");
    appendCell(row, `${user.email}${user.is_admin ? " · admin" : ""}`);
    appendCell(row, user.name);
    appendCell(row, user.region);
    appendCell(row, user.gardens_count);
    appendCell(row, user.plants_count);
    appendCell(row, user.diagnoses_count);
    body.append(row);
  });
}

function renderAdminJobs(jobs) {
  const body = byId("adminJobs");
  body.replaceChildren();
  jobs.forEach((job) => {
    const row = element("tr");
    appendCell(row, job.id.slice(0, 8));
    const statusCell = element("td");
    statusCell.append(element("span", `status-pill ${job.status}`, job.status));
    row.append(statusCell);
    appendCell(row, job.attempts);
    appendCell(row, job.user_id);
    appendCell(row, job.error_type);
    appendCell(row, new Date(job.created_at).toLocaleString(uiLocale()));
    body.append(row);
  });
}

function renderAdminRequests(requests) {
  const body = byId("adminRequests");
  body.replaceChildren();
  requests.forEach((request) => {
    const row = element("tr");
    appendCell(row, request.request_id);
    appendCell(row, request.model_name);
    const statusCell = element("td");
    statusCell.append(element("span", `status-pill ${request.success ? "" : "failed"}`, request.success ? "success" : request.error_type || "failed"));
    row.append(statusCell);
    appendCell(row, `${request.input_tokens} / ${request.output_tokens}`);
    appendCell(row, `${request.response_ms} ms`);
    appendCell(row, Number(request.estimated_cost).toFixed(4));
    body.append(row);
  });
}

function renderAdminCatalog(partners, products) {
  const partnerSelect = byId("adminProductPartner");
  const partnerList = byId("adminPartnersList");
  const productBody = byId("adminProducts");
  partnerSelect.replaceChildren();
  partnerList.replaceChildren();
  productBody.replaceChildren();

  const activePartners = partners.filter((partner) => partner.active);
  activePartners.forEach((partner) => {
    const option = element("option", "", partner.name);
    option.value = partner.id;
    partnerSelect.append(option);
  });
  const productSubmit = byId("adminProductForm").querySelector("button[type='submit']");
  productSubmit.disabled = activePartners.length === 0;

  if (!partners.length) partnerList.append(element("span", "muted", "Партнёров пока нет"));
  partners.forEach((partner) => {
    const chip = element("span", `partner-chip${partner.active ? "" : " inactive"}`);
    chip.append(element("span", "", `${partner.name}${partner.active ? "" : ` · ${t("отключён")}`}`));
    if (partner.active) {
      const deactivate = element("button", "button small ghost", "Отключить");
      deactivate.type = "button";
      deactivate.addEventListener("click", async () => {
        deactivate.disabled = true;
        try {
          await api(`/api/v1/admin/partners/${partner.id}`, { method: "DELETE" });
          await loadAdmin();
        } catch (error) { showToast(error.message); deactivate.disabled = false; }
      });
      chip.append(deactivate);
    }
    partnerList.append(chip);
  });

  products.forEach((product) => {
    const row = element("tr");
    appendCell(row, `${product.name}${product.sku ? ` · ${product.sku}` : ""}${product.price_cents === null ? "" : ` · ${formatProductPrice(product)}`}`);
    appendCell(row, product.partner_name);
    appendCell(row, product.category);
    appendCell(row, product.regions.length ? product.regions.join(", ") : "Все");
    const statusCell = element("td");
    const productStatus = product.active ? product.moderation_status : "отключён";
    statusCell.append(element("span", `status-pill ${productStatus === "approved" ? "" : "failed"}`, productStatus));
    row.append(statusCell);
    const actionCell = element("td");
    if (product.active && product.moderation_status !== "approved") {
      [["Одобрить", "approved"], ["Отклонить", "rejected"]].forEach(([label, moderationStatus]) => {
        const moderate = element("button", `button small ${moderationStatus === "rejected" ? "ghost" : ""}`, label);
        moderate.type = "button";
        moderate.addEventListener("click", async () => {
          moderate.disabled = true;
          try {
            await api(`/api/v1/admin/products/${product.id}/moderation`, {
              method: "PATCH", body: JSON.stringify({ status: moderationStatus, note: null }),
            });
            await loadAdmin();
          } catch (error) { showToast(error.message); moderate.disabled = false; }
        });
        actionCell.append(moderate);
      });
    }
    if (product.active) {
      const deactivate = element("button", "button small ghost", "Отключить");
      deactivate.type = "button";
      deactivate.addEventListener("click", async () => {
        deactivate.disabled = true;
        try {
          await api(`/api/v1/admin/products/${product.id}`, { method: "DELETE" });
          await loadAdmin();
        } catch (error) { showToast(error.message); deactivate.disabled = false; }
      });
      actionCell.append(deactivate);
    }
    row.append(actionCell);
    productBody.append(row);
  });
}

function renderAdminRegistry(registry, products, rules) {
  const registryBody = byId("adminRegistry");
  const rulesBody = byId("adminRules");
  const productSelect = byId("adminRuleProduct");
  const registrySelect = byId("adminRuleRegistry");
  registryBody.replaceChildren();
  rulesBody.replaceChildren();
  productSelect.replaceChildren();
  registrySelect.replaceChildren(element("option", "", "Без записи реестра"));
  registrySelect.firstElementChild.value = "";

  registry.forEach((item) => {
    const row = element("tr");
    appendCell(row, item.product_name);
    appendCell(row, item.jurisdiction);
    appendCell(row, item.registration_number);
    appendCell(row, item.valid_until || "—");
    appendCell(row, item.status);
    const source = element("td");
    const link = element("a", "", item.source_name);
    link.href = item.source_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    source.append(link);
    row.append(source);
    registryBody.append(row);

    if (item.status === "active") {
      const option = element("option", "", `${item.product_name} · ${item.jurisdiction} ${item.registration_number}`);
      option.value = item.id;
      registrySelect.append(option);
    }
  });

  products.filter((product) => product.active && product.moderation_status === "approved").forEach((product) => {
    const option = element("option", "", `${product.name} · ${product.partner_name}`);
    option.value = product.id;
    productSelect.append(option);
  });
  byId("adminRuleForm").querySelector("button[type='submit']").disabled = !productSelect.options.length;

  rules.forEach((rule) => {
    const row = element("tr");
    appendCell(row, rule.product_id);
    appendCell(row, rule.crop_name);
    appendCell(row, rule.plant_taxon_id || "—");
    appendCell(row, rule.problem_name);
    appendCell(row, rule.problem_code || "—");
    appendCell(row, rule.region_code);
    appendCell(row, rule.country_code || "—");
    appendCell(row, rule.registry_entry_id || "—");
    appendCell(row, rule.expert_verified ? "Да" : "Нет");
    rulesBody.append(row);
  });
}

function renderAdminLeads(leads) {
  const body = byId("adminLeads");
  body.replaceChildren();
  leads.forEach((lead) => {
    const row = element("tr");
    appendCell(row, lead.product_name);
    appendCell(row, lead.partner_name);
    appendCell(row, lead.source === "diagnosis" ? "Диагностика" : "Каталог");
    appendCell(row, lead.status);
    appendCell(row, lead.conversion_value_cents === null ? "—" : money(lead.conversion_value_cents));
    appendCell(row, new Date(lead.created_at).toLocaleString(uiLocale()));
    const actions = element("td");
    if (lead.status === "conversion_claimed") {
      [["Подтвердить", true], ["Отклонить", false]].forEach(([label, approved]) => {
        const button = element("button", `button small ${approved ? "" : "ghost"}`, label);
        button.type = "button";
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            await api(`/api/v1/admin/leads/${lead.id}/conversion`, {
              method: "PATCH", body: JSON.stringify({ approved, note: null }),
            });
            await loadAdmin();
          } catch (error) { showToast(error.message); button.disabled = false; }
        });
        actions.append(button);
      });
    }
    row.append(actions);
    body.append(row);
  });
}

function renderAdminInvoices(invoices) {
  const body = byId("adminInvoices");
  body.replaceChildren();
  invoices.forEach((invoice) => {
    const row = element("tr");
    appendCell(row, invoice.invoice_number || "—");
    appendCell(row, invoice.partner_id);
    appendCell(row, `${invoice.period_start} — ${invoice.period_end}`);
    appendCell(row, invoice.confirmed_leads_count);
    appendCell(row, money(invoice.total_cents, invoice.currency));
    appendCell(row, invoice.status);
    const actions = element("td");
    if (invoice.pdf_sha256) {
      const download = element("button", "button small ghost", "PDF");
      download.type = "button";
      download.addEventListener("click", async () => {
        download.disabled = true;
        try {
          await downloadAuthenticatedFile(
            `/api/v1/admin/partner-invoices/${invoice.id}/pdf`,
            `${invoice.invoice_number || "invoice-" + invoice.id}.pdf`,
          );
        } catch (error) { showToast(error.message); }
        finally { download.disabled = false; }
      });
      actions.append(download);
    }
    if (invoice.cancellation_pdf_sha256) {
      const cancellation = element("button", "button small ghost", t("Аннулирование PDF"));
      cancellation.type = "button";
      cancellation.addEventListener("click", async () => {
        cancellation.disabled = true;
        try {
          await downloadAuthenticatedFile(
            `/api/v1/admin/partner-invoices/${invoice.id}/cancellation.pdf`,
            `${invoice.cancellation_number || "invoice-cancellation-" + invoice.id}.pdf`,
          );
        } catch (error) { showToast(error.message); }
        finally { cancellation.disabled = false; }
      });
      actions.append(cancellation);
    }
    if (invoice.pdf_sha256 && invoice.status !== "void") {
      const send = element("button", "button small ghost", "Email");
      send.type = "button";
      send.addEventListener("click", async () => {
        if (!window.confirm(t("Отправить PDF на зафиксированный в счёте email?"))) return;
        send.disabled = true;
        try {
          const delivery = await api(`/api/v1/admin/partner-invoices/${invoice.id}/send`, {
            method: "POST", body: JSON.stringify({ confirm_resend: true }),
          });
          showToast(t(delivery.status === "sent" ? "Счёт отправлен" : "Отправка записана в журнал разработки"));
        } catch (error) { showToast(error.message); }
        finally { send.disabled = false; }
      });
      actions.append(send);
    }
    if (invoice.status === "issued") {
      [["Оплачен", "paid"], ["Аннулировать", "void"]].forEach(([label, status]) => {
        const button = element("button", `button small ${status === "void" ? "ghost" : ""}`, label);
        button.type = "button";
        button.addEventListener("click", async () => {
          const reason = status === "void"
            ? window.prompt(t("Укажите причину аннулирования счёта"), "")
            : null;
          const externalId = status === "paid"
            ? window.prompt(t("Введите внешний ID ручного платежа"), "")
            : null;
          const paymentNote = status === "paid"
            ? window.prompt(t("Введите основание ручной отметки оплаты"), "")
            : null;
          if (status === "void" && (!reason || reason.trim().length < 3)) {
            showToast(t("Причина аннулирования обязательна"));
            return;
          }
          if (status === "paid" && (
            !externalId || externalId.trim().length < 3
            || !paymentNote || paymentNote.trim().length < 3
          )) {
            showToast(t("Для ручной оплаты нужны внешний ID и основание"));
            return;
          }
          button.disabled = true;
          try {
            const path = status === "paid"
              ? `/api/v1/admin/partner-invoices/${invoice.id}/manual-payment`
              : `/api/v1/admin/partner-invoices/${invoice.id}`;
            const payload = status === "paid" ? {
              external_id: externalId.trim(),
              payment_method: "bank_transfer",
              booking_date: new Date().toISOString().slice(0, 10),
              note: paymentNote.trim(),
            } : { status, reason: reason.trim() };
            await api(path, {
              method: status === "paid" ? "POST" : "PATCH",
              body: JSON.stringify(payload),
            });
            await loadAdmin();
          } catch (error) { showToast(error.message); button.disabled = false; }
        });
        actions.append(button);
      });
    }
    row.append(actions);
    body.append(row);
  });
}

function renderAdminPayments(payments) {
  const body = byId("adminPayments");
  body.replaceChildren();
  payments.forEach((payment) => {
    const row = element("tr");
    appendCell(row, `${payment.source} · ${payment.external_id}`);
    appendCell(row, payment.booking_date);
    appendCell(row, money(payment.amount_cents, payment.currency));
    appendCell(row, payment.reference);
    appendCell(row, payment.status);
    const actions = element("td");
    if (payment.status === "unmatched") {
      const match = element("button", "button small", "Сопоставить");
      match.type = "button";
      match.addEventListener("click", async () => {
        const invoiceId = Number(window.prompt(t("ID выставленного счёта")));
        if (!Number.isInteger(invoiceId) || invoiceId < 1) return;
        try {
          await api(`/api/v1/admin/partner-payments/${payment.id}`, {
            method: "PATCH", body: JSON.stringify({ action: "match", invoice_id: invoiceId, note: null }),
          });
          await loadAdmin();
        } catch (error) { showToast(error.message); }
      });
      const reject = element("button", "button small ghost", "Отклонить");
      reject.type = "button";
      reject.addEventListener("click", async () => {
        if (!window.confirm(t("Отклонить банковскую операцию без сопоставления?"))) return;
        try {
          await api(`/api/v1/admin/partner-payments/${payment.id}`, {
            method: "PATCH", body: JSON.stringify({ action: "reject", invoice_id: null, note: null }),
          });
          await loadAdmin();
        } catch (error) { showToast(error.message); }
      });
      actions.append(match, reject);
    }
    row.append(actions);
    body.append(row);
  });
}

function renderAdminMembers(partners, members) {
  const select = byId("adminMemberPartner");
  const list = byId("adminPartnerMembers");
  select.replaceChildren();
  list.replaceChildren();
  partners.filter((partner) => partner.active).forEach((partner) => {
    const option = element("option", "", partner.name);
    option.value = partner.id;
    select.append(option);
  });
  byId("adminPartnerMemberForm").querySelector("button[type='submit']").disabled = select.options.length === 0;
  if (!members.length) list.append(element("span", "muted", "Сотрудники ещё не назначены"));
  members.forEach((member) => {
    const chip = element("span", `partner-chip${member.active ? "" : " inactive"}`);
    chip.append(element("span", "", `${member.email} · ${member.partner_name} · ${member.role}${member.active ? "" : ` · ${t("отключён")}`}`));
    if (member.active) {
      const deactivate = element("button", "button small ghost", "Отключить");
      deactivate.type = "button";
      deactivate.addEventListener("click", async () => {
        deactivate.disabled = true;
        try {
          await api(`/api/v1/admin/partner-members/${member.id}`, { method: "DELETE" });
          await loadAdmin();
        } catch (error) { showToast(error.message); deactivate.disabled = false; }
      });
      chip.append(deactivate);
    }
    list.append(chip);
  });
}

async function loadAdmin() {
  if (!state.profile?.is_admin) return;
  const status = byId("adminStatus");
  status.textContent = "Загружаем метрики…";
  status.className = "muted";
  try {
    const [overview, users, jobs, requests] = await Promise.all([
      api("/api/v1/admin/overview"),
      api("/api/v1/admin/users?limit=50"),
      api("/api/v1/admin/diagnosis-jobs?limit=50"),
      api("/api/v1/admin/ai-requests?limit=50"),
    ]);
    let partners = [];
    let products = [];
    let leads = [];
    let members = [];
    let invoices = [];
    let payments = [];
    let registry = [];
    let rules = [];
    if (state.partnerCommerceEnabled) {
      [partners, products, leads, members, registry, rules] = await Promise.all([
        api("/api/v1/admin/partners"),
        api("/api/v1/admin/products"),
        api("/api/v1/admin/leads?limit=100"),
        api("/api/v1/admin/partner-members"),
        api("/api/v1/admin/regulated-products?limit=500"),
        api("/api/v1/admin/product-recommendation-rules?limit=200"),
      ]);
      if (state.b2bInvoicingEnabled) {
        [invoices, payments] = await Promise.all([
          api("/api/v1/admin/partner-invoices"),
          api("/api/v1/admin/partner-payments?limit=100"),
        ]);
      }
    }
    renderAdminOverview(overview);
    renderAdminUsers(users);
    renderAdminJobs(jobs);
    renderAdminRequests(requests);
    renderAdminCatalog(partners, products);
    renderAdminLeads(leads);
    renderAdminMembers(partners, members);
    renderAdminInvoices(invoices);
    renderAdminPayments(payments);
    renderAdminRegistry(registry, products, rules);
    const invoicePartner = byId("adminInvoicePartner");
    invoicePartner.replaceChildren();
    partners.filter((partner) => partner.active).forEach((partner) => {
      const option = element("option", "", partner.name);
      option.value = partner.id;
      invoicePartner.append(option);
    });
    byId("adminInvoiceForm").querySelector("button[type='submit']").disabled = !invoicePartner.options.length;
    status.textContent = `${t("Последнее обновление")}: ${new Date().toLocaleTimeString(uiLocale())}`;
  } catch (error) {
    status.textContent = error.message;
    status.className = "form-message error";
  }
}

async function showAdminView() {
  if (!state.profile?.is_admin) return;
  state.selectedPlant = null;
  renderPlants();
  byId("emptyState").classList.add("hidden");
  byId("plantWorkspace").classList.add("hidden");
  byId("calendarWorkspace").classList.add("hidden");
  byId("partnerWorkspace").classList.add("hidden");
  byId("billingWorkspace").classList.add("hidden");
  byId("telegramWorkspace").classList.add("hidden");
  byId("adminWorkspace").classList.remove("hidden");
  await loadAdmin();
}

function renderPartnerOverview(overview) {
  byId("partnerTitle").textContent = overview.partner_name;
  const metrics = byId("partnerMetrics");
  metrics.replaceChildren();
  [
    ["Активные товары", overview.active_products],
    ["Всего товаров", overview.total_products],
    ["Все переходы", overview.total_leads],
    ["Переходы за 30 дней", overview.leads_last_30_days],
    ["Конверсии на проверке", overview.pending_conversion_claims],
    ["Подтверждённые конверсии", overview.confirmed_conversions],
    ["К следующему счёту", money(overview.uninvoiced_amount_cents, overview.billing_currency)],
  ].forEach(([label, value]) => {
    const card = element("article", "card admin-metric");
    card.append(element("strong", "", value), element("span", "", label));
    metrics.append(card);
  });
}

function renderPartnerProducts(products) {
  const body = byId("partnerProducts");
  body.replaceChildren();
  products.forEach((product) => {
    const row = element("tr");
    appendCell(row, `${product.name}${product.sku ? ` · ${product.sku}` : ""}${product.price_cents === null ? "" : ` · ${formatProductPrice(product)}`}`);
    appendCell(row, product.category);
    appendCell(row, product.regions.length ? product.regions.join(", ") : "Все");
    const status = element("td");
    status.append(element("span", `status-pill ${product.active ? "" : "failed"}`, product.active ? "активен" : "отключён"));
    row.append(status);
    const actions = element("td");
    if (state.partnerAccount.can_manage_products) {
      const toggle = element("button", "button small ghost", product.active ? "Отключить" : "Включить");
      toggle.type = "button";
      toggle.addEventListener("click", async () => {
        toggle.disabled = true;
        try {
          await api(`/api/v1/partner/products/${product.id}`, {
            method: "PATCH",
            body: JSON.stringify({ active: !product.active }),
          });
          await loadPartner();
        } catch (error) { showToast(error.message); toggle.disabled = false; }
      });
      actions.append(toggle);
    }
    row.append(actions);
    body.append(row);
  });
}

function renderPartnerLeads(leads) {
  const body = byId("partnerLeads");
  body.replaceChildren();
  leads.forEach((lead) => {
    const row = element("tr");
    appendCell(row, lead.product_name);
    appendCell(row, lead.source === "diagnosis" ? "Диагностика" : "Каталог");
    appendCell(row, lead.status);
    appendCell(row, lead.conversion_value_cents === null ? "—" : money(lead.conversion_value_cents));
    appendCell(row, new Date(lead.created_at).toLocaleString(uiLocale()));
    const actions = element("td");
    if (state.partnerAccount.can_manage_products && ["clicked", "rejected"].includes(lead.status)) {
      const claim = element("button", "button small", "Заявить продажу");
      claim.type = "button";
      claim.addEventListener("click", async () => {
        const reference = window.prompt(t("Внутренний номер заказа (без данных клиента)"));
        if (!reference) return;
        const amount = window.prompt(t("Сумма продажи в EUR"));
        if (amount === null) return;
        const conversionValue = Math.round(Number(amount.replace(",", ".")) * 100);
        if (!Number.isFinite(conversionValue) || conversionValue < 0) {
          showToast(t("Укажите корректную сумму продажи"));
          return;
        }
        claim.disabled = true;
        try {
          await api(`/api/v1/partner/leads/${lead.id}/conversion`, {
            method: "POST",
            body: JSON.stringify({ partner_reference: reference.trim(), conversion_value_cents: conversionValue }),
          });
          await loadPartner();
        } catch (error) { showToast(error.message); claim.disabled = false; }
      });
      actions.append(claim);
    }
    row.append(actions);
    body.append(row);
  });
}

function renderPartnerInvoices(invoices) {
  const body = byId("partnerInvoices");
  body.replaceChildren();
  invoices.forEach((invoice) => {
    const row = element("tr");
    appendCell(row, invoice.invoice_number || "—");
    appendCell(row, `${invoice.period_start} — ${invoice.period_end}`);
    appendCell(row, invoice.confirmed_leads_count);
    appendCell(row, money(invoice.total_cents, invoice.currency));
    appendCell(row, new Date(invoice.due_at).toLocaleDateString(uiLocale()));
    appendCell(row, invoice.status);
    const actions = element("td");
    if (invoice.pdf_sha256) {
      const download = element("button", "button small ghost", "PDF");
      download.type = "button";
      download.addEventListener("click", async () => {
        download.disabled = true;
        try {
          await downloadAuthenticatedFile(
            `/api/v1/partner/invoices/${invoice.id}/pdf`,
            `${invoice.invoice_number || "invoice-" + invoice.id}.pdf`,
          );
        } catch (error) { showToast(error.message); }
        finally { download.disabled = false; }
      });
      actions.append(download);
    }
    if (invoice.cancellation_pdf_sha256) {
      const cancellation = element("button", "button small ghost", t("Аннулирование PDF"));
      cancellation.type = "button";
      cancellation.addEventListener("click", async () => {
        cancellation.disabled = true;
        try {
          await downloadAuthenticatedFile(
            `/api/v1/partner/invoices/${invoice.id}/cancellation.pdf`,
            `${invoice.cancellation_number || "invoice-cancellation-" + invoice.id}.pdf`,
          );
        } catch (error) { showToast(error.message); }
        finally { cancellation.disabled = false; }
      });
      actions.append(cancellation);
    }
    row.append(actions);
    body.append(row);
  });
}

async function loadPartner() {
  if (!state.partnerAccount) return;
  const status = byId("partnerStatus");
  status.textContent = "Загружаем кабинет…";
  try {
    const [overview, products, leads, invoices] = await Promise.all([
      api("/api/v1/partner/overview"),
      api("/api/v1/partner/products"),
      api("/api/v1/partner/leads?limit=100"),
      api("/api/v1/partner/invoices?limit=100"),
    ]);
    renderPartnerOverview(overview);
    renderPartnerProducts(products);
    renderPartnerLeads(leads);
    renderPartnerInvoices(invoices);
    byId("partnerProductForm").classList.toggle("hidden", !state.partnerAccount.can_manage_products);
    status.textContent = `${t(state.partnerAccount.role)} · ${t("обновлено")} ${new Date().toLocaleTimeString(uiLocale())}`;
  } catch (error) {
    status.textContent = error.message;
    status.className = "form-message error";
  }
}

async function showPartnerView() {
  if (!state.partnerAccount) return;
  state.selectedPlant = null;
  renderPlants();
  byId("emptyState").classList.add("hidden");
  byId("plantWorkspace").classList.add("hidden");
  byId("calendarWorkspace").classList.add("hidden");
  byId("adminWorkspace").classList.add("hidden");
  byId("billingWorkspace").classList.add("hidden");
  byId("telegramWorkspace").classList.add("hidden");
  byId("partnerWorkspace").classList.remove("hidden");
  await loadPartner();
}

function billingStatusLabel(status) {
  return t({
    active: "активна",
    trialing: "пробный период",
    pending: "ожидает подтверждения",
    incomplete: "не завершена",
    past_due: "требуется оплата",
    canceled: "отменена",
    unpaid: "не оплачена",
    unsupported_price: "неподдерживаемый тариф",
  }[status] || status);
}

function renderBilling(plans, subscription) {
  const container = byId("billingPlans");
  container.replaceChildren();
  plans.forEach((plan) => {
    const card = element("article", `card billing-plan${subscription.plan === plan.code ? " current" : ""}`);
    const price = plan.monthly_amount === 0
      ? "Бесплатно"
      : new Intl.NumberFormat(uiLocale(), { style: "currency", currency: plan.currency.toUpperCase() }).format(plan.monthly_amount / 100) + ` ${t("/ месяц")}`;
    card.append(element("span", "eyebrow", subscription.plan === plan.code ? "Текущий тариф" : "Тариф"));
    card.append(element("h2", "", plan.name), element("strong", "billing-price", price));
    const features = element("ul", "feature-list");
    plan.features.forEach((feature) => features.append(element("li", "", feature)));
    card.append(features);
    if (plan.code === "pro" && subscription.plan !== "pro") {
      const checkout = element("button", "button primary", plan.checkout_available ? "Перейти к оплате" : "Оплата ещё не подключена");
      checkout.type = "button";
      checkout.disabled = !plan.checkout_available;
      checkout.addEventListener("click", async () => {
        checkout.disabled = true;
        try {
          const redirect = await api("/api/v1/billing/checkout", {
            method: "POST",
            body: JSON.stringify({ plan: "pro" }),
          });
          window.location.assign(redirect.url);
        } catch (error) { showToast(error.message); checkout.disabled = false; }
      });
      card.append(checkout);
    }
    container.append(card);
  });
  const manage = byId("manageBilling");
  manage.classList.toggle("hidden", !subscription.can_manage);
  const period = subscription.current_period_end
    ? ` · ${t("период до")} ${new Date(subscription.current_period_end).toLocaleDateString(uiLocale())}`
    : "";
  const cancellation = subscription.cancel_at_period_end ? t(" · отменится в конце периода") : "";
  byId("billingStatus").textContent = `Подписка: ${billingStatusLabel(subscription.status)}${period}${cancellation}`;
}

async function loadBilling() {
  const status = byId("billingStatus");
  status.textContent = "Загружаем тарифы…";
  status.className = "muted";
  try {
    const [plans, subscription] = await Promise.all([
      api("/api/v1/billing/plans"),
      api("/api/v1/billing/subscription"),
    ]);
    renderBilling(plans, subscription);
  } catch (error) {
    status.textContent = error.message;
    status.className = "form-message error";
  }
}

async function showBillingView() {
  state.selectedPlant = null;
  renderPlants();
  byId("emptyState").classList.add("hidden");
  byId("plantWorkspace").classList.add("hidden");
  byId("calendarWorkspace").classList.add("hidden");
  byId("adminWorkspace").classList.add("hidden");
  byId("partnerWorkspace").classList.add("hidden");
  byId("telegramWorkspace").classList.add("hidden");
  byId("billingWorkspace").classList.remove("hidden");
  await loadBilling();
}

function renderTelegramStatus(status) {
  const create = byId("createTelegramLink");
  const unlink = byId("unlinkTelegram");
  create.classList.toggle("hidden", !status.enabled || status.linked);
  unlink.classList.toggle("hidden", !status.linked);
  if (!status.enabled) {
    byId("telegramTitle").textContent = "Telegram пока не подключён";
    byId("telegramStatus").textContent = "Администратор ещё не настроил бота.";
  } else if (status.linked) {
    byId("telegramTitle").textContent = "Telegram подключён";
    byId("telegramStatus").textContent = [status.username ? `@${status.username}` : null, status.language].filter(Boolean).join(" · ");
  } else {
    byId("telegramTitle").textContent = "Подключить Telegram";
    byId("telegramStatus").textContent = "Создайте одноразовую ссылку и нажмите Start в личном чате с ботом.";
  }
}

async function loadTelegram() {
  const statusNode = byId("telegramStatus");
  statusNode.textContent = "Проверяем состояние…";
  statusNode.className = "muted";
  try {
    const status = await api("/api/v1/telegram/status");
    renderTelegramStatus(status);
  } catch (error) {
    statusNode.textContent = error.message;
    statusNode.className = "form-message error";
  }
}

async function loadNotifications() {
  const list = byId("notificationList");
  list.replaceChildren();
  try {
    const notifications = await api("/api/v1/notifications");
    if (!notifications.length) {
      list.append(element("p", "muted", "Уведомлений пока нет."));
      return;
    }
    notifications.forEach((notification) => {
      const card = element("article", `timeline-card${notification.read_at ? " completed" : ""}`);
      card.append(
        element("strong", "", notification.title),
        element("p", "", notification.body),
        element("time", "muted", new Date(notification.event_at).toLocaleString(uiLocale())),
      );
      if (!notification.read_at) {
        const mark = element("button", "button small ghost", "Прочитано");
        mark.type = "button";
        mark.addEventListener("click", async () => {
          await api(`/api/v1/notifications/${notification.id}/read`, { method: "PATCH" });
          await loadNotifications();
        });
        card.append(mark);
      }
      list.append(card);
    });
  } catch (error) {
    list.append(element("p", "form-message error", error.message));
  }
}

async function showTelegramView() {
  state.selectedPlant = null;
  renderPlants();
  byId("emptyState").classList.add("hidden");
  byId("plantWorkspace").classList.add("hidden");
  byId("calendarWorkspace").classList.add("hidden");
  byId("adminWorkspace").classList.add("hidden");
  byId("partnerWorkspace").classList.add("hidden");
  byId("billingWorkspace").classList.add("hidden");
  byId("telegramWorkspace").classList.remove("hidden");
  byId("telegramLinkResult").replaceChildren();
  await Promise.all([loadTelegram(), loadNotifications()]);
}

function careEventLabel(type) {
  return t({
    watering: "Полив",
    fertilizing: "Подкормка",
    treatment: "Обработка",
    pruning: "Обрезка",
    transplanting: "Пересадка",
    observation: "Наблюдение",
  }[type] || type);
}

function defaultReminderDate() {
  const date = new Date();
  date.setDate(date.getDate() + 1);
  date.setHours(9, 0, 0, 0);
  return localDateTimeValue(date);
}

function reminderKindLabel(kind) {
  return t({
    inspection: "Осмотр",
    other: "Другое",
    watering: "Полив",
    fertilizing: "Подкормка",
    treatment: "Обработка",
    pruning: "Обрезка",
    transplanting: "Пересадка",
  }[kind] || kind);
}

async function loadWeather() {
  if (!state.selectedPlant) return;
  const status = byId("weatherStatus");
  const dailyList = byId("weatherDaily");
  const warningList = byId("weatherWarnings");
  status.textContent = "Загружаем прогноз…";
  status.className = "muted";
  dailyList.replaceChildren();
  warningList.replaceChildren();
  try {
    const forecast = await api(`/api/v1/gardens/${state.selectedPlant.garden_id}/weather`);
    status.replaceChildren();
    status.append(document.createTextNode(`${forecast.location}${forecast.country_code ? `, ${forecast.country_code}` : ""} · `));
    const source = element("a", "", forecast.provider);
    source.href = forecast.source_url;
    source.target = "_blank";
    source.rel = "noreferrer noopener";
    status.append(source);

    forecast.warnings.forEach((warning) => {
      const card = element("div", `weather-warning ${warning.severity}`);
      const date = new Date(`${warning.date}T12:00:00`);
      card.append(
        element("strong", "", `${warning.title} · ${date.toLocaleDateString(uiLocale(), { day: "2-digit", month: "short" })}`),
        element("span", "", warning.advice),
      );
      warningList.append(card);
    });

    forecast.daily.slice(0, 5).forEach((day) => {
      const card = element("article", "weather-day");
      const date = new Date(`${day.date}T12:00:00`);
      card.append(
        element("strong", "", date.toLocaleDateString(uiLocale(), { weekday: "short", day: "2-digit", month: "short" })),
        element("div", "weather-temperature", `${Math.round(day.temperature_max_c)}° / ${Math.round(day.temperature_min_c)}°`),
        element("div", "weather-meta", `Осадки: ${day.precipitation_mm.toFixed(1)} мм`),
        element("div", "weather-meta", `Ветер: ${Math.round(day.wind_speed_max_kmh)} км/ч`),
      );
      dailyList.append(card);
    });
  } catch (error) {
    status.textContent = error.message;
    status.className = "form-message error";
  }
}

async function loadCareEvents() {
  if (!state.selectedPlant) return;
  try {
    const events = await api(`/api/v1/plants/${state.selectedPlant.id}/care-events`);
    const list = byId("careEventList");
    list.replaceChildren();
    if (!events.length) {
      list.append(element("p", "muted", "Записей ухода пока нет."));
      return;
    }
    events.forEach((careEvent) => {
      const item = element("article", "care-item");
      const heading = element("div");
      heading.append(
        element("div", "care-kind", careEventLabel(careEvent.event_type)),
        element("time", "care-meta", new Date(careEvent.occurred_at).toLocaleString(uiLocale())),
      );
      const details = element("div");
      const measurement = careEvent.amount ? `${careEvent.amount}${careEvent.unit ? ` ${careEvent.unit}` : ""}` : null;
      details.append(element("div", "", [measurement, careEvent.product].filter(Boolean).join(" · ") || "Без дополнительных данных"));
      if (careEvent.notes) details.append(element("div", "care-meta", careEvent.notes));
      const remove = element("button", "care-delete", "Удалить");
      remove.type = "button";
      remove.addEventListener("click", async () => {
        remove.disabled = true;
        try {
          await api(`/api/v1/care-events/${careEvent.id}`, { method: "DELETE" });
          await loadCareEvents();
        } catch (error) {
          remove.disabled = false;
          showToast(error.message);
        }
      });
      item.append(heading, details, remove);
      list.append(item);
    });
  } catch (error) { showToast(error.message); }
}

async function loadReminders() {
  if (!state.selectedPlant) return;
  try {
    const reminders = await api(`/api/v1/plants/${state.selectedPlant.id}/reminders`);
    const list = byId("reminderList");
    list.replaceChildren();
    if (!reminders.length) {
      list.append(element("p", "muted", "Активных напоминаний пока нет."));
      return;
    }
    reminders.forEach((reminder) => {
      const item = element("article", "reminder-item");
      const check = element("input", "reminder-check");
      check.type = "checkbox";
      check.checked = reminder.completed;
      check.setAttribute("aria-label", `Выполнить: ${reminder.title}`);
      check.addEventListener("change", async () => {
        check.disabled = true;
        try {
          await api(`/api/v1/reminders/${reminder.id}`, { method: "PATCH", body: JSON.stringify({ completed: check.checked }) });
          await loadReminders();
          showToast("Напоминание выполнено");
        } catch (error) {
          check.checked = !check.checked;
          check.disabled = false;
          showToast(error.message);
        }
      });
      const details = element("div");
      details.append(
        element("strong", "", reminder.title),
        element("div", "care-meta", `${reminderKindLabel(reminder.kind)} · ${new Date(reminder.due_at).toLocaleString(uiLocale())}`),
      );
      if (reminder.recurrence) {
        const recurrenceLabel = { daily: "ежедневно", weekly: "еженедельно", monthly: "ежемесячно" }[reminder.recurrence];
        details.append(element("div", "care-meta", `${recurrenceLabel} · ${reminder.preferred_channel} · ${reminder.timezone}`));
      }
      if (reminder.notes) details.append(element("div", "care-meta", reminder.notes));
      const snooze = element("button", "button ghost small-action", "Отложить на час");
      snooze.type = "button";
      snooze.addEventListener("click", async () => {
        snooze.disabled = true;
        try {
          await api(`/api/v1/reminders/${reminder.id}`, {
            method: "PATCH", body: JSON.stringify({ snooze_minutes: 60 }),
          });
          await loadReminders();
        } catch (error) { snooze.disabled = false; showToast(error.message); }
      });
      const skip = element("button", "button ghost small-action", "Пропустить один раз");
      skip.type = "button";
      skip.classList.toggle("hidden", !reminder.recurrence);
      skip.addEventListener("click", async () => {
        skip.disabled = true;
        try {
          await api(`/api/v1/reminders/${reminder.id}`, {
            method: "PATCH", body: JSON.stringify({ skip_occurrence: true }),
          });
          await loadReminders();
        } catch (error) { skip.disabled = false; showToast(error.message); }
      });
      const remove = element("button", "care-delete", "Удалить");
      remove.type = "button";
      remove.addEventListener("click", async () => {
        remove.disabled = true;
        try {
          await api(`/api/v1/reminders/${reminder.id}`, { method: "DELETE" });
          await loadReminders();
        } catch (error) {
          remove.disabled = false;
          showToast(error.message);
        }
      });
      const actions = element("div", "entity-actions");
      actions.append(snooze, skip, remove);
      item.append(check, details, actions);
      list.append(item);
    });
  } catch (error) { showToast(error.message); }
}

function renderPhotoSelection(files) {
  const preview = byId("photoPreview");
  preview.replaceChildren();
  resetObjectUrls();
  [...files].slice(0, 5).forEach((file) => {
    const url = URL.createObjectURL(file);
    state.objectUrls.push(url);
    const image = element("img");
    image.src = url;
    image.alt = file.name;
    preview.append(image);
  });
}

function confidenceLabel(value) {
  return t({ high: "высокая", medium: "средняя", low: "низкая" }[value] || value);
}

function addList(parent, title, items) {
  if (!items?.length) return;
  parent.append(element("strong", "", title));
  const list = element("ul");
  items.forEach((item) => list.append(element("li", "", item)));
  parent.append(list);
}

async function renderResult(diagnosis) {
  const panel = byId("resultPanel");
  panel.replaceChildren();
  const result = diagnosis.result;
  const status = element("span", `result-status${result.input_status === "valid" ? "" : " warning"}`,
    result.input_status === "valid" ? "Предварительная оценка" : "Анализ остановлен");
  panel.append(status);

  if (result.input_status !== "valid") {
    const card = element("div", "cannot-card");
    card.append(
      element("h2", "", "Нужна другая фотография"),
      element("p", "", result.cannot_analyze_reason || "Недостаточно данных для безопасного анализа."),
    );
    const actions = element("ul", "safe-actions");
    (result.safe_actions || []).forEach((action) => actions.append(element("li", "", action)));
    card.append(actions);
    panel.append(card);
  } else {
    panel.append(element("h2", "", "Возможные причины"), element("p", "muted", result.disclaimer));
    result.possible_causes.forEach((cause) => {
      const card = element("article", "cause-card");
      const header = element("div", "cause-header");
      header.append(element("strong", "", cause.name), element("span", "confidence", `Уверенность: ${confidenceLabel(cause.confidence)}`));
      card.append(header);
      addList(card, "Совпавшие признаки", cause.matched_signs);
      addList(card, "Что проверить", cause.checks);
      if (cause.source_ids?.length) {
        const tags = element("div", "source-tags");
        cause.source_ids.forEach((id) => tags.append(element("span", "source-tag", id)));
        card.append(tags);
      }
      panel.append(card);
    });
    addList(panel, "Безопасные действия", result.safe_actions);
  }

  if (result.sources?.length) {
    const sourceTitle = element("h3", "", "Справочные источники");
    const list = element("ul", "safe-actions");
    result.sources.forEach((url) => {
      const link = element("a", "", url);
      link.href = url;
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      const item = element("li");
      item.append(link);
      list.append(item);
    });
    panel.append(sourceTitle, list);
  }

  await appendProducts(panel, diagnosis.id);
  await appendQuestions(panel, diagnosis);
  appendReanalyze(panel, diagnosis.id);
  appendFeedback(panel, diagnosis.id);
}

async function appendProducts(panel, diagnosisId) {
  if (!state.partnerCommerceEnabled) return;
  let products;
  try {
    products = await api(`/api/v1/recommendations/${diagnosisId}/products`);
  } catch (_) {
    return;
  }
  if (!products.length) return;
  const section = element("section", "product-section");
  section.append(
    element("span", "commercial-label", "Реклама · предложения партнёров"),
    element("h3", "", "Полезные товары"),
    element("p", "muted", "Товары не подтверждают диагноз и не заменяют проверку условий ухода."),
  );
  const grid = element("div", "product-grid");
  products.forEach((product) => {
    const card = element("article", "product-card");
    if (product.image_url) {
      const image = element("img", "product-image");
      image.src = product.image_url;
      image.alt = product.name;
      image.loading = "lazy";
      image.referrerPolicy = "no-referrer";
      card.append(image);
    }
    card.append(
      element("span", "commercial-label", product.commercial_label),
      element("strong", "", product.name),
      element("div", "care-meta", product.partner_name),
    );
    if (product.price_cents !== null) card.append(element("strong", "product-price", formatProductPrice(product)));
    if (product.description) card.append(element("p", "", product.description));
    const visit = element("button", "button ghost", "Перейти к партнёру");
    visit.type = "button";
    visit.addEventListener("click", async () => {
      visit.disabled = true;
      try {
        const lead = await api(`/api/v1/products/${product.id}/lead`, {
          method: "POST",
          body: JSON.stringify({ diagnosis_id: diagnosisId }),
        });
        window.location.assign(lead.redirect_url);
      } catch (error) {
        visit.disabled = false;
        showToast(error.message);
      }
    });
    card.append(visit);
    grid.append(card);
  });
  section.append(grid);
  panel.append(section);
}

function appendReanalyze(panel, diagnosisId) {
  const actions = element("div", "result-actions");
  const button = element("button", "button ghost", "Повторить анализ");
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "Анализируем…";
    try {
      const job = await api(`/api/v1/diagnoses/${diagnosisId}/reanalyze`, { method: "POST" });
      const updated = await waitForDiagnosisJob(job, button);
      await renderResult(updated);
      await loadHistory();
      showToast("Создана новая версия анализа");
    } catch (error) {
      showToast(`${error.message}${error.requestId ? ` · ${error.requestId}` : ""}`);
      button.disabled = false;
      button.textContent = "Повторить анализ";
    }
  });
  actions.append(button);
  panel.append(actions);
}

async function appendQuestions(panel, diagnosis) {
  const questions = await api(`/api/v1/diagnoses/${diagnosis.id}/questions`);
  if (!questions.length) return;
  const form = element("form", "compact-form");
  form.append(element("h3", "", "Уточняющие вопросы"));
  questions.forEach((question) => {
    const label = element("label", "", question.text);
    const input = element("textarea");
    input.rows = 2;
    input.maxLength = 3000;
    input.required = true;
    input.dataset.questionId = question.id;
    label.append(input);
    form.append(label);
  });
  const submit = element("button", "button primary", "Ответить и пересмотреть");
  submit.type = "submit";
  form.append(submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      const answers = [...form.querySelectorAll("textarea")].map((input) => ({ question_id: Number(input.dataset.questionId), answer: input.value }));
      const job = await api(`/api/v1/diagnoses/${diagnosis.id}/answers`, { method: "POST", body: JSON.stringify({ answers }) });
      const updated = await waitForDiagnosisJob(job, submit);
      await renderResult(updated);
      await loadHistory();
    } catch (error) { showToast(error.message); }
    finally { submit.disabled = false; }
  });
  panel.append(form);
}

function appendFeedback(panel, diagnosisId) {
  const form = element("form", "compact-form");
  form.append(element("h3", "", "Оценить результат"));
  const select = element("select");
  [[5, "5 — очень полезно"], [4, "4 — полезно"], [3, "3 — частично"], [2, "2 — мало пользы"], [1, "1 — бесполезно"]]
    .forEach(([value, label]) => { const option = element("option", "", label); option.value = value; select.append(option); });
  const ratingLabel = element("label", "", "Оценка");
  ratingLabel.append(select);
  const comment = element("textarea");
  comment.placeholder = "Комментарий (необязательно)";
  comment.maxLength = 2000;
  const commentLabel = element("label", "", "Комментарий");
  commentLabel.append(comment);
  const submit = element("button", "button small", "Сохранить отзыв");
  submit.type = "submit";
  form.append(ratingLabel, commentLabel, submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      const rating = Number(select.value);
      await api(`/api/v1/diagnoses/${diagnosisId}/feedback`, {
        method: "POST",
        body: JSON.stringify({ rating, helpful: rating >= 4, comment: comment.value || null }),
      });
      showToast("Спасибо, отзыв сохранён");
    } catch (error) { showToast(error.message); }
    finally { submit.disabled = false; }
  });
  panel.append(form);
}

async function photoBlobUrl(photoId) {
  const response = await fetch(`/api/v1/photos/${photoId}`, { headers: { Authorization: `Bearer ${state.token}` } });
  if (!response.ok) return null;
  const url = URL.createObjectURL(await response.blob());
  state.objectUrls.push(url);
  return url;
}

async function loadHistory() {
  if (!state.selectedPlant) return;
  try {
    const history = await api(`/api/v1/plants/${state.selectedPlant.id}/history`);
    const list = byId("historyList");
    list.replaceChildren();
    resetObjectUrls();
    if (!history.diagnoses.length) {
      list.append(element("p", "muted", "Диагностик пока нет."));
      return;
    }
    for (const diagnosis of history.diagnoses) {
      const item = element("article", "history-item");
      const date = new Date(diagnosis.created_at);
      item.append(element("time", "history-date", date.toLocaleDateString(uiLocale(), { day: "2-digit", month: "short", year: "numeric" })));
      const content = element("div");
      content.append(element("strong", "", diagnosis.result.input_status === "valid" ? "Предварительная диагностика" : "Анализ не выполнен"));
      content.append(element("p", "muted", diagnosis.symptoms));
      const photos = element("div", "photo-preview");
      for (const photoId of diagnosis.photo_ids) {
        const url = await photoBlobUrl(photoId);
        if (url) { const image = element("img"); image.src = url; image.alt = "Фотография растения"; photos.append(image); }
      }
      if (photos.childElementCount) content.append(photos);
      history.answers.filter((answer) => answer.diagnosis_id === diagnosis.id).forEach((answer) => {
        const block = element("div", "history-answer");
        block.append(element("strong", "", answer.question), element("div", "", answer.answer));
        content.append(block);
      });
      const open = element("button", "button ghost", "Открыть результат");
      open.type = "button";
      open.addEventListener("click", () => renderResult(diagnosis));
      content.append(open);
      item.append(content);
      list.append(item);
    }
  } catch (error) { showToast(error.message); }
}

byId("loginTab").addEventListener("click", () => switchAuth("login"));
byId("registerTab").addEventListener("click", () => switchAuth("register"));
document.querySelector(".segmented[role='tablist']").addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const showRegister = event.key === "ArrowRight" || event.key === "End";
  switchAuth(showRegister ? "register" : "login");
  byId(showRegister ? "registerTab" : "loginTab").focus();
});
byId("loginForm").addEventListener("submit", (event) => {
  event.preventDefault();
  authenticate("/api/v1/auth/login", formJson(event.currentTarget));
});
byId("registerForm").addEventListener("submit", (event) => {
  event.preventDefault();
  authenticate("/api/v1/auth/register", { ...formJson(event.currentTarget), language: window.I18N?.getLanguage() || "ru" });
});
byId("showPasswordReset").addEventListener("click", () => {
  byId("loginForm").classList.add("hidden");
  document.querySelector(".segmented").classList.add("hidden");
  byId("passwordResetRequestForm").classList.remove("hidden");
  authError.classList.add("hidden");
});
document.querySelectorAll(".auth-back").forEach((button) => button.addEventListener("click", () => switchAuth("login")));
byId("passwordResetRequestForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/v1/auth/password-reset/request", {
      method: "POST", body: JSON.stringify(formJson(event.currentTarget)),
    });
    authError.textContent = "Если аккаунт существует, ссылка отправлена на указанный email.";
    authError.className = "form-message";
  } catch (error) {
    authError.textContent = error.message;
    authError.className = "form-message error";
  }
});
byId("passwordResetConfirmForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = new URLSearchParams(window.location.hash.slice(1)).get("token");
  try {
    await api("/api/v1/auth/password-reset/confirm", {
      method: "POST", body: JSON.stringify({ ...formJson(event.currentTarget), token }),
    });
    window.history.replaceState({}, "", window.location.pathname + window.location.search);
    switchAuth("login");
    authError.textContent = "Пароль изменён. Теперь можно войти.";
    authError.className = "form-message";
  } catch (error) {
    authError.textContent = error.message;
    authError.className = "form-message error";
  }
});
byId("languageSelect").value = window.I18N?.getLanguage() || "ru";
byId("languageSelect").addEventListener("change", async (event) => {
  const language = event.target.value;
  window.I18N?.setLanguage(language);
  if (!state.token) return;
  try {
    state.profile = await api("/api/v1/users/me", {
      method: "PATCH",
      body: JSON.stringify({ language }),
    });
    showToast(t("Язык сохранён"));
  } catch (error) { showToast(error.message); }
});
byId("logoutButton").addEventListener("click", async () => {
  try {
    await fetch("/api/v1/auth/logout", { method: "POST", credentials: "same-origin" });
  } finally {
    clearSession();
  }
});
byId("adminButton").addEventListener("click", showAdminView);
byId("refreshAdmin").addEventListener("click", loadAdmin);
byId("partnerButton").addEventListener("click", showPartnerView);
byId("refreshPartner").addEventListener("click", loadPartner);
byId("billingButton").addEventListener("click", showBillingView);
byId("refreshBilling").addEventListener("click", loadBilling);
byId("telegramButton").addEventListener("click", showTelegramView);
byId("refreshTelegram").addEventListener("click", loadTelegram);
byId("createTelegramLink").addEventListener("click", async () => {
  const button = byId("createTelegramLink");
  button.disabled = true;
  try {
    const link = await api("/api/v1/telegram/link", { method: "POST" });
    const result = byId("telegramLinkResult");
    result.replaceChildren(
      element("span", "muted", `${t("Одноразовый код до")} ${new Date(link.expires_at).toLocaleTimeString(uiLocale())}`),
      element("code", "", link.code),
    );
    if (link.deep_link) {
      const open = element("a", "button primary", "Открыть бота в Telegram");
      open.href = link.deep_link;
      open.target = "_blank";
      open.rel = "noopener noreferrer";
      result.append(open);
    }
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; }
});
byId("unlinkTelegram").addEventListener("click", async () => {
  if (!window.confirm("Отключить Telegram от аккаунта AI Garden?")) return;
  const button = byId("unlinkTelegram");
  button.disabled = true;
  try {
    await api("/api/v1/telegram/link", { method: "DELETE" });
    byId("telegramLinkResult").replaceChildren();
    await loadTelegram();
    showToast("Telegram отключён");
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; }
});
byId("manageBilling").addEventListener("click", async () => {
  const button = byId("manageBilling");
  button.disabled = true;
  try {
    const redirect = await api("/api/v1/billing/portal", { method: "POST" });
    window.location.assign(redirect.url);
  } catch (error) { showToast(error.message); button.disabled = false; }
});
byId("exportAccountData").addEventListener("click", async () => {
  try {
    const exported = await api("/api/v1/users/me/export");
    const blob = new Blob([JSON.stringify(exported, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `ai-garden-data-${new Date().toISOString().slice(0, 10)}.json`;
    link.click();
    URL.revokeObjectURL(url);
    showToast("Экспорт данных подготовлен");
  } catch (error) { showToast(error.message); }
});
byId("deleteAccount").addEventListener("click", async () => {
  const password = window.prompt(t("Введите пароль для безвозвратного удаления аккаунта"));
  if (!password) return;
  if (!window.confirm(t("Удалить аккаунт, все данные и фотографии? Это действие нельзя отменить."))) return;
  try {
    const result = await api("/api/v1/users/me", {
      method: "DELETE", body: JSON.stringify({ password }),
    });
    clearSession();
    showToast(result?.account_blocked
      ? "Аккаунт заблокирован; удаление завершится после отмены подписки"
      : "Аккаунт и данные удалены");
  } catch (error) { showToast(error.message); }
});
byId("adminPartnerForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type='submit']");
  submit.disabled = true;
  try {
    const values = formJson(form);
    await api("/api/v1/admin/partners", {
      method: "POST",
      body: JSON.stringify({
        ...values,
        legal_name: values.legal_name || null,
        registration_number: values.registration_number || null,
        vat_number: values.vat_number || null,
        billing_address: values.billing_address || null,
        billing_email: values.billing_email || null,
      }),
    });
    form.reset();
    await loadAdmin();
    showToast("Партнёр добавлен");
  } catch (error) { showToast(error.message); }
  finally { submit.disabled = false; }
});
byId("adminProductForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type='submit']");
  submit.disabled = true;
  try {
    const values = formJson(form);
    await api("/api/v1/admin/products", {
      method: "POST",
      body: JSON.stringify({
        ...values,
        partner_id: Number(values.partner_id),
        sku: values.sku || null,
        description: values.description || null,
        image_url: values.image_url || null,
        price_cents: values.price_cents ? Number(values.price_cents) : null,
        currency: values.currency,
        in_stock: form.elements.in_stock.checked,
        regions: values.regions.split(",").map((region) => region.trim()).filter(Boolean),
      }),
    });
    form.reset();
    await loadAdmin();
    showToast("Товар добавлен");
  } catch (error) { showToast(error.message); }
  finally {
    submit.disabled = byId("adminProductPartner").options.length === 0;
  }
});
byId("adminPartnerMemberForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type='submit']");
  submit.disabled = true;
  try {
    const values = formJson(form);
    await api(`/api/v1/admin/partners/${Number(values.partner_id)}/members`, {
      method: "POST",
      body: JSON.stringify({ email: values.email, role: values.role }),
    });
    form.reset();
    await loadAdmin();
    showToast("Сотрудник назначен");
  } catch (error) { showToast(error.message); }
  finally { submit.disabled = byId("adminMemberPartner").options.length === 0; }
});
byId("adminInvoiceForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type='submit']");
  const values = formJson(form);
  submit.disabled = true;
  try {
    await api(`/api/v1/admin/partners/${Number(values.partner_id)}/invoices`, {
      method: "POST",
      body: JSON.stringify({
        period_start: values.period_start,
        period_end: values.period_end,
        due_days: Number(values.due_days),
      }),
    });
    await loadAdmin();
    showToast("Счёт сформирован");
  } catch (error) { showToast(error.message); }
  finally { submit.disabled = byId("adminInvoicePartner").options.length === 0; }
});
byId("adminPaymentImportForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type='submit']");
  const values = formJson(form);
  submit.disabled = true;
  try {
    const records = JSON.parse(values.records);
    if (!Array.isArray(records) || records.length === 0) throw new Error("JSON должен содержать непустой массив операций");
    const result = await api("/api/v1/admin/partner-payments/import", {
      method: "POST", body: JSON.stringify({ source: values.source, records }),
    });
    byId("adminPaymentImportResult").textContent = `${t("Импортировано")}: ${result.imported}; ${t("сопоставлено")}: ${result.matched}; ${t("без совпадения")}: ${result.unmatched}; ${t("дубликатов")}: ${result.duplicates}`;
    await loadAdmin();
  } catch (error) { showToast(error instanceof SyntaxError ? "Некорректный JSON операций" : error.message); }
  finally { submit.disabled = false; }
});
byId("adminRegistryImportForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type='submit']");
  const values = formJson(form);
  submit.disabled = true;
  try {
    const records = JSON.parse(values.records);
    if (!Array.isArray(records) || records.length === 0) throw new Error("JSON должен содержать непустой массив записей");
    const result = await api("/api/v1/admin/regulated-products/import", {
      method: "POST",
      body: JSON.stringify({
        source_name: values.source_name,
        source_url: values.source_url,
        source_version: values.source_version,
        complete_snapshot: form.elements.complete_snapshot.checked,
        records,
      }),
    });
    byId("adminRegistryResult").textContent = `${t("Создано")}: ${result.created}; ${t("обновлено")}: ${result.updated}; ${t("правил отключено")}: ${result.invalidated_rules}`;
    await loadAdmin();
    showToast("Реестр импортирован");
  } catch (error) { showToast(error instanceof SyntaxError ? "Некорректный JSON реестра" : error.message); }
  finally { submit.disabled = false; }
});
byId("adminRuleForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type='submit']");
  const values = formJson(form);
  submit.disabled = true;
  try {
    await api("/api/v1/admin/product-recommendation-rules", {
      method: "POST",
      body: JSON.stringify({
        product_id: Number(values.product_id),
        registry_entry_id: values.registry_entry_id ? Number(values.registry_entry_id) : null,
        crop_name: values.crop_name,
        plant_taxon_id: values.plant_taxon_id || null,
        problem_name: values.problem_name,
        problem_code: values.problem_code || null,
        safe_action: values.safe_action,
        region_code: values.region_code,
        country_code: values.country_code ? values.country_code.toUpperCase() : null,
        registration_country: null,
        registration_number: null,
        registration_url: null,
        registration_expires_on: null,
        expert_verified: form.elements.expert_verified.checked,
      }),
    });
    form.reset();
    await loadAdmin();
    showToast("Правило добавлено");
  } catch (error) { showToast(error.message); }
  finally { submit.disabled = byId("adminRuleProduct").options.length === 0; }
});
byId("partnerProductForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type='submit']");
  submit.disabled = true;
  try {
    const values = formJson(form);
    await api("/api/v1/partner/products", {
      method: "POST",
      body: JSON.stringify({
        ...values,
        sku: values.sku || null,
        description: values.description || null,
        image_url: values.image_url || null,
        price_cents: values.price_cents ? Number(values.price_cents) : null,
        currency: values.currency,
        in_stock: form.elements.in_stock.checked,
        regions: values.regions.split(",").map((region) => region.trim()).filter(Boolean),
      }),
    });
    form.reset();
    await loadPartner();
    showToast("Товар добавлен");
  } catch (error) { showToast(error.message); }
  finally { submit.disabled = false; }
});

byId("partnerCsvImportForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("button[type='submit']");
  submit.disabled = true;
  try {
    const body = new FormData(form);
    const result = await api("/api/v1/partner/products/import/csv", { method: "POST", body });
    form.reset();
    showToast(`CSV: +${result.created}, ↻${result.updated}`);
    await loadPartner();
  } catch (error) { showToast(error.message); }
  finally { submit.disabled = false; }
});
byId("dashboardSettingsButton").addEventListener("click", openDashboardSettings);
byId("closeDashboardSettings").addEventListener("click", closeDashboardSettings);
byId("saveDashboardSettings").addEventListener("click", closeDashboardSettings);
byId("dashboardSettingsForm").addEventListener("change", (event) => {
  const checkbox = event.target.closest("input[type='checkbox']");
  if (!checkbox) return;
  state.dashboardWidgets[checkbox.value] = checkbox.checked;
  saveDashboardPreferences();
  applyDashboardPreferences();
});
byId("showAllDashboardWidgets").addEventListener("click", () => {
  DASHBOARD_WIDGETS.forEach((name) => { state.dashboardWidgets[name] = true; });
  byId("dashboardSettingsForm").querySelectorAll("input[type='checkbox']").forEach((checkbox) => {
    checkbox.checked = true;
  });
  saveDashboardPreferences();
  applyDashboardPreferences();
});
byId("dashboardSettingsModal").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) closeDashboardSettings();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !byId("dashboardSettingsModal").classList.contains("hidden")) {
    closeDashboardSettings();
  }
});
byId("showGardenForm").addEventListener("click", () => {
  state.pendingPlantAfterGarden = false;
  openGardenForm();
  byId("gardenForm").elements.name.focus();
});
byId("showPlantForm").addEventListener("click", () => {
  if (!state.selectedGardenId) {
    state.pendingPlantAfterGarden = true;
    openGardenForm();
    byId("gardenForm").elements.name.focus();
    showToast(t("Сначала создайте сад. После сохранения откроется форма растения."));
  } else {
    openPlantForm();
    byId("plantForm").elements.name.focus();
  }
});
byId("showCalendar").addEventListener("click", showCalendarView);
byId("calendarRangeForm").addEventListener("submit", (event) => {
  event.preventDefault();
  loadCalendar();
});
byId("editGarden").addEventListener("click", () => {
  const garden = state.gardens.find((item) => item.id === state.selectedGardenId);
  if (garden) openGardenForm(garden);
});
byId("deleteGarden").addEventListener("click", async () => {
  const garden = state.gardens.find((item) => item.id === state.selectedGardenId);
  if (!garden || !window.confirm(t(`Удалить сад «${garden.name}» вместе со всеми растениями и историями?`))) return;
  const button = byId("deleteGarden");
  button.disabled = true;
  try {
    await api(`/api/v1/gardens/${garden.id}`, { method: "DELETE" });
    state.gardens = state.gardens.filter((item) => item.id !== garden.id);
    state.plants = state.plants.filter((item) => item.garden_id !== garden.id);
    state.selectedGardenId = state.gardens[0]?.id || null;
    state.selectedPlant = null;
    byId("gardenForm").classList.add("hidden");
    byId("plantForm").classList.add("hidden");
    renderGardens();
    renderPlants();
    showEmptyState();
    showToast("Сад удалён");
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; }
});
byId("editPlant").addEventListener("click", () => {
  if (state.selectedPlant) openPlantForm(state.selectedPlant);
});
byId("deletePlant").addEventListener("click", async () => {
  const plant = state.selectedPlant;
  if (!plant || !window.confirm(t(`Удалить растение «${plant.name}» вместе с фотографиями и историями?`))) return;
  const button = byId("deletePlant");
  button.disabled = true;
  try {
    await api(`/api/v1/plants/${plant.id}`, { method: "DELETE" });
    state.plants = state.plants.filter((item) => item.id !== plant.id);
    state.selectedPlant = null;
    byId("plantForm").classList.add("hidden");
    renderPlants();
    showEmptyState();
    showToast("Растение удалено");
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; }
});
byId("gardenForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    const values = formJson(form);
    const editing = state.editingGardenId;
    const openPlantAfterCreate = !editing && state.pendingPlantAfterGarden;
    const garden = await api(editing ? `/api/v1/gardens/${editing}` : "/api/v1/gardens", {
      method: editing ? "PATCH" : "POST",
      body: JSON.stringify({ ...values, location: values.location || null }),
    });
    if (editing) {
      state.gardens = state.gardens.map((item) => item.id === garden.id ? garden : item);
    } else {
      state.gardens.unshift(garden);
    }
    state.selectedGardenId = garden.id;
    state.editingGardenId = null;
    state.pendingPlantAfterGarden = false;
    form.reset();
    form.classList.add("hidden");
    renderGardens(); renderPlants();
    if (openPlantAfterCreate) {
      openPlantForm();
      byId("plantForm").elements.name.focus();
    }
    if (state.selectedPlant?.garden_id === garden.id) await loadWeather();
  } catch (error) { showToast(error.message); }
});
byId("plantForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    const values = formJson(form);
    const editing = state.editingPlantId;
    const payload = {
      ...values,
      species: values.species || null,
      taxon_id: values.taxon_id || null,
    };
    if (!editing) {
      payload.garden_id = state.selectedGardenId;
      payload.region = state.profile.region;
    }
    const plant = await api(editing ? `/api/v1/plants/${editing}` : "/api/v1/plants", {
      method: editing ? "PATCH" : "POST",
      body: JSON.stringify(payload),
    });
    if (editing) {
      state.plants = state.plants.map((item) => item.id === plant.id ? plant : item);
    } else {
      state.plants.unshift(plant);
    }
    state.editingPlantId = null;
    form.reset();
    form.classList.add("hidden");
    await selectPlant(plant);
  } catch (error) { showToast(error.message); }
});
byId("photoInput").addEventListener("change", (event) => renderPhotoSelection(event.target.files));
byId("refreshHistory").addEventListener("click", () => Promise.all([loadHistory(), loadCareEvents(), loadReminders()]));
byId("refreshWeather").addEventListener("click", loadWeather);
byId("careEventForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.selectedPlant) return;
  const form = event.currentTarget;
  const submit = form.querySelector("button[type='submit']");
  submit.disabled = true;
  try {
    const values = formJson(form);
    await api(`/api/v1/plants/${state.selectedPlant.id}/care-events`, {
      method: "POST",
      body: JSON.stringify({
        event_type: values.event_type,
        occurred_at: new Date(values.occurred_at).toISOString(),
        amount: values.amount ? Number(values.amount) : null,
        unit: values.unit || null,
        product: values.product || null,
        notes: values.notes || null,
      }),
    });
    form.reset();
    byId("careOccurredAt").value = localDateTimeValue(new Date());
    await loadCareEvents();
    showToast("Запись ухода добавлена");
  } catch (error) { showToast(error.message); }
  finally { submit.disabled = false; }
});
byId("reminderForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.selectedPlant) return;
  const form = event.currentTarget;
  const submit = form.querySelector("button[type='submit']");
  submit.disabled = true;
  try {
    const values = formJson(form);
    await api(`/api/v1/plants/${state.selectedPlant.id}/reminders`, {
      method: "POST",
      body: JSON.stringify({
        kind: values.kind,
        title: values.title,
        due_at: new Date(values.due_at).toISOString(),
        notes: values.notes || null,
        recurrence: values.recurrence || null,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        preferred_channel: values.preferred_channel || "web",
      }),
    });
    form.reset();
    byId("reminderDueAt").value = defaultReminderDate();
    await loadReminders();
    showToast("Напоминание добавлено");
  } catch (error) { showToast(error.message); }
  finally { submit.disabled = false; }
});
byId("diagnosisForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = byId("diagnoseButton");
  const message = byId("diagnosisMessage");
  const files = byId("photoInput").files;
  if (!state.selectedPlant || files.length < 1 || files.length > 5) {
    showToast("Выберите от 1 до 5 фотографий");
    return;
  }
  button.disabled = true;
  message.textContent = "Проверяем фотографии и выполняем анализ…";
  message.className = "form-message";
  try {
    const uploadBody = new FormData();
    [...files].forEach((file) => uploadBody.append("files", file));
    const photos = await api(`/api/v1/plants/${state.selectedPlant.id}/photos`, { method: "POST", body: uploadBody });
    const values = formJson(form);
    const job = await api("/api/v1/diagnoses/async", {
      method: "POST",
      body: JSON.stringify({
        plant_id: state.selectedPlant.id,
        symptoms: values.symptoms,
        damaged_part: values.damaged_part,
        photo_ids: photos.map((photo) => photo.id),
      }),
    });
    const diagnosis = await waitForDiagnosisJob(job, message);
    await renderResult(diagnosis);
    await loadHistory();
    message.textContent = diagnosis.result.input_status === "valid" ? "Анализ завершён" : "Нужны более подходящие данные";
  } catch (error) {
    message.textContent = `${error.message}${error.requestId ? ` · ${error.requestId}` : ""}`;
    message.className = "form-message error";
  } finally { button.disabled = false; }
});

async function boot() {
  try {
    const health = await api("/health");
    state.partnerCommerceEnabled = Boolean(health.partner_commerce_enabled);
    state.b2bInvoicingEnabled = Boolean(health.b2b_invoicing_enabled);
    const badge = byId("providerBadge");
    badge.textContent = health.demo_mode ? "Демонстрационный режим" : "Мультимодальный AI";
    badge.classList.remove("hidden");
  } catch (_) { /* health badge is non-critical */ }
  const actionParams = new URLSearchParams(window.location.hash.slice(1));
  const action = actionParams.get("action");
  const actionToken = actionParams.get("token");
  if (action === "verify-email" && actionToken) {
    try {
      await api("/api/v1/auth/email-verification/confirm", {
        method: "POST", body: JSON.stringify({ token: actionToken }),
      });
      window.history.replaceState({}, "", window.location.pathname + window.location.search);
      authError.textContent = "Email подтверждён. Теперь можно войти.";
      authError.className = "form-message";
    } catch (error) {
      authError.textContent = error.message;
      authError.className = "form-message error";
    }
  } else if (action === "reset-password" && actionToken) {
    byId("loginForm").classList.add("hidden");
    byId("registerForm").classList.add("hidden");
    document.querySelector(".segmented").classList.add("hidden");
    byId("passwordResetConfirmForm").classList.remove("hidden");
  }
  if (state.token) await enterApplication();
}

boot();
