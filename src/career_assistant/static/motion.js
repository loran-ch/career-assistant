(function () {
  "use strict";

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  document.documentElement.classList.add("motion-ready");

  if (!window.gsap || reducedMotion.matches) {
    return;
  }

  const gsap = window.gsap;
  const ScrollTrigger = window.ScrollTrigger;
  const canHover = window.matchMedia("(hover: hover) and (pointer: fine)").matches;

  if (ScrollTrigger) {
    gsap.registerPlugin(ScrollTrigger);
  }

  document.documentElement.classList.add("motion-enabled");
  gsap.defaults({ ease: "power3.out" });

  function visible(element) {
    return element && element.getClientRects().length > 0;
  }

  function directChildren(container) {
    if (!container) {
      return [];
    }
    return Array.from(container.children).filter(function (element) {
      return !["SCRIPT", "STYLE", "LINK", "META"].includes(element.tagName) && visible(element);
    });
  }

  function animateAuthPage() {
    const timeline = gsap.timeline({
      defaults: { duration: 0.48, ease: "power3.out" }
    });

    timeline
      .from(".auth-header > *", {
        y: -8,
        autoAlpha: 0,
        duration: 0.4,
        stagger: 0.06
      })
      .from(
        ".auth-panel",
        {
          y: 22,
          scale: 0.985,
          autoAlpha: 0,
          duration: 0.62
        },
        "-=0.24"
      )
      .from(
        ".auth-panel > *",
        {
          y: 10,
          autoAlpha: 0,
          duration: 0.38,
          stagger: 0.045
        },
        "-=0.42"
      )
      .from(
        ".auth-form label, .auth-form button",
        {
          y: 8,
          autoAlpha: 0,
          duration: 0.32,
          stagger: 0.045
        },
        "-=0.34"
      )
      .from(
        ".auth-footer",
        {
          y: 6,
          autoAlpha: 0,
          duration: 0.32
        },
        "-=0.2"
      );

    gsap.fromTo(
      ".auth-error, .auth-panel .notice",
      { x: -7 },
      {
        x: 0,
        duration: 0.38,
        ease: "power2.out",
        delay: 0.42
      }
    );
  }

  function setupScrollReveals(targets) {
    if (!targets.length) {
      return;
    }

    if (!ScrollTrigger) {
      gsap.from(targets, {
        y: 16,
        autoAlpha: 0,
        duration: 0.5,
        stagger: 0.06
      });
      return;
    }

    gsap.set(targets, { y: 18, autoAlpha: 0 });
    ScrollTrigger.batch(targets, {
      start: "top 92%",
      once: true,
      batchMax: 5,
      onEnter: function (batch) {
        gsap.to(batch, {
          y: 0,
          autoAlpha: 1,
          duration: 0.54,
          stagger: 0.055,
          overwrite: true,
          clearProps: "transform"
        });
      }
    });
  }

  function animateAppPage() {
    const topLevel = directChildren(document.querySelector("main"));
    const viewportLimit = window.innerHeight * 0.94;
    const firstScreen = [];
    const belowFold = [];

    topLevel.forEach(function (element) {
      if (element.getBoundingClientRect().top < viewportLimit) {
        firstScreen.push(element);
      } else {
        belowFold.push(element);
      }
    });

    const timeline = gsap.timeline({
      defaults: { duration: 0.46, ease: "power3.out" }
    });

    timeline
      .from(".app-brand", {
        x: -9,
        autoAlpha: 0,
        duration: 0.4
      })
      .from(
        ".app-nav > *",
        {
          y: -7,
          autoAlpha: 0,
          duration: 0.32,
          stagger: 0.035
        },
        "-=0.2"
      )
      .from(
        firstScreen.slice(0, 6),
        {
          y: 14,
          autoAlpha: 0,
          duration: 0.48,
          stagger: 0.06
        },
        "-=0.24"
      );

    setupScrollReveals(belowFold);
  }

  function setupCardMotion() {
    if (!canHover) {
      return;
    }

    gsap.utils.toArray("main article, .assistant-layout > section").forEach(function (card) {
      let hovering = false;

      card.addEventListener("pointerenter", function () {
        hovering = true;
        gsap.to(card, {
          y: -2,
          duration: 0.22,
          ease: "power2.out",
          overwrite: "auto"
        });
      });

      card.addEventListener("pointerleave", function () {
        if (!hovering) {
          return;
        }
        hovering = false;
        gsap.to(card, {
          y: 0,
          duration: 0.28,
          ease: "power2.out",
          overwrite: "auto",
          clearProps: "transform"
        });
      });
    });
  }

  function setupButtonMotion() {
    if (!canHover) {
      return;
    }

    gsap.utils
      .toArray(
        "button:not([disabled]), a.button, .assistant-filter a, .app-nav > a, .auth-switch a"
      )
      .forEach(function (control) {
        control.addEventListener("pointerenter", function () {
          gsap.to(control, {
            scale: 1.015,
            duration: 0.18,
            ease: "power2.out",
            overwrite: "auto"
          });
        });

        control.addEventListener("pointerleave", function () {
          gsap.to(control, {
            scale: 1,
            duration: 0.22,
            ease: "power2.out",
            overwrite: "auto",
            clearProps: "transform"
          });
        });

        control.addEventListener("pointerdown", function () {
          gsap.to(control, {
            scale: 0.975,
            duration: 0.1,
            ease: "power1.out",
            overwrite: "auto"
          });
        });

        control.addEventListener("pointerup", function () {
          gsap.to(control, {
            scale: 1.015,
            duration: 0.16,
            ease: "power2.out",
            overwrite: "auto"
          });
        });
      });
  }

  function animateFreshContent(container) {
    if (!container || reducedMotion.matches) {
      return;
    }

    const elements = Array.from(container.children).filter(function (element) {
      return element.tagName !== "SCRIPT" && !element.hidden;
    });

    if (!elements.length) {
      return;
    }

    gsap.fromTo(
      elements,
      { y: 8, autoAlpha: 0 },
      {
        y: 0,
        autoAlpha: 1,
        duration: 0.38,
        stagger: 0.045,
        overwrite: true,
        clearProps: "transform"
      }
    );
  }

  function animateAssistantGroup() {
    const group = document.querySelector(".assistant-job-group:not([hidden])");
    if (!group) {
      return;
    }

    const rows = group.querySelectorAll(".assistant-date-bucket, label");
    if (!rows.length) {
      return;
    }

    gsap.fromTo(
      rows,
      { y: 6, autoAlpha: 0 },
      {
        y: 0,
        autoAlpha: 1,
        duration: 0.3,
        stagger: 0.025,
        overwrite: true,
        clearProps: "transform"
      }
    );
  }

  function animateChatMessage(message) {
    if (!message) {
      return;
    }

    gsap.fromTo(
      message,
      { y: 8, autoAlpha: 0 },
      {
        y: 0,
        autoAlpha: 1,
        duration: 0.34,
        ease: "power2.out",
        clearProps: "transform"
      }
    );
  }

  function setupDynamicContent() {
    document.addEventListener("career:motion-refresh", function (event) {
      animateFreshContent(event.detail && event.detail.element);
    });

    const chatLog = document.getElementById("chat-log");
    if (chatLog) {
      const observer = new MutationObserver(function (mutations) {
        mutations.forEach(function (mutation) {
          mutation.addedNodes.forEach(function (node) {
            if (!(node instanceof Element)) {
              return;
            }
            if (node.matches(".msg")) {
              animateChatMessage(node);
            } else {
              node.querySelectorAll(".msg").forEach(animateChatMessage);
            }
          });
        });
      });
      observer.observe(chatLog, { childList: true });
    }
  }

  function setupFormFeedback() {
    document.addEventListener(
      "submit",
      function (event) {
        const form = event.target;
        if (!(form instanceof HTMLFormElement)) {
          return;
        }

        requestAnimationFrame(function () {
          if (event.defaultPrevented || !form.isConnected) {
            return;
          }
          form.classList.add("is-submitting");
          const submit = form.querySelector('button[type="submit"], button:not([type])');
          if (submit) {
            gsap.to(submit, {
              scale: 0.985,
              duration: 0.12,
              ease: "power1.out"
            });
          }
        });
      },
      true
    );
  }

  window.CareerMotion = Object.freeze({
    refresh: animateFreshContent,
    revealAssistantGroup: animateAssistantGroup,
    revealChatMessage: animateChatMessage
  });

  gsap.context(function () {
    if (document.body.classList.contains("auth-body")) {
      animateAuthPage();
    } else {
      animateAppPage();
    }
    gsap.fromTo(
      ".notice",
      { x: -6 },
      { x: 0, duration: 0.36, ease: "power2.out", stagger: 0.04 }
    );
    setupCardMotion();
    setupButtonMotion();
    setupDynamicContent();
    setupFormFeedback();
  });

  function refreshTriggers() {
    if (ScrollTrigger) {
      ScrollTrigger.refresh();
    }
  }

  window.addEventListener("load", refreshTriggers, { once: true });
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(refreshTriggers);
  }
})();
