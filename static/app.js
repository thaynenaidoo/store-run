const currency = new Intl.NumberFormat('en-NZ', { style: 'currency', currency: 'NZD' });
let csrfToken = '';
let appState = null;
let historyState = null;
let selectedHistoryMonth = '';
let toastTimer;

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const money = value => currency.format(Number(value) || 0);
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);

async function api(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  if (options.method && options.method !== 'GET') headers['X-CSRF-Token'] = csrfToken;
  const response = await fetch(url, { ...options, headers, body: options.body === undefined ? undefined : JSON.stringify(options.body) });
  const type = response.headers.get('content-type') || '';
  const result = type.includes('application/json') ? await response.json() : null;
  if (!response.ok) throw new Error(result?.error || `Request failed (${response.status}).`);
  return result;
}

async function boot() {
  try {
    const sessionState = await api('/api/session');
    csrfToken = sessionState.csrf_token;
    if (!sessionState.user) {
      showAuth(sessionState.needs_setup);
      return;
    }
    showApp(sessionState.user);
    await refreshState();
    await loadPricingStores();
  } catch (error) {
    showToast(error.message);
  }
}

function showAuth(needsSetup) {
  $('#app-shell').hidden = true;
  $('#auth-screen').hidden = false;
  $('#auth-title').innerHTML = needsSetup ? 'Make this place<br>your own.' : 'Your next run,<br>in good order.';
  $('#auth-eyebrow').textContent = needsSetup ? 'FIRST-TIME SETUP' : 'HOUSEHOLD ACCESS';
  $('#auth-copy').textContent = needsSetup
    ? 'Create the owner sign-in. You can add the rest of the household after.'
    : 'Sign in to see the shared list and latest prices.';
  $('#auth-name-wrap').hidden = !needsSetup;
  $('#auth-form [name="username"]').autocomplete = needsSetup ? 'username' : 'username';
  $('#auth-form [name="password"]').autocomplete = needsSetup ? 'new-password' : 'current-password';
  $('#auth-submit').innerHTML = needsSetup ? 'Create household <span aria-hidden="true">↗</span>' : 'Sign in <span aria-hidden="true">↗</span>';
  $('#auth-form').dataset.setup = String(needsSetup);
}

function showApp(user) {
  $('#auth-screen').hidden = true;
  $('#app-shell').hidden = false;
  $('#user-name').textContent = user.name;
  $('#user-dot').textContent = (user.name.trim()[0] || 'N').toUpperCase();
  $('#member-button').hidden = user.role !== 'admin';
  $('#household-button').hidden = user.role !== 'admin';
  $('#today-label').textContent = new Intl.DateTimeFormat('en-NZ', { weekday: 'short', day: 'numeric', month: 'short' }).format(new Date()).toUpperCase();
}

async function refreshState() {
  appState = await api('/api/state');
  renderAll();
}

function renderAll() {
  if (!appState) return;
  $('#household-name').textContent = appState.household?.name || 'Our Household';
  renderOverview();
  renderStoreLists();
  renderPricingSettings();
  renderShopping();
  renderCatalog();
  renderRecent();
  if (historyState) renderHistory();
}

function renderOverview() {
  const { stats, shopping, items } = appState;
  $('#metric-list').textContent = stats.shopping_count;
  $('#metric-estimate').textContent = money(stats.list_value);
  $('#metric-spend').textContent = money(stats.month_spend);
  $('#metric-catalog').textContent = stats.item_count;
  $('#nav-count').textContent = stats.shopping_count;
  $('#overview-empty').hidden = shopping.length > 0;
  $('#overview-shopping').hidden = shopping.length === 0;
  $('#overview-shopping').innerHTML = shopping.slice(0, 5).map(item => shoppingRow(item, false)).join('');
  $('#legacy-note').hidden = stats.legacy_count === 0;
  if (stats.legacy_count) {
    $('#legacy-note').textContent = `${stats.legacy_count} imported purchases · ${money(stats.legacy_total)}. Original month labels retained; no year was assigned.`;
  }
  renderChart(stats.monthly_spend, stats.legacy_monthly_spend);
}

function renderChart(rows, legacyRows) {
  const hasDatedSpend = rows.some(row => Number(row.total) > 0);
  const useLegacy = !hasDatedSpend && legacyRows.length > 0;
  if (useLegacy) {
    $('#period-tag').textContent = 'YEAR UNKNOWN';
    $('#chart-legend-copy').textContent = 'Imported month totals · year unknown';
    const max = Math.max(...legacyRows.map(row => Number(row.total) || 0), 1);
    $('#spend-chart').innerHTML = legacyRows.map(month => {
      const height = Math.max(8, Number(month.total) / max * 100);
      return `<div class="chart-column" title="${escapeHtml(month.month)} · year unknown: ${money(month.total)}"><div class="chart-bar-wrap"><div class="chart-bar" style="height:${height}%"></div></div><span class="chart-label">${escapeHtml(month.month.slice(0, 3))}</span></div>`;
    }).join('');
    return;
  }
  $('#period-tag').textContent = '6 MONTHS';
  $('#chart-legend-copy').textContent = 'Dated purchases';
  const today = new Date();
  const months = [];
  for (let offset = 5; offset >= 0; offset -= 1) {
    const monthDate = new Date(today.getFullYear(), today.getMonth() - offset, 1);
    const key = `${monthDate.getFullYear()}-${String(monthDate.getMonth() + 1).padStart(2, '0')}`;
    months.push({ key, label: new Intl.DateTimeFormat('en-NZ', { month: 'short' }).format(monthDate), total: 0 });
  }
  for (const row of rows) {
    const month = months.find(entry => entry.key === row.month);
    if (month) month.total = Number(row.total) || 0;
  }
  const max = Math.max(...months.map(month => month.total), 1);
  $('#spend-chart').innerHTML = months.map((month, index) => {
    const height = month.total > 0 ? Math.max(8, month.total / max * 100) : 3;
    return `<div class="chart-column" title="${escapeHtml(month.label)}: ${money(month.total)}"><div class="chart-bar-wrap"><div class="chart-bar ${index === months.length - 1 ? 'current' : ''}" style="height:${height}%"></div></div><span class="chart-label">${escapeHtml(month.label)}</span></div>`;
  }).join('');
}

