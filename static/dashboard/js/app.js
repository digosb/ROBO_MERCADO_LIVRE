(() => {
  const csrfToken = document.querySelector('[name="csrfmiddlewaretoken"]')?.value;
  const toastRegion = document.getElementById('toast-region');

  function showToast(message, isError = false) {
    if (!toastRegion) return;
    const toast = document.createElement('div');
    toast.className = `toast${isError ? ' toast-error' : ''}`;
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('class', 'icon');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', isError ? '#i-alert' : '#i-check');
    icon.append(use);
    const text = document.createElement('span');
    text.textContent = message;
    toast.append(icon, text);
    toastRegion.append(toast);
    window.setTimeout(() => toast.remove(), 4800);
  }

  async function sendRequest(url, options = {}) {
    const response = await fetch(url, {
      credentials: 'same-origin',
      ...options,
      headers: {
        'X-CSRFToken': csrfToken || '',
        ...(options.headers || {}),
      },
    });
    const data = await response.json();
    if (!response.ok || data.success === false) {
      throw new Error(data.message || `A solicitação falhou (${response.status}).`);
    }
    return data;
  }

  async function refreshDashboard() {
    const response = await fetch('/api/status/', { credentials: 'same-origin' });
    if (!response.ok) throw new Error('Não foi possível atualizar o estado do painel.');
    const data = await response.json();
    const values = {
      'metric-offers': data.metrics.offers_found,
      'metric-valid': data.metrics.valid_offers,
      'metric-affiliates': data.metrics.affiliate_links,
      'metric-messages': data.metrics.messages_sent,
      'metric-errors': data.metrics.errors,
    };
    Object.entries(values).forEach(([id, value]) => {
      const element = document.getElementById(id);
      if (element) element.textContent = value;
    });
    const status = document.getElementById('robot-status');
    const statusLabel = document.getElementById('robot-status-label');
    const summaryTitle = document.getElementById('robot-summary-title');
    if (status && statusLabel) {
      status.className = `status-chip status-${data.status}`;
      statusLabel.textContent = data.status_label;
    }
    if (summaryTitle) {
      summaryTitle.textContent = {
        running: 'Robô em execução',
        paused: 'Robô pausado',
        stopped: 'Robô parado',
      }[data.status] || 'Estado desconhecido';
    }
    const lastExecution = document.getElementById('last-execution');
    if (lastExecution) {
      lastExecution.textContent = data.last_execution
        ? new Intl.DateTimeFormat('pt-BR', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(data.last_execution))
        : 'Nenhuma execução';
    }
  }

  document.querySelectorAll('[data-robot-action]').forEach((button) => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const result = await sendRequest(button.dataset.actionUrl, { method: 'POST' });
        showToast(result.message);
        if (document.getElementById('robot-status')) await refreshDashboard();
      } catch (error) {
        showToast(error.message || 'Não foi possível executar a ação.', true);
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelector('[data-whatsapp-prepare]')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      const result = await sendRequest(form.dataset.prepareUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ offer_name: form.elements.offer_name.value }),
      });
      const preview = document.getElementById('message-preview');
      preview.querySelector('p').textContent = result.preview;
      preview.hidden = false;
      showToast(result.message);
    } catch (error) {
      showToast(error.message || 'Não foi possível preparar a prévia.', true);
    } finally {
      button.disabled = false;
    }
  });

  const menuToggle = document.querySelector('[data-menu-toggle]');
  const closeMenu = () => {
    document.body.classList.remove('menu-open');
    menuToggle?.setAttribute('aria-expanded', 'false');
  };
  menuToggle?.addEventListener('click', () => {
    const open = document.body.classList.toggle('menu-open');
    menuToggle.setAttribute('aria-expanded', String(open));
  });
  document.querySelector('[data-close-menu]')?.addEventListener('click', closeMenu);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeMenu();
  });

  const date = document.querySelector('[data-current-date]');
  if (date) {
    date.textContent = new Intl.DateTimeFormat('pt-BR', {
      weekday: 'short',
      day: '2-digit',
      month: 'short',
    }).format(new Date());
  }
})();
