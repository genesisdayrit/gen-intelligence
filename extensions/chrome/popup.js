// DOM Elements
const saveForm = document.getElementById('save-form');
const successState = document.getElementById('success-state');
const configRequired = document.getElementById('config-required');
const titleInput = document.getElementById('title-input');
const saveBtn = document.getElementById('save-btn');
const saveBtnText = document.getElementById('save-btn-text');
const saveSpinner = document.getElementById('save-spinner');
const urlDisplay = document.getElementById('url-display');
const errorMessage = document.getElementById('error-message');
const openObsidianBtn = document.getElementById('open-obsidian-btn');
const openSettingsBtn = document.getElementById('open-settings-btn');
const settingsBtn = document.getElementById('settings-btn');

// State
let currentUrl = '';
let savedFilePath = '';
let savedVaultName = '';

// Initialize popup
async function init() {
  // Check if settings are configured
  const settings = await chrome.storage.sync.get(['apiBaseUrl', 'apiKey']);

  if (!settings.apiBaseUrl || !settings.apiKey) {
    showConfigRequired();
    return;
  }

  // Get current tab info
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab) {
    currentUrl = tab.url;
    titleInput.value = tab.title || '';
    urlDisplay.textContent = formatUrl(currentUrl);
  }

  // Focus title input
  titleInput.focus();
  titleInput.select();
}

// Format URL for display (truncate if too long)
function formatUrl(url) {
  if (url.length > 60) {
    return url.substring(0, 57) + '...';
  }
  return url;
}

// Show config required state
function showConfigRequired() {
  saveForm.classList.add('hidden');
  successState.classList.add('hidden');
  configRequired.classList.remove('hidden');
}

// Show success state
function showSuccess() {
  saveForm.classList.add('hidden');
  configRequired.classList.add('hidden');
  successState.classList.remove('hidden');
  openObsidianBtn.focus();
}

// Show error
function showError(message) {
  errorMessage.textContent = message;
  errorMessage.classList.remove('hidden');
}

// Hide error
function hideError() {
  errorMessage.classList.add('hidden');
}

// Set loading state
function setLoading(loading) {
  saveBtn.disabled = loading;
  saveBtnText.textContent = loading ? 'Saving...' : 'Save page';
  if (loading) {
    saveSpinner.classList.remove('hidden');
  } else {
    saveSpinner.classList.add('hidden');
  }
}

// Save link to Knowledge Hub
async function saveLink() {
  hideError();
  setLoading(true);

  try {
    const settings = await chrome.storage.sync.get(['apiBaseUrl', 'apiKey']);
    const title = titleInput.value.trim() || null;

    const response = await fetch(`${settings.apiBaseUrl}/share/link`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-API-Key': settings.apiKey,
      },
      body: JSON.stringify({
        url: currentUrl,
        title: title,
      }),
    });

    if (response.status === 401) {
      showError('Invalid API key - check extension settings');
      setLoading(false);
      return;
    }

    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
      showError(error.detail || 'Failed to save link');
      setLoading(false);
      return;
    }

    const data = await response.json();
    console.log('API Response:', data);
    savedFilePath = data.file_path;
    savedVaultName = data.vault_name;

    setLoading(false);
    showSuccess();
  } catch (err) {
    console.error('Save error:', err);
    showError('Connection error - check your network');
    setLoading(false);
  }
}

// Open in Obsidian
function openInObsidian() {
  if (savedVaultName && savedFilePath) {
    // Remove .md extension for Obsidian URL
    const filePathWithoutExt = savedFilePath.replace(/\.md$/, '');
    const obsidianUrl = `obsidian://open?vault=${encodeURIComponent(savedVaultName)}&file=${encodeURIComponent(filePathWithoutExt)}`;
    console.log('Opening Obsidian URL:', obsidianUrl);
    // Use window.open for custom URL schemes (chrome.tabs.create doesn't work well with obsidian://)
    window.open(obsidianUrl);
    window.close();
  } else {
    console.error('Cannot open Obsidian: vault or file path missing', { savedVaultName, savedFilePath });
  }
}

// Open settings page
function openSettings() {
  chrome.runtime.openOptionsPage();
  window.close();
}

// Event Listeners
saveBtn.addEventListener('click', saveLink);
openObsidianBtn.addEventListener('click', openInObsidian);
openSettingsBtn.addEventListener('click', openSettings);
settingsBtn.addEventListener('click', openSettings);

// Enter key handlers
document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    // If in save form state
    if (!saveForm.classList.contains('hidden') && !saveBtn.disabled) {
      e.preventDefault();
      saveLink();
    }
    // If in success state
    else if (!successState.classList.contains('hidden')) {
      openInObsidian();
    }
  }
});

// Initialize on load
init();