function shoppingRow(item, includeStore = true) {
  const meta = [item.category, item.note].filter(Boolean).map(escapeHtml).join(' · ');
  const link = retailerLinkMarkup(item);
  const grocerLink = item.grocer_product_url ? grocerLinkMarkup(item) : '';
  return `<div class="shopping-row ${includeStore ? '' : 'overview-row'}" data-entry="${item.id}">
    <div class="shopping-row-main"><div class="item-name">${escapeHtml(item.name)}</div><div class="item-meta">${meta || 'Household regular'}</div></div>
    <div class="row-price">${money(item.unit_price * item.quantity)}</div>
    <div class="quantity-control"><button class="small-action" data-action="adjust-quantity" data-item="${item.item_id}" data-delta="-1" title="Reduce quantity" aria-label="Reduce ${escapeHtml(item.name)} quantity">−</button><span>${escapeHtml(item.quantity)}</span><button class="small-action" data-action="adjust-quantity" data-item="${item.item_id}" data-delta="1" title="Increase quantity" aria-label="Increase ${escapeHtml(item.name)} quantity">＋</button></div>
    <div class="row-actions">${includeStore ? `${grocerLink}${link}<button class="small-action" data-action="edit-item" data-item="${item.item_id}" title="Edit item" aria-label="Edit ${escapeHtml(item.name)}">✎</button>` : ''}<button class="small-action bought" data-action="bought" data-item="${item.item_id}" title="Mark purchased" aria-label="Mark ${escapeHtml(item.name)} purchased">✓</button><button class="small-action remove" data-action="remove-list" data-entry="${item.id}" title="Remove from list" aria-label="Remove ${escapeHtml(item.name)} from list">×</button></div>
  </div>`;
}

function retailerForStore(storeName) {
  const store = String(storeName || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const retailers = [
    { match: value => value === 'pak n save', domain: 'paknsave.co.nz', name: "PAK'nSAVE", mark: 'PNS' },
    { match: value => value.includes('chemist warehouse'), domain: 'chemistwarehouse.co.nz', name: 'Chemist Warehouse', mark: 'CW' },
    { match: value => value.includes('woolworths'), domain: 'woolworths.co.nz', name: 'Woolworths', mark: 'WW' },
    { match: value => value.includes('nespresso'), domain: 'nespresso.com', name: 'Nespresso', mark: 'N' },
    { match: value => value.includes('bostock'), domain: 'bostocksorganic.co.nz', name: "Bostock's Organic", mark: 'BO', action: 'Browse' },
  ];
  return retailers.find(entry => entry.match(store)) || null;
}

function storeLookupUrl(item) {
  const retailer = retailerForStore(item.store);
  if (item.product_url) {
    try {
      const url = new URL(item.product_url);
      if (url.protocol === 'https:' && (!retailer || url.hostname === retailer.domain || url.hostname.endsWith(`.${retailer.domain}`))) return url.href;
    } catch {
      // Use the retailer's own search for invalid saved URLs.
    }
  }
  if (retailer?.domain === 'paknsave.co.nz') return paknsaveItemUrl(item);
  if (retailer?.domain === 'chemistwarehouse.co.nz') return `https://www.chemistwarehouse.co.nz/search?searchtext=${encodeURIComponent(item.name)}`;
  if (retailer?.domain === 'woolworths.co.nz') return `https://www.woolworths.co.nz/shop/searchproducts?search=${encodeURIComponent(item.name)}`;
  if (retailer?.domain === 'nespresso.com') return `https://www.nespresso.com/nz/en/search?query=${encodeURIComponent(item.name)}`;
  if (retailer?.domain === 'bostocksorganic.co.nz') return 'https://bostocksorganic.co.nz/shop/';
  const query = encodeURIComponent(`${item.name} ${item.store || ''}`);
  return `https://www.google.com/search?q=${query}`;
}

function retailerLinkMarkup(item) {
  const retailer = retailerForStore(item.store);
  const destination = retailer?.name || item.store || 'retailer';
  let productLink = Boolean(item.product_url && !retailer);
  if (item.product_url && retailer) {
    try {
      const url = new URL(item.product_url);
      productLink = url.protocol === 'https:' && (url.hostname === retailer.domain || url.hostname.endsWith(`.${retailer.domain}`));
    } catch {
      productLink = false;
    }
  }
  const markClass = `mark-${retailer?.mark.toLowerCase() || 'other'}`;
  const action = productLink ? 'Open' : (retailer?.action || 'Search');
  return `<a class="small-action link-out retailer-link" href="${escapeHtml(storeLookupUrl(item))}" target="_blank" rel="noopener" title="${action} ${escapeHtml(item.name)} at ${escapeHtml(destination)}" aria-label="${action} ${escapeHtml(item.name)} at ${escapeHtml(destination)}"><span aria-hidden="true" class="retailer-mark ${markClass}">${retailer?.mark || '↗'}</span></a>`;
}

function grocerLinkMarkup(item) {
  const url = item.grocer_product_url || `https://grocer.nz/search?term=${encodeURIComponent(item.name)}&offset=0&category=`;
  const label = item.grocer_product_url ? 'Open matched Grocer product' : 'Search this item on Grocer';
  return `<a class="small-action link-out grocer-link" href="${escapeHtml(url)}" target="_blank" rel="noopener" title="${label} for ${escapeHtml(item.name)}" aria-label="${label} for ${escapeHtml(item.name)}"><span aria-hidden="true" class="retailer-mark">G</span></a>`;
}

function renderShopping() {
  const { shopping, items } = appState;
  const query = ($('#shopping-search')?.value || '').trim().toLowerCase();
  const matches = shopping.filter(item => `${item.name} ${item.store} ${item.category} ${item.note || ''}`.toLowerCase().includes(query));
  const grouped = new Map();
  for (const item of matches) {
    const store = item.store || 'Unassigned';
    if (!grouped.has(store)) grouped.set(store, []);
    grouped.get(store).push(item);
  }
  $('#full-shopping').innerHTML = [...grouped.entries()].map(([store, list]) => {
    const total = list.reduce((sum, item) => sum + item.unit_price * item.quantity, 0);
    const categories = new Map([
      ['Beverages', []],
      ['Food', []],
      ['Household', []],
    ]);
    for (const item of list) {
      const category = storeListCategory(item.category);
      categories.get(category).push(item);
    }
    const sections = [...categories.entries()].map(([category, categoryItems]) => {
      const categoryTotal = categoryItems.reduce((sum, item) => sum + item.unit_price * item.quantity, 0);
      return `<section class="shopping-category"><div class="shopping-category-heading"><h3>${category}</h3><span>${categoryItems.length} ${categoryItems.length === 1 ? 'ITEM' : 'ITEMS'}</span><strong>${money(categoryTotal)}</strong></div>${categoryItems.map(item => shoppingRow(item)).join('')}</section>`;
    }).join('');
    return `<section class="store-group"><div class="store-group-heading"><h2>${escapeHtml(store)}</h2><span>${list.length} ${list.length === 1 ? 'ITEM' : 'ITEMS'}</span></div>${sections}<div class="store-total">STORE ESTIMATE <strong>${money(total)}</strong></div></section>`;
  }).join('');
  $('#shopping-empty').hidden = shopping.length > 0;
  $('#full-shopping').hidden = matches.length === 0;
  $('#shopping-search-empty').hidden = shopping.length === 0 || matches.length > 0;
  $('#shopping-search-count').textContent = query ? `${matches.length} of ${shopping.length} items` : `${shopping.length} ${shopping.length === 1 ? 'ITEM' : 'ITEMS'}`;
  $('#shopping-total').textContent = money(shopping.reduce((sum, item) => sum + item.unit_price * item.quantity, 0));
  const select = $('#add-item-select');
  const previous = select.value;
  select.innerHTML = '<option value="">Choose an item…</option>' + items.map(item => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.store)}</option>`).join('');
  if ([...select.options].some(option => option.value === previous)) select.value = previous;
  $('#add-selected-button').disabled = items.length === 0;
}

function storeListCategory(category) {
  if (String(category).toLowerCase() === 'beverages') return 'Beverages';
  if (String(category).toLowerCase() === 'household') return 'Household';
  return 'Food';
}

function paknsaveItemUrl(item) {
  try {
    const savedUrl = new URL(item.product_url);
    const paknsaveHost = savedUrl.hostname === 'paknsave.co.nz' || savedUrl.hostname.endsWith('.paknsave.co.nz');
    if (savedUrl.protocol === 'https:' && paknsaveHost && savedUrl.pathname.startsWith('/shop/product/')) {
      return savedUrl.href;
    }
  } catch {
    // Missing or non-URL product links use the retailer's search instead.
  }
  return `https://www.paknsave.co.nz/shop/search?pg=1&q=${encodeURIComponent(item.name)}`;
}

