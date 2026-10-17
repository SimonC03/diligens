// Diligens: small progressive enhancements. Every figure shown comes from the server.

(function () {
  "use strict";

  function $(selector, root) {
    return (root || document).querySelector(selector);
  }

  function $$(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }

  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) {
          throw new Error(data.error || data.detail || "Förfrågan misslyckades.");
        }
        return data;
      });
    });
  }

  // ------------------------------------------------------------ job progress

  function pollJob(root) {
    var url = root.getAttribute("data-job");
    var steps = $$(".step", root);
    var clock = $("[data-elapsed]", root);
    var failure = $("[data-failure]", root);

    function render(job) {
      steps.forEach(function (step, index) {
        step.classList.toggle("done", index < job.current);
        step.classList.toggle("active", index === job.current && job.status === "körs");
        step.classList.toggle("failed", index === job.current && job.status === "fel");
        var detail = $(".step-detail", step);
        if (detail) {
          detail.textContent = index === job.current ? job.detail : "";
        }
      });
      if (clock) {
        clock.textContent = job.seconds.toFixed(1).replace(".", ",") + " s";
      }
    }

    function fail(message) {
      if (failure) {
        $("[data-failure-text]", failure).textContent = message;
        failure.hidden = false;
      }
    }

    function tick() {
      fetch(url)
        .then(function (response) {
          if (response.status === 404) {
            throw new Error("gone");
          }
          return response.json();
        })
        .then(function (job) {
          render(job);
          if (job.status === "klar") {
            window.location.assign(job.target);
          } else if (job.status === "fel") {
            fail(job.error);
          } else {
            window.setTimeout(tick, 600);
          }
        })
        .catch(function (error) {
          if (error.message === "gone") {
            fail("Körningen finns inte längre, till exempel efter en omstart av appen. Starta den igen.");
          } else {
            window.setTimeout(tick, 1500);  // the app may be busy; keep asking
          }
        });
    }

    tick();
  }

  // --------------------------------------------------------------- dropzone

  function dropzone(zone) {
    var input = $("input[type=file]", zone);
    var name = $(".dropzone-file", zone);

    function show() {
      if (input.files && input.files.length) {
        name.textContent = input.files[0].name;
        name.hidden = false;
      }
    }

    input.addEventListener("change", show);
    ["dragenter", "dragover"].forEach(function (type) {
      zone.addEventListener(type, function (event) {
        event.preventDefault();
        zone.classList.add("over");
      });
    });
    ["dragleave", "drop"].forEach(function (type) {
      zone.addEventListener(type, function () {
        zone.classList.remove("over");
      });
    });
    zone.addEventListener("drop", function (event) {
      event.preventDefault();
      if (event.dataTransfer && event.dataTransfer.files.length) {
        input.files = event.dataTransfer.files;
        show();
      }
    });
  }

  // ----------------------------------------------------------- bridge choice

  function bridge(table) {
    var url = table.getAttribute("data-bridge");
    var boxes = $$("input[data-include]", table);
    var notice = $("[data-memo-stale]");

    var saved = boxes.map(function (box) {
      return box.checked;
    });

    function update() {
      boxes.forEach(function (box) {
        box.disabled = true;
      });
      postJson(url, {
        inkludera: boxes.map(function (box) {
          return box.checked;
        }),
      })
        .then(function (data) {
          $("[data-total=ebitda]").textContent = data.justerad_ebitda;
          $("[data-total=revenue]").textContent = data.justerad_omsattning;
          $("[data-total=margin]").textContent = data.marginal;
          if (notice) {
            notice.hidden = data.memo_aktuellt;
          }
          saved = boxes.map(function (box) {
            box.closest("tr").classList.toggle("subtle", !box.checked);
            return box.checked;
          });
        })
        .catch(function (error) {
          // Nothing was saved: show the choice that is saved, not the one that failed.
          boxes.forEach(function (box, index) {
            box.checked = saved[index];
          });
          window.alert(error.message);
        })
        .finally(function () {
          boxes.forEach(function (box) {
            box.disabled = false;
          });
        });
    }

    boxes.forEach(function (box) {
      box.addEventListener("change", update);
    });
  }

  // ----------------------------------------------------------------- drawer

  var drawer = $("[data-drawer]");
  var backdrop = $("[data-drawer-backdrop]");

  function closeDrawer() {
    if (drawer) {
      drawer.hidden = true;
      backdrop.hidden = true;
    }
  }

  function openDrawer(url) {
    if (!drawer) {
      window.location.assign(url);
      return;
    }
    $("[data-drawer-body]", drawer).textContent = "Hämtar verifikatet …";
    drawer.hidden = false;
    backdrop.hidden = false;
    $("[data-drawer-link]", drawer).setAttribute("href", url);
    fetch(url + (url.indexOf("?") < 0 ? "?" : "&") + "del=1")
      .then(function (response) {
        if (!response.ok) {
          throw new Error("Verifikatet kunde inte visas.");
        }
        return response.text();
      })
      .then(function (html) {
        // Server rendered and escaped fragment of this app.
        $("[data-drawer-body]", drawer).innerHTML = html;
      })
      .catch(function (error) {
        $("[data-drawer-body]", drawer).textContent =
          error.message === "Failed to fetch" ? "Appen svarar inte." : error.message;
      });
  }

  // ------------------------------------------------------------- SNI tokens

  function sniPicker(root) {
    var input = $("[data-sni-input]", root);
    var hidden = $("input[name=sni]", root);
    var tokens = $("[data-sni-tokens]", root);
    var options = {};
    $$("datalist option", root).forEach(function (option) {
      options[option.value.split(" ")[0]] = option.value;
    });
    var codes = hidden.value
      ? hidden.value.split(",").map(function (code) {
          return code.trim();
        })
      : [];

    function render() {
      hidden.value = codes.join(",");
      tokens.innerHTML = "";
      codes.forEach(function (code) {
        var token = document.createElement("span");
        token.className = "token";
        token.textContent = options[code] || code;
        var remove = document.createElement("button");
        remove.type = "button";
        remove.setAttribute("aria-label", "Ta bort " + code);
        remove.textContent = "×";
        remove.addEventListener("click", function () {
          codes = codes.filter(function (other) {
            return other !== code;
          });
          render();
        });
        token.appendChild(remove);
        tokens.appendChild(token);
      });
    }

    function add() {
      var code = input.value.trim().split(" ")[0];
      if (/^\d{5}$/.test(code) && codes.indexOf(code) < 0) {
        codes.push(code);
        input.value = "";
        render();
      }
    }

    input.addEventListener("change", add);
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        add();
      }
    });
    root.closest("form").addEventListener("submit", function () {
      add();
    });
    render();
  }

  // ------------------------------------------------------------------- init

  function themeChoice() {
    var stored = null;
    try {
      stored = window.localStorage.getItem("tema");
    } catch (e) {
      stored = null;
    }
    var buttons = $$("[data-theme-choice]");

    function show(choice) {
      buttons.forEach(function (button) {
        button.setAttribute(
          "aria-pressed",
          String(button.getAttribute("data-theme-choice") === choice)
        );
      });
    }

    show(stored === "light" || stored === "dark" ? stored : "system");
    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        var choice = button.getAttribute("data-theme-choice");
        try {
          if (choice === "system") {
            window.localStorage.removeItem("tema");
          } else {
            window.localStorage.setItem("tema", choice);
          }
        } catch (e) {
          // the choice then lasts for this page only
        }
        if (choice === "system") {
          delete document.documentElement.dataset.theme;
        } else {
          document.documentElement.dataset.theme = choice;
        }
        show(choice);
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    themeChoice();
    $$("[data-job]").forEach(pollJob);
    $$(".dropzone").forEach(dropzone);
    $$("[data-bridge]").forEach(bridge);
    $$("[data-sni]").forEach(sniPicker);

    $$("[data-voucher]").forEach(function (row) {
      row.addEventListener("click", function (event) {
        if (event.target.closest("a, button, input")) {
          return;
        }
        openDrawer(row.getAttribute("data-voucher"));
      });
    });

    $$("[data-href]").forEach(function (row) {
      row.addEventListener("click", function (event) {
        if (!event.target.closest("a, button, input")) {
          window.location.assign(row.getAttribute("data-href"));
        }
      });
    });

    $$("[data-drawer-close]").forEach(function (button) {
      button.addEventListener("click", closeDrawer);
    });
    if (backdrop) {
      backdrop.addEventListener("click", closeDrawer);
    }
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        closeDrawer();
      }
    });

    $$("[data-copy]").forEach(function (button) {
      button.addEventListener("click", function () {
        var source = $(button.getAttribute("data-copy"));
        var text = source.getAttribute("data-text") || source.innerText;
        navigator.clipboard.writeText(text).then(function () {
          var label = button.textContent;
          button.textContent = "Kopierat";
          window.setTimeout(function () {
            button.textContent = label;
          }, 1600);
        });
      });
    });

    $$("[data-test]").forEach(function (button) {
      button.addEventListener("click", function () {
        var out = $("[data-test-result=" + button.getAttribute("data-test") + "]");
        button.setAttribute("aria-busy", "true");
        out.textContent = "Testar …";
        postJson("/api/test/" + button.getAttribute("data-test"))
          .then(function (data) {
            out.textContent = data.message;
            out.className = "badge " + (data.ok ? "pos" : "neg");
          })
          .catch(function (error) {
            out.textContent = error.message;
            out.className = "badge neg";
          })
          .finally(function () {
            button.removeAttribute("aria-busy");
          });
      });
    });

    $$("form[data-busy]").forEach(function (form) {
      form.addEventListener("submit", function () {
        var button = $("button[type=submit]", form);
        if (button) {
          button.setAttribute("aria-busy", "true");
          button.textContent = form.getAttribute("data-busy");
        }
      });
    });

    $$("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (!window.confirm(form.getAttribute("data-confirm"))) {
          event.preventDefault();
        }
      });
    });
  });
})();
