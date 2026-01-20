// DOM Elements
const settingsForm = document.getElementById('settings-form');
const apiBaseUrlInput = document.getElementById('api-base-url');
const apiKeyInput = document.getElementById('api-key');
const statusMessage = document.getElementById('status-message');

// Load saved settings
async function loadSettings() {
  const settings = await chrome.storage.sync.get(['apiBaseUrl', 'apiKey']);
  if (settings.apiBaseUrl) {
    apiBaseUrlInput.value = settings.apiBaseUrl;
  }
  if (settings.apiKey) {
    apiKeyInput.value = settings.apiKey;
  }
}

// Show status message
function showStatus(message, isError = false) {
  statusMessage.textContent = message;
  statusMessage.classList.remove('hidden', 'success', 'error');
  statusMessage.classList.add(isError ? 'error' : 'success');

  // Auto-hide after 3 seconds
  setTimeout(() => {
    statusMessage.classList.add('hidden');
  }, 3000);
}

// Save settings
async function saveSettings(e) {
  e.preventDefault();

  const apiBaseUrl = apiBaseUrlInput.value.trim().replace(/\/$/, ''); // Remove trailing slash
  const apiKey = apiKeyInput.value.trim();

  if (!apiBaseUrl || !apiKey) {
    showStatus('Please fill in all fields', true);
    return;
  }

  try {
    await chrome.storage.sync.set({
      apiBaseUrl: apiBaseUrl,
      apiKey: apiKey,
    });
    showStatus('Settings saved successfully!');
  } catch (err) {
    console.error('Save error:', err);
    showStatus('Failed to save settings', true);
  }
}

// Event Listeners
settingsForm.addEventListener('submit', saveSettings);

// Initialize
loadSettings();