function renderStoreLists() {
  const grouped = new Map();
  for (const item of appState.items) {
    const store = item.store || 'Unassigned';
    if (!grouped.has(store)) grouped.set(store, []);
    grouped.get(store).push(item);
  }
  const select = $('#fixed-store-select');
  const previous = select.value;
  const priority = ["Pak 'n Save", "Chemist Warehouse"];
  const stores = [...grouped.keys()].sort((left, right) => {
    const leftRank = priority.indexOf(left);
    const rightRank = priority.indexOf(right);
    if (leftRank !== -1 || rightRank !== -1) {
      return (leftRank === -1 ? priority.length : leftRank) - (rightRank === -1 ? priority.length : rightRank);
    }
    return left.localeCompare(right);
  });
  select.innerHTML = stores.map(store => `<option value="${escapeHtml(store)}">${escapeHtml(store)} · ${grouped.get(store).length} items</option>`).join('');
  if (stores.includes(previous)) select.value = previous;
  else if (stores.includes("Pak 'n Save")) select.value = "Pak 'n Save";
  const store = select.value;
  const items = grouped.get(store) || [];
  const query = ($('#fixed-list-search')?.value || '').trim().toLowerCase();
  const visibleItems = items.filter(item => `${item.name} ${item.category} ${item.store}`.toLowerCase().includes(query));
  $('#paknsave-handoff-button').hidden = store !== "Pak 'n Save";
  const totalQuantity = items.reduce((sum, item) => sum + Number(item.forecast_qty || 0), 0);
  const total = items.reduce((sum, item) => sum + Number(item.forecast_qty || 0) * Number(item.unit_price || 0), 0);
  $('#fixed-item-count').textContent = items.length;
  $('#fixed-quantity-count').textContent = Number.isInteger(totalQuantity) ? totalQuantity : totalQuantity.toFixed(1);
  $('#fixed-store-total').textContent = money(total);
  $('#fixed-list-empty').hidden = appState.items.length > 0;
  $('#fixed-list-table').hidden = items.length === 0 || visibleItems.length === 0;
  $('#fixed-list-search-empty').hidden = items.length === 0 || visibleItems.length > 0;
  $('#fixed-list-search-count').textContent = query ? `${visibleItems.length} of ${items.length} items` : `${items.length} ${items.length === 1 ? 'ITEM' : 'ITEMS'}`;
  $('#use-store-list-button').disabled = items.length === 0 || !items.some(item => Number(item.forecast_qty) > 0);
  const categories = new Map([
    ['Beverages', []],
    ['Food', []],
    ['Household', []],
  ]);
  for (const item of visibleItems) categories.get(storeListCategory(item.category)).push(item);
  $('#fixed-list-table').innerHTML = [...categories.entries()].filter(([, groupItems]) => groupItems.length > 0).map(([category, groupItems]) => {
    const categoryTotal = groupItems.reduce((sum, item) => sum + Number(item.forecast_qty || 0) * Number(item.unit_price || 0), 0);
    const rows = groupItems.map(item => {
      const sourceCategory = storeListCategory(item.category) === item.category ? '' : item.category;
      const grocerLink = grocerLinkMarkup(item);
      const retailerLink = retailerLinkMarkup(item);
      return `<article class="fixed-list-row ${Number(item.forecast_qty) <= 0 ? 'zero-quantity' : ''}" data-fixed-item="${item.id}">
        <div class="fixed-item-main"><div class="item-name">${escapeHtml(item.name)}</div><div class="fixed-list-category">${escapeHtml(sourceCategory)}</div></div>
        <label class="fixed-quantity"><span class="visually-hidden">Usual quantity for ${escapeHtml(item.name)}</span><input type="number" min="0" max="100000" step="1" value="${escapeHtml(item.forecast_qty)}" data-fixed-quantity="${item.id}" aria-label="Usual quantity for ${escapeHtml(item.name)}"><span>qty</span></label>
        <div class="fixed-list-unit">${money(item.unit_price)} each</div>
        <div class="fixed-line-total">${money(Number(item.forecast_qty) * Number(item.unit_price))}</div>
        <div class="fixed-list-actions">${grocerLink}${retailerLink}<button class="small-action" data-action="add-list" data-item="${item.id}" title="Add usual quantity to Store Run list" aria-label="Add ${escapeHtml(item.name)} to Store Run list">＋</button><button class="small-action" data-action="edit-item" data-item="${item.id}" title="Edit item" aria-label="Edit ${escapeHtml(item.name)}">✎</button></div>
      </article>`;
    }).join('');
    return `<section class="fixed-category-group"><header class="fixed-category-heading"><h2>${category}</h2><span>${groupItems.length} ${groupItems.length === 1 ? 'ITEM' : 'ITEMS'}</span><strong>${money(categoryTotal)}</strong></header>${rows}</section>`;
  }).join('');
}

