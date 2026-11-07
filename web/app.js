/**
 * Taiwan Real Estate Intelligence Platform - Frontend Application
 */

const STATE = {
  periods: [],
  currentIndex: 0,
  timeMode: 'single',      // 'single' or 'range'
  rangeStart: null,        // e.g. '2023Q1'
  rangeEnd: null,          // e.g. '2024Q3'
  regionTree: {},          // { '新竹市': [{ district: '東區', full_district: '新竹市東區' }, ...] }
  activeMetric: 'volume',  // 'volume', 'price', 'momentum', 'heat'
  momentumType: 'tx',      // 'tx' (成交量增幅), 'price' (價格增幅)
  momentumInterval: 'qoq', // 'mom' (月增), 'qoq' (季增), 'yoy' (年增)
  activeLevel: 'county',   // 'county', 'town', 'road'
  hotspotType: 'all',      // 'all', 'road', 'project'
  hotspotAllHistoryOnZoom: true, // When zoomed into district/street, show all historical doors (2012~present)
  activeAgeFilter: 'all',  // 'all', '0-5', '5-10', '10-20', '20-30', '30-999'
  selectedRegion: null,    // null = national
  activeBasemap: 'dark',   // 'dark', 'streets', 'satellite'
  isPlaying: false,
  playTimer: null,
  map: null,
  tileLayer: null,
  labelLayer: null,
  geojsonLayer: null,
  markersLayer: null,
  renderedMarkerKeys: new Set(),
  hotspotFilterSignature: '',
  updateViewSeq: 0,
  chart: null,
  desktopBackendUrl: '',
  colorConfig: {
    autoScale: true,
    palette: 'cool_blues',
    currentMin: 0,
    currentMax: 100,
    customMin: null,
    customMax: null,
  },
};

const SUPABASE_CONFIG = {
  url: 'https://pwvhmsslyzwmcueqwljb.supabase.co',
  anonKey: 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InB3dmhtc3NseXp3bWN1ZXF3bGpiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTEyMTE0OTMsImV4cCI6MjEwNjc4NzQ5M30.uk5Swq9XNwAzT-8ZWsEwerNkfyDQhOTRGxcfB4BgypE'
};

const DEFAULT_DESKTOP_TUNNEL = 'https://stand-evaluating-reality-distinguished.trycloudflare.com';

// API Fetch Helper targeting Desktop Backend with automatic tunnel fallback
async function apiFetch(path, options = {}) {
  const base = (STATE.desktopBackendUrl || '').replace(/\/+$/, '');
  const cleanPath = path.startsWith('/') ? path : `/${path}`;
  const fullUrl = path.startsWith('http://') || path.startsWith('https://') ? path : `${base}${cleanPath}`;
  const opts = { ...options };
  opts.headers = {
    'ngrok-skip-browser-warning': 'true',
    ...(opts.headers || {})
  };
  try {
    const res = await fetch(fullUrl, opts);
    if (!res.ok && base && (res.status === 403 || res.status === 502 || res.status === 530)) {
      return fetch(cleanPath, opts);
    }
    return res;
  } catch (err) {
    if (base && !path.startsWith('http')) {
      return fetch(cleanPath, opts);
    }
    throw err;
  }
}

const COLOR_PALETTES = {
  momentum_tw: {
    name: '台股動量 (綠跌-灰平-紅漲)',
    stops: ['#10b981', '#34d399', '#475569', '#f87171', '#ef4444', '#b91c1c']
  },
  momentum_blue_red: {
    name: '金融動量 (深藍跌-灰平-焰紅漲)',
    stops: ['#1e3a8a', '#3b82f6', '#475569', '#f97316', '#ef4444', '#b91c1c']
  },
  emerald_rose: {
    name: '翠綠至烈紅 (標準推薦)',
    stops: ['#059669', '#10b981', '#f59e0b', '#f97316', '#ef4444', '#b91c1c']
  },
  blue_red: {
    name: '深藍至暖紅 (經典對比)',
    stops: ['#1e40af', '#3b82f6', '#38bdf8', '#f59e0b', '#ef4444', '#dc2626']
  },
  magma: {
    name: '烈火金黃 (高對比熱力)',
    stops: ['#475569', '#3b82f6', '#10b981', '#f59e0b', '#ea580c', '#b91c1c']
  },
  plasma: {
    name: '幻彩紫金 (Plasma)',
    stops: ['#3b0764', '#7c3aed', '#ec4899', '#f97316', '#facc15']
  },
  cool_blues: {
    name: '純藍漸變 (量能專用)',
    stops: ['#dbeafe', '#93c5fd', '#60a5fa', '#2563eb', '#1d4ed8', '#0f172a']
  }
};

const DEFAULT_METRIC_PALETTES = {
  volume: 'cool_blues',
  price: 'emerald_rose',
  momentum: 'momentum_tw',
  heat: 'magma'
};

function hexToRgb(hex) {
  hex = hex.replace('#', '');
  if (hex.length === 3) hex = hex.split('').map(c => c + c).join('');
  const num = parseInt(hex, 16);
  return [(num >> 16) & 255, (num >> 8) & 255, num & 255];
}

function rgbToHex(r, g, b) {
  return '#' + [r, g, b].map(x => {
    const hex = Math.round(Math.max(0, Math.min(255, x))).toString(16);
    return hex.length === 1 ? '0' + hex : hex;
  }).join('');
}

function interpolateColors(colorStops, t) {
  t = Math.max(0, Math.min(1, t));
  const n = colorStops.length - 1;
  const pos = t * n;
  const idx = Math.min(Math.floor(pos), n - 1);
  const frac = pos - idx;
  const c1 = hexToRgb(colorStops[idx]);
  const c2 = hexToRgb(colorStops[idx + 1]);
  const r = c1[0] + (c2[0] - c1[0]) * frac;
  const g = c1[1] + (c2[1] - c1[1]) * frac;
  const b = c1[2] + (c2[2] - c1[2]) * frac;
  return rgbToHex(r, g, b);
}

function getAgeParams() {
  const age = STATE.activeAgeFilter;
  if (!age || age === 'all') return '';
  if (age === '0-5') return '&min_age=0&max_age=5';
  if (age === '5-10') return '&min_age=5.001&max_age=10';
  if (age === '10-20') return '&min_age=10.001&max_age=20';
  if (age === '20-30') return '&min_age=20.001&max_age=30';
  if (age === '30-999') return '&min_age=30.001&max_age=999';
  return '';
}

function getTimeParams() {
  if (STATE.timeMode === 'range' && STATE.rangeStart && STATE.rangeEnd) {
    return `&start_period=${encodeURIComponent(STATE.rangeStart)}&end_period=${encodeURIComponent(STATE.rangeEnd)}`;
  }
  const currentPeriod = STATE.periods[STATE.currentIndex] || '2024Q3';
  return `&period=${encodeURIComponent(currentPeriod)}`;
}

function getTimeLabel() {
  if (STATE.timeMode === 'range' && STATE.rangeStart && STATE.rangeEnd) {
    return `${STATE.rangeStart} ~ ${STATE.rangeEnd}`;
  }
  return STATE.periods[STATE.currentIndex] || '最新季度';
}

function getMetricDisplayName(metric) {
  if (metric === 'volume') return '成交筆數';
  if (metric === 'price') return '平均單價';
  if (metric === 'heat') return '市場熱度';
  if (metric === 'momentum') return STATE.momentumType === 'tx' ? '成交量增幅' : '價格增幅';
  return '分析數值';
}

function flyToLocation(lat, lng, zoom = 15, name = '') {
  if (!STATE.map || !lat || !lng) return;
  STATE.map.flyTo([lat, lng], zoom, {
    animate: true,
    duration: 1.2
  });
  if (name) {
    showToast(`📍 正在聚焦定位至：${name}`);
  }
}
window.flyToLocation = flyToLocation;

async function switchToRoadLevelAndFly(lat, lng, zoom = 15, name = '') {
  if (STATE.activeLevel !== 'road') {
    STATE.activeLevel = 'road';
    document.querySelectorAll('.level-btn').forEach(b => {
      if (b.dataset.level === 'road') {
        b.classList.add('active');
        b.classList.remove('text-slate-400');
      } else {
        b.classList.remove('active');
        b.classList.add('text-slate-400');
      }
    });
    const hotspotSubBar = document.getElementById('hotspot-sub-controls');
    if (hotspotSubBar) hotspotSubBar.classList.remove('hidden');
    await updateView();
  }
  flyToLocation(lat, lng, zoom, name);
}
window.switchToRoadLevelAndFly = switchToRoadLevelAndFly;

// Basemap URLs (100% Free, Official ESRI ArcGIS Basemaps with Road Labels)
const BASEMAP_TILES = {
  dark: {
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',
    labelUrl: 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}',
    options: { maxZoom: 18, attribution: 'Tiles &copy; Esri &mdash; Esri Dark Gray' }
  },
  streets: {
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
    options: { maxZoom: 19, attribution: 'Tiles &copy; Esri &mdash; World Street Map' }
  },
  taiwan_emap: {
    url: '/api/tiles/emap/{z}/{y}/{x}',
    options: { maxNativeZoom: 19, maxZoom: 20, attribution: '&copy; 內政部國土測繪中心 臺灣通用電子地圖' }
  },
  satellite: {
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    labelUrl: 'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
    options: { maxZoom: 19, attribution: 'Tiles &copy; Esri, Earthstar Geographics' }
  }
};

// Auth & Desktop Helpers
function getStoredAuth() {
  try {
    const raw = localStorage.getItem('tw_re_auth');
    if (!raw) return null;
    return JSON.parse(raw);
  } catch (e) {
    return null;
  }
}

function checkIsAuthenticated() {
  const auth = getStoredAuth();
  return !!(auth && auth.user);
}

async function initAppData() {
  await loadRegions();
  await loadPeriods();
  await loadCrawlerStatus();
}

function initAuthAndDesktop() {
  // 1. Initialize Desktop Backend URL
  let savedDesktopUrl = localStorage.getItem('tw_re_desktop_url');
  const isLocalHost = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';

  // Auto-upgrade stale ngrok or previous quick-tunnel URLs saved in browser localStorage
  if (savedDesktopUrl && (savedDesktopUrl.includes('ngrok') || (savedDesktopUrl.includes('trycloudflare.com') && savedDesktopUrl !== DEFAULT_DESKTOP_TUNNEL))) {
    savedDesktopUrl = isLocalHost ? '' : DEFAULT_DESKTOP_TUNNEL;
    localStorage.setItem('tw_re_desktop_url', savedDesktopUrl);
  }
  
  if (savedDesktopUrl !== null) {
    STATE.desktopBackendUrl = savedDesktopUrl;
  } else if (!isLocalHost) {
    STATE.desktopBackendUrl = DEFAULT_DESKTOP_TUNNEL;
  } else {
    STATE.desktopBackendUrl = '';
  }

  const backendInput = document.getElementById('desktop-backend-input');
  if (backendInput) backendInput.value = STATE.desktopBackendUrl;

  // 2. Auth State Check
  const authModal = document.getElementById('auth-modal');
  const authForm = document.getElementById('auth-form');
  const authErrorMsg = document.getElementById('auth-error-msg');
  const currentUserName = document.getElementById('current-user-name');
  const btnLogout = document.getElementById('btn-logout');

  if (checkIsAuthenticated()) {
    const auth = getStoredAuth();
    if (authModal) authModal.classList.add('hidden');
    if (currentUserName) currentUserName.innerText = auth.user;
  } else {
    if (authModal) authModal.classList.remove('hidden');
  }

  if (authForm) {
    authForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (authErrorMsg) authErrorMsg.classList.add('hidden');
      const submitBtn = document.getElementById('btn-auth-submit');
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerText = '驗證中...';
      }

      const username = document.getElementById('auth-username').value.trim();
      const password = document.getElementById('auth-password').value.trim();

      let loginSuccess = false;
      let token = 'token_' + Date.now();

      // Check credentials:
      // 1) hotpotlu / qQ!0963067171
      // 2) wlhong / wlhong
      const VALID_ACCOUNTS = {
        'hotpotlu': 'qQ!0963067171',
        'hotpotlu@gmail.com': 'qQ!0963067171',
        'wlhong': 'wlhong',
        'wlhong@gmail.com': 'wlhong'
      };
      if (VALID_ACCOUNTS[username] && VALID_ACCOUNTS[username] === password) {
        loginSuccess = true;
      }

      // Also attempt Supabase Auth endpoint if not already matched
      if (!loginSuccess) {
        try {
          const supabaseRes = await fetch(`${SUPABASE_CONFIG.url}/auth/v1/token?grant_type=password`, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'apikey': SUPABASE_CONFIG.anonKey
            },
            body: JSON.stringify({
              email: username.includes('@') ? username : `${username}@gmail.com`,
              password: password
            })
          });
          const supabaseData = await supabaseRes.json();
          if (supabaseRes.ok && supabaseData.access_token) {
            loginSuccess = true;
            token = supabaseData.access_token;
          }
        } catch (err) {
          console.warn('Supabase auth network ping:', err);
        }
      }

      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.innerHTML = '<i data-lucide="log-in" class="w-4 h-4"></i><span>登入系統</span>';
        if (window.lucide) lucide.createIcons();
      }

      if (loginSuccess) {
        const displayUser = username.split('@')[0];
        localStorage.setItem('tw_re_auth', JSON.stringify({
          user: displayUser,
          token: token,
          timestamp: Date.now()
        }));
        if (authModal) authModal.classList.add('hidden');
        if (currentUserName) currentUserName.innerText = displayUser;
        showToast('登入成功，系統已就緒');
        await initAppData();
      } else {
        if (authErrorMsg) authErrorMsg.classList.remove('hidden');
      }
    });
  }

  if (btnLogout) {
    btnLogout.addEventListener('click', () => {
      localStorage.removeItem('tw_re_auth');
      if (authModal) authModal.classList.remove('hidden');
      showToast('已登出系統');
    });
  }

  // 3. Desktop Connection Modal & Controls
  const desktopModal = document.getElementById('desktop-modal');
  const btnDesktopConn = document.getElementById('btn-desktop-connection');
  const btnCloseDesktopModal = document.getElementById('btn-close-desktop-modal');
  const btnTestConn = document.getElementById('btn-test-desktop-connection');
  const btnSaveConn = document.getElementById('btn-save-desktop-connection');

  if (btnDesktopConn) {
    btnDesktopConn.addEventListener('click', () => {
      if (backendInput) backendInput.value = STATE.desktopBackendUrl;
      if (desktopModal) desktopModal.classList.remove('hidden');
    });
  }
  if (btnCloseDesktopModal) {
    btnCloseDesktopModal.addEventListener('click', () => {
      if (desktopModal) desktopModal.classList.add('hidden');
    });
  }

  const pingDesktop = async (targetUrl) => {
    try {
      const url = (targetUrl !== undefined ? targetUrl : STATE.desktopBackendUrl).replace(/\/+$/, '');
      const testEndpoint = (url || '') + '/api/crawler/status';
      const res = await fetch(testEndpoint, {
        headers: { 'ngrok-skip-browser-warning': 'true' }
      });
      return res.ok;
    } catch (e) {
      return false;
    }
  };

  const updateDesktopStatusUI = (isOnline) => {
    const dot = document.getElementById('desktop-status-dot');
    const text = document.getElementById('desktop-status-text');
    const modalStatus = document.getElementById('desktop-modal-status');

    if (dot) dot.className = `w-2 h-2 rounded-full ${isOnline ? 'bg-emerald-400' : 'bg-rose-500'}`;
    if (text) text.innerText = isOnline ? 'Desktop 已連線' : 'Desktop 離線';
    if (modalStatus) {
      modalStatus.className = `inline-flex items-center gap-1.5 font-semibold ${isOnline ? 'text-emerald-400' : 'text-rose-400'}`;
      modalStatus.innerHTML = `<span class="w-2 h-2 rounded-full ${isOnline ? 'bg-emerald-400 animate-pulse' : 'bg-rose-500'}"></span>${isOnline ? '連線正常 (Active)' : '連線失敗 (Offline)'}`;
    }
  };

  if (btnTestConn) {
    btnTestConn.addEventListener('click', async () => {
      btnTestConn.disabled = true;
      btnTestConn.innerHTML = '<i data-lucide="loader" class="w-3.5 h-3.5 animate-spin"></i><span>測試中...</span>';
      const testUrl = (backendInput ? backendInput.value.trim() : '');
      const ok = await pingDesktop(testUrl);
      btnTestConn.disabled = false;
      btnTestConn.innerHTML = '<i data-lucide="activity" class="w-3.5 h-3.5"></i><span>測試連線</span>';
      if (window.lucide) lucide.createIcons();
      updateDesktopStatusUI(ok);
      if (ok) {
        showToast('連線測試成功！已連通本機 Desktop 伺服器');
      } else {
        showToast('連線測試失敗，請確認 ngrok 或本機伺服器是否開啟');
      }
    });
  }

  if (btnSaveConn) {
    btnSaveConn.addEventListener('click', async () => {
      const newUrl = backendInput ? backendInput.value.trim().replace(/\/+$/, '') : '';
      STATE.desktopBackendUrl = newUrl;
      localStorage.setItem('tw_re_desktop_url', newUrl);
      if (desktopModal) desktopModal.classList.add('hidden');
      showToast('已更新 Desktop 連線網址，正在重新載入資料...');
      const ok = await pingDesktop(newUrl);
      updateDesktopStatusUI(ok);
      if (checkIsAuthenticated()) {
        await initAppData();
      }
    });
  }

  // Initial desktop check
  pingDesktop().then(updateDesktopStatusUI);
}

// Initialize Application
document.addEventListener('DOMContentLoaded', async () => {
  if (window.lucide) lucide.createIcons();
  initAuthAndDesktop();
  initMap();
  initChart();
  bindEvents();
  if (checkIsAuthenticated()) {
    await initAppData();
  }
});

// 1. Map Initialization
function initMap() {
  STATE.map = L.map('map', {
    zoomControl: false,
    attributionControl: false,
    preferCanvas: true,
  }).setView([23.7, 120.95], 7.5);

  L.control.zoom({ position: 'topleft' }).addTo(STATE.map);

  // Dedicated pane for ESRI road names and place text labels
  // zIndex 450 sits ABOVE the GeoJSON polygon fills (zIndex 400) and BELOW markers (zIndex 600)
  STATE.map.createPane('esriLabels');
  STATE.map.getPane('esriLabels').style.zIndex = 450;
  STATE.map.getPane('esriLabels').style.pointerEvents = 'none';

  // Dedicated pane for Road and Project Hotspot markers (above labels, above GeoJSON polygons)
  STATE.map.createPane('hotspotPane');
  STATE.map.getPane('hotspotPane').style.zIndex = 650;
  STATE.hotspotRenderer = L.canvas({ pane: 'hotspotPane', padding: 0.5 });

  // Set default Esri Dark Gray Base + Road Labels
  setBasemap('dark');

  // Layer group for road and project markers
  STATE.markersLayer = L.layerGroup().addTo(STATE.map);

  // Dynamically & incrementally load new spots in the panned/zoomed viewport without clearing existing markers
  let moveTimer = null;
  STATE.map.on('moveend', () => {
    if (STATE.activeLevel !== 'road') return;
    if (STATE.map.getZoom() < 11) return;
    if (moveTimer) clearTimeout(moveTimer);
    moveTimer = setTimeout(() => {
      renderLocationHotspots(null, null, { incremental: true, scanViewport: true });
    }, 180);
  });
}

