(() => {
  "use strict";

  const form = document.querySelector("[data-avatar-crop-form]");
  if (!form) {
    return;
  }

  const settings = form.closest("[data-avatar-settings]");
  const fileInput = form.querySelector("[data-avatar-file]");
  const editor = form.querySelector("[data-avatar-crop-editor]");
  const stage = form.querySelector("[data-avatar-crop-stage]");
  const image = form.querySelector("[data-avatar-crop-image]");
  const zoomInput = form.querySelector("[data-avatar-zoom]");
  const zoomOutput = form.querySelector("[data-avatar-zoom-output]");
  const hiddenX = form.querySelector("[data-avatar-crop-x]");
  const hiddenY = form.querySelector("[data-avatar-crop-y]");
  const hiddenZoom = form.querySelector("[data-avatar-crop-zoom]");
  const adjustButton = settings?.querySelector("[data-avatar-adjust]");
  const resetButton = form.querySelector("[data-avatar-reset]");
  const cancelButton = form.querySelector("[data-avatar-cancel]");

  if (!fileInput || !editor || !stage || !image || !zoomInput || !hiddenX || !hiddenY || !hiddenZoom) {
    return;
  }

  const minZoom = Number(zoomInput.min) || 1;
  const maxZoom = Number(zoomInput.max) || 3;
  let mode = "upload";
  let objectUrl = "";
  let naturalWidth = 0;
  let naturalHeight = 0;
  let centerX = Number(form.dataset.cropX) || 0.5;
  let centerY = Number(form.dataset.cropY) || 0.5;
  let zoom = Number(form.dataset.cropZoom) || minZoom;
  let drag = null;

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  function clampCenter() {
    if (!naturalWidth || !naturalHeight) {
      return;
    }
    const cropWidth = Math.min(naturalWidth, naturalHeight * 3 / 4) / zoom;
    const cropHeight = cropWidth * 4 / 3;
    const halfWidth = cropWidth / (2 * naturalWidth);
    const halfHeight = cropHeight / (2 * naturalHeight);
    centerX = halfWidth >= 0.5 ? 0.5 : clamp(centerX, halfWidth, 1 - halfWidth);
    centerY = halfHeight >= 0.5 ? 0.5 : clamp(centerY, halfHeight, 1 - halfHeight);
  }

  function syncFields() {
    hiddenX.value = centerX.toFixed(6);
    hiddenY.value = centerY.toFixed(6);
    hiddenZoom.value = zoom.toFixed(6);
    zoomInput.value = String(zoom);
    zoomOutput.value = `${Math.round(zoom * 100)}%`;
    zoomOutput.textContent = zoomOutput.value;
  }

  function render() {
    if (!naturalWidth || !naturalHeight) {
      return;
    }
    clampCenter();
    const stageBounds = stage.getBoundingClientRect();
    if (!stageBounds.width || !stageBounds.height) {
      return;
    }
    const coverScale = Math.max(
      stageBounds.width / naturalWidth,
      stageBounds.height / naturalHeight
    );
    const displayWidth = naturalWidth * coverScale * zoom;
    const displayHeight = naturalHeight * coverScale * zoom;
    image.style.width = `${displayWidth}px`;
    image.style.height = `${displayHeight}px`;
    image.style.left = `${stageBounds.width / 2 - centerX * displayWidth}px`;
    image.style.top = `${stageBounds.height / 2 - centerY * displayHeight}px`;
    syncFields();
  }

  function setMode(nextMode) {
    mode = nextMode;
    form.action = nextMode === "adjust" ? form.dataset.adjustAction : form.dataset.uploadAction;
    fileInput.required = nextMode !== "adjust";
  }

  function releaseObjectUrl() {
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      objectUrl = "";
    }
  }

  function loadSource(source, reset) {
    if (!source) {
      return;
    }
    if (reset) {
      centerX = 0.5;
      centerY = 0.5;
      zoom = minZoom;
    }
    naturalWidth = 0;
    naturalHeight = 0;
    image.onload = () => {
      naturalWidth = image.naturalWidth;
      naturalHeight = image.naturalHeight;
      clampCenter();
      requestAnimationFrame(render);
    };
    image.src = source;
    editor.hidden = false;
  }

  fileInput.addEventListener("change", () => {
    const file = fileInput.files?.[0];
    if (!file) {
      return;
    }
    releaseObjectUrl();
    objectUrl = URL.createObjectURL(file);
    setMode("upload");
    loadSource(objectUrl, true);
  });

  adjustButton?.addEventListener("click", () => {
    releaseObjectUrl();
    fileInput.value = "";
    setMode("adjust");
    loadSource(form.dataset.sourceUrl, false);
  });

  zoomInput.addEventListener("input", () => {
    zoom = clamp(Number(zoomInput.value) || minZoom, minZoom, maxZoom);
    render();
  });

  resetButton?.addEventListener("click", () => {
    centerX = 0.5;
    centerY = 0.5;
    zoom = minZoom;
    render();
  });

  cancelButton?.addEventListener("click", () => {
    editor.hidden = true;
    releaseObjectUrl();
    fileInput.value = "";
    setMode("upload");
  });

  stage.addEventListener("pointerdown", (event) => {
    if (!naturalWidth || !naturalHeight) {
      return;
    }
    const stageBounds = stage.getBoundingClientRect();
    const coverScale = Math.max(
      stageBounds.width / naturalWidth,
      stageBounds.height / naturalHeight
    );
    drag = {
      pointerX: event.clientX,
      pointerY: event.clientY,
      centerX,
      centerY,
      displayWidth: naturalWidth * coverScale * zoom,
      displayHeight: naturalHeight * coverScale * zoom
    };
    stage.setPointerCapture?.(event.pointerId);
    stage.classList.add("is-dragging");
    event.preventDefault();
  });

  stage.addEventListener("pointermove", (event) => {
    if (!drag) {
      return;
    }
    centerX = drag.centerX - (event.clientX - drag.pointerX) / drag.displayWidth;
    centerY = drag.centerY - (event.clientY - drag.pointerY) / drag.displayHeight;
    render();
  });

  function endDrag(event) {
    if (!drag) {
      return;
    }
    drag = null;
    stage.releasePointerCapture?.(event.pointerId);
    stage.classList.remove("is-dragging");
  }

  stage.addEventListener("pointerup", endDrag);
  stage.addEventListener("pointercancel", endDrag);

  stage.addEventListener("keydown", (event) => {
    const step = 0.01;
    const actions = {
      ArrowLeft: () => { centerX -= step; },
      ArrowRight: () => { centerX += step; },
      ArrowUp: () => { centerY -= step; },
      ArrowDown: () => { centerY += step; },
      "+": () => { zoom = clamp(zoom + 0.05, minZoom, maxZoom); },
      "=": () => { zoom = clamp(zoom + 0.05, minZoom, maxZoom); },
      "-": () => { zoom = clamp(zoom - 0.05, minZoom, maxZoom); }
    };
    if (!actions[event.key]) {
      return;
    }
    event.preventDefault();
    actions[event.key]();
    render();
  });

  form.addEventListener("submit", () => {
    setMode(mode);
    syncFields();
  });

  if ("ResizeObserver" in window) {
    new ResizeObserver(render).observe(stage);
  } else {
    window.addEventListener("resize", render);
  }

  setMode("upload");
  syncFields();
})();