function renderPricingSettings() {
  const { store_id: selectedId, store_name: selectedName, refreshes } = appState.pricing;
  const refresh = refreshes.find(entry => Number(entry.store_id) === Number(selectedId));
  const activeStore = $('#fixed-store-select')?.value;
  const retailerRefresh = appState.items
    .filter(item => item.store === activeStore && item.price_refreshed_at)
    .sort((left, right) => right.price_refreshed_at.localeCompare(left.price_refreshed_at))[0];
  const latestRefresh = retailerRefresh && (!refresh || retailerRefresh.price_refreshed_at > refresh.refreshed_at)
    ? { refreshed_at: retailerRefresh.price_refreshed_at, source: retailerRefresh.price_refreshed_store_name }
    : refresh && { refreshed_at: refresh.refreshed_at, source: selectedName };
  $('#price-refresh-time').textContent = latestRefresh
    ? `Last refreshed from ${latestRefresh.source} · ${new Intl.DateTimeFormat('en-NZ', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(latestRefresh.refreshed_at))}`
    : `Retailer product pages · Grocer reference: ${selectedName || 'PAK’nSAVE Highland Park'}`;
}

async function loadPricingStores() {
  try {
    const result = await api('/api/pricing/stores');
    const highlandPark = result.stores.find(store => /highland park/i.test(store.name));
    const defaultStore = highlandPark || result.stores[0];
    if (!defaultStore) return;
    if (Number(appState?.pricing.store_id) !== Number(defaultStore.id)
      || appState?.pricing.store_name !== defaultStore.name) {
      await savePricingStore(defaultStore);
    }
    renderPricingSettings();
  } catch (error) {
    showToast(error.message);
  }
}

async function savePricingStore(store) {
  try {
    await api('/api/pricing/store', {
      method: 'PUT',
      body: { store_id: Number(store.id), store_name: store.name },
    });
    if (appState) {
      appState.pricing.store_id = Number(store.id);
      appState.pricing.store_name = store.name;
      renderPricingSettings();
    }
  } catch (error) {
    showToast(error.message);
  }
}

async function refreshPrices() {
  const button = $('#refresh-prices-button');
  button.disabled = true;
  button.innerHTML = '<span aria-hidden="true">↻</span> Checking…';
  try {
    const result = await api('/api/pricing/refresh', {
      method: 'POST',
      body: { store: $('#fixed-store-select').value },
    });
    await refreshState();
    showToast(`${result.updated.length} prices updated; ${result.unmatched.length} need a product link or review.`);
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.innerHTML = '<span aria-hidden="true">↻</span> Update prices';
  }
}

async function searchGrocerProducts() {
  const query = $('#grocer-product-search').value.trim();
  if (query.length < 2) return showToast('Enter at least two characters.');
  const button = $('#grocer-product-search-button');
  const results = $('#grocer-search-results');
  button.disabled = true;
  button.textContent = 'Searching…';
  results.innerHTML = '';
  try {
    const response = await api(`/api/pricing/search?q=${encodeURIComponent(query)}`);
    results.innerHTML = response.results.map(product => `<button class="grocer-search-result" type="button" data-action="select-grocer-product" data-product-id="${product.id}" data-product-name="${escapeHtml(product.name)}" data-product-url="${escapeHtml(product.url)}" data-product-price="${product.price ?? ''}"><strong>${escapeHtml(product.name)}</strong><span>${escapeHtml(product.size || product.brand || '')}</span><span>${product.price === null ? 'No Highland Park price' : money(product.price)}</span></button>`).join('') || '<p class="empty-inline">No products found.</p>';
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = 'Search Grocer';
  }
}

async function saveUsualQuantity(input) {
  const item = appState.items.find(entry => entry.id === Number(input.dataset.fixedQuantity));
  if (!item) return;
  const quantity = Number(input.value);
  if (!Number.isInteger(quantity) || quantity < 0 || quantity > 100000) {
    input.value = item.forecast_qty;
    return showToast('Enter a whole-number quantity from 0 to 100000.');
  }
  if (quantity === Number(item.forecast_qty)) return;
  try {
    await api(`/api/items/${item.id}`, { method: 'PUT', body: { ...item, forecast_qty: quantity } });
    await refreshState();
    showToast('Usual store quantity updated.');
  } catch (error) {
    showToast(error.message);
    input.value = item.forecast_qty;
  }
}

async function useStoreList() {
  const store = $('#fixed-store-select').value;
  if (!store) return;
  try {
    const result = await api('/api/shopping/store', { method: 'POST', body: { store } });
    await refreshState();
    showToast(`${result.added} ${result.added === 1 ? 'item' : 'items'} from ${store} added to this run.`);
    switchView('shopping');
  } catch (error) {
    showToast(error.message);
  }
}

async function handoffPaknSaveList(event) {
  const store = $('#fixed-store-select').value;
  if (store !== "Pak 'n Save") {
    event.preventDefault();
    return;
  }
  const items = appState.items.filter(item => item.store === store && Number(item.forecast_qty) > 0);
  if (!items.length) {
    event.preventDefault();
    return showToast('This Pak’n Save list has no items with a positive quantity.');
  }
  const lines = [
    "PAK'nSAVE Highland Park shopping list",
    `Prepared ${new Intl.DateTimeFormat('en-NZ', { dateStyle: 'medium' }).format(new Date())}`,
    '',
    ...items.map(item => `${item.forecast_qty} x ${item.name}  |  ${money(Number(item.forecast_qty) * Number(item.unit_price))}`),
    '',
    `Estimated total: ${money(items.reduce((sum, item) => sum + Number(item.forecast_qty) * Number(item.unit_price), 0))}`,
  ];
  try {
    await navigator.clipboard.writeText(lines.join('\n'));
    showToast(`Copied ${items.length} items. Pak’n Save opened in a new tab; add the items to your account cart there.`);
  } catch (_error) {
    showToast('Pak’n Save opened. Clipboard access was blocked; copy the list manually from Store lists.');
  }
}

