(() => {
  'use strict';

  const STORAGE_KEY = 'ridgewalker-tutorial-complete-v1';
  const DISCLAIMER_STORAGE_KEY = 'ridgewalker-disclaimer-accepted-v1';

  const welcomeEl = document.getElementById('tutorial-welcome');
  const spotlightEl = document.getElementById('tutorial-spotlight');
  const tooltipEl = document.getElementById('tutorial-tooltip');
  const titleEl = document.getElementById('tutorial-title');
  const copyEl = document.getElementById('tutorial-copy');
  const progressEl = document.getElementById('tutorial-progress');
  const backEl = document.getElementById('tutorial-back');
  const nextEl = document.getElementById('tutorial-next');
  const helpEl = document.getElementById('tutorial-help');

  const initialDisclaimerEl = document.getElementById('initial-disclaimer');
  const disclaimerUnderstandEl = document.getElementById('disclaimer-understand');

  const licenceModalEl = document.getElementById('licence-modal');
  const openLicenceEl = document.getElementById('open-licence-modal');
  const closeLicenceEl = document.getElementById('close-licence-modal');
  const licenceDoneEl = document.getElementById('licence-done');

  // Increase the size of the tutorial box and its text.
  const TUTORIAL_STYLE_ID = 'ridgewalker-tutorial-size-overrides';

  if (!document.getElementById(TUTORIAL_STYLE_ID)) {
    const styleEl = document.createElement('style');

    styleEl.id = TUTORIAL_STYLE_ID;

    styleEl.textContent = `
      #tutorial-tooltip {
        width: min(460px, calc(100vw - 32px));
        padding: 28px;
        border-radius: 16px;
      }

      #tutorial-tooltip .tutorial-tooltip-header {
        margin-bottom: 12px;
      }

      #tutorial-tooltip .tutorial-progress {
        font-size: 0.84rem;
        line-height: 1.25;
        letter-spacing: 0.09em;
      }

      #tutorial-tooltip .tutorial-close {
        width: 36px;
        height: 36px;
        display: grid;
        place-items: center;
        padding: 0;
        font-size: 1.65rem;
      }

      #tutorial-tooltip h2 {
        margin-bottom: 12px;
        font-size: 1.5rem;
        line-height: 1.25;
      }

      #tutorial-tooltip p {
        font-size: 1rem;
        line-height: 1.6;
      }

      #tutorial-tooltip .tutorial-tooltip-actions {
        gap: 14px;
        margin-top: 30px;
      }

      #tutorial-tooltip .tutorial-tooltip-actions button {
        min-height: 48px;
        padding: 12px 20px;
        border-radius: 10px;
        font-size: 0.9rem;
      }

    @media (max-width: 768px) {
      #tutorial-tooltip {
        width: auto;
        padding: 24px;
    }   
  }
`;

    document.head.appendChild(styleEl);
  }

  const steps = [
    {
      target: '#search',
      title: 'Find your starting area',
      copy: 'Select the search icon and enter a New Zealand place name to move the map to that area before planning your route.',
      placement: 'left'
    },
    {
      target: '#tutorial-layers',
      title: 'Adjust the terrain view',
      copy: 'Use the Topo Opacity slider to adjust how clearly the topographic layer appears on the map.',
      placement: 'right'
    },
    {
      target: '#mode-switch',
      title: 'Choose how to plan',
      copy: 'Use 2-Point for a direct route, or Multi-Point to guide the route through additional locations.',
      placement: 'right'
    },
    {
      target: () => {
        const multiModeEl = document.getElementById('mode-multi');

        return multiModeEl && !multiModeEl.hidden
          ? '#mode-multi'
          : '#mode-fixed';
      },
      title: 'Set your route points',
      copy: 'In 2-Point mode, place a Start Pin and End Pin. In Multi-Point mode, use + Add Waypoint to add at least three points. Drag any pin to fine-tune its position.',
      placement: 'right'
    },
    {
      target: '#generate-route',
      title: 'Generate a terrain-aware route',
      copy: 'Once your points are selected, RidgeWalker will calculate a route using terrain and elevation data.',
      placement: 'right'
    },
    {
      target: '#tutorial-results',
      title: 'Review and export',
      copy: 'Check the route distance, estimated time and climb, then export it as a GPX file for a compatible navigation app.',
      placement: 'right'
    }
  ];

  let activeStep = 0;
  let previousFocus = null;

  function setHidden(element, hidden) {
    if (!element) return;

    element.hidden = hidden;
  }

  function rememberComplete() {
    try {
      localStorage.setItem(STORAGE_KEY, 'true');
    } catch (_) {
      // The tour still works when storage is unavailable.
    }
  }

  function hasCompletedTour() {
    try {
      return localStorage.getItem(STORAGE_KEY) === 'true';
    } catch (_) {
      return false;
    }
  }

  function rememberDisclaimerAccepted() {
    try {
      localStorage.setItem(DISCLAIMER_STORAGE_KEY, 'true');
    } catch (_) {
      // The disclaimer still works when storage is unavailable.
    }
  }

  function hasAcceptedDisclaimer() {
    try {
      return localStorage.getItem(DISCLAIMER_STORAGE_KEY) === 'true';
    } catch (_) {
      return false;
    }
  }

  function showWelcome() {
    previousFocus = document.activeElement;
    setHidden(welcomeEl, false);

    requestAnimationFrame(() => {
      document.getElementById('tutorial-start')?.focus();
    });
  }

  function showInitialDisclaimer() {
    previousFocus = document.activeElement;
    setHidden(initialDisclaimerEl, false);

    requestAnimationFrame(() => {
      disclaimerUnderstandEl?.focus();
    });
  }

  function acceptInitialDisclaimer() {
    rememberDisclaimerAccepted();
    setHidden(initialDisclaimerEl, true);
    showWelcome();
  }

  function closeWelcome() {
    setHidden(welcomeEl, true);
  }

  function endTour({ completed = false } = {}) {
    if (completed) {
      rememberComplete();
    }

    setHidden(spotlightEl, true);
    setHidden(tooltipEl, true);

    document.body.classList.remove('tutorial-open');

    window.removeEventListener('resize', positionStep);
    window.removeEventListener('scroll', positionStep, true);

    (helpEl || previousFocus)?.focus?.();
  }

  function targetRectFor(step) {
    const targetValue =
      typeof step.target === 'function'
        ? step.target()
        : step.target;

    const selectors = Array.isArray(targetValue)
      ? targetValue
      : [targetValue];

    for (const selector of selectors) {
      if (!selector) continue;

      const target = document.querySelector(selector);

      if (!target || target.hidden) continue;

      const rect = target.getBoundingClientRect();

      if (rect.width && rect.height) {
        return rect;
      }
    }

    return null;
  }

  function positionTooltip(rect, preferredPlacement) {
    const gap = 18;
    const edge = 16;

    const tooltipRect = tooltipEl.getBoundingClientRect();

    const availableRight = window.innerWidth - rect.right;
    const availableLeft = rect.left;
    const availableBelow = window.innerHeight - rect.bottom;

    let placement = preferredPlacement;

    if (
      placement === 'right' &&
      availableRight < tooltipRect.width + gap
    ) {
      placement =
        availableLeft >= tooltipRect.width + gap
          ? 'left'
          : 'below';
    }

    if (
      placement === 'below' &&
      availableBelow < tooltipRect.height + gap
    ) {
      placement = 'above';
    }

    let left;
    let top;

    if (placement === 'left') {
      left = rect.left - tooltipRect.width - gap;
      top = rect.top;
    } else if (placement === 'below') {
      left = rect.left;
      top = rect.bottom + gap;
    } else if (placement === 'above') {
      left = rect.left;
      top = rect.top - tooltipRect.height - gap;
    } else {
      left = rect.right + gap;
      top = rect.top;
    }

    left = Math.max(
      edge,
      Math.min(
        left,
        window.innerWidth - tooltipRect.width - edge
      )
    );

    top = Math.max(
      edge,
      Math.min(
        top,
        window.innerHeight - tooltipRect.height - edge
      )
    );

    tooltipEl.classList.remove(
      'is-left',
      'is-right',
      'is-below',
      'is-above'
    );

    tooltipEl.classList.add(`is-${placement}`);

    tooltipEl.style.left = `${Math.round(left)}px`;
    tooltipEl.style.top = `${Math.round(top)}px`;
  }

  function positionStep() {
    if (tooltipEl.hidden) return;

    const step = steps[activeStep];
    const rect = targetRectFor(step);

    if (!rect) return;

    const padding = 8;

    spotlightEl.style.left =
      `${Math.round(rect.left - padding)}px`;

    spotlightEl.style.top =
      `${Math.round(rect.top - padding)}px`;

    spotlightEl.style.width =
      `${Math.round(rect.width + padding * 2)}px`;

    spotlightEl.style.height =
      `${Math.round(rect.height + padding * 2)}px`;

    positionTooltip(rect, step.placement);
  }

  function renderStep(index) {
    activeStep = Math.max(
      0,
      Math.min(index, steps.length - 1)
    );

    const step = steps[activeStep];

    titleEl.textContent = step.title;
    copyEl.textContent = step.copy;

    progressEl.textContent =
      `${activeStep + 1} of ${steps.length}`;

    backEl.disabled = activeStep === 0;

    backEl.style.visibility =
      activeStep === 0
        ? 'hidden'
        : 'visible';

    nextEl.textContent =
      activeStep === steps.length - 1
        ? 'Start planning'
        : 'Next';

    setHidden(spotlightEl, false);
    setHidden(tooltipEl, false);

    requestAnimationFrame(() => {
      positionStep();
      nextEl.focus();
    });
  }

  function startTour() {
    closeWelcome();

    previousFocus = document.activeElement;

    document.body.classList.add('tutorial-open');

    window.addEventListener(
      'resize',
      positionStep
    );

    window.addEventListener(
      'scroll',
      positionStep,
      true
    );

    renderStep(0);
  }

  function showLicenceModal() {
    previousFocus = document.activeElement;

    setHidden(licenceModalEl, false);

    requestAnimationFrame(() => {
      closeLicenceEl?.focus();
    });
  }

  function closeLicenceModal() {
    setHidden(licenceModalEl, true);

    previousFocus?.focus?.();
  }

  document
    .getElementById('tutorial-start')
    ?.addEventListener('click', startTour);

  disclaimerUnderstandEl?.addEventListener(
    'click',
    acceptInitialDisclaimer
  );

  document
    .getElementById('tutorial-skip')
    ?.addEventListener('click', () => {
      rememberComplete();
      closeWelcome();
      helpEl?.focus();
    });

  document
    .getElementById('tutorial-close')
    ?.addEventListener('click', () => {
      endTour();
    });

  helpEl?.addEventListener('click', startTour);

  // Reposition the tutorial after changing between
  // 2-Point and Multi-Point modes.
  document
    .getElementById('mode-switch')
    ?.addEventListener('click', () => {
      if (tooltipEl?.hidden) return;

      requestAnimationFrame(() => {
        requestAnimationFrame(positionStep);
      });
    });

  // Keep the first spotlight aligned when the
  // search control expands or closes.
  document
    .getElementById('search-toggle')
    ?.addEventListener('click', () => {
      if (tooltipEl?.hidden || activeStep !== 0) {
        return;
      }

      requestAnimationFrame(() => {
        requestAnimationFrame(positionStep);
      });
    });

  backEl?.addEventListener('click', () => {
    renderStep(activeStep - 1);
  });

  nextEl?.addEventListener('click', () => {
    if (activeStep === steps.length - 1) {
      endTour({ completed: true });
    } else {
      renderStep(activeStep + 1);
    }
  });

  openLicenceEl?.addEventListener(
    'click',
    showLicenceModal
  );

  closeLicenceEl?.addEventListener(
    'click',
    closeLicenceModal
  );

  licenceDoneEl?.addEventListener(
    'click',
    closeLicenceModal
  );

  welcomeEl?.addEventListener('click', event => {
    if (event.target === welcomeEl) {
      rememberComplete();
      closeWelcome();
    }
  });

  licenceModalEl?.addEventListener('click', event => {
    if (event.target === licenceModalEl) {
      closeLicenceModal();
    }
  });

  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;

    if (!licenceModalEl?.hidden) {
      closeLicenceModal();
    } else if (!initialDisclaimerEl?.hidden) {
      disclaimerUnderstandEl?.focus();
    } else if (!welcomeEl?.hidden) {
      closeWelcome();
    } else if (!tooltipEl?.hidden) {
      endTour();
    }
  });

  if (!hasAcceptedDisclaimer()) {
    window.addEventListener(
      'load',
      () => setTimeout(showInitialDisclaimer, 450),
      { once: true }
    );
  } else if (!hasCompletedTour()) {
    window.addEventListener(
      'load',
      () => setTimeout(showWelcome, 450),
      { once: true }
    );
  }
})();