function setBasemap(type) {
  if (STATE.tileLayer) {
    STATE.map.removeLayer(STATE.tileLayer);
    STATE.tileLayer = null;
  }
  if (STATE.labelLayer) {
    STATE.map.removeLayer(STATE.labelLayer);
    STATE.labelLayer = null;
  }
  const config = BASEMAP_TILES[type] || BASEMAP_TILES.dark;
  STATE.tileLayer = L.tileLayer(config.url, config.options).addTo(STATE.map);

  // Overlay ESRI road names and place labels if available
  if (config.labelUrl) {
    STATE.labelLayer = L.tileLayer(config.labelUrl, {
      pane: 'esriLabels',
      maxZoom: config.options.maxZoom || 18,
      opacity: 0.95
    }).addTo(STATE.map);
  }

  STATE.activeBasemap = type;

  // Toggle background between light and dark to eliminate black tile loading voids
  const mapEl = document.getElementById('map');
  const parentEl = mapEl ? mapEl.parentElement : null;
  const isLightMap = (type === 'taiwan_emap' || type === 'streets');
  if (mapEl) mapEl.style.backgroundColor = isLightMap ? '#f8fafc' : '#020617';
  if (parentEl) parentEl.style.backgroundColor = isLightMap ? '#f8fafc' : '#020617';

  // Update switcher button styles
  document.querySelectorAll('.basemap-btn').forEach(btn => {
    if (btn.dataset.basemap === type) {
      btn.className = 'basemap-btn active px-2.5 py-1 rounded-lg text-slate-100 bg-slate-800 transition';
    } else {
      btn.className = 'basemap-btn px-2.5 py-1 rounded-lg text-slate-400 hover:text-white transition';
    }
  });
}

// 1b. Load Region Hierarchy (Counties & Districts)
async function loadRegions() {
  try {
    const res = await apiFetch('/api/regions');
    const data = await res.json();
    STATE.regionTree = data.regions || {};

    const countySel = document.getElementById('select-global-county');
    if (!countySel) return;
    countySel.innerHTML = '<option value="">全省 (全國合計)</option>';
    const counties = Object.keys(STATE.regionTree).sort();
    counties.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c;
      opt.innerText = c;
      countySel.appendChild(opt);
    });
  } catch (err) {
    console.error('Failed to load regions:', err);
  }
}

function updateTownDropdown(countyName) {
  const townSel = document.getElementById('select-global-town');
  if (!townSel) return;
  if (!countyName || !STATE.regionTree[countyName]) {
    townSel.innerHTML = '<option value="">全部鄉鎮市區</option>';
    townSel.disabled = true;
    return;
  }
  const towns = STATE.regionTree[countyName] || [];
  townSel.innerHTML = '<option value="">全部鄉鎮市區</option>';
  towns.forEach(t => {
    const opt = document.createElement('option');
    opt.value = t.full_district;
    opt.innerText = t.district;
    townSel.appendChild(opt);
  });
  townSel.disabled = false;
}

function syncRegionDropdowns(selectedRegion) {
  const countySel = document.getElementById('select-global-county');
  const townSel = document.getElementById('select-global-town');
  if (!countySel || !townSel) return;

  if (!selectedRegion) {
    countySel.value = "";
    townSel.innerHTML = '<option value="">全部鄉鎮市區</option>';
    townSel.disabled = true;
    return;
  }

  // Check if it's a county directly
  if (STATE.regionTree[selectedRegion]) {
    countySel.value = selectedRegion;
    updateTownDropdown(selectedRegion);
    townSel.value = "";
    return;
  }

  // Check if it's a town in any county
  for (const [c, towns] of Object.entries(STATE.regionTree)) {
    const match = towns.find(t => t.full_district === selectedRegion || t.district === selectedRegion);
    if (match) {
      countySel.value = c;
      updateTownDropdown(c);
      townSel.value = match.full_district;
      return;
    }
  }
}

// 2. Load Available Periods
async function loadPeriods() {
  try {
    const res = await apiFetch('/api/periods');
    const data = await res.json();
    STATE.periods = data.periods || [];

    if (STATE.periods.length === 0) {
      showToast('尚未有聚合資料，請先抓取資料。');
      return;
    }

    const slider = document.getElementById('period-slider');
    slider.min = 0;
    slider.max = STATE.periods.length - 1;
    STATE.currentIndex = STATE.periods.length - 1;
    slider.value = STATE.currentIndex;

    document.getElementById('label-start-period').innerText = STATE.periods[0];
    document.getElementById('label-end-period').innerText = STATE.periods[STATE.periods.length - 1];

    // Populate Range Start & End Dropdowns
    const selStart = document.getElementById('select-range-start');
    const selEnd = document.getElementById('select-range-end');
    if (selStart && selEnd) {
      selStart.innerHTML = '';
      selEnd.innerHTML = '';
      STATE.periods.forEach(p => {
        const opt1 = document.createElement('option');
        opt1.value = p;
        opt1.innerText = p;
        selStart.appendChild(opt1);

        const opt2 = document.createElement('option');
        opt2.value = p;
        opt2.innerText = p;
        selEnd.appendChild(opt2);
      });
      // Default: last 4 quarters (1 year)
      const defaultStartIdx = Math.max(0, STATE.periods.length - 4);
      selStart.value = STATE.periods[defaultStartIdx];
      selEnd.value = STATE.periods[STATE.periods.length - 1];
      STATE.rangeStart = selStart.value;
      STATE.rangeEnd = selEnd.value;
    }

    updatePeriodDisplay();
    await updateView();
  } catch (err) {
    console.error('Failed to load periods:', err);
    showToast('載入時間軸失敗');
  }
}

function updatePeriodDisplay() {
  const label = getTimeLabel();
  document.getElementById('label-current-period').innerText = label;
  document.getElementById('aside-period-desc').innerText = `觀測週期: ${label}`;
  const chartBtnLabel = document.getElementById('chart-btn-period-label');
  if (chartBtnLabel) chartBtnLabel.innerText = label;
}

// 3. Color Scale Resolver with Dynamic Auto-Scaling & Custom Palettes
function computeAutoScaleRange(features) {
  const metric = STATE.activeMetric;
  const values = [];

  if (metric === 'momentum') {
    features.forEach(f => {
      const p = f.properties || {};
      if ((p.tx_count || 0) === 0) return;
      let v = 0;
      if (STATE.momentumType === 'tx') {
        if (STATE.momentumInterval === 'mom') v = p.mom_tx_change;
        else if (STATE.momentumInterval === 'qoq') v = p.qoq_tx_change;
        else v = p.yoy_tx_change;
      } else {
        if (STATE.momentumInterval === 'mom') v = p.mom_price_change;
        else if (STATE.momentumInterval === 'qoq') v = p.qoq_price_change;
        else v = p.yoy_price_change;
      }
      if (v !== undefined && v !== null && !isNaN(v)) {
        values.push(Number(v));
      }
    });

    if (values.length === 0) {
      return { min: -25, max: 25 };
    }

    values.sort((a, b) => a - b);
    const p5Idx = Math.floor(values.length * 0.05);
    const p95Idx = Math.min(values.length - 1, Math.ceil(values.length * 0.95) - 1);
    const p5 = values[p5Idx];
    const p95 = values[p95Idx];
    // Center at 0 symmetrically for diverging palette
    const bound = Math.max(Math.abs(p5), Math.abs(p95), 10);
    const boundRounded = Math.min(100, Math.ceil(bound / 5) * 5);
    return { min: -boundRounded, max: boundRounded };
  }

  features.forEach(f => {
    const p = f.properties || {};
    let v = 0;
    if (metric === 'volume') v = p.tx_count || 0;
    else if (metric === 'price') v = p.avg_unit_price || 0;
    else if (metric === 'heat') v = p.heat_score || 0;

    if (v > 0) values.push(v);
  });

  if (values.length === 0) {
    if (metric === 'volume') return { min: 0, max: 100 };
    if (metric === 'price') return { min: 10, max: 80 };
    return { min: 0, max: 100 };
  }

  values.sort((a, b) => a - b);

  // 2nd percentile and 98th percentile to prevent single extreme luxury/typo outliers from squashing color contrast
  const pMinIdx = Math.floor(values.length * 0.02);
  const pMaxIdx = Math.min(values.length - 1, Math.ceil(values.length * 0.98) - 1);

  let minVal = values[pMinIdx];
  let maxVal = values[pMaxIdx];

  if (maxVal <= minVal) {
    maxVal = minVal + 1;
  }

  if (metric === 'volume') {
    minVal = Math.floor(minVal);
    maxVal = Math.ceil(maxVal);
  } else {
    minVal = Math.floor(minVal * 10) / 10;
    maxVal = Math.ceil(maxVal * 10) / 10;
  }

  return { min: minVal, max: maxVal };
}

function getColorForMetric(props, metric) {
  if (!props) return '#1e293b';

  // No transactions
  if (!props.tx_count || props.tx_count === 0) {
    return '#334155';
  }

  let val = 0;
  if (metric === 'volume') {
    val = props.tx_count || 0;
  } else if (metric === 'price') {
    val = props.avg_unit_price || 0;
  } else if (metric === 'heat') {
    val = props.heat_score || 0;
  } else if (metric === 'momentum') {
    if (STATE.momentumType === 'tx') {
      if (STATE.momentumInterval === 'mom') val = props.mom_tx_change || 0;
      else if (STATE.momentumInterval === 'qoq') val = props.qoq_tx_change || 0;
      else val = props.yoy_tx_change || 0;
    } else {
      if (STATE.momentumInterval === 'mom') val = props.mom_price_change || 0;
      else if (STATE.momentumInterval === 'qoq') val = props.qoq_price_change || 0;
      else val = props.yoy_price_change || 0;
    }
  }

  const min = STATE.colorConfig.currentMin;
  const max = STATE.colorConfig.currentMax;

  let t = 0.5;
  if (max > min) {
    t = (val - min) / (max - min);
  }
  t = Math.max(0, Math.min(1, t));

  const paletteKey = STATE.colorConfig.palette;
  const defaultKey = DEFAULT_METRIC_PALETTES[metric] || 'emerald_rose';
  const palette = COLOR_PALETTES[paletteKey] || COLOR_PALETTES[defaultKey] || COLOR_PALETTES.emerald_rose;
  return interpolateColors(palette.stops, t);
}

function updateLegend() {
  const metric = STATE.activeMetric;
  const cfg = STATE.colorConfig;

  // Title & Unit
  const titleText = document.getElementById('legend-title-text');
  const unitEl = document.getElementById('legend-unit');
  if (metric === 'volume') {
    if (titleText) titleText.innerText = '成交量分佈';
    if (unitEl) unitEl.innerText = '件數 (筆)';
  } else if (metric === 'price') {
    if (titleText) titleText.innerText = '每坪平均單價';
    if (unitEl) unitEl.innerText = '萬元 / 坪';
  } else if (metric === 'heat') {
    if (titleText) titleText.innerText = '市場熱度評分';
    if (unitEl) unitEl.innerText = '0 - 100 指數';
  } else if (metric === 'momentum') {
    const typeLabel = STATE.momentumType === 'tx' ? '成交量' : '交易單價';
    const intervalLabel = STATE.momentumInterval === 'mom' ? '月增率 (MoM)' : (STATE.momentumInterval === 'qoq' ? '季增率 (QoQ)' : '年增率 (YoY)');
    if (titleText) titleText.innerText = `${typeLabel} ${intervalLabel}`;
    if (unitEl) unitEl.innerText = '變化率 (%)';
  }

  // Palette & Gradient Bar
  const defaultKey = DEFAULT_METRIC_PALETTES[metric] || 'emerald_rose';
  const palette = COLOR_PALETTES[cfg.palette] || COLOR_PALETTES[defaultKey] || COLOR_PALETTES.emerald_rose;
  const gradientEl = document.getElementById('legend-gradient');
  if (gradientEl) {
    gradientEl.style.background = `linear-gradient(to right, ${palette.stops.join(', ')})`;
  }

  // Min / Mid / Max Labels
  const minVal = cfg.currentMin;
  const maxVal = cfg.currentMax;
  const midVal = (minVal + maxVal) / 2;

  let minLabel = '';
  let midLabel = '';
  let maxLabel = '';

  if (metric === 'momentum') {
    minLabel = `${minVal > 0 ? '+' : ''}${minVal}%`;
    midLabel = `${midVal > 0 ? '+' : ''}${midVal.toFixed(0)}%`;
    maxLabel = `${maxVal > 0 ? '+' : ''}${maxVal}%`;
  } else if (metric === 'volume') {
    minLabel = `${minVal.toLocaleString()} 筆`;
    midLabel = `${Math.round(midVal).toLocaleString()} 筆`;
    maxLabel = `${maxVal.toLocaleString()}+ 筆`;
  } else if (metric === 'price') {
    minLabel = `${minVal} 萬`;
    midLabel = `${midVal.toFixed(1)} 萬`;
    maxLabel = `${maxVal}+ 萬`;
  } else {
    minLabel = `${minVal} 分`;
    midLabel = `${midVal.toFixed(0)} 分`;
    maxLabel = `${maxVal}+ 分`;
  }

  const lblMin = document.getElementById('lbl-legend-min');
  const lblMid = document.getElementById('lbl-legend-mid');
  const lblMax = document.getElementById('lbl-legend-max');
  if (lblMin) lblMin.innerText = minLabel;
  if (lblMid) lblMid.innerText = midLabel;
  if (lblMax) lblMax.innerText = maxLabel;

  // Inputs sync
  const inputMin = document.getElementById('input-color-min');
  const inputMax = document.getElementById('input-color-max');
  const hintEl = document.getElementById('custom-range-hint');
  const chkAuto = document.getElementById('chk-auto-color');
  const selPalette = document.getElementById('select-color-palette');

  if (chkAuto) chkAuto.checked = cfg.autoScale;
  if (selPalette) selPalette.value = cfg.palette;

  if (cfg.autoScale) {
    if (inputMin) { inputMin.value = minVal; inputMin.disabled = true; inputMin.classList.add('opacity-50'); }
    if (inputMax) { inputMax.value = maxVal; inputMax.disabled = true; inputMax.classList.add('opacity-50'); }
    if (hintEl) { hintEl.innerText = '(自動自適應中)'; hintEl.className = 'text-[10px] text-emerald-400 font-medium'; }
  } else {
    if (inputMin) { inputMin.value = minVal; inputMin.disabled = false; inputMin.classList.remove('opacity-50'); }
    if (inputMax) { inputMax.value = maxVal; inputMax.disabled = false; inputMax.classList.remove('opacity-50'); }
    if (hintEl) { hintEl.innerText = '(手動自訂模式)'; hintEl.className = 'text-[10px] text-amber-400 font-medium'; }
  }
}