function renderCatalog() {
  const query = ($('#catalog-search').value || '').trim().toLowerCase();
  const items = appState.items.filter(item => `${item.name} ${item.store} ${item.category}`.toLowerCase().includes(query));
  $('#catalog-count').textContent = `${items.length} ${items.length === 1 ? 'ITEM' : 'ITEMS'}`;
  $('#catalog-list').hidden = items.length === 0;
  $('#catalog-empty').hidden = appState.items.length > 0;
  if (appState.items.length > 0 && items.length === 0) {
    $('#catalog-list').innerHTML = '<div class="empty-inline">No catalog items match that search.</div>';
    $('#catalog-list').hidden = false;
    return;
  }
  $('#catalog-list').innerHTML = items.map(item => `<article class="catalog-row">
    <div class="catalog-main"><div class="item-name">${escapeHtml(item.name)}</div><div class="catalog-category">${escapeHtml(item.category)}</div></div>
    <div class="catalog-store">${escapeHtml(item.store)}</div>
    <div class="catalog-qty">Usual qty ${escapeHtml(item.forecast_qty)}</div>
    <div class="catalog-price">${money(item.unit_price)} <span class="item-meta">/ ea</span></div>
    <div class="catalog-actions">${retailerLinkMarkup(item)}<button class="small-action" data-action="add-list" data-item="${item.id}" title="Add to shopping list" aria-label="Add ${escapeHtml(item.name)} to shopping list">＋</button><button class="small-action" data-action="edit-item" data-item="${item.id}" title="Edit item" aria-label="Edit ${escapeHtml(item.name)}">✎</button></div>
  </article>`).join('');
}

function renderRecent() {
  const recent = appState.recent;
  $('#purchases-empty').hidden = recent.length > 0;
  $('.purchase-table-wrap').hidden = recent.length === 0;
  $('#recent-purchases').innerHTML = recent.map(item => {
    const when = item.purchase_date
      ? new Intl.DateTimeFormat('en-NZ', { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(`${item.purchase_date}T12:00:00`))
      : `${escapeHtml(item.source_period)} · year unknown`;
    return `<tr><td>${escapeHtml(item.item_name)}<div class="item-meta">Qty ${escapeHtml(item.quantity)} × ${money(item.unit_price)}</div></td><td>${escapeHtml(item.store || '—')}</td><td>${when}</td><td class="align-right">${money(item.total)}</td><td class="purchase-delete-cell"><button class="small-action purchase-delete" data-action="delete-purchase" data-purchase="${item.id}" title="Remove this purchase" aria-label="Remove purchase of ${escapeHtml(item.item_name)}">×</button></td></tr>`;
  }).join('');
}

async function loadHistory() {
  try {
    historyState = await api('/api/history');
    renderHistory();
  } catch (error) {
    showToast(error.message);
  }
}

function renderHistory() {
  if (!historyState) return;
  const { months, categories, stores, highlights, undated_legacy: undated } = historyState;
  $('#history-range').textContent = `${new Intl.DateTimeFormat('en-NZ', { month: 'long', year: 'numeric' }).format(new Date(`${months[0].month}-01T12:00:00`))} to ${new Intl.DateTimeFormat('en-NZ', { month: 'long', year: 'numeric' }).format(new Date(`${months.at(-1).month}-01T12:00:00`))}`;
  $('#history-total').textContent = money(highlights.total_spend);
  $('#history-average').textContent = money(highlights.average_active_month);
  $('#history-purchase-count').textContent = highlights.purchase_count;
  $('#history-undated-total').textContent = money(undated.total);
  $('#history-undated-count').textContent = `${undated.count} entries · year unknown`;
  const maxMonth = Math.max(...months.map(month => Number(month.total)), 0);
  $('#history-chart').innerHTML = months.map(month => {
    const foodHeight = maxMonth ? Number(month.food) / maxMonth * 100 : 0;
    const householdHeight = maxMonth ? Number(month.household) / maxMonth * 100 : 0;
    const label = new Intl.DateTimeFormat('en-NZ', { month: 'short' }).format(new Date(`${month.month}-01T12:00:00`));
    return `<div class="history-month-column" title="${escapeHtml(month.label)}: ${money(month.total)} · Food ${money(month.food)} · Household ${money(month.household)}"><div class="history-stack"><span class="history-segment food" style="height:${foodHeight}%"></span><span class="history-segment household" style="height:${householdHeight}%"></span></div><span class="history-month-label">${label}</span></div>`;
  }).join('');
  const nonzeroCategories = categories.filter(row => row.total > 0);
  const categoryMax = Math.max(...nonzeroCategories.map(row => row.total), 1);
  $('#history-categories').innerHTML = nonzeroCategories.length
    ? nonzeroCategories.map(row => `<div class="history-bar-row"><span class="history-bar-label">${escapeHtml(row.category)}</span><div class="history-bar-track"><div class="history-bar-fill ${row.category === 'Household' ? 'household-fill' : ''}" style="width:${row.total / categoryMax * 100}%"></div></div><strong class="history-bar-value">${money(row.total)} <span>${row.share}%</span></strong></div>`).join('')
    : '<div class="empty-inline">No dated spending yet.</div>';
  const storeMax = Math.max(...stores.map(row => row.total), 1);
  $('#history-stores').innerHTML = stores.length
    ? stores.map(row => `<div class="history-bar-row"><span class="history-bar-label">${escapeHtml(row.store)}${row.unknown_amount_count ? `<small class="unknown-amount-note">${row.unknown_amount_count} amount${row.unknown_amount_count === 1 ? '' : 's'} unknown</small>` : ''}</span><div class="history-bar-track"><div class="history-bar-fill" style="width:${row.total / storeMax * 100}%"></div></div><strong class="history-bar-value">${money(row.total)} <span>${row.share}%</span></strong></div>`).join('')
    : '<div class="empty-inline">No dated spending yet.</div>';
  $('#history-months').innerHTML = months.slice().reverse().map(month => {
    const current = month.month === months.at(-1).month ? 'current-month' : '';
    return `<tr class="${current}"><td><button class="history-month-trigger" data-action="view-history-month" data-month="${month.month}">${escapeHtml(month.label)}</button></td><td class="align-right">${money(month.food)}</td><td class="align-right">${money(month.household)}</td><td class="align-right"><strong>${money(month.total)}</strong>${month.unknown_amount_count ? `<div class="unknown-amount-note">${month.unknown_amount_count} unknown amount${month.unknown_amount_count === 1 ? '' : 's'}</div>` : ''}</td></tr>`;
  }).join('');
  $('#history-purchases-empty').hidden = historyState.purchases.length > 0;
  $('#history-purchases').innerHTML = historyState.purchases.slice().reverse().map(purchase => {
    const when = new Intl.DateTimeFormat('en-NZ', { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(`${purchase.purchase_date}T12:00:00`));
    return `<tr><td>${escapeHtml(purchase.item_name)}<div class="item-meta">Qty ${escapeHtml(purchase.quantity)} × ${money(purchase.unit_price)}</div></td><td>${escapeHtml(purchase.category)}</td><td>${escapeHtml(purchase.store || '—')}</td><td>${when}</td><td class="align-right">${money(purchase.total)}</td><td class="purchase-delete-cell"><button class="small-action purchase-delete" data-action="delete-purchase" data-purchase="${purchase.id}" title="Remove this purchase" aria-label="Remove purchase of ${escapeHtml(purchase.item_name)}">×</button></td></tr>`;
  }).join('');
  $('#manual-spend-empty').hidden = historyState.manual_spend.length > 0;
  $('#manual-spend-rows').innerHTML = historyState.manual_spend.map(entry => {
    const month = new Intl.DateTimeFormat('en-NZ', { month: 'short', year: 'numeric' }).format(new Date(`${entry.month}-01T12:00:00`));
    const description = [entry.store, entry.detail, entry.note].filter(Boolean).map(escapeHtml).join(' · ');
    return `<tr><td>${month}</td><td>${escapeHtml(entry.category)}</td><td>${description || '—'}</td><td class="align-right">${entry.amount === null ? '<span class="amount-unknown">Not recorded</span>' : money(entry.amount)}</td><td class="purchase-delete-cell"><button class="small-action purchase-delete" data-action="delete-manual-spend" data-entry="${entry.id}" title="Remove store run entry" aria-label="Remove ${escapeHtml(entry.category)} spend at ${escapeHtml(entry.store)} for ${month}">×</button></td></tr>`;
  }).join('');
  $('#history-empty').hidden = highlights.purchase_count > 0;
  $('#history-legacy-note').hidden = undated.count === 0;
  if (undated.count) {
    $('#history-legacy-note').textContent = `${undated.count} imported purchases totaling ${money(undated.total)} have month labels but no year. They are kept separate and not included in this dated 12-month report.`;
  }
  const highlightRows = [];
  if (highlights.top_month) highlightRows.push(['Biggest month', `${highlights.top_month.label} · ${money(highlights.top_month.total)}`]);
  if (highlights.top_store) highlightRows.push(['Top store', `${highlights.top_store.store} · ${money(highlights.top_store.total)}`]);
  if (highlights.largest_purchase) highlightRows.push(['Largest purchase', `${highlights.largest_purchase.name} · ${money(highlights.largest_purchase.total)}`]);
  if (highlights.most_repeated_item) highlightRows.push(['Most repeated item', `${highlights.most_repeated_item.name} · ${highlights.most_repeated_item.count} shops`]);
  if (highlights.active_months) highlightRows.push(['Shopping cadence', `${highlights.active_months} active months out of 12`]);
  if (!highlightRows.length) highlightRows.push(['Getting started', 'Dated purchase patterns will appear here.']);
  $('#history-highlights').innerHTML = highlightRows.map(([label, value]) => `<div class="highlight-row"><span class="highlight-label">${escapeHtml(label)}</span><strong class="highlight-value">${escapeHtml(value)}</strong></div>`).join('');
}

function openHistoryMonth(monthKey) {
  selectedHistoryMonth = monthKey;
  const monthPurchases = historyState.purchases.filter(purchase => purchase.purchase_date?.slice(0, 7) === monthKey);
  const monthManualSpend = historyState.manual_spend.filter(entry => entry.month === monthKey);
  const manualGroups = new Map();
  for (const entry of monthManualSpend) {
    const key = `${entry.category}\u0000${entry.store}\u0000${entry.detail || 'General'}`;
    const group = manualGroups.get(key) || {
      category: entry.category,
      store: entry.store,
      detail: entry.detail || 'General',
      amount: 0,
      unknownAmountCount: 0,
      notes: [],
    };
    if (entry.amount === null) group.unknownAmountCount += 1;
    else group.amount += Number(entry.amount);
    if (entry.note && !group.notes.includes(entry.note)) group.notes.push(entry.note);
    manualGroups.set(key, group);
  }
  const monthLabel = new Intl.DateTimeFormat('en-NZ', { month: 'long', year: 'numeric' }).format(new Date(`${monthKey}-01T12:00:00`));
  $('#history-month-title').textContent = monthLabel;
  $('#history-month-purchase-empty').hidden = monthPurchases.length > 0;
  $('#history-month-purchases').innerHTML = monthPurchases.slice().reverse().map(purchase => {
    const when = new Intl.DateTimeFormat('en-NZ', { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(`${purchase.purchase_date}T12:00:00`));
    return `<article class="history-purchase-card"><div class="history-purchase-main"><strong>${escapeHtml(purchase.item_name)}</strong><span>${escapeHtml(when)} · ${escapeHtml(purchase.store || '—')} · ${escapeHtml(purchase.category)}</span><span>Qty ${escapeHtml(purchase.quantity)} · Total <strong>${money(purchase.total)}</strong></span></div><label class="history-price-field">Unit price<input type="number" min="0" step="0.01" required value="${Number(purchase.unit_price).toFixed(2)}" data-purchase-price="${purchase.id}" aria-label="Unit price for ${escapeHtml(purchase.item_name)}"></label><div class="history-purchase-actions"><button class="button button-quiet" data-action="update-purchase-price" data-purchase="${purchase.id}">Save price</button><button class="small-action purchase-delete" data-action="delete-purchase" data-purchase="${purchase.id}" title="Remove this purchase" aria-label="Remove purchase of ${escapeHtml(purchase.item_name)}">×</button></div></article>`;
  }).join('');
  $('#history-month-manual').innerHTML = monthManualSpend.length
    ? `<div class="history-manual-summary"><strong>Store runs &amp; offline purchases</strong>${[...manualGroups.values()].map(group => `<div class="history-manual-entry"><span><b>${escapeHtml(group.category)}</b> · ${escapeHtml(group.store)} · ${escapeHtml(group.detail)}${group.notes.length ? `<small>${group.notes.map(escapeHtml).join(' · ')}</small>` : ''}</span><strong>${group.amount ? money(group.amount) : ''}${group.unknownAmountCount ? `<small>${group.unknownAmountCount} amount${group.unknownAmountCount === 1 ? '' : 's'} not recorded</small>` : ''}${!group.amount && !group.unknownAmountCount ? money(0) : ''}</strong></div>`).join('')}</div>`
    : '';
  $('#history-month-modal').showModal();
}

function switchView(viewName) {
  $$('.view').forEach(view => view.classList.toggle('active', view.id === `view-${viewName}`));
  $$('.nav-link').forEach(link => link.classList.toggle('active', link.dataset.view === viewName));
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function showToast(message) {
  const toast = $('#toast');
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 2600);
}

function openItemModal(item = null) {
  const form = $('#item-form');
  form.reset();
  $('#item-message').textContent = '';
  $('#item-modal-title').textContent = item ? 'Edit an item' : 'Add an item';
  $('#grocer-match-target').textContent = item ? `Matching Grocer product for: ${item.name}` : 'Save the new item before assigning a Grocer product.';
  $('#grocer-product-search').value = item?.name || '';
  $('#grocer-search-results').innerHTML = '';
  $('#item-refresh-message').textContent = '';
  $('#item-refresh-message').classList.remove('error');
  form.elements.id.value = item?.id || '';
  form.elements.name.value = item?.name || '';
  form.elements.category.value = item?.category || '';
  form.elements.store.value = item?.store || '';
  form.elements.forecast_qty.value = item?.forecast_qty ?? 1;
  form.elements.unit_price.value = item?.unit_price ?? 0;
  form.elements.product_url.value = item?.product_url || '';
  form.elements.grocer_product_id.value = item?.grocer_product_id || '';
  form.elements.grocer_product_url.value = item?.grocer_product_url || '';
  const refreshButton = $('#refresh-item-price-button');
  refreshButton.dataset.itemId = item?.id || '';
  refreshButton.disabled = !item?.id || !(item?.grocer_product_id || item?.grocer_product_url || item?.product_url);
  const retailerLink = $('#open-retailer-product');
  retailerLink.hidden = !isPaknsaveProductUrl(item?.product_url);
  retailerLink.href = retailerLink.hidden ? '#' : item.product_url;
  $('#item-price-refresh-time').textContent = item?.price_refreshed_at
    ? `Last refreshed${item.price_refreshed_store_name ? ` from ${item.price_refreshed_store_name}` : ''}: ${new Intl.DateTimeFormat('en-NZ', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(item.price_refreshed_at))}`
    : item?.grocer_product_id || item?.grocer_product_url || item?.product_url
      ? 'This item has not been refreshed yet.'
      : 'Save a Grocer or retailer product link to enable refresh.';
  $('#item-modal').showModal();
}

function isPaknsaveProductUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:'
      && (url.hostname === 'paknsave.co.nz' || url.hostname.endsWith('.paknsave.co.nz'))
      && url.pathname.startsWith('/shop/product/');
  } catch {
    return false;
  }
}

async function submitAuth(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  $('#auth-message').textContent = '';
  try {
    const endpoint = form.dataset.setup === 'true' ? '/api/setup' : '/api/login';
    await api(endpoint, { method: 'POST', body: data });
    const sessionState = await api('/api/session');
    csrfToken = sessionState.csrf_token;
    showApp(sessionState.user);
    await refreshState();
    await loadPricingStores();
  } catch (error) {
    $('#auth-message').textContent = error.message;
  }
}

async function refreshOneItemPrice() {
  const button = $('#refresh-item-price-button');
  const itemId = Number(button.dataset.itemId);
  if (!itemId || button.disabled) return;
  const feedback = $('#item-refresh-message');
  feedback.textContent = '';
  feedback.classList.remove('error');
  button.disabled = true;
  button.innerHTML = '<span aria-hidden="true">↻</span> Updating…';
  try {
    const result = await api(`/api/items/${itemId}/refresh-price`, { method: 'POST', body: {} });
    const item = appState.items.find(entry => entry.id === itemId);
    if (item) {
      item.unit_price = result.price;
      item.price_refreshed_at = result.refreshed_at;
      item.price_refreshed_store_id = appState.pricing.store_id;
      item.price_refreshed_store_name = result.store_name;
    }
    $('#item-form').elements.unit_price.value = result.price.toFixed(2);
    $('#item-price-refresh-time').textContent = `Last refreshed from ${result.store_name}: ${new Intl.DateTimeFormat('en-NZ', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(result.refreshed_at))}`;
    renderStoreLists();
    renderCatalog();
    feedback.textContent = `${result.name} updated to ${money(result.price)}.`;
  } catch (error) {
    feedback.textContent = error.message;
    feedback.classList.add('error');
  } finally {
    button.disabled = false;
    button.innerHTML = '<span aria-hidden="true">↻</span> Update this item’s price';
  }
}

async function submitItem(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  const id = data.id;
  delete data.id;
  data.forecast_qty = Number(data.forecast_qty);
  data.unit_price = Number(data.unit_price);
  data.grocer_product_id = data.grocer_product_id ? Number(data.grocer_product_id) : null;
  $('#item-message').textContent = '';
  try {
    await api(id ? `/api/items/${id}` : '/api/items', { method: id ? 'PUT' : 'POST', body: data });
    $('#item-modal').close();
    await refreshState();
    showToast(id ? 'Catalog item updated.' : 'Catalog item added.');
  } catch (error) {
    $('#item-message').textContent = error.message;
  }
}

async function submitMember(event) {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget));
  $('#member-message').textContent = '';
  try {
    await api('/api/members', { method: 'POST', body: data });
    $('#member-modal').close();
    event.currentTarget.reset();
    showToast('Household sign-in created.');
  } catch (error) {
    $('#member-message').textContent = error.message;
  }
}