// 4. Update Map and View Layers
async function updateView() {
  if (STATE.periods.length === 0) return;
  const mySeq = ++STATE.updateViewSeq;
  const timeLabel = getTimeLabel();

  updatePeriodDisplay();

  try {
    // Determine level for base boundaries
    const fetchLevel = STATE.activeLevel === 'road' ? 'town' : STATE.activeLevel;
    const res = await apiFetch(`/api/map/data?level=${fetchLevel}${getTimeParams()}${getAgeParams()}`);
    const geojsonData = await res.json();
    if (mySeq !== STATE.updateViewSeq) return; // Ignore stale response if user already switched again!

    // Compute dynamic color auto-scaling if enabled
    if (STATE.colorConfig.autoScale) {
      const autoRange = computeAutoScaleRange(geojsonData.features || []);
      STATE.colorConfig.currentMin = autoRange.min;
      STATE.colorConfig.currentMax = autoRange.max;
    } else {
      if (STATE.colorConfig.customMin !== null && STATE.colorConfig.customMax !== null) {
        STATE.colorConfig.currentMin = STATE.colorConfig.customMin;
        STATE.colorConfig.currentMax = STATE.colorConfig.customMax;
      }
    }

    updateLegend();

    if (STATE.geojsonLayer) {
      STATE.map.removeLayer(STATE.geojsonLayer);
    }
    if (STATE.activeLevel !== 'road') {
      STATE.markersLayer.clearLayers();
      STATE.renderedMarkerKeys.clear();
      STATE.hotspotFilterSignature = '';
    }

    // Render Polygons
    STATE.geojsonLayer = L.geoJSON(geojsonData, {
      style: (feature) => {
        const props = feature.properties;
        const color = getColorForMetric(props, STATE.activeMetric);
        const isSelected = STATE.selectedRegion &&
          (props.COUNTYNAME === STATE.selectedRegion || props.FULLNAME === STATE.selectedRegion);

        const isRoadLevel = STATE.activeLevel === 'road';
        if (isRoadLevel) {
          // 細分模式：不塗任何色塊，只要細緻框出鄉鎮市區輪廓！
          return {
            fill: false,
            fillOpacity: 0,
            fillColor: 'transparent',
            weight: isSelected ? 2.5 : 1.2,
            opacity: isSelected ? 1 : 0.65,
            color: isSelected ? '#38bdf8' : '#64748b',
            dashArray: isSelected ? null : '3, 4',
          };
        }

        return {
          fillColor: color,
          weight: isSelected ? 2.5 : 1,
          opacity: 1,
          color: isSelected ? '#fbbf24' : '#334155',
          fillOpacity: 0.75,
        };
      },
      onEachFeature: (feature, layer) => {
        const p = feature.properties;
        const regionName = fetchLevel === 'county' ? p.COUNTYNAME : p.FULLNAME;

        const formatChangePill = (val, label) => {
          if (val === null || val === undefined || isNaN(val)) {
            return `<span class="inline-flex items-center text-[11px] whitespace-nowrap"><span class="text-slate-500 text-[10px] mr-0.5">${label}:</span><span class="text-slate-500 font-medium">—</span></span>`;
          }
          const num = Number(val);
          const sign = num > 0 ? '+' : '';
          const color = num > 0 ? 'text-red-400 font-semibold' : (num < 0 ? 'text-emerald-400 font-semibold' : 'text-slate-400 font-medium');
          return `<span class="inline-flex items-center text-[11px] whitespace-nowrap"><span class="text-slate-500 text-[10px] mr-0.5">${label}:</span><span class="${color}">${sign}${num.toFixed(1)}%</span></span>`;
        };

        const tipContent = `
          <div class="custom-tooltip-box">
            <div class="flex items-center justify-between pb-1.5 mb-2 border-b border-slate-700/80">
              <span class="font-bold text-sm text-white tracking-wide">${regionName}</span>
              <span class="text-[10px] px-2 py-0.5 rounded-full bg-blue-500/20 text-blue-300 border border-blue-500/30 font-mono font-medium">${timeLabel}</span>
            </div>
            <table class="w-full text-xs" style="border-collapse: separate; border-spacing: 0 4px;">
              <tr>
                <td class="text-slate-400 whitespace-nowrap text-left" style="width: 65px;">成交總量</td>
                <td class="text-right whitespace-nowrap">
                  <span class="font-bold text-blue-400 text-sm">${(p.tx_count || 0).toLocaleString()}</span>
                  <span class="text-[11px] text-slate-400 ml-0.5">筆</span>
                </td>
              </tr>
              <tr>
                <td class="text-slate-400 text-[11px] whitespace-nowrap text-left align-top pt-0.5">量動量趨勢</td>
                <td class="text-right whitespace-nowrap space-x-1.5 pt-0.5">
                  ${(p.tx_count || 0) === 0 ? '<span class="text-slate-500 text-[11px] font-medium">當期無成交</span>' : `
                    ${formatChangePill(p.mom_tx_change, '月')}
                    ${formatChangePill(p.qoq_tx_change, '季')}
                    ${formatChangePill(p.yoy_tx_change, '年')}
                  `}
                </td>
              </tr>
              <tr style="height: 4px;"></tr>
              <tr>
                <td class="text-slate-400 whitespace-nowrap text-left">平均單價</td>
                <td class="text-right whitespace-nowrap">
                  <span class="font-bold text-emerald-400 text-sm">${p.avg_unit_price || 0}</span>
                  <span class="text-[11px] text-slate-400 ml-0.5">萬/坪</span>
                </td>
              </tr>
              <tr>
                <td class="text-slate-400 text-[11px] whitespace-nowrap text-left align-top pt-0.5">價動量趨勢</td>
                <td class="text-right whitespace-nowrap space-x-1.5 pt-0.5">
                  ${(p.tx_count || 0) === 0 ? '<span class="text-slate-500 text-[11px] font-medium">當期無成交</span>' : `
                    ${formatChangePill(p.mom_price_change, '月')}
                    ${formatChangePill(p.qoq_price_change, '季')}
                    ${formatChangePill(p.yoy_price_change, '年')}
                  `}
                </td>
              </tr>
              <tr style="height: 4px;"></tr>
              <tr>
                <td class="text-slate-400 whitespace-nowrap text-left">市場熱度</td>
                <td class="text-right whitespace-nowrap">
                  <span class="inline-flex items-center px-1.5 py-0.5 rounded text-[11px] font-medium bg-amber-500/20 text-amber-300 border border-amber-500/30">
                    ${p.heat_level || '無'} (${p.heat_score || 0}分)
                  </span>
                </td>
              </tr>
            </table>
          </div>
        `;
        layer.bindTooltip(tipContent, { className: 'custom-leaflet-tooltip', sticky: true });

        layer.on('mouseover', () => {
          if (STATE.activeLevel === 'road') {
            layer.setStyle({ weight: 2.5, color: '#38bdf8', opacity: 1 });
          } else {
            layer.setStyle({ fillOpacity: 0.95, weight: 2, color: '#94a3b8' });
          }
        });
        layer.on('mouseout', () => {
          STATE.geojsonLayer.resetStyle(layer);
        });

        layer.on('click', () => {
          selectRegion(regionName, layer);
        });
      }
    }).addTo(STATE.map);

    // Execute Hotspot rendering, KPI overview, and Sidebar tabs concurrently!
    const tasks = [
      loadRanking(),
      loadTopRoads(STATE.selectedRegion),
      loadTopProjects(STATE.selectedRegion),
    ];
    if (STATE.activeLevel === 'road') {
      tasks.push(renderLocationHotspots(geojsonData));
    }
    if (!STATE.selectedRegion) {
      tasks.push(loadNationalSummary());
      tasks.push(loadHistoricalTrend('national', '全國合計'));
    } else {
      tasks.push(loadRegionDetail(fetchLevel, STATE.selectedRegion));
    }
    await Promise.all(tasks);

  } catch (err) {
    console.error('Failed to update view:', err);
  }
}