async function submitManualSpend(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  data.amount = data.amount.trim() ? Number(data.amount) : null;
  $('#manual-spend-message').textContent = '';
  try {
    await api('/api/manual-spend', { method: 'POST', body: data });
    $('#manual-spend-modal').close();
    form.reset();
    await loadHistory();
    await refreshState();
    showToast('Store run added to History.');
  } catch (error) {
    $('#manual-spend-message').textContent = error.message;
  }
}

function openManualSpendModal() {
  const form = $('#manual-spend-form');
  form.reset();
  form.elements.month.value = new Date().toISOString().slice(0, 7);
  $('#manual-spend-message').textContent = '';
  $('#manual-spend-modal').showModal();
}

async function submitHousehold(event) {
  event.preventDefault();
  const form = event.currentTarget;
  $('#household-message').textContent = '';
  try {
    await api('/api/household', { method: 'PUT', body: Object.fromEntries(new FormData(form)) });
    $('#household-modal').close();
    await refreshState();
    showToast('Household name updated.');
  } catch (error) {
    $('#household-message').textContent = error.message;
  }
}

function openHouseholdModal() {
  const form = $('#household-form');
  form.reset();
  form.elements.name.value = appState?.household?.name || '';
  $('#household-message').textContent = '';
  $('#household-modal').showModal();
}

async function handleAction(event) {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const action = button.dataset.action;
  try {
    if (action === 'view-history-month') {
      openHistoryMonth(button.dataset.month);
    } else if (action === 'update-purchase-price') {
      const purchaseId = Number(button.dataset.purchase);
      const priceInput = $(`[data-purchase-price="${purchaseId}"]`, $('#history-month-modal'));
      const unitPrice = Number(priceInput?.value);
      if (!priceInput?.value.trim() || !Number.isFinite(unitPrice) || unitPrice < 0) return showToast('Enter a valid non-negative unit price.');
      await api(`/api/purchases/${purchaseId}`, { method: 'PUT', body: { unit_price: unitPrice } });
      await loadHistory();
      openHistoryMonth(selectedHistoryMonth);
      await refreshState();
      showToast('Purchase price updated.');
    } else if (action === 'remove-list') {
      await api(`/api/shopping/${button.dataset.entry}`, { method: 'DELETE' });
      await refreshState();
    } else if (action === 'adjust-quantity') {
      const item = appState.shopping.find(entry => entry.item_id === Number(button.dataset.item));
      if (!item) return;
      const quantity = item.quantity + Number(button.dataset.delta);
      if (quantity <= 0) {
        await api(`/api/shopping/${item.id}`, { method: 'DELETE' });
      } else {
        await api('/api/shopping', { method: 'POST', body: { item_id: item.item_id, quantity, note: item.note } });
      }
      await refreshState();
    } else if (action === 'bought') {
      const item = appState.shopping.find(entry => entry.item_id === Number(button.dataset.item));
      if (!item) return;
      await api('/api/purchases', { method: 'POST', body: { item_id: item.item_id, quantity: item.quantity, unit_price: item.unit_price } });
      await refreshState();
      if (historyState) await loadHistory();
      showToast('Added to the purchase log.');
    } else if (action === 'delete-purchase') {
      const purchaseId = Number(button.dataset.purchase);
      if (!Number.isInteger(purchaseId) || !window.confirm('Remove this purchase record? Spend totals and history highlights will be recalculated.')) return;
      await api(`/api/purchases/${purchaseId}`, { method: 'DELETE' });
      await refreshState();
      await loadHistory();
      if ($('#history-month-modal').open) openHistoryMonth(selectedHistoryMonth);
      showToast('Purchase record removed.');
    } else if (action === 'delete-manual-spend') {
      const entryId = Number(button.dataset.entry);
      if (!Number.isInteger(entryId) || !window.confirm('Remove this manual monthly spend entry? History totals will be recalculated.')) return;
      await api(`/api/manual-spend/${entryId}`, { method: 'DELETE' });
      await loadHistory();
      showToast('Manual spend entry removed.');
    } else if (action === 'add-list') {
      const item = appState.items.find(entry => entry.id === Number(button.dataset.item));
      if (!item) return;
      await addItemToList(item);
    } else if (action === 'edit-item') {
      const item = appState.items.find(entry => entry.id === Number(button.dataset.item));
      if (item) openItemModal(item);
    } else if (action === 'select-grocer-product') {
      $('#item-form').elements.grocer_product_id.value = button.dataset.productId;
      $('#item-form').elements.grocer_product_url.value = button.dataset.productUrl;
      if (button.dataset.productPrice) $('#item-form').elements.unit_price.value = button.dataset.productPrice;
      showToast(`Selected ${button.dataset.productName}. Save the item to use this match on refresh.`);
    } else if (action === 'archive-item') {
      const item = appState.items.find(entry => entry.id === Number(button.dataset.item));
      if (item && window.confirm(`Remove ${item.name} from the catalog? Purchase history will remain.`)) {
        await api(`/api/items/${item.id}`, { method: 'DELETE' });
        await refreshState();
      }
    }
  } catch (error) {
    showToast(error.message);
  }
}

async function addItemToList(item, quantity = item.forecast_qty || 1) {
  await api('/api/shopping', { method: 'POST', body: { item_id: item.id, quantity } });
  await refreshState();
  showToast('Added to the shopping list.');
}

$('#auth-form').addEventListener('submit', submitAuth);
$('#item-form').addEventListener('submit', submitItem);
$('#member-form').addEventListener('submit', submitMember);
$('#manual-spend-form').addEventListener('submit', submitManualSpend);
$('#household-form').addEventListener('submit', submitHousehold);
$('#catalog-search').addEventListener('input', renderCatalog);
$('#shopping-search').addEventListener('input', renderShopping);
$('#fixed-list-search').addEventListener('input', renderStoreLists);
$('#fixed-store-select').addEventListener('change', () => {
  renderStoreLists();
  renderPricingSettings();
});
$('#refresh-prices-button').addEventListener('click', refreshPrices);
$('#refresh-item-price-button').addEventListener('click', refreshOneItemPrice);
$('#grocer-product-search-button').addEventListener('click', searchGrocerProducts);
$('#grocer-product-search').addEventListener('keydown', event => {
  if (event.key === 'Enter') {
    event.preventDefault();
    searchGrocerProducts();
  }
});
$('#fixed-list-table').addEventListener('change', event => {
  if (event.target.matches('[data-fixed-quantity]')) saveUsualQuantity(event.target);
});
$('#use-store-list-button').addEventListener('click', useStoreList);
$('#paknsave-handoff-button').addEventListener('click', event => { handoffPaknSaveList(event); });
$('#export-button').addEventListener('click', () => { window.location.href = '/api/export.csv'; });
$('#history-refresh-button').addEventListener('click', loadHistory);
$('#add-manual-spend-button').addEventListener('click', openManualSpendModal);
$('#household-button').addEventListener('click', openHouseholdModal);
$('#member-button').addEventListener('click', () => {
  $('#member-message').textContent = '';
  $('#member-form').reset();
  $('#member-modal').showModal();
});
$('#logout-button').addEventListener('click', async () => {
  try {
    await api('/api/logout', { method: 'POST', body: {} });
    appState = null;
    const sessionState = await api('/api/session');
    csrfToken = sessionState.csrf_token;
    showAuth(false);
  } catch (error) {
    showToast(error.message);
  }
});
$('#add-selected-button').addEventListener('click', async () => {
  const item = appState?.items.find(entry => entry.id === Number($('#add-item-select').value));
  if (!item) return showToast('Choose an item first.');
  try { await addItemToList(item); } catch (error) { showToast(error.message); }
});
document.addEventListener('click', async event => {
  const viewButton = event.target.closest('[data-view]');
  if (viewButton) {
    switchView(viewButton.dataset.view);
    if (viewButton.dataset.view === 'history') {
      await loadHistory();
      if (viewButton.dataset.historyMonth === 'current' && historyState) {
        openHistoryMonth(new Date().toISOString().slice(0, 7));
      }
    }
  }
  const openButton = event.target.closest('[data-open-item]');
  if (openButton) openItemModal();
  if (event.target.closest('[data-close-modal]')) event.target.closest('dialog').close();
  await handleAction(event);
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') $$('.modal[open]').forEach(modal => modal.close());
});

boot();