// 5. Render Granular Location Hotspots (Roads & Building Projects) on Map
async function renderLocationHotspots(geojsonData, period, options = {}) {
  try {
    const zoomLvl = STATE.map ? STATE.map.getZoom() : 7.5;
    const isZoomedIn = zoomLvl >= 11;
    const isStreetOrDistZoom = zoomLvl >= 12;

    const firstP = (STATE.periods && STATE.periods.length > 0) ? STATE.periods[0] : '2012Q3';
    const lastP = (STATE.periods && STATE.periods.length > 0) ? STATE.periods[STATE.periods.length - 1] : '2026Q1';
    const useAllHist = Boolean(STATE.hotspotAllHistoryOnZoom && (isStreetOrDistZoom || STATE.selectedRegion));

    const timeLabel = useAllHist ? `${firstP}~${lastP}` : getTimeLabel();
    const timeParams = useAllHist
      ? `&start_period=${encodeURIComponent(firstP)}&end_period=${encodeURIComponent(lastP)}`
      : getTimeParams();
    const ageParams = getAgeParams();
    const hotspotType = STATE.hotspotType || 'all';
    const isIncrementalPan = Boolean(options.incremental);

    const regionParam = (!options.scanViewport && !isStreetOrDistZoom && STATE.selectedRegion)
      ? `&region=${encodeURIComponent(STATE.selectedRegion)}`
      : '';

    let bboxParam = '';
    if (isZoomedIn && STATE.map) {
      const b = STATE.map.getBounds();
      bboxParam = `&min_lat=${b.getSouth().toFixed(4)}&max_lat=${b.getNorth().toFixed(4)}&min_lng=${b.getWest().toFixed(4)}&max_lng=${b.getEast().toFixed(4)}`;
    }

    // Signature of analytical/visual settings; only clear existing points when these settings change
    const filterSig = `${timeParams}|${ageParams}|${STATE.activeMetric}|${hotspotType}|${STATE.colorConfig.palette}|${STATE.colorConfig.currentMin}|${STATE.colorConfig.currentMax}|${STATE.momentumType}|${STATE.momentumInterval}|${useAllHist}`;
    const shouldResetLayer = Boolean(options.forceReset) || (!isIncrementalPan && filterSig !== STATE.hotspotFilterSignature);

    // When region is selected or map is zoomed in, fetch all unmerged distinct addresses (no truncation!)
    const limit = (STATE.selectedRegion || isZoomedIn) ? 4500 : 2500;
    const url = `/api/locations/hotspots?type=${hotspotType}&limit=${limit}${timeParams}${ageParams}${regionParam}${bboxParam}`;

    const res = await apiFetch(url);
    const data = await res.json();
    const hotspots = data.hotspots || [];

    if (shouldResetLayer) {
      if (STATE.markersLayer) {
        STATE.markersLayer.clearLayers();
      }
      STATE.renderedMarkerKeys.clear();
      STATE.hotspotFilterSignature = filterSig;
    }

    if (!hotspots.length) return;

    hotspots.forEach(item => {
      const lat = item.lat;
      const lng = item.lng;
      if (!lat || !lng) return;

      const spotKey = `${item.full_district || ''}_${item.name || ''}_${item.road || ''}_${item.type || ''}`;
      if (STATE.renderedMarkerKeys.has(spotKey)) {
        return; // Already rendered on map — keep existing marker untouched!
      }
      STATE.renderedMarkerKeys.add(spotKey);

      const isProject = item.type === 'project';
      const isDoor = item.type === 'door';
      const txCount = item.tx_count || 0;
      // Proportional marker sizing: min 6.5px, max 22px
      const radius = Math.min(22, Math.max(6.5, Math.round(Math.sqrt(txCount) * 2.6)));

      const commName = (item.project_name && item.project_name !== 'nan') ? item.project_name.trim() : (isProject ? item.name : '');
      const hasComm = Boolean(commName);
      // Dynamic color matching current active analytical metric & palette
      const fillColor = getColorForMetric(item, STATE.activeMetric);
      const strokeColor = (isProject || hasComm) ? '#fbbf24' : (isDoor ? '#34d399' : '#38bdf8');
      const typeBadgeText = isProject ? '🏢 指標建案' : (hasComm ? `🏢 ${commName}` : (isDoor ? '📍 門牌地址' : '🛣️ 主要路段'));
      const badgeCls = (isProject || hasComm)
        ? 'bg-amber-500/20 text-amber-300 border border-amber-500/30'
        : (isDoor ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30' : 'bg-sky-500/20 text-sky-300 border border-sky-500/30');

      const marker = L.circleMarker([lat, lng], {
        pane: 'hotspotPane',
        renderer: STATE.hotspotRenderer,
        radius: radius,
        fillColor: fillColor,
        color: strokeColor,
        weight: isProject ? 2.2 : 1.8,
        opacity: 0.95,
        fillOpacity: 0.88,
      });

      // Quick hover tooltip
      let metricValueHtml = '';
      if (STATE.activeMetric === 'volume') {
        metricValueHtml = `<span class="text-blue-400 font-bold">${txCount.toLocaleString()} 筆</span>`;
      } else if (STATE.activeMetric === 'price') {
        metricValueHtml = `<span class="text-emerald-400 font-bold">${item.avg_unit_price} 萬/坪</span>`;
      } else if (STATE.activeMetric === 'heat') {
        metricValueHtml = `<span class="text-amber-300 font-bold">${item.heat_level || '持平'} (${item.heat_score || 0}分)</span>`;
      } else if (STATE.activeMetric === 'momentum') {
        const val = STATE.momentumType === 'tx'
          ? (STATE.momentumInterval === 'mom' ? item.mom_tx_change : (STATE.momentumInterval === 'qoq' ? item.qoq_tx_change : item.yoy_tx_change))
          : (STATE.momentumInterval === 'mom' ? item.mom_price_change : (STATE.momentumInterval === 'qoq' ? item.qoq_price_change : item.yoy_price_change));
        const num = Number(val || 0);
        const sign = num > 0 ? '+' : '';
        const clr = num > 0 ? 'text-red-400' : (num < 0 ? 'text-emerald-400' : 'text-slate-400');
        const metricName = STATE.momentumType === 'tx' ? '量動量' : '價動量';
        metricValueHtml = `<span class="${clr} font-bold">${sign}${num.toFixed(1)}% (${metricName})</span>`;
      }

      const addrSub = item.door_addr && !item.name.includes(item.door_addr)
        ? `${item.full_district}${item.road || ''}${item.door_addr}`
        : `${item.full_district}${item.road && !item.name.includes(item.road) ? item.road : ''}`;

      // Format Age, Building Ping, Land Ping, and Parking info
      const hasAge = item.avg_age !== null && item.avg_age !== undefined;
      const ageText = hasAge
        ? `${item.avg_age} 年${item.build_year ? ` (${item.build_year}年建)` : ''}`
        : (isProject ? '預售 / 新建案' : '—');
      const ageShort = hasAge ? `${item.avg_age}年` : (isProject ? '預售/新建' : '—');
      const bPingText = item.avg_building_ping ? `${item.avg_building_ping} 坪${txCount > 1 ? ' (均)' : ''}` : '—';
      const lPingText = item.avg_land_ping ? `${item.avg_land_ping} 坪${txCount > 1 ? ' (均)' : ''}` : '—';
      const parkCnt = item.has_parking_count || 0;
      const bType = (item.berth_type && item.berth_type !== 'nan') ? item.berth_type : '';
      let parkingHtml = '';
      let parkingShort = '';
      if (txCount === 1) {
        if (parkCnt > 0) {
          parkingHtml = `<span class="text-sky-400 font-semibold">🚗 含車位${bType ? ` (${bType})` : ''}</span>`;
          parkingShort = `🚗 含車位`;
        } else {
          parkingHtml = `<span class="text-slate-400">無車位</span>`;
          parkingShort = `無車位`;
        }
      } else {
        if (parkCnt === txCount && parkCnt > 0) {
          parkingHtml = `<span class="text-sky-400 font-semibold">🚗 全含車位 (${parkCnt}/${txCount}筆${bType ? ` · ${bType}` : ''})</span>`;
          parkingShort = `🚗 全含車位`;
        } else if (parkCnt > 0) {
          parkingHtml = `<span class="text-sky-400 font-semibold">🚗 含車位 ${parkCnt}/${txCount} 筆${bType ? ` (${bType})` : ''}</span>`;
          parkingShort = `🚗 車位 ${parkCnt}/${txCount}筆`;
        } else {
          parkingHtml = `<span class="text-slate-400">無車位 (0/${txCount}筆)</span>`;
          parkingShort = `無車位`;
        }
      }

      const hoverTip = `
        <div class="custom-tooltip-box py-1 px-2">
          <div class="flex items-center gap-1.5 text-xs font-bold text-white">
            <span class="${isProject ? 'text-amber-400' : (isDoor ? 'text-emerald-400' : 'text-sky-400')}">${typeBadgeText}</span>
            <span>${item.name}</span>
          </div>
          <div class="text-[11px] text-slate-300 mt-0.5">${addrSub}</div>
          <div class="text-[10px] text-slate-300 mt-1 flex items-center flex-wrap gap-1.5 bg-slate-900/80 px-1.5 py-0.5 rounded border border-slate-700/60">
            <span>屋齡: <b class="text-white">${ageShort}</b></span>
            <span class="text-slate-600">|</span>
            <span>建坪: <b class="text-white">${item.avg_building_ping || '—'}坪</b></span>
            <span class="text-slate-600">|</span>
            <span>地坪: <b class="text-white">${item.avg_land_ping || '—'}坪</b></span>
            <span class="text-slate-600">|</span>
            <span class="${parkCnt > 0 ? 'text-sky-300 font-semibold' : 'text-slate-400'}">${parkingShort}</span>
          </div>
          <div class="text-[11px] text-slate-200 mt-1 flex items-center justify-between gap-2 border-t border-slate-700/60 pt-1">
            <span class="text-slate-400">${getMetricDisplayName(STATE.activeMetric)}:</span>
            ${metricValueHtml}
          </div>
        </div>
      `;
      marker.bindTooltip(hoverTip, { className: 'custom-leaflet-tooltip', sticky: true });

      // Click Detailed Popup
      const formatChangePill = (val, label) => {
        if (val === null || val === undefined || isNaN(val)) {
          return `<span class="text-slate-500 text-[10px] whitespace-nowrap">${label}: —</span>`;
        }
        const num = Number(val);
        const sign = num > 0 ? '+' : '';
        const color = num > 0 ? 'text-red-400 font-semibold' : (num < 0 ? 'text-emerald-400 font-semibold' : 'text-slate-400 font-medium');
        return `<span class="text-[11px] whitespace-nowrap"><span class="text-slate-500 text-[10px] mr-0.5">${label}:</span><span class="${color}">${sign}${num.toFixed(1)}%</span></span>`;
      };

      const projExtra = (!isProject && item.projects_list && item.projects_list.length > 0)
        ? `<div class="mt-1.5 text-[11px] text-amber-300 font-medium bg-amber-500/10 p-1.5 rounded border border-amber-500/20">
             🏢 涵蓋指標建案: ${item.projects_list.join(', ')}
           </div>`
        : '';

      const buildingTypeTag = (item.building_type)
        ? `<div class="text-[11px] text-slate-400 mt-0.5">建築型態: <span class="text-slate-200">${item.building_type}</span></div>`
        : '';

      const safeComm = commName.replace(/'/g, "\\'");
      const safeName = (item.name || '').replace(/'/g, "\\'");
      const safeRoad = (item.road || '').replace(/'/g, "\\'");
      const safeDoor = (item.door_addr || '').replace(/'/g, "\\'");
      const popupTitle = (hasComm && !isProject) ? `${commName} <span class="text-xs font-normal text-slate-300">(${item.name})</span>` : item.name;

      const popupHtml = `
        <div class="p-1.5 min-w-[260px]">
          <div class="flex items-center justify-between pb-1.5 mb-2 border-b border-slate-700">
            <div>
              <div class="flex items-center gap-1 mb-1">
                <span class="inline-block px-1.5 py-0.5 text-[10px] rounded font-semibold ${badgeCls}">${typeBadgeText}</span>
                <button class="px-1.5 py-0.5 text-[10px] rounded bg-slate-800 hover:bg-slate-700 text-amber-300 border border-slate-700 transition"
                  onclick="promptTagCommunityName('${item.full_district}', '${safeName}', '${safeComm}', '${safeRoad}', '${safeDoor}')"
                  title="標註或修改此門牌對應之建案名稱">
                  🏷️ ${hasComm ? '改名' : '標註建案'}
                </button>
              </div>
              <div class="font-bold text-sm text-white">${popupTitle}</div>
              <div class="text-[11px] text-slate-400">${addrSub}</div>
              ${buildingTypeTag}
            </div>
            <span class="text-[10px] px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-300 font-mono self-start">${timeLabel}</span>
          </div>

          <table class="w-full text-xs" style="border-collapse: separate; border-spacing: 0 3px;">
            <tr>
              <td class="text-slate-400">屋齡</td>
              <td class="text-right font-semibold text-amber-300">${ageText}</td>
            </tr>
            <tr>
              <td class="text-slate-400">移轉建坪</td>
              <td class="text-right font-semibold text-slate-100">${bPingText}</td>
            </tr>
            <tr>
              <td class="text-slate-400">土地地坪</td>
              <td class="text-right font-medium text-slate-200">${lPingText}</td>
            </tr>
            <tr>
              <td class="text-slate-400">有無車位</td>
              <td class="text-right">${parkingHtml}</td>
            </tr>
            <tr>
              <td colspan="2"><div class="border-t border-slate-700/70 my-1"></div></td>
            </tr>
            <tr>
              <td class="text-slate-400">成交筆數</td>
              <td class="text-right font-bold text-blue-400">${txCount.toLocaleString()} 筆</td>
            </tr>
            <tr>
              <td class="text-slate-400 align-top pt-0.5">量動量</td>
              <td class="text-right space-x-1 pt-0.5">
                ${formatChangePill(item.mom_tx_change, '月')}
                ${formatChangePill(item.qoq_tx_change, '季')}
                ${formatChangePill(item.yoy_tx_change, '年')}
              </td>
            </tr>
            <tr>
              <td class="text-slate-400">平均單價</td>
              <td class="text-right font-bold text-emerald-400">${item.avg_unit_price} 萬/坪</td>
            </tr>
            <tr>
              <td class="text-slate-400 align-top pt-0.5">價動量</td>
              <td class="text-right space-x-1 pt-0.5">
                ${formatChangePill(item.mom_price_change, '月')}
                ${formatChangePill(item.qoq_price_change, '季')}
                ${formatChangePill(item.yoy_price_change, '年')}
              </td>
            </tr>
            <tr>
              <td class="text-slate-400">單價區間</td>
              <td class="text-right text-slate-300 text-[11px]">${item.min_unit_price} ~ ${item.max_unit_price} 萬/坪</td>
            </tr>
            <tr>
              <td class="text-slate-400">平均總價</td>
              <td class="text-right text-slate-200 font-medium">${item.avg_total_price} 萬元</td>
            </tr>
            <tr>
              <td class="text-slate-400">熱度評估</td>
              <td class="text-right">
                <span class="inline-flex items-center px-1.5 py-0.2 rounded text-[10px] font-medium bg-amber-500/20 text-amber-300 border border-amber-500/30">
                  ${item.heat_level || '持平'} (${item.heat_score || 0}分)
                </span>
              </td>
            </tr>
          </table>

          ${projExtra}

          <div class="mt-2.5 pt-2 border-t border-slate-700/80 flex flex-col gap-1.5">
            <button class="w-full py-1.5 px-2 text-center bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 text-white rounded text-[11px] font-semibold transition shadow"
              onclick="openCommunityTrendModal('${item.full_district}', '${safeComm}', '${safeName}', '${safeRoad}', '${safeDoor}')">
              📈 查看建案歷年價格走勢與轉手獲利
            </button>
            <div class="flex items-center gap-1.5">
              <button class="flex-1 py-1 px-2 text-center bg-blue-600 hover:bg-blue-500 text-white rounded text-[11px] font-medium transition"
                onclick="openTransactionDrawer('${timeLabel}', '${item.full_district}', '${safeRoad}', ${isProject ? `'${safeName}'` : 'null'}, ${isDoor ? `'${safeName}'` : 'null'})">
                查看交易明細 &rarr;
              </button>
              <button class="py-1 px-2 text-center bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded text-[11px] font-medium transition"
                onclick="flyToLocation(${lat}, ${lng}, 16, '${safeName}')" title="聚焦放大此地點">
                🔍 放大
              </button>
            </div>
          </div>
        </div>
      `;

      marker.bindPopup(popupHtml);
      STATE.markersLayer.addLayer(marker);
    });
  } catch (err) {
    console.error('Failed to render location hotspots:', err);
  }
}

// 6. Region Selection
async function selectRegion(regionName, layer = null) {
  STATE.selectedRegion = regionName;
  syncRegionDropdowns(regionName);

  const levelTitle = STATE.activeLevel === 'county' ? '縣市詳情' : (STATE.activeLevel === 'road' ? '路段建案分析' : '鄉鎮市區詳情');
  const levelBadge = document.getElementById('aside-level-badge');
  const regionNameEl = document.getElementById('aside-region-name');
  if (levelBadge) levelBadge.innerText = levelTitle;
  if (regionNameEl) regionNameEl.innerText = regionName;

  if (STATE.geojsonLayer) {
    STATE.geojsonLayer.setStyle((feature) => {
      const p = feature.properties;
      const isSelected = STATE.selectedRegion &&
        (p.COUNTYNAME === STATE.selectedRegion || p.FULLNAME === STATE.selectedRegion);

      if (STATE.activeLevel === 'road') {
        // 細分模式：不塗任何色塊，只要細緻框出鄉鎮市區輪廓！
        return {
          fill: false,
          fillOpacity: 0,
          fillColor: 'transparent',
          weight: isSelected ? 2.5 : 1.2,
          opacity: isSelected ? 1 : 0.65,
          color: isSelected ? '#38bdf8' : '#64748b',
          dashArray: isSelected ? null : '3, 4',
        };
      }

      return {
        fillColor: getColorForMetric(p, STATE.activeMetric),
        weight: isSelected ? 3 : 1,
        color: isSelected ? '#fbbf24' : '#334155',
        fillOpacity: isSelected ? 0.9 : 0.75,
      };
    });
  }

  // Smoothly zoom into the selected district or county
  if (!layer && STATE.geojsonLayer) {
    let combinedBounds = null;
    let matchCount = 0;
    STATE.geojsonLayer.eachLayer(l => {
      const p = l.feature && l.feature.properties;
      if (p) {
        if (p.FULLNAME === regionName || p.TOWNNAME === regionName) {
          layer = l;
        } else if (p.COUNTYNAME === regionName && typeof l.getBounds === 'function') {
          matchCount++;
          if (!combinedBounds) {
            combinedBounds = L.latLngBounds(l.getBounds().getSouthWest(), l.getBounds().getNorthEast());
          } else {
            combinedBounds.extend(l.getBounds());
          }
        }
      }
    });

    if (matchCount > 0 && (!layer || matchCount > 1) && combinedBounds && combinedBounds.isValid()) {
      STATE.map.fitBounds(combinedBounds, { padding: [30, 30], maxZoom: 14 });
      layer = null;
    }
  }
  if (layer && typeof layer.getBounds === 'function') {
    STATE.map.fitBounds(layer.getBounds(), { padding: [30, 30], maxZoom: 15 });
  }

  // Determine whether regionName is a county or a town
  const isCounty = STATE.regionTree && STATE.regionTree[regionName] !== undefined;
  const queryLevel = isCounty ? 'county' : (STATE.activeLevel === 'county' ? 'county' : 'town');
  await Promise.all([
    loadRegionDetail(queryLevel, regionName),
    loadTopRoads(regionName),
    loadTopProjects(regionName),
    STATE.activeLevel === 'road' ? renderLocationHotspots(null) : Promise.resolve()
  ]);
}

// 7. Load Region Details
async function loadRegionDetail(level, name) {
  const currentPeriod = STATE.periods[STATE.currentIndex];
  try {
    const res = await apiFetch(`/api/trends?level=${level}&name=${encodeURIComponent(name)}${getAgeParams()}`);
    const data = await res.json();
    const series = data.series || [];

    let currRecord;
    if (STATE.timeMode === 'range' && STATE.rangeStart && STATE.rangeEnd) {
      // Multi-quarter interval aggregation
      const inRangeSeries = series.filter(s => s.period >= STATE.rangeStart && s.period <= STATE.rangeEnd);
      if (inRangeSeries.length > 0) {
        const totalTx = inRangeSeries.reduce((acc, s) => acc + (s.tx_count || 0), 0);
        const totalAmount = inRangeSeries.reduce((acc, s) => acc + (s.total_amount_yi || 0), 0);
        const weightedPrice = totalTx > 0
          ? inRangeSeries.reduce((acc, s) => acc + ((s.avg_unit_price || 0) * (s.tx_count || 0)), 0) / totalTx
          : 0;
        const avgHeat = inRangeSeries.reduce((acc, s) => acc + (s.heat_score || 0), 0) / inRangeSeries.length;
        currRecord = {
          tx_count: totalTx,
          total_amount_yi: Math.round(totalAmount * 10) / 10,
          avg_unit_price: Math.round(weightedPrice * 10) / 10,
          median_unit_price: Math.round(weightedPrice * 10) / 10,
          heat_score: Math.round(avgHeat * 10) / 10,
          heat_level: avgHeat >= 75 ? '爆發熱絡' : (avgHeat >= 58 ? '穩健成長' : (avgHeat >= 42 ? '盤整持平' : '冷卻觀望')),
          mom_tx_change: null,
          qoq_tx_change: null,
          yoy_tx_change: null,
          mom_price_change: null,
          qoq_price_change: null,
          yoy_price_change: null
        };
      } else {
        currRecord = {};
      }
    } else {
      currRecord = series.find(s => s.period === currentPeriod) || {};
    }

    updateKPICards(currRecord, name);
    updateTrendChart(series, name);
  } catch (err) {
    console.error('Failed to load region detail:', err);
  }
}

// 8. Load National Summary
async function loadNationalSummary() {
  try {
    const res = await apiFetch(`/api/summary?${getTimeParams().replace(/^&/, '')}${getAgeParams()}`);
    const summary = await res.json();

    document.getElementById('aside-level-badge').innerText = '全國總覽';
    document.getElementById('aside-region-name').innerText = '全台灣 (全國合計)';

    updateKPICards({
      tx_count: summary.total_tx,
      mom_tx_change: summary.mom_tx_change,
      qoq_tx_change: summary.qoq_tx_change,
      yoy_tx_change: summary.yoy_tx_change,
      avg_unit_price: summary.avg_unit_price,
      mom_price_change: summary.mom_price_change,
      qoq_price_change: summary.qoq_price_change,
      yoy_price_change: summary.yoy_price_change,
      total_amount_yi: summary.total_amount_yi,
      median_unit_price: summary.avg_unit_price,
      heat_score: summary.heat_score,
      heat_level: summary.heat_score >= 75 ? '爆發熱絡' : (summary.heat_score >= 58 ? '穩健成長' : (summary.heat_score >= 42 ? '盤整持平' : '冷卻觀望')),
    }, '全國');
  } catch (err) {
    console.error('Failed to load national summary:', err);
  }
}

function updateKPICards(record, title) {
  const vol = record.tx_count || 0;
  document.getElementById('kpi-volume').innerText = vol.toLocaleString();

  const formatKpiChange = (val, label) => {
    if (val === null || val === undefined || isNaN(val) || vol === 0) {
      return `<span title="${label}增率"><span class="text-slate-500 text-[10px]">${label}</span> <span class="text-slate-500 font-medium">—</span></span>`;
    }
    const num = Number(val);
    const sign = num > 0 ? '+' : '';
    const color = num > 0 ? 'text-red-400 font-medium' : (num < 0 ? 'text-emerald-400 font-medium' : 'text-slate-400 font-medium');
    return `<span title="${label}增率"><span class="text-slate-500 text-[10px]">${label}</span> <span class="${color}">${sign}${num.toFixed(1)}%</span></span>`;
  };

  const volEl = document.getElementById('kpi-vol-growth');
  volEl.innerHTML = `
    <div class="flex items-center gap-1.5 text-xs flex-wrap font-mono">
      ${formatKpiChange(record.mom_tx_change, '月')}
      <span class="text-slate-700">|</span>
      ${formatKpiChange(record.qoq_tx_change, '季')}
      <span class="text-slate-700">|</span>
      ${formatKpiChange(record.yoy_tx_change, '年')}
    </div>
  `;

  const price = record.avg_unit_price || 0;
  document.getElementById('kpi-price').innerText = `${price} 萬/坪`;

  const priceEl = document.getElementById('kpi-price-growth');
  priceEl.innerHTML = `
    <div class="flex items-center gap-1.5 text-xs flex-wrap font-mono">
      ${formatKpiChange(record.mom_price_change, '月')}
      <span class="text-slate-700">|</span>
      ${formatKpiChange(record.qoq_price_change, '季')}
      <span class="text-slate-700">|</span>
      ${formatKpiChange(record.yoy_price_change, '年')}
    </div>
  `;

  document.getElementById('kpi-total-amount').innerText = (record.total_amount_yi || 0).toLocaleString();
  document.getElementById('kpi-median-price').innerText = `中位數: ${record.median_unit_price || 0} 萬/坪`;

  const heat = record.heat_score || 0;
  const level = record.heat_level || '無資料';
  document.getElementById('kpi-heat').innerText = `${heat} 分`;
  document.getElementById('kpi-heat-desc').innerText = `評估等級: ${level}`;

  const badge = document.getElementById('aside-heat-badge');
  badge.innerText = level;
  badge.className = 'text-xs font-semibold px-2 py-0.5 rounded-full';
  if (level === '爆發熱絡') badge.classList.add('heat-badge-hot');
  else if (level === '穩健成長') badge.classList.add('heat-badge-warm');
  else if (level === '盤整持平') badge.classList.add('heat-badge-neutral');
  else badge.classList.add('heat-badge-cool');
}

// 9. Chart.js Dual-Axis Historical Trend Chart with Click Interaction!
function initChart() {
  const ctx = document.getElementById('trendChart').getContext('2d');
  STATE.chart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: [],
      datasets: [
        {
          label: '成交量 (筆)',
          data: [],
          backgroundColor: 'rgba(59, 130, 246, 0.45)',
          borderColor: '#3b82f6',
          borderWidth: 1,
          yAxisID: 'yVolume',
          order: 2,
        },
        {
          type: 'line',
          label: '平均單價 (萬/坪)',
          data: [],
          borderColor: '#10b981',
          backgroundColor: 'rgba(16, 185, 129, 0.1)',
          borderWidth: 2.5,
          tension: 0.25,
          yAxisID: 'yPrice',
          order: 1,
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      // CLICK ON TREND CHART TO VIEW DETAILED TRANSACTIONS!
      onClick: async (event, elements) => {
        if (elements && elements.length > 0) {
          const index = elements[0].index;
          const clickedPeriod = STATE.chart.data.labels[index];
          if (!clickedPeriod) return;

          // Sync timeline slider & map view
          const periodIdx = STATE.periods.indexOf(clickedPeriod);
          if (periodIdx !== -1 && periodIdx !== STATE.currentIndex) {
            STATE.currentIndex = periodIdx;
            document.getElementById('period-slider').value = periodIdx;
            updatePeriodDisplay();
            await updateView();
          }
          // Open detailed transactions explorer!
          openTransactionDrawer(clickedPeriod, STATE.selectedRegion);
        }
      },
      plugins: {
        legend: {
          labels: { color: '#94a3b8', font: { size: 10 } }
        },
        tooltip: {
          backgroundColor: 'rgba(15, 23, 42, 0.95)',
          titleColor: '#fff',
          bodyColor: '#cbd5e1',
          borderColor: '#334155',
          borderWidth: 1,
          callbacks: {
            footer: (items) => {
              const p = items[0]?.label;
              return `👉 點擊查看【${p}】成交明細`;
            }
          }
        }
      },
      scales: {
        x: {
          ticks: { color: '#64748b', font: { size: 9 }, maxRotation: 45 },
          grid: { display: false },
        },
        yVolume: {
          type: 'linear',
          position: 'left',
          ticks: { color: '#60a5fa', font: { size: 9 } },
          grid: { color: 'rgba(51, 65, 85, 0.3)' },
        },
        yPrice: {
          type: 'linear',
          position: 'right',
          ticks: { color: '#34d399', font: { size: 9 } },
          grid: { display: false },
        }
      }
    }
  });
}

function updateTrendChart(series, title) {
  if (!STATE.chart) return;
  document.getElementById('chart-sub-label').innerText = `${title} 歷年走勢`;

  const currentPeriod = STATE.periods[STATE.currentIndex];
  const chartBtnLabel = document.getElementById('chart-btn-period-label');
  if (chartBtnLabel) chartBtnLabel.innerText = currentPeriod;

  const labels = series.map(s => s.period);
  const volumes = series.map(s => s.tx_count);
  const prices = series.map(s => s.avg_unit_price);

  // Dynamic bar styling: brightly highlight the current active period!
  const bgColors = labels.map(p => (p === currentPeriod ? 'rgba(245, 158, 11, 0.95)' : 'rgba(59, 130, 246, 0.35)'));
  const borderColors = labels.map(p => (p === currentPeriod ? '#fbbf24' : '#3b82f6'));
  const borderWidths = labels.map(p => (p === currentPeriod ? 2.5 : 1));

  STATE.chart.data.labels = labels;
  STATE.chart.data.datasets[0].data = volumes;
  STATE.chart.data.datasets[0].backgroundColor = bgColors;
  STATE.chart.data.datasets[0].borderColor = borderColors;
  STATE.chart.data.datasets[0].borderWidth = borderWidths;

  STATE.chart.data.datasets[1].data = prices;
  STATE.chart.data.datasets[1].pointRadius = labels.map(p => (p === currentPeriod ? 6 : 2));
  STATE.chart.data.datasets[1].pointBackgroundColor = labels.map(p => (p === currentPeriod ? '#f59e0b' : '#10b981'));
  STATE.chart.data.datasets[1].pointBorderColor = labels.map(p => (p === currentPeriod ? '#fff' : '#10b981'));

  STATE.chart.update();
}

async function loadHistoricalTrend(level, name) {
  try {
    const res = await apiFetch(`/api/trends?level=${level}&name=${encodeURIComponent(name)}${getAgeParams()}`);
    const data = await res.json();
    updateTrendChart(data.series || [], name);
  } catch (err) {
    console.error('Failed to load historical trend:', err);
  }
}

// 10. Top 10 Ranking Leaderboard
async function loadRanking() {
  const metricSelect = document.getElementById('select-ranking-metric').value;
  const levelParam = STATE.activeLevel === 'road' ? 'town' : STATE.activeLevel;
  try {
    const res = await apiFetch(`/api/ranking?metric=${metricSelect}&level=${levelParam}&limit=10${getTimeParams()}${getAgeParams()}`);
    const data = await res.json();
    const ranking = data.ranking || [];

    const container = document.getElementById('ranking-list');
    container.innerHTML = '';

    if (ranking.length === 0) {
      container.innerHTML = '<div class="text-slate-500 text-center py-4">無排行資料</div>';
      return;
    }

    ranking.forEach((item, idx) => {
      const row = document.createElement('div');
      row.className = 'flex items-center justify-between p-2 rounded-lg bg-slate-950/70 hover:bg-slate-800/80 cursor-pointer transition border border-slate-800/60';

      const rankBadge = idx < 3
        ? `<span class="w-5 h-5 rounded-full bg-amber-500/20 text-amber-400 font-bold flex items-center justify-center text-[10px]">${idx + 1}</span>`
        : `<span class="w-5 h-5 rounded-full bg-slate-800 text-slate-400 font-bold flex items-center justify-center text-[10px]">${idx + 1}</span>`;

      row.innerHTML = `
        <div class="flex items-center space-x-2.5">
          ${rankBadge}
          <div>
            <div class="font-medium text-slate-200">${item.region_name}</div>
            <div class="text-[10px] text-slate-400">成交: ${item.tx_count} 筆 | 單價: ${item.avg_unit_price} 萬/坪</div>
          </div>
        </div>
        <div class="text-right">
          <div class="font-semibold ${item.heat_score >= 75 ? 'text-red-400' : 'text-orange-400'}">${item.heat_score} 分</div>
          <div class="text-[10px] text-slate-400">${item.heat_level}</div>
        </div>
      `;

      row.addEventListener('click', () => {
        selectRegion(item.region_name);
      });

      container.appendChild(row);
    });
  } catch (err) {
    console.error('Failed to load ranking:', err);
  }
}

// 11. Top Roads Tab Loader
async function loadTopRoads(region) {
  const container = document.getElementById('roads-list');
  try {
    const res = await apiFetch(`/api/roads?limit=20${getTimeParams()}${getAgeParams()}${region ? `&region=${encodeURIComponent(region)}` : ''}`);
    const data = await res.json();
    const roads = data.roads || [];

    container.innerHTML = '';
    if (roads.length === 0) {
      container.innerHTML = '<div class="text-slate-500 text-center py-4">此期無路段資料</div>';
      return;
    }

    const timeLabel = getTimeLabel();
    roads.forEach(r => {
      const el = document.createElement('div');
      el.className = 'p-2 rounded-lg bg-slate-950/70 hover:bg-slate-800/80 cursor-pointer transition border border-slate-800/60';
      const projBadge = r.projects_list && r.projects_list.length > 0
        ? `<div class="text-[10px] text-amber-300 font-medium truncate mt-0.5">🏢 建案: ${r.projects_list.join(', ')}</div>`
        : '';

      const locateBtn = (r.lat && r.lng)
        ? `<button class="locate-btn px-2 py-0.5 rounded bg-sky-500/20 hover:bg-sky-500/40 text-sky-300 border border-sky-500/30 text-[10px] font-medium transition"
             title="在地圖上定位此路段">
             📍 定位
           </button>`
        : '';

      el.innerHTML = `
        <div class="flex items-center justify-between">
          <div class="font-medium text-slate-200">${r.full_district} ${r.road}</div>
          <div class="flex items-center gap-1.5">
            <span class="font-bold text-emerald-400 text-xs">${r.avg_unit_price} 萬/坪</span>
            ${locateBtn}
          </div>
        </div>
        <div class="flex items-center justify-between text-[10px] text-slate-400 mt-0.5">
          <span>成交: ${r.tx_count} 筆 | 均總價: ${r.avg_total_price} 萬</span>
          <span>單價: ${r.min_unit_price}~${r.max_unit_price} 萬</span>
        </div>
        ${projBadge}
      `;

      el.addEventListener('click', (e) => {
        if (e.target.closest('.locate-btn')) {
          e.stopPropagation();
          switchToRoadLevelAndFly(r.lat, r.lng, 15, `${r.full_district} ${r.road}`);
          return;
        }
        openTransactionDrawer(timeLabel, r.full_district, r.road);
      });

      container.appendChild(el);
    });
  } catch (err) {
    console.error('Failed to load top roads:', err);
  }
}

// 12. Top Building Projects Tab Loader
async function loadTopProjects(region) {
  const container = document.getElementById('projects-list');
  try {
    const res = await apiFetch(`/api/projects?limit=25${getTimeParams()}${getAgeParams()}${region ? `&region=${encodeURIComponent(region)}` : ''}`);
    const data = await res.json();
    const projects = data.projects || [];

    container.innerHTML = '';
    if (projects.length === 0) {
      container.innerHTML = '<div class="text-slate-500 text-center py-4">無預售建案資料</div>';
      return;
    }

    const timeLabel = getTimeLabel();
    projects.forEach(p => {
      const el = document.createElement('div');
      el.className = 'p-2 rounded-lg bg-slate-950/70 hover:bg-slate-800/80 cursor-pointer transition border border-slate-800/60';

      const locateBtn = (p.lat && p.lng)
        ? `<button class="locate-btn px-2 py-0.5 rounded bg-amber-500/20 hover:bg-amber-500/40 text-amber-300 border border-amber-500/30 text-[10px] font-medium transition"
             title="在地圖上定位此建案">
             📍 定位
           </button>`
        : '';

      const trendBtn = `<button class="trend-btn px-2 py-0.5 rounded bg-purple-600/30 hover:bg-purple-600/50 text-purple-200 border border-purple-500/40 text-[10px] font-medium transition"
             title="查看此建案歷年價格走勢與同戶轉手獲利">
             📈 走勢
           </button>`;

      el.innerHTML = `
        <div class="flex items-center justify-between">
          <div class="font-bold text-amber-400 flex items-center gap-1.5 truncate">
            <span class="w-1.5 h-1.5 rounded-full bg-amber-400 shrink-0"></span>
            <span class="truncate">${p.project_name}</span>
          </div>
          <div class="flex items-center gap-1 shrink-0">
            <span class="font-bold text-emerald-400 text-xs">${p.avg_unit_price} 萬/坪</span>
            ${trendBtn}
            ${locateBtn}
          </div>
        </div>
        <div class="text-[10px] text-slate-300 mt-0.5">${p.full_district} ${p.road || ''}${p.door_addr ? p.door_addr : ''}</div>
        <div class="flex items-center justify-between text-[10px] text-slate-400 mt-1">
          <span>揭露成交: ${p.tx_count} 筆 | 型態: ${p.building_type || '大樓'}</span>
          <span>均總價: ${p.avg_total_price} 萬</span>
        </div>
      `;

      el.addEventListener('click', (e) => {
        if (e.target.closest('.locate-btn')) {
          e.stopPropagation();
          switchToRoadLevelAndFly(p.lat, p.lng, 16, `${p.project_name} (${p.full_district})`);
          return;
        }
        if (e.target.closest('.trend-btn')) {
          e.stopPropagation();
          openCommunityTrendModal(p.full_district, p.project_name, p.project_name, p.road || '', p.door_addr || '');
          return;
        }
        openCommunityTrendModal(p.full_district, p.project_name, p.project_name, p.road || '', p.door_addr || '');
      });

      container.appendChild(el);
    });
  } catch (err) {
    console.error('Failed to load top projects:', err);
  }
}

// 13. Detailed Transactions Modal Drawer
async function openTransactionDrawer(period, region, road, project, door, defaultKeyword = '') {
  if (STATE.map) {
    STATE.map.closePopup();
  }
  const modal = document.getElementById('tx-modal');
  modal.classList.remove('hidden');

  if (defaultKeyword) {
    const kwInput = document.getElementById('tx-search-keyword');
    if (kwInput) kwInput.value = defaultKeyword;
    const chkAllTime = document.getElementById('chk-tx-all-time');
    if (chkAllTime) chkAllTime.checked = true;
  }

  const timeLabel = period || getTimeLabel();
  let title = '實價登錄成交明細';
  if (door) title = `【${door}】門牌成交明細`;
  else if (project) title = `【${project}】建案成交明細`;
  else if (road) title = `${region || ''} ${road} 成交明細`;
  else if (region) title = `${region} 成交明細`;
  else if (defaultKeyword) title = `【${defaultKeyword}】搜尋成交明細`;

  document.getElementById('tx-modal-title').innerText = title;
  document.getElementById('tx-modal-sub').innerText = `篩選: 週期 ${timeLabel} | 區域 ${region || '全國'} ${road ? `| 路段 ${road}` : ''} ${door ? `| 門牌 ${door}` : ''}`;

  const modalAgeSelect = document.getElementById('tx-filter-age');
  if (modalAgeSelect && STATE.activeAgeFilter) {
    modalAgeSelect.value = STATE.activeAgeFilter;
  }

  STATE.modalCurrentParams = { period: timeLabel, region, road, project, door };
  await fetchAndRenderTransactions(timeLabel, region, road, project, door);
}

async function fetchAndRenderTransactions(period, region, road, project, door) {
  const keyword = document.getElementById('tx-search-keyword').value.trim();
  const sort = document.getElementById('tx-sort-select').value;
  const ageFilter = document.getElementById('tx-filter-age') ? document.getElementById('tx-filter-age').value : 'all';
  const chkAllTime = document.getElementById('chk-tx-all-time');
  let isAllTime = chkAllTime && chkAllTime.checked;
  if (keyword && !isAllTime && chkAllTime) {
    chkAllTime.checked = true;
    isAllTime = true;
  }
  const chkSpecial = document.getElementById('tx-filter-special');
  const excludeSpecial = chkSpecial ? chkSpecial.checked : false;
  const tbody = document.getElementById('tx-table-body');
  tbody.innerHTML = '<tr><td colspan="7" class="py-8 text-center text-slate-400">載入明細中...</td></tr>';

  // If the user typed a full address/district/road keyword, do not restrict to stale region/road/project/door
  const kwHasOwnLocation = keyword && /[市縣區鄉鎮路街大道段號]/.test(keyword);
  const effRegion = kwHasOwnLocation ? null : region;
  const effRoad = kwHasOwnLocation ? null : road;
  const effProject = kwHasOwnLocation ? null : project;
  const effDoor = kwHasOwnLocation ? null : door;

  const timeQuery = isAllTime ? '&period=all' : getTimeParams();
  let url = `/api/transactions?limit=100&sort_by=${sort}${timeQuery}${excludeSpecial ? '&exclude_special=true' : ''}`;
  if (effRegion) url += `&region=${encodeURIComponent(effRegion)}`;
  if (effRoad) url += `&road=${encodeURIComponent(effRoad)}`;
  if (effProject) url += `&project=${encodeURIComponent(effProject)}`;
  if (effDoor) url += `&door=${encodeURIComponent(effDoor)}`;
  if (keyword) url += `&keyword=${encodeURIComponent(keyword)}`;

  if (ageFilter === '0-5') url += '&min_age=0&max_age=5';
  else if (ageFilter === '5-10') url += '&min_age=5.001&max_age=10';
  else if (ageFilter === '10-20') url += '&min_age=10.001&max_age=20';
  else if (ageFilter === '20-30') url += '&min_age=20.001&max_age=30';
  else if (ageFilter === '30-999') url += '&min_age=30.001';

  try {
    const res = await apiFetch(url);
    const data = await res.json();
    const items = data.items || [];
    document.getElementById('tx-modal-count').innerText = `${data.total.toLocaleString()} 筆`;

    tbody.innerHTML = '';
    if (items.length === 0) {
      if (!isAllTime) {
        tbody.innerHTML = `
          <tr>
            <td colspan="7" class="py-10 text-center text-slate-400">
              <div class="text-sm font-medium mb-1.5 text-slate-300">於當前週期 (${period}) 查無成交記錄</div>
              <div class="text-xs text-slate-500 mb-3">此標的之成交可能發生在其他年份或季度 (例如 2016 或 2018 年)</div>
              <button onclick="document.getElementById('chk-tx-all-time').checked = true; const p = STATE.modalCurrentParams || {}; fetchAndRenderTransactions(p.period, p.region, p.road, p.project, p.door);"
                class="px-3.5 py-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-xs font-semibold shadow transition inline-flex items-center gap-1.5">
                📅 切換搜尋「全部歷史年份」
              </button>
            </td>
          </tr>
        `;
      } else {
        tbody.innerHTML = '<tr><td colspan="7" class="py-8 text-center text-slate-500">查無符合條件之成交記錄</td></tr>';
      }
      return;
    }

    items.forEach(it => {
      const tr = document.createElement('tr');
      tr.className = 'hover:bg-slate-800/60 transition text-slate-200 cursor-pointer group';
      tr.title = '點擊展開此戶與同門牌歷年成交紀錄';

      const safeProj = (it.project_name || '').replace(/'/g, "\\'");
      const safeDist = (it.full_district || '').replace(/'/g, "\\'");
      const safeRoad = (it.road || '').replace(/'/g, "\\'");
      const projTag = it.project_name
        ? `<button onclick="event.stopPropagation(); openCommunityTrendModal('${safeDist}', '${safeProj}', '${safeProj}', '${safeRoad}', '')" title="點擊查看【${it.project_name}】建案價格走勢與轉手獲利" class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-amber-500/20 hover:bg-amber-500/35 text-amber-300 font-semibold border border-amber-500/30 ml-1 cursor-pointer transition">🏢 建案: ${it.project_name} 📈</button>`
        : '';

      const specialTag = it.is_special_trade === 1
        ? `<span class="inline-block px-1 py-0.2 rounded text-[9px] bg-red-500/20 text-red-400 border border-red-500/30 ml-1" title="${it.notes}">特殊交易</span>`
        : '';

      let ageTag = '';
      if (it.building_age !== null && it.building_age !== undefined) {
        const age = it.building_age;
        const bYear = it.build_year ? `(${it.build_year}年建)` : '';
        if (age <= 5) {
          ageTag = `<span class="inline-block px-1.5 py-0.5 rounded text-[10px] bg-emerald-500/20 text-emerald-300 font-medium border border-emerald-500/30 ml-1">新成屋 ${age}年 ${bYear}</span>`;
        } else if (age <= 15) {
          ageTag = `<span class="inline-block px-1.5 py-0.5 rounded text-[10px] bg-blue-500/20 text-blue-300 font-medium border border-blue-500/30 ml-1">次新屋 ${age}年 ${bYear}</span>`;
        } else if (age <= 30) {
          ageTag = `<span class="inline-block px-1.5 py-0.5 rounded text-[10px] bg-amber-500/20 text-amber-300 font-medium border border-amber-500/30 ml-1">中古屋 ${age}年 ${bYear}</span>`;
        } else {
          ageTag = `<span class="inline-block px-1.5 py-0.5 rounded text-[10px] bg-purple-500/20 text-purple-300 font-medium border border-purple-500/30 ml-1">老屋 ${age}年 ${bYear}</span>`;
        }
      }

      const priceColor = it.unit_price_wan_ping > 60 ? 'text-red-400' : (it.unit_price_wan_ping > 35 ? 'text-amber-400' : 'text-emerald-400');

      const hasPark = (it.target_type && it.target_type.includes('車位')) || (it.berth_category && it.berth_category !== 'nan') || (it.berth_area_ping > 0);
      const bCat = (it.berth_category && it.berth_category !== 'nan') ? it.berth_category : '';
      const parkBadge = hasPark
        ? `<div class="mt-0.5"><span class="inline-block px-1.5 py-0.5 rounded text-[10px] bg-sky-500/20 text-sky-300 border border-sky-500/30 font-medium">🚗 含車位${bCat ? ` (${bCat})` : ''}${it.berth_area_ping > 0 ? ` · ${it.berth_area_ping}坪` : ''}</span></div>`
        : `<div class="mt-0.5"><span class="inline-block px-1.5 py-0.5 rounded text-[10px] bg-slate-800 text-slate-400 border border-slate-700/60">無車位</span></div>`;

      const safeAddr = (it.address || `${it.full_district || ''}${it.road || ''}`).replace(/'/g, "\\'");
      const locateBtn = `<button onclick="event.stopPropagation(); locateAddressOnMap('${safeAddr}')" title="在地圖上定位此門牌位置" class="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] bg-blue-500/20 hover:bg-blue-500/35 text-blue-300 border border-blue-500/30 ml-1 cursor-pointer transition">📍 地圖定位</button>`;

      tr.innerHTML = `
        <td class="py-2.5 text-slate-400 font-mono">${it.trade_date || '--'}</td>
        <td class="py-2.5">
          <div class="font-medium text-slate-100 flex items-center flex-wrap">
            <span>${it.full_district} ${it.road || ''}</span>
            ${projTag}
            ${locateBtn}
            ${specialTag}
          </div>
          <div class="text-[11px] text-slate-300 font-mono truncate max-w-xs">${it.address || ''}</div>
        </td>
        <td class="py-2.5">
          <div class="flex items-center flex-wrap">${it.building_type || '住宅'} ${ageTag}</div>
          <div class="text-[11px] text-slate-300 font-medium mt-0.5">${it.floor || '--'} / 共${it.total_floors || '--'}</div>
        </td>
        <td class="py-2.5">
          <div class="font-semibold text-slate-200">建坪: ${it.building_area_ping || 0} 坪</div>
          <div class="text-[11px] text-amber-300/90 mt-0.5">土地: ${it.land_area_ping || 0} 坪</div>
        </td>
        <td class="py-2.5 text-slate-300">
          <div>${it.room_count}房 ${it.hall_count}廳 ${it.bath_count}衛</div>
          ${parkBadge}
        </td>
        <td class="py-2.5 text-right font-bold ${priceColor}">${it.unit_price_wan_ping} 萬/坪</td>
        <td class="py-2.5 text-right">
          <div class="font-bold text-slate-100">${it.total_price_wan.toLocaleString()} 萬元</div>
          <div class="mt-0.5">
            <span class="tx-hist-toggle-badge inline-block px-2 py-0.5 rounded-full text-[10px] bg-blue-500/15 group-hover:bg-blue-500/30 text-blue-300 border border-blue-500/30 transition">
              🔎 歷年紀錄 ▾
            </span>
          </div>
        </td>
      `;

      tr.addEventListener('click', async () => {
        const nextRow = tr.nextElementSibling;
        if (nextRow && nextRow.classList.contains('unit-history-expand-row')) {
          nextRow.remove();
          tr.classList.remove('bg-slate-800/70');
          return;
        }
        tr.classList.add('bg-slate-800/70');
        const expandTr = document.createElement('tr');
        expandTr.className = 'unit-history-expand-row bg-slate-950/95 border-b border-slate-800';
        expandTr.innerHTML = `<td colspan="7" class="p-3 text-center text-slate-400 text-xs">正在查詢「${it.address || ''}」歷年買賣紀錄...</td>`;
        tr.insertAdjacentElement('afterend', expandTr);

        try {
          const qParams = new URLSearchParams({
            region: it.full_district || '',
            road: it.road || '',
            address: it.address || '',
            floor: it.floor || ''
          });
          const hRes = await apiFetch(`/api/unit/history?${qParams.toString()}`);
          const hData = await hRes.json();
          expandTr.innerHTML = `<td colspan="7" class="p-3">${buildUnitHistoryExpansionHtml(it, hData.same_unit_records || [], hData.same_door_records || [], hData.unit_label, hData.door_label)}</td>`;
        } catch (e) {
          expandTr.innerHTML = `<td colspan="7" class="p-3 text-center text-red-400 text-xs">查詢歷年紀錄失敗</td>`;
        }
      });

      tbody.appendChild(tr);
    });
  } catch (err) {
    console.error('Failed to load transactions:', err);
    tbody.innerHTML = '<tr><td colspan="7" class="py-8 text-center text-red-400">載入失敗</td></tr>';
  }
}

// 14. Crawler Status
async function loadCrawlerStatus() {
  try {
    const res = await apiFetch('/api/crawler/status');
    const data = await res.json();

    document.getElementById('stat-total-tx').innerText = data.total_transactions.toLocaleString();
    document.getElementById('stat-total-periods').innerText = data.total_periods;
    document.getElementById('stat-latest-date').innerText = data.latest_transaction_date || '無';
    const sysTimeEl = document.getElementById('sys-update-time-text');
    if (sysTimeEl && data.system_updated_at) {
      sysTimeEl.innerText = data.system_updated_at;
    }

    document.getElementById('modal-stat-records').innerText = `${data.total_transactions.toLocaleString()} 筆`;
    document.getElementById('modal-stat-seasons').innerText = `${data.completed_seasons_count} 季`;
    document.getElementById('modal-crawler-status').innerText = data.crawler_running ? '爬取執行中...' : '閒置中';
  } catch (err) {
    console.error('Failed to load crawler status:', err);
  }
}

// 15. Event Listeners
function bindEvents() {
  // Metric buttons
  document.querySelectorAll('.metric-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.metric-btn').forEach(b => {
        b.classList.remove('active');
        b.classList.add('text-slate-400');
      });
      btn.classList.add('active');
      btn.classList.remove('text-slate-400');
      STATE.activeMetric = btn.dataset.metric;

      const momControls = document.getElementById('momentum-sub-controls');
      if (momControls) {
        if (btn.dataset.metric === 'momentum') {
          momControls.classList.remove('hidden');
        } else {
          momControls.classList.add('hidden');
        }
      }

      if (STATE.colorConfig.autoScale) {
        STATE.colorConfig.palette = DEFAULT_METRIC_PALETTES[btn.dataset.metric] || 'emerald_rose';
      }
      updateView();
    });
  });

  // Momentum Type buttons (成交量增幅 vs 價格增幅)
  document.querySelectorAll('.mom-type-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.mom-type-btn').forEach(b => {
        b.classList.remove('active');
        b.classList.add('text-slate-400');
      });
      btn.classList.add('active');
      btn.classList.remove('text-slate-400');
      STATE.momentumType = btn.dataset.type;
      updateView();
    });
  });

  // Momentum Interval buttons (月增 vs 季增 vs 年增)
  document.querySelectorAll('.mom-interval-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.mom-interval-btn').forEach(b => {
        b.classList.remove('active');
        b.classList.add('text-slate-400');
      });
      btn.classList.add('active');
      btn.classList.remove('text-slate-400');
      STATE.momentumInterval = btn.dataset.interval;
      updateView();
    });
  });

  // Color Bar Auto-Scale Checkbox (打一個勾勾就自動調整顏色)
  const chkAutoColor = document.getElementById('chk-auto-color');
  if (chkAutoColor) {
    chkAutoColor.addEventListener('change', (e) => {
      STATE.colorConfig.autoScale = e.target.checked;
      if (e.target.checked) {
        STATE.colorConfig.customMin = null;
        STATE.colorConfig.customMax = null;
      }
      updateView();
      showToast(e.target.checked ? '已啟用自動自適應顏色' : '已關閉自動顏色，可自行設定數值區間');
    });
  }

  // Toggle Custom Color Bar Panel
  const btnToggleCustom = document.getElementById('btn-toggle-legend-custom');
  const panelCustom = document.getElementById('legend-custom-panel');
  if (btnToggleCustom && panelCustom) {
    btnToggleCustom.addEventListener('click', () => {
      panelCustom.classList.toggle('hidden');
      const isHidden = panelCustom.classList.contains('hidden');
      const txtBtn = document.getElementById('txt-legend-custom-btn');
      if (txtBtn) txtBtn.innerText = isHidden ? '自訂' : '收合';
    });
  }

  // Palette Selector
  const selPalette = document.getElementById('select-color-palette');
  if (selPalette) {
    selPalette.addEventListener('change', (e) => {
      STATE.colorConfig.palette = e.target.value;
      updateView();
    });
  }

  // Apply Custom Color Range
  const btnApplyCustom = document.getElementById('btn-apply-custom-color');
  if (btnApplyCustom) {
    btnApplyCustom.addEventListener('click', () => {
      const minVal = parseFloat(document.getElementById('input-color-min').value);
      const maxVal = parseFloat(document.getElementById('input-color-max').value);
      if (!isNaN(minVal) && !isNaN(maxVal) && maxVal > minVal) {
        STATE.colorConfig.autoScale = false;
        STATE.colorConfig.customMin = minVal;
        STATE.colorConfig.customMax = maxVal;
        STATE.colorConfig.currentMin = minVal;
        STATE.colorConfig.currentMax = maxVal;
        const chkAuto = document.getElementById('chk-auto-color');
        if (chkAuto) chkAuto.checked = false;
        updateView();
        showToast(`已套用自訂色階範圍: ${minVal} ~ ${maxVal}`);
      } else {
        showToast('請輸入有效的最大值與最小值 (最大值須大於最小值)');
      }
    });
  }

  // Reset Custom Color Range
  const btnResetCustom = document.getElementById('btn-reset-custom-color');
  if (btnResetCustom) {
    btnResetCustom.addEventListener('click', () => {
      STATE.colorConfig.autoScale = true;
      STATE.colorConfig.customMin = null;
      STATE.colorConfig.customMax = null;
      STATE.colorConfig.palette = DEFAULT_METRIC_PALETTES[STATE.activeMetric] || 'emerald_rose';
      const chkAuto = document.getElementById('chk-auto-color');
      if (chkAuto) chkAuto.checked = true;
      updateView();
      showToast('已重設為自動動態調整色階');
    });
  }

  // Level buttons (county / town / road)
  document.querySelectorAll('.level-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.level-btn').forEach(b => {
        b.classList.remove('active');
        b.classList.add('text-slate-400');
      });
      btn.classList.add('active');
      btn.classList.remove('text-slate-400');
      STATE.activeLevel = btn.dataset.level;
      STATE.selectedRegion = null;

      const hotspotSubBar = document.getElementById('hotspot-sub-controls');
      if (hotspotSubBar) {
        if (STATE.activeLevel === 'road') {
          hotspotSubBar.classList.remove('hidden');
        } else {
          hotspotSubBar.classList.add('hidden');
        }
      }

      updateView();
    });
  });

  // Hotspot Sub-category buttons (all / road / project)
  document.querySelectorAll('.hotspot-type-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.hotspot-type-btn').forEach(b => {
        b.classList.remove('active');
        b.classList.add('text-slate-400');
      });
      btn.classList.add('active');
      btn.classList.remove('text-slate-400');
      STATE.hotspotType = btn.dataset.type || 'all';
      if (STATE.activeLevel === 'road') {
        const currentPeriod = STATE.periods[STATE.currentIndex];
        renderLocationHotspots(null, currentPeriod, { forceReset: true });
      } else {
        updateView();
      }
    });
  });

  const chkHotspotAllHist = document.getElementById('chk-hotspot-all-history');
  if (chkHotspotAllHist) {
    chkHotspotAllHist.addEventListener('change', (e) => {
      STATE.hotspotAllHistoryOnZoom = e.target.checked;
      if (STATE.activeLevel === 'road') {
        renderLocationHotspots(null, null, { forceReset: true, scanViewport: true });
      }
      showToast(e.target.checked ? '已啟用：放大顯示 2012~至今歷年全部成交門牌' : '已切換：僅顯示當前時間軸週期之成交門牌');
    });
  }

  // Basemap style buttons (dark / streets / satellite)
  document.querySelectorAll('.basemap-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      setBasemap(btn.dataset.basemap);
    });
  });

  // Timeline Slider (instant label update + 80ms debounced data load to avoid flooding on drag)
  const slider = document.getElementById('period-slider');
  let sliderTimer = null;
  slider.addEventListener('input', (e) => {
    STATE.currentIndex = parseInt(e.target.value);
    updatePeriodDisplay();
    if (sliderTimer) clearTimeout(sliderTimer);
    sliderTimer = setTimeout(() => {
      updateView();
    }, 80);
  });

  // Step Prev / Next
  document.getElementById('btn-step-prev').addEventListener('click', () => {
    if (STATE.currentIndex > 0) {
      STATE.currentIndex--;
      slider.value = STATE.currentIndex;
      updateView();
    }
  });

  document.getElementById('btn-step-next').addEventListener('click', () => {
    if (STATE.currentIndex < STATE.periods.length - 1) {
      STATE.currentIndex++;
      slider.value = STATE.currentIndex;
      updateView();
    }
  });

  // Play / Pause Animation
  const playBtn = document.getElementById('btn-play');
  playBtn.addEventListener('click', () => {
    STATE.isPlaying = !STATE.isPlaying;
    const icon = document.getElementById('play-icon');

    if (STATE.isPlaying) {
      icon.setAttribute('data-lucide', 'pause');
      lucide.createIcons();
      STATE.playTimer = setInterval(() => {
        if (STATE.currentIndex < STATE.periods.length - 1) {
          STATE.currentIndex++;
        } else {
          STATE.currentIndex = 0;
        }
        slider.value = STATE.currentIndex;
        updateView();
      }, 1400);
    } else {
      icon.setAttribute('data-lucide', 'play');
      lucide.createIcons();
      clearInterval(STATE.playTimer);
    }
  });

  // Reset Map View
  document.getElementById('btn-reset-map').addEventListener('click', () => {
    STATE.map.setView([23.7, 120.95], 7.5);
    STATE.selectedRegion = null;
    STATE.hotspotFilterSignature = '';
    syncRegionDropdowns(null);
    updateView();
  });

  // Direct Region Selection (縣市 / 鄉鎮市區 / 全省)
  const countySelect = document.getElementById('select-global-county');
  const townSelect = document.getElementById('select-global-town');
  const resetNationalBtn = document.getElementById('btn-reset-national');

  if (countySelect) {
    countySelect.addEventListener('change', async (e) => {
      const county = e.target.value;
      updateTownDropdown(county);
      if (!county) {
        STATE.selectedRegion = null;
        if (STATE.map) STATE.map.setView([23.7, 120.95], 7.5);
        await updateView();
      } else {
        await selectRegion(county);
      }
    });
  }

  if (townSelect) {
    townSelect.addEventListener('change', async (e) => {
      const town = e.target.value;
      if (town) {
        if (STATE.activeLevel === 'county') {
          STATE.activeLevel = 'town';
          document.querySelectorAll('.level-btn').forEach(b => {
            const isActive = b.dataset.level === 'town';
            b.classList.toggle('active', isActive);
            b.classList.toggle('text-slate-400', !isActive);
          });
          await updateView();
        }
        await selectRegion(town);
      } else {
        const county = countySelect ? countySelect.value : '';
        if (county) {
          await selectRegion(county);
        } else {
          STATE.selectedRegion = null;
          await updateView();
        }
      }
    });
  }

  if (resetNationalBtn) {
    resetNationalBtn.addEventListener('click', async () => {
      STATE.selectedRegion = null;
      STATE.hotspotFilterSignature = '';
      syncRegionDropdowns(null);
      if (STATE.map) STATE.map.setView([23.7, 120.95], 7.5);
      await updateView();
    });
  }

  // Time Dimension Mode (Single Quarter vs Multi-Quarter Interval)
  const btnTimeSingle = document.getElementById('btn-time-mode-single');
  const btnTimeRange = document.getElementById('btn-time-mode-range');
  const timeSingleControls = document.getElementById('time-single-controls');
  const timeRangeControls = document.getElementById('time-range-controls');

  if (btnTimeSingle && btnTimeRange) {
    btnTimeSingle.addEventListener('click', () => {
      STATE.timeMode = 'single';
      btnTimeSingle.classList.add('active');
      btnTimeSingle.classList.remove('text-slate-400');
      btnTimeRange.classList.remove('active');
      btnTimeRange.classList.add('text-slate-400');
      if (timeSingleControls) timeSingleControls.classList.remove('hidden');
      if (timeRangeControls) timeRangeControls.classList.add('hidden');
      updateView();
    });

    btnTimeRange.addEventListener('click', () => {
      STATE.timeMode = 'range';
      btnTimeRange.classList.add('active');
      btnTimeRange.classList.remove('text-slate-400');
      btnTimeSingle.classList.remove('active');
      btnTimeSingle.classList.add('text-slate-400');
      if (timeSingleControls) timeSingleControls.classList.add('hidden');
      if (timeRangeControls) timeRangeControls.classList.remove('hidden');

      if (!STATE.rangeStart || !STATE.rangeEnd) {
        const len = STATE.periods.length;
        if (len > 0) {
          STATE.rangeEnd = STATE.periods[len - 1];
          STATE.rangeStart = STATE.periods[Math.max(0, len - 4)];
          const selStart = document.getElementById('select-range-start');
          const selEnd = document.getElementById('select-range-end');
          if (selStart) selStart.value = STATE.rangeStart;
          if (selEnd) selEnd.value = STATE.rangeEnd;
        }
      }
      updateView();
    });
  }

  const selRangeStart = document.getElementById('select-range-start');
  const selRangeEnd = document.getElementById('select-range-end');

  if (selRangeStart) {
    selRangeStart.addEventListener('change', (e) => {
      STATE.rangeStart = e.target.value;
      if (STATE.rangeEnd && STATE.rangeStart > STATE.rangeEnd) {
        STATE.rangeEnd = STATE.rangeStart;
        if (selRangeEnd) selRangeEnd.value = STATE.rangeEnd;
      }
      updateView();
    });
  }

  if (selRangeEnd) {
    selRangeEnd.addEventListener('change', (e) => {
      STATE.rangeEnd = e.target.value;
      if (STATE.rangeStart && STATE.rangeEnd < STATE.rangeStart) {
        STATE.rangeStart = STATE.rangeEnd;
        if (selRangeStart) selRangeStart.value = STATE.rangeStart;
      }
      updateView();
    });
  }

  document.querySelectorAll('.range-preset-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const preset = btn.dataset.preset;
      const len = STATE.periods.length;
      if (len === 0) return;
      STATE.rangeEnd = STATE.periods[len - 1];
      if (preset === '1y') {
        STATE.rangeStart = STATE.periods[Math.max(0, len - 4)];
      } else if (preset === '3y') {
        STATE.rangeStart = STATE.periods[Math.max(0, len - 12)];
      } else if (preset === 'all') {
        STATE.rangeStart = STATE.periods[0];
      }
      if (selRangeStart) selRangeStart.value = STATE.rangeStart;
      if (selRangeEnd) selRangeEnd.value = STATE.rangeEnd;
      updateView();
    });
  });

  // Global Building Age Filter
  const globalAgeSelect = document.getElementById('select-global-age');
  if (globalAgeSelect) {
    globalAgeSelect.addEventListener('change', (e) => {
      STATE.activeAgeFilter = e.target.value;
      const modalAgeSelect = document.getElementById('tx-filter-age');
      if (modalAgeSelect) {
        modalAgeSelect.value = e.target.value;
      }
      updateView();
    });
  }

  // Ranking metric selector
  document.getElementById('select-ranking-metric').addEventListener('change', () => {
    const currentPeriod = STATE.periods[STATE.currentIndex];
    loadRanking(currentPeriod);
  });

  // Sidebar Tabs (Ranking / Roads / Projects)
  document.querySelectorAll('.side-tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.side-tab-btn').forEach(b => {
        b.className = 'side-tab-btn font-medium px-2.5 py-1 rounded-lg text-slate-400 hover:text-slate-200';
      });
      btn.className = 'side-tab-btn active font-medium px-2.5 py-1 rounded-lg text-blue-400 bg-slate-800';

      const tab = btn.dataset.tab;
      document.getElementById('tab-content-ranking').classList.toggle('hidden', tab !== 'ranking');
      document.getElementById('tab-content-roads').classList.toggle('hidden', tab !== 'roads');
      document.getElementById('tab-content-projects').classList.toggle('hidden', tab !== 'projects');
    });
  });

  // Open Transaction Drawer Button in sidebar
  document.getElementById('btn-open-tx-drawer').addEventListener('click', () => {
    const currentPeriod = STATE.periods[STATE.currentIndex];
    openTransactionDrawer(currentPeriod, STATE.selectedRegion);
  });

  const btnChartView = document.getElementById('btn-chart-view-current');
  if (btnChartView) {
    btnChartView.addEventListener('click', () => {
      const currentPeriod = STATE.periods[STATE.currentIndex];
      openTransactionDrawer(currentPeriod, STATE.selectedRegion);
    });
  }

  // Close Transaction Modal
  document.getElementById('btn-close-tx-modal').addEventListener('click', () => {
    document.getElementById('tx-modal').classList.add('hidden');
  });

  // Close modals when clicking outer backdrop
  document.getElementById('tx-modal').addEventListener('click', (e) => {
    if (e.target.id === 'tx-modal') {
      document.getElementById('tx-modal').classList.add('hidden');
    }
  });

  document.getElementById('crawler-modal').addEventListener('click', (e) => {
    if (e.target.id === 'crawler-modal') {
      document.getElementById('crawler-modal').classList.add('hidden');
    }
  });

  // Close modals with Escape key
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      document.getElementById('tx-modal').classList.add('hidden');
      document.getElementById('crawler-modal').classList.add('hidden');
    }
  });

  // Global Address & Landmark Quick Search
  const globalSearchInput = document.getElementById('global-search-input');
  const globalSearchBtn = document.getElementById('btn-global-search');

  async function handleGlobalSearch(customQuery = null) {
    const rawQ = (typeof customQuery === 'string' && customQuery.trim())
      ? customQuery.trim()
      : (globalSearchInput ? globalSearchInput.value.trim() : '');
    if (!rawQ) {
      showToast('請輸入地址、門牌或建案名稱');
      return;
    }
    showToast(`🔍 正在精確搜尋與定位：${rawQ}...`);

    try {
      const res = await apiFetch(`/api/search/locate?q=${encodeURIComponent(rawQ)}`);
      const data = await res.json();
      if (!data.found && data.total_tx === 0) {
        showToast(`查無符合「${rawQ}」之交易或地理位置`);
        return;
      }

      const loc = data.location || {};
      const normQuery = data.query || rawQ;
      if (globalSearchInput && data.query) {
        globalSearchInput.value = data.query;
      }

      // Close any open modals so the map and marker popup are clearly visible
      const txModalEl = document.getElementById('tx-modal');
      if (txModalEl) txModalEl.classList.add('hidden');
      const ctModalEl = document.getElementById('community-trend-modal');
      if (ctModalEl) ctModalEl.classList.add('hidden');

      // Switch map level to 'road' (細分: 路段/建案) so polygons don't cover the building
      if (STATE.activeLevel !== 'road') {
        STATE.activeLevel = 'road';
        document.querySelectorAll('.level-btn').forEach(b => {
          const isRoad = b.dataset.level === 'road';
          b.classList.toggle('active', isRoad);
          b.classList.toggle('text-slate-400', !isRoad);
        });
        const hotspotSubBar = document.getElementById('hotspot-sub-controls');
        if (hotspotSubBar) hotspotSubBar.classList.remove('hidden');
      }

      // If in single-quarter mode and the searched address's latest transaction is in another quarter, sync timeline to that quarter so its hotspot dot also renders on map
      if (data.latest_quarter && STATE.timeMode === 'single' && Array.isArray(STATE.periods)) {
        const qIdx = STATE.periods.indexOf(data.latest_quarter);
        if (qIdx >= 0 && qIdx !== STATE.currentIndex) {
          STATE.currentIndex = qIdx;
          const pSlider = document.getElementById('period-slider');
          if (pSlider) pSlider.value = qIdx;
          updatePeriodDisplay();
        }
      }

      await updateView();

      if (loc.lat && loc.lng && STATE.map) {
        if (STATE.searchMarker) {
          STATE.map.removeLayer(STATE.searchMarker);
        }
        const pulseIcon = L.divIcon({
          className: 'search-pulse-icon',
          html: `<div class="relative flex items-center justify-center">
                   <div class="animate-ping absolute w-9 h-9 rounded-full bg-amber-400 opacity-75"></div>
                   <div class="w-7 h-7 rounded-full bg-blue-600 border-2 border-white shadow-xl flex items-center justify-center text-xs text-white font-bold cursor-pointer">📍</div>
                 </div>`,
          iconSize: [28, 28],
          iconAnchor: [14, 14]
        });
        const sDist = (loc.full_district || '').replace(/'/g, "\\'");
        const sProj = (loc.project_name || '').replace(/'/g, "\\'");
        const sSpot = (loc.spot_name || loc.name || normQuery).replace(/'/g, "\\'");
        const sRoad = (loc.road || '').replace(/'/g, "\\'");
        const sQ = normQuery.replace(/'/g, "\\'");
        const projLine = loc.project_name
          ? `<div class="text-amber-300 text-xs font-semibold mt-0.5">🏢 建案：${loc.project_name}</div>`
          : '';

        const firstTx = (data.transactions && data.transactions.length > 0) ? data.transactions[0] : null;
        let txDetailHtml = '';
        if (firstTx) {
          const flShow = firstTx.floor_display || firstTx.floor || '--';
          const ageShow = (firstTx.building_age !== null && firstTx.building_age !== undefined) ? `屋齡 ${firstTx.building_age}年` : '';
          txDetailHtml = `
            <div class="mt-1.5 pt-1.5 border-t border-slate-700/80 text-[11px] text-slate-200 space-y-0.5">
              <div class="font-mono text-amber-200">${firstTx.address || loc.address || ''}</div>
              <div>樓層: <b>${flShow} / 共${firstTx.total_floors || '--'}</b> ${ageShow ? `｜ ${ageShow}` : ''}</div>
              <div>移轉建坪: <b class="text-emerald-300">${firstTx.building_area_ping || 0} 坪</b> ｜ 土地: <b class="text-amber-300">${firstTx.land_area_ping || 0} 坪</b></div>
              <div>最近成交 (${firstTx.trade_date || firstTx.trade_quarter || ''}): <b class="text-white">${(firstTx.total_price_wan || 0).toLocaleString()} 萬</b> <span class="text-emerald-400">(${firstTx.unit_price_wan_ping} 萬/坪)</span></div>
            </div>
          `;
        }

        STATE.searchMarker = L.marker([loc.lat, loc.lng], { icon: pulseIcon, zIndexOffset: 1000 }).addTo(STATE.map);
        STATE.searchMarker.bindPopup(`
          <div class="p-1.5 text-xs min-w-[245px]">
            <div class="font-bold text-sm text-white">${loc.name || normQuery}</div>
            ${projLine}
            <div class="text-slate-300 text-[11px] mt-0.5">${loc.full_district || ''} ${loc.road || ''}</div>
            <div class="text-emerald-400 font-semibold mt-1">歷史均價: ${data.avg_unit_price} 萬/坪 | 累計成交: ${data.total_tx} 筆</div>
            ${txDetailHtml}
            <div class="mt-2.5 flex flex-col gap-1.5">
              <button onclick="openCommunityTrendModal('${sDist}', '${sProj}', '${sSpot}', '${sRoad}', '')" 
                class="w-full py-1.5 bg-gradient-to-r from-amber-600 to-orange-600 hover:from-amber-500 hover:to-orange-500 text-white rounded text-[11px] font-semibold transition shadow">
                📈 建案行情與細項 (含土地/建坪/歷年紀錄)
              </button>
              <button onclick="openTransactionDrawer('全部歷史', '${sDist}', '${sRoad}', null, null, '${sQ}')" 
                class="w-full py-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded text-[11px] font-medium transition shadow">
                📋 查看全歷史成交明細 (${data.total_tx}筆) &rarr;
              </button>
            </div>
          </div>
        `, { maxWidth: 320, autoPan: true });

        STATE.map.flyTo([loc.lat, loc.lng], 17, { animate: true, duration: 1.0 });
        STATE.searchMarker.openPopup();
        STATE.map.once('moveend', () => {
          if (STATE.searchMarker) {
            STATE.searchMarker.openPopup();
          }
        });
      }

      showToast(`📍 已在地圖精準定位：${loc.name || normQuery} (共 ${data.total_tx} 筆歷史成交)`);

    } catch (err) {
      console.error('Search locate failed:', err);
      showToast('搜尋定位請求失敗');
    }
  }
  window.locateAddressOnMap = handleGlobalSearch;

  if (globalSearchInput) {
    globalSearchInput.addEventListener('keyup', (e) => {
      if (e.key === 'Enter') handleGlobalSearch();
    });
  }
  if (globalSearchBtn) {
    globalSearchBtn.addEventListener('click', handleGlobalSearch);
  }

  // Transaction Search & Filter Input Handlers
  document.getElementById('tx-search-keyword').addEventListener('keyup', (e) => {
    if (e.key === 'Enter') {
      const p = STATE.modalCurrentParams || {};
      fetchAndRenderTransactions(p.period, p.region, p.road, p.project, p.door);
    }
  });
  const txSearchTriggerBtn = document.getElementById('btn-tx-search-trigger');
  if (txSearchTriggerBtn) {
    txSearchTriggerBtn.addEventListener('click', () => {
      const p = STATE.modalCurrentParams || {};
      fetchAndRenderTransactions(p.period, p.region, p.road, p.project, p.door);
    });
  }
  const chkAllTime = document.getElementById('chk-tx-all-time');
  if (chkAllTime) {
    chkAllTime.addEventListener('change', () => {
      const p = STATE.modalCurrentParams || {};
      fetchAndRenderTransactions(p.period, p.region, p.road, p.project, p.door);
    });
  }
  document.getElementById('tx-sort-select').addEventListener('change', () => {
    const p = STATE.modalCurrentParams || {};
    fetchAndRenderTransactions(p.period, p.region, p.road, p.project, p.door);
  });
  const ageSelect = document.getElementById('tx-filter-age');
  if (ageSelect) {
    ageSelect.addEventListener('change', (e) => {
      STATE.activeAgeFilter = e.target.value;
      const globalAge = document.getElementById('select-global-age');
      if (globalAge) globalAge.value = e.target.value;
      const p = STATE.modalCurrentParams || {};
      fetchAndRenderTransactions(p.period, p.region, p.road, p.project, p.door);
      updateView();
    });
  }
  const specialCheck = document.getElementById('tx-filter-special');
  if (specialCheck) {
    specialCheck.addEventListener('change', () => {
      const p = STATE.modalCurrentParams || {};
      fetchAndRenderTransactions(p.period, p.region, p.road, p.project, p.door);
    });
  }

  // Crawl Latest Button
  document.getElementById('btn-crawl-latest').addEventListener('click', async () => {
    const spinner = document.getElementById('crawl-spinner');
    spinner.classList.add('animate-spin-custom');
    showToast('正在向內政部實價登錄下載並解析最新批次...');

    try {
      const res = await apiFetch('/api/crawler/crawl_latest', { method: 'POST' });
      const data = await res.json();
      if (res.ok) {
        showToast('最新資料抓取作業已在背景啟動！');
        setTimeout(async () => {
          await loadPeriods();
          await loadCrawlerStatus();
          spinner.classList.remove('animate-spin-custom');
        }, 4000);
      } else {
        showToast(`錯誤: ${data.detail || '啟動失敗'}`);
        spinner.classList.remove('animate-spin-custom');
      }
    } catch (err) {
      showToast('請求失敗');
      spinner.classList.remove('animate-spin-custom');
    }
  });

  // Crawler Modal Handlers
  const modal = document.getElementById('crawler-modal');
  document.getElementById('btn-open-crawler-modal').addEventListener('click', () => {
    loadCrawlerStatus();
    modal.classList.remove('hidden');
  });
  document.getElementById('btn-close-crawler-modal').addEventListener('click', () => {
    modal.classList.add('hidden');
  });

  // Trigger Backfill Button
  document.getElementById('btn-trigger-backfill').addEventListener('click', async () => {
    const start = document.getElementById('backfill-start-season').value.trim() || '101S3';
    const end = document.getElementById('backfill-end-season').value.trim();
    showToast(`啟動從 ${start} 開始之歷史資料補齊作業...`);

    try {
      const url = `/api/crawler/backfill?start_season=${start}${end ? `&end_season=${end}` : ''}`;
      const res = await apiFetch(url, { method: 'POST' });
      const data = await res.json();
      if (res.ok) {
        showToast('歷史補齊作業已在背景啟動！可於終端機查看實時進度。');
        modal.classList.add('hidden');
      } else {
        showToast(`錯誤: ${data.detail || '啟動失敗'}`);
      }
    } catch (err) {
      showToast('請求失敗');
    }
  });
}

function showToast(msg) {
  const toast = document.getElementById('toast');
  document.getElementById('toast-msg').innerText = msg;
  toast.classList.remove('hidden');
  setTimeout(() => {
    toast.classList.add('hidden');
  }, 3500);
}

let communityTrendChartInstance = null;

async function promptTagCommunityName(region, spotName, currentComm, road = '', doorAddr = '') {
  const newName = window.prompt(
    `請輸入「${region} ${spotName}」對應的建案/社區名稱：\n(設定後將自動歸戶並更新地圖標記)`,
    currentComm || ''
  );
  if (!newName || !newName.trim()) return;
  try {
    const params = new URLSearchParams({
      region: region || '',
      spot_name: spotName || '',
      community_name: newName.trim(),
      road: road || '',
      door_addr: doorAddr || ''
    });
    const res = await apiFetch(`/api/community/tag?${params.toString()}`, { method: 'POST' });
    if (res.ok) {
      showToast(`已將 ${spotName} 標註為建案「${newName.trim()}」！`);
      clearLocationHotspots();
      await renderLocationHotspots();
    } else {
      showToast('標註失敗，請稍後再試');
    }
  } catch (err) {
    console.error('Tag community failed:', err);
    showToast('標註失敗');
  }
}

function buildUnitHistoryExpansionHtml(clickedItem, sameUnitRecords, sameDoorRecords, unitLabel = '', doorLabel = '') {
  const flDisp = clickedItem.floor_display || clickedItem.floor || '';
  const uTitle = unitLabel || `${clickedItem.road_door || clickedItem.address || ''} · ${flDisp}`;
  const dTitle = doorLabel || clickedItem.road_door || clickedItem.address || '';

  // Sort chronological ascending to compute price diffs for same-unit records
  const unitAsc = [...(sameUnitRecords || [])].sort((a, b) => (a.trade_date || '').localeCompare(b.trade_date || ''));

  let unitHtml = '';
  if (unitAsc.length >= 2) {
    const rowsHtml = unitAsc.map((u, idx) => {
      const prev = idx > 0 ? unitAsc[idx - 1] : null;
      let diffHtml = '<span class="text-slate-500">首次登錄入手</span>';
      if (prev && prev.total_price_wan > 0 && u.total_price_wan > 0) {
        const diffWan = Math.round((u.total_price_wan - prev.total_price_wan) * 10) / 10;
        const diffPct = Math.round((diffWan / prev.total_price_wan) * 1000) / 10;
        const sign = diffWan > 0 ? '+' : '';
        const clr = diffWan > 0 ? 'text-red-400' : (diffWan < 0 ? 'text-emerald-400' : 'text-slate-300');
        diffHtml = `<span class="font-bold ${clr}">${sign}${diffWan.toLocaleString()} 萬 (${sign}${diffPct}%)</span>`;
      }
      const isCurrent = u.id === clickedItem.id || (u.trade_date === clickedItem.trade_date && u.total_price_wan === clickedItem.total_price_wan);
      const hasPark = (u.target_type && u.target_type.includes('車位')) || (u.berth_category && u.berth_category !== 'nan') || (u.berth_area_ping > 0);
      const parkStr = hasPark ? `🚗 含車位${u.berth_area_ping > 0 ? ` (${u.berth_area_ping}坪)` : ''}` : '無車位';
      return `
        <tr class="${isCurrent ? 'bg-amber-500/10' : ''} border-b border-slate-800/60">
          <td class="py-1.5 px-2 font-mono text-slate-300">
            第 ${idx + 1} 次 · ${u.trade_date} (${u.trade_quarter || ''})
            ${isCurrent ? '<span class="ml-1 px-1.5 py-0.2 rounded bg-amber-500/30 text-amber-200 text-[10px]">本筆</span>' : ''}
          </td>
          <td class="py-1.5 px-2 text-slate-200">建坪 <b>${u.building_area_ping || 0}</b> 坪 ｜ 土地 <b>${u.land_area_ping || 0}</b> 坪</td>
          <td class="py-1.5 px-2 text-slate-300">${parkStr}</td>
          <td class="py-1.5 px-2 text-right font-semibold text-emerald-400">${u.unit_price_wan_ping} 萬/坪</td>
          <td class="py-1.5 px-2 text-right font-bold text-white">${(u.total_price_wan || 0).toLocaleString()} 萬</td>
          <td class="py-1.5 px-2 text-right">${diffHtml}</td>
        </tr>
      `;
    }).join('');

    unitHtml = `
      <div class="mb-3">
        <div class="text-xs font-bold text-amber-300 mb-1.5 flex items-center gap-1.5">
          <span>🔥 同戶歷年買賣紀錄（${uTitle}）— 共 ${unitAsc.length} 筆歷年交易</span>
        </div>
        <table class="w-full text-left text-[11px] border-collapse bg-slate-900/90 rounded-lg overflow-hidden border border-amber-500/30">
          <thead>
            <tr class="bg-slate-950 text-slate-400 border-b border-slate-800">
              <th class="py-1.5 px-2">歷次買賣日期</th>
              <th class="py-1.5 px-2">移轉建坪 / 土地地坪</th>
              <th class="py-1.5 px-2">車位資訊</th>
              <th class="py-1.5 px-2 text-right">每坪單價</th>
              <th class="py-1.5 px-2 text-right">成交總價</th>
              <th class="py-1.5 px-2 text-right">與前次價差 / 漲跌幅</th>
            </tr>
          </thead>
          <tbody>${rowsHtml}</tbody>
        </table>
      </div>
    `;
  } else {
    unitHtml = `
      <div class="mb-2.5 px-3 py-2 rounded-lg bg-slate-900/90 border border-slate-800 text-xs text-slate-300 flex items-center justify-between flex-wrap gap-2">
        <span>📌 <b>同戶歷年紀錄（${uTitle}）</b>：自 2012 年實價登錄以來僅有此 <b>1 筆</b>成交紀錄（${clickedItem.trade_date} · 成交總價 <b>${(clickedItem.total_price_wan || 0).toLocaleString()} 萬</b> · 建坪 <b>${clickedItem.building_area_ping || 0} 坪</b> · 土地 <b>${clickedItem.land_area_ping || 0} 坪</b>），目前尚無其他歷年轉手紀錄。</span>
      </div>
    `;
  }

  // Same-door records across all floors
  const doorDesc = [...(sameDoorRecords || [])].sort((a, b) => (b.trade_date || '').localeCompare(a.trade_date || ''));
  let doorHtml = '';
  if (doorDesc.length > 1) {
    const dRows = doorDesc.slice(0, 15).map(d => {
      const isCurrent = d.id === clickedItem.id || (d.trade_date === clickedItem.trade_date && d.total_price_wan === clickedItem.total_price_wan && d.floor === clickedItem.floor);
      const hasPark = (d.target_type && d.target_type.includes('車位')) || (d.berth_category && d.berth_category !== 'nan') || (d.berth_area_ping > 0);
      const parkStr = hasPark ? `🚗 含車位${d.berth_area_ping > 0 ? ` (${d.berth_area_ping}坪)` : ''}` : '無車位';
      return `
        <tr class="${isCurrent ? 'bg-blue-500/15' : ''} border-b border-slate-800/50">
          <td class="py-1 px-2 font-mono text-slate-300">
            ${d.trade_date} (${d.trade_quarter || ''})
            ${isCurrent ? '<span class="ml-1 px-1.5 py-0.2 rounded bg-blue-500/30 text-blue-200 text-[10px]">本筆</span>' : ''}
          </td>
          <td class="py-1 px-2 font-semibold text-white">${d.floor_display || d.floor || '--'} / 共${d.total_floors || '--'}</td>
          <td class="py-1 px-2 text-slate-300">建坪 <b>${d.building_area_ping || 0}</b> 坪 ｜ 土地 <b>${d.land_area_ping || 0}</b> 坪</td>
          <td class="py-1 px-2 text-slate-400">${d.room_count || 0}房${d.hall_count || 0}廳${d.bath_count || 0}衛 · ${parkStr}</td>
          <td class="py-1 px-2 text-right font-semibold text-emerald-400">${d.unit_price_wan_ping} 萬/坪</td>
          <td class="py-1 px-2 text-right font-bold text-slate-100">${(d.total_price_wan || 0).toLocaleString()} 萬</td>
        </tr>
      `;
    }).join('');
    doorHtml = `
      <div>
        <div class="text-xs font-bold text-blue-300 mb-1.5 flex items-center gap-1.5">
          <span>🏢 同門牌各樓層歷年成交對照（${dTitle}）— 共 ${doorDesc.length} 筆歷年成交</span>
        </div>
        <table class="w-full text-left text-[11px] border-collapse bg-slate-900/80 rounded-lg overflow-hidden border border-slate-800">
          <thead>
            <tr class="bg-slate-950 text-slate-400 border-b border-slate-800">
              <th class="py-1 px-2">交易日期</th>
              <th class="py-1 px-2">樓層</th>
              <th class="py-1 px-2">移轉建坪 / 土地地坪</th>
              <th class="py-1 px-2">格局與車位</th>
              <th class="py-1 px-2 text-right">每坪單價</th>
              <th class="py-1 px-2 text-right">成交總價</th>
            </tr>
          </thead>
          <tbody>${dRows}</tbody>
        </table>
      </div>
    `;
  }

  return `
    <div class="rounded-xl bg-slate-900/70 border border-blue-500/30 p-3 space-y-2 shadow-inner">
      ${unitHtml}
      ${doorHtml}
    </div>
  `;
}

async function openCommunityTrendModal(region, projectName, spotName, road = '', doorAddr = '') {
  if (STATE.map) {
    STATE.map.closePopup();
  }
  const modal = document.getElementById('community-trend-modal');
  if (!modal) return;
  modal.classList.remove('hidden');

  const closeBtn = document.getElementById('btn-close-ct-modal');
  if (closeBtn) {
    closeBtn.onclick = () => modal.classList.add('hidden');
  }

  document.getElementById('ct-title').innerText = projectName || spotName || '載入中...';
  document.getElementById('ct-region-badge').innerText = region || '--';
  document.getElementById('ct-spec-badge').innerText = '正在分析歷史價格走勢與同戶轉手紀錄...';
  document.getElementById('ct-doors-list').innerText = '載入中...';
  const ctTxTbody = document.getElementById('ct-tx-tbody');
  if (ctTxTbody) {
    ctTxTbody.innerHTML = '<tr><td colspan="7" class="py-6 text-center text-slate-500">載入建案歷年成交細項中...</td></tr>';
  }
  document.getElementById('ct-repeat-tbody').innerHTML = '<tr><td colspan="6" class="py-4 text-center text-slate-500">分析中...</td></tr>';

  const renameBtn = document.getElementById('btn-ct-rename');
  if (renameBtn) {
    renameBtn.onclick = async () => {
      await promptTagCommunityName(region, spotName || projectName, projectName, road, doorAddr);
      modal.classList.add('hidden');
    };
  }

  try {
    const params = new URLSearchParams();
    if (region) params.set('region', region);
    if (projectName) params.set('project_name', projectName);
    if (spotName) params.set('spot_name', spotName);
    if (road) params.set('road', road);
    if (doorAddr) params.set('door_addr', doorAddr);

    const res = await apiFetch(`/api/community/trend?${params.toString()}`);
    const data = await res.json();

    document.getElementById('ct-title').innerText = data.display_name || projectName || spotName;
    document.getElementById('ct-region-badge').innerText = `${data.region || region || ''} ${data.road || ''}`;

    const specs = [];
    if (data.building_type) specs.push(data.building_type);
    if (data.avg_age !== null && data.avg_age !== undefined) {
      specs.push(`屋齡 ${data.avg_age} 年${data.build_year ? ` (${data.build_year}年完工)` : ''}`);
    } else if (data.build_year) {
      specs.push(`${data.build_year}年完工`);
    }
    if (data.total_floors) specs.push(`總樓層 ${data.total_floors}`);
    if (data.summary && data.summary.avg_building_ping) specs.push(`均建坪 ${data.summary.avg_building_ping} 坪`);
    if (data.summary && data.summary.avg_land_ping) specs.push(`均地坪 ${data.summary.avg_land_ping} 坪`);
    document.getElementById('ct-spec-badge').innerText = specs.join(' ｜ ');

    const allTxs = data.transactions || [];
    const doors = data.doors || [];
    let activeQuarterFilter = null;
    let activeDoorFilter = null;

    // Render interactive door filter pills
    function renderDoorsBar() {
      const doorsContainer = document.getElementById('ct-doors-list');
      if (!doorsContainer) return;
      if (doors.length === 0) {
        doorsContainer.innerText = spotName || '--';
        return;
      }
      const allBtnClass = !activeDoorFilter
        ? 'bg-blue-600 text-white border-blue-500'
        : 'bg-slate-900 hover:bg-slate-800 text-slate-300 border-slate-700';
      let pillsHtml = `<button data-door="" class="ct-door-pill px-2 py-0.5 rounded-md border text-[11px] transition ${allBtnClass}">全部門牌 (${allTxs.length}筆)</button>`;
      doors.forEach(d => {
        const cnt = allTxs.filter(t => t.road_door === d || (t.address && t.address.includes(d))).length;
        const isAct = activeDoorFilter === d;
        const cls = isAct
          ? 'bg-blue-600 text-white border-blue-500 font-semibold'
          : 'bg-slate-900 hover:bg-slate-800 text-slate-300 border-slate-700';
        pillsHtml += `<button data-door="${d}" class="ct-door-pill px-2 py-0.5 rounded-md border text-[11px] transition ${cls}">${d}${cnt > 0 ? ` (${cnt}筆)` : ''}</button>`;
      });
      doorsContainer.innerHTML = `<div class="inline-flex items-center gap-1.5 flex-wrap">${pillsHtml}</div>`;
      doorsContainer.querySelectorAll('.ct-door-pill').forEach(btn => {
        btn.addEventListener('click', () => {
          const dVal = btn.getAttribute('data-door') || null;
          activeDoorFilter = (activeDoorFilter === dVal) ? null : dVal;
          renderDoorsBar();
          renderCommunityTransactionsTable();
        });
      });
    }

    // Render Detailed Transactions Table with click-to-expand unit history
    function renderCommunityTransactionsTable() {
      if (!ctTxTbody) return;
      const filterBadge = document.getElementById('ct-tx-filter-badge');
      const clearBtn = document.getElementById('btn-ct-clear-filter');

      let filtered = allTxs;
      const filterParts = [];
      if (activeQuarterFilter) {
        filtered = filtered.filter(t => t.trade_quarter === activeQuarterFilter);
        filterParts.push(`季度: ${activeQuarterFilter}`);
      }
      if (activeDoorFilter) {
        filtered = filtered.filter(t => t.road_door === activeDoorFilter || (t.address && t.address.includes(activeDoorFilter)));
        filterParts.push(`門牌: ${activeDoorFilter}`);
      }

      if (filterBadge) {
        filterBadge.innerText = filterParts.length > 0
          ? `${filterParts.join(' ｜ ')} (共 ${filtered.length} 筆)`
          : `全部歷史 (共 ${filtered.length} 筆)`;
      }
      if (clearBtn) {
        if (activeQuarterFilter || activeDoorFilter) {
          clearBtn.classList.remove('hidden');
        } else {
          clearBtn.classList.add('hidden');
        }
      }

      ctTxTbody.innerHTML = '';
      if (filtered.length === 0) {
        ctTxTbody.innerHTML = '<tr><td colspan="7" class="py-6 text-center text-slate-500">此篩選條件下無成交紀錄</td></tr>';
        return;
      }

      filtered.forEach(it => {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-slate-900/90 transition text-slate-200 cursor-pointer group';
        tr.title = '點擊展開此戶與同門牌歷年買賣紀錄';

        const hasPark = (it.target_type && it.target_type.includes('車位')) || (it.berth_category && it.berth_category !== 'nan') || (it.berth_area_ping > 0);
        const bCat = (it.berth_category && it.berth_category !== 'nan') ? it.berth_category : '';
        const parkBadge = hasPark
          ? `<span class="inline-block px-1.5 py-0.5 rounded text-[10px] bg-sky-500/20 text-sky-300 border border-sky-500/30 font-medium">🚗 含車位${bCat ? ` (${bCat})` : ''}${it.berth_area_ping > 0 ? ` · ${it.berth_area_ping}坪` : ''}${it.berth_price_wan > 0 ? ` · ${it.berth_price_wan}萬` : ''}</span>`
          : `<span class="inline-block px-1.5 py-0.5 rounded text-[10px] bg-slate-800 text-slate-400 border border-slate-700/60">無車位</span>`;

        const flShow = it.floor_display || it.floor || '--';
        const ageShow = (it.building_age !== null && it.building_age !== undefined) ? `屋齡 ${it.building_age}年` : '';
        const netDiffers = it.net_unit_price && Math.abs(it.net_unit_price - it.unit_price_wan_ping) >= 0.2;

        const uCnt = it.same_unit_count || 1;
        const dCnt = it.same_door_count || 1;
        let histBadge = '';
        if (uCnt >= 2) {
          histBadge = `<span class="inline-block px-2 py-0.5 rounded-full text-[10px] bg-amber-500/20 group-hover:bg-amber-500/35 text-amber-300 border border-amber-500/40 font-semibold transition">🔥 同戶轉手 ${uCnt} 筆 ▾</span>`;
        } else if (dCnt >= 2) {
          histBadge = `<span class="inline-block px-2 py-0.5 rounded-full text-[10px] bg-blue-500/20 group-hover:bg-blue-500/35 text-blue-300 border border-blue-500/30 font-medium transition">📂 同戶1筆 · 同門牌${dCnt}筆 ▾</span>`;
        } else {
          histBadge = `<span class="inline-block px-2 py-0.5 rounded-full text-[10px] bg-slate-800 group-hover:bg-slate-700 text-slate-300 border border-slate-700 transition">🔎 歷年紀錄 (1筆) ▾</span>`;
        }

        const safeAddr = (it.address || `${region || ''}${it.road_door || ''}`).replace(/'/g, "\\'");

        tr.innerHTML = `
          <td class="py-2.5 font-mono text-slate-300">
            <div>${it.trade_date || '--'}</div>
            <div class="text-[10px] text-slate-500">${it.trade_quarter || ''}</div>
          </td>
          <td class="py-2.5">
            <div class="font-semibold text-white font-mono flex items-center gap-1.5 flex-wrap">
              <span>${it.address || it.road_door || '--'}</span>
              <button onclick="event.stopPropagation(); locateAddressOnMap('${safeAddr}')" title="關閉視窗並在地圖上定位此門牌" class="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-sans bg-blue-500/20 hover:bg-blue-500/40 text-blue-300 border border-blue-500/30 transition cursor-pointer">📍 地圖</button>
            </div>
            <div class="text-[11px] text-slate-400 mt-0.5">${flShow} / 共${it.total_floors || '--'} ${ageShow ? `｜ ${ageShow}` : ''}</div>
          </td>
          <td class="py-2.5">
            <div class="font-semibold text-slate-100">移轉建坪: <span class="text-emerald-300">${it.building_area_ping || 0} 坪</span></div>
            <div class="text-[11px] text-amber-300/90 mt-0.5">土地地坪: <b>${it.land_area_ping || 0} 坪</b></div>
          </td>
          <td class="py-2.5">
            <div class="text-slate-300">${it.room_count || 0}房 ${it.hall_count || 0}廳 ${it.bath_count || 0}衛</div>
            <div class="mt-0.5">${parkBadge}</div>
          </td>
          <td class="py-2.5 text-right">
            <div class="font-bold text-emerald-400">${it.unit_price_wan_ping} 萬/坪</div>
            ${netDiffers ? `<div class="text-[10px] text-cyan-400 mt-0.5">扣車位: ${it.net_unit_price} 萬/坪</div>` : ''}
          </td>
          <td class="py-2.5 text-right font-bold text-white text-sm">
            ${(it.total_price_wan || 0).toLocaleString()} 萬元
          </td>
          <td class="py-2.5 text-right">
            ${histBadge}
          </td>
        `;

        tr.addEventListener('click', () => {
          const nextRow = tr.nextElementSibling;
          if (nextRow && nextRow.classList.contains('ct-unit-history-row')) {
            nextRow.remove();
            tr.classList.remove('bg-slate-800/80');
            return;
          }
          tr.classList.add('bg-slate-800/80');
          const sameDoorList = allTxs.filter(t => t.road_door && t.road_door === it.road_door);
          const sameUnitList = sameDoorList.filter(t => (t.floor_display || t.floor) === (it.floor_display || it.floor));
          const expandTr = document.createElement('tr');
          expandTr.className = 'ct-unit-history-row bg-slate-950/95 border-b border-slate-800';
          expandTr.innerHTML = `<td colspan="7" class="p-3">${buildUnitHistoryExpansionHtml(
            it,
            sameUnitList.length > 0 ? sameUnitList : [it],
            sameDoorList.length > 0 ? sameDoorList : [it],
            `${it.road_door || ''} · ${flShow}`,
            it.road_door || ''
          )}</td>`;
          tr.insertAdjacentElement('afterend', expandTr);
        });

        ctTxTbody.appendChild(tr);
      });
    }

    const clearFilterBtn = document.getElementById('btn-ct-clear-filter');
    if (clearFilterBtn) {
      clearFilterBtn.onclick = () => {
        activeQuarterFilter = null;
        activeDoorFilter = null;
        renderDoorsBar();
        renderCommunityTransactionsTable();
        updateChartBarHighlight();
      };
    }

    renderDoorsBar();
    renderCommunityTransactionsTable();

    const s = data.summary || {};
    document.getElementById('ct-kpi-latest-price').innerText = s.latest_avg_price ? `${s.latest_avg_price} 萬/坪` : '--';
    document.getElementById('ct-kpi-latest-period').innerText = s.latest_period ? `最新季度: ${s.latest_period}` : '--';
    document.getElementById('ct-kpi-net-price').innerText = s.latest_net_price ? `${s.latest_net_price} 萬/坪` : (s.latest_avg_price ? `${s.latest_avg_price} 萬/坪` : '--');

    const chg = Number(s.all_time_change_pct || 0);
    const chgEl = document.getElementById('ct-kpi-change');
    chgEl.innerText = `${chg > 0 ? '+' : ''}${chg.toFixed(1)}%`;
    chgEl.className = `text-lg font-bold mt-0.5 ${chg > 0 ? 'text-red-400' : (chg < 0 ? 'text-emerald-400' : 'text-slate-300')}`;
    document.getElementById('ct-kpi-first-price').innerText = s.first_period ? `首期 (${s.first_period}): ${s.first_avg_price} 萬/坪` : '--';

    document.getElementById('ct-kpi-peak-price').innerText = s.peak_avg_price ? `${s.peak_avg_price} 萬/坪` : '--';
    document.getElementById('ct-kpi-peak-period').innerText = s.peak_period ? `創高季度: ${s.peak_period}` : '--';
    document.getElementById('ct-kpi-tx-count').innerText = `${(s.total_tx || 0).toLocaleString()} 筆`;
    document.getElementById('ct-kpi-repeat-count').innerText = `同戶歷次轉手: ${s.repeat_sales_count || 0} 筆`;

    // Render Chart.js Trend Chart with interactive bar/point click filtering
    const qList = data.quarters || [];
    const ctx = document.getElementById('ct-trend-chart').getContext('2d');
    if (communityTrendChartInstance) {
      communityTrendChartInstance.destroy();
    }

    function updateChartBarHighlight() {
      if (!communityTrendChartInstance) return;
      const labels = communityTrendChartInstance.data.labels || [];
      communityTrendChartInstance.data.datasets[2].backgroundColor = labels.map(p =>
        p === activeQuarterFilter ? 'rgba(245, 158, 11, 0.85)' : 'rgba(59, 130, 246, 0.35)'
      );
      communityTrendChartInstance.data.datasets[2].borderColor = labels.map(p =>
        p === activeQuarterFilter ? '#fbbf24' : 'rgba(59, 130, 246, 0.7)'
      );
      communityTrendChartInstance.update();
    }

    communityTrendChartInstance = new Chart(ctx, {
      data: {
        labels: qList.map(q => q.period),
        datasets: [
          {
            type: 'line',
            label: '平均單價 (萬/坪)',
            data: qList.map(q => q.avg_unit_price),
            borderColor: '#34d399',
            backgroundColor: 'rgba(52, 211, 153, 0.12)',
            borderWidth: 2.5,
            pointRadius: 3.5,
            tension: 0.25,
            yAxisID: 'yPrice',
            fill: true,
          },
          {
            type: 'line',
            label: '扣車位純建坪單價 (萬/坪)',
            data: qList.map(q => q.net_unit_price),
            borderColor: '#22d3ee',
            borderDash: [4, 3],
            borderWidth: 2,
            pointRadius: 2.5,
            tension: 0.25,
            yAxisID: 'yPrice',
            fill: false,
          },
          {
            type: 'bar',
            label: '成交筆數',
            data: qList.map(q => q.tx_count),
            backgroundColor: qList.map(() => 'rgba(59, 130, 246, 0.35)'),
            borderColor: qList.map(() => 'rgba(59, 130, 246, 0.7)'),
            borderWidth: 1,
            borderRadius: 4,
            yAxisID: 'yVol',
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        onClick: (e, elements) => {
          if (elements && elements.length > 0) {
            const idx = elements[0].index;
            const clickedPeriod = qList[idx]?.period;
            if (clickedPeriod) {
              activeQuarterFilter = (activeQuarterFilter === clickedPeriod) ? null : clickedPeriod;
              updateChartBarHighlight();
              renderCommunityTransactionsTable();
              const txSec = document.getElementById('ct-tx-section');
              if (txSec) txSec.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
          }
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: 'rgba(15, 23, 42, 0.95)',
            borderColor: '#334155',
            borderWidth: 1,
            callbacks: {
              footer: (items) => {
                const p = items[0]?.label;
                return `👉 點擊篩選【${p}】成交細項 (含土地/建坪/總價)`;
              }
            }
          }
        },
        scales: {
          x: {
            ticks: { color: '#94a3b8', font: { size: 10 }, maxTicksLimit: 14 },
            grid: { color: 'rgba(51, 65, 85, 0.25)' }
          },
          yPrice: {
            type: 'linear',
            position: 'left',
            ticks: { color: '#34d399', font: { size: 10 }, callback: v => `${v}萬` },
            grid: { color: 'rgba(51, 65, 85, 0.3)' }
          },
          yVol: {
            type: 'linear',
            position: 'right',
            beginAtZero: true,
            ticks: { color: '#60a5fa', font: { size: 10 }, stepSize: 1 },
            grid: { drawOnChartArea: false }
          }
        }
      }
    });

    // Render Repeat Sales Table
    const repeats = data.repeat_sales || [];
    const rBody = document.getElementById('ct-repeat-tbody');
    if (repeats.length === 0) {
      rBody.innerHTML = '<tr><td colspan="6" class="py-4 text-center text-slate-500">目前此建案尚無同門牌同樓層之二次轉手紀錄 (可點擊上方任一成交細項檢視同門牌各樓層歷年成交)</td></tr>';
    } else {
      rBody.innerHTML = repeats.map(r => {
        const sign = r.profit_wan > 0 ? '+' : '';
        const clr = r.profit_wan > 0 ? 'text-red-400' : (r.profit_wan < 0 ? 'text-emerald-400' : 'text-slate-300');
        return `
          <tr class="hover:bg-slate-900/70 transition">
            <td class="py-2 font-semibold text-white">${r.unit_label}</td>
            <td class="py-2 text-slate-300">${r.area_ping ? `${r.area_ping} 坪` : '--'}</td>
            <td class="py-2 text-slate-300">
              <span class="font-mono text-slate-400">${r.prev_date}</span> · 
              <b>${r.prev_total_wan.toLocaleString()} 萬</b> (${r.prev_unit_price} 萬/坪)
            </td>
            <td class="py-2 text-slate-200">
              <span class="font-mono text-slate-400">${r.curr_date}</span> · 
              <b>${r.curr_total_wan.toLocaleString()} 萬</b> (${r.curr_unit_price} 萬/坪)
            </td>
            <td class="py-2 text-slate-400">${r.hold_years ? `約 ${r.hold_years} 年` : '--'}</td>
            <td class="py-2 text-right font-bold ${clr}">
              ${sign}${r.profit_wan.toLocaleString()} 萬 (${sign}${r.profit_pct}%)
            </td>
          </tr>
        `;
      }).join('');
    }
  } catch (err) {
    console.error('Failed to load community trend:', err);
    document.getElementById('ct-spec-badge').innerText = '載入失敗';
  }
}

// Expose handlers globally for HTML attributes and popups
window.openTransactionDrawer = openTransactionDrawer;
window.openCommunityTrendModal = openCommunityTrendModal;
window.promptTagCommunityName = promptTagCommunityName;
window.selectRegion = selectRegion;

