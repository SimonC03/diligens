// Runs before the page is painted, so a chosen theme never flashes the other one first.
(function () {
  "use strict";
  try {
    var choice = new URLSearchParams(window.location.search).get("tema") ||
      window.localStorage.getItem("tema");
    if (choice === "light" || choice === "dark") {
      document.documentElement.dataset.theme = choice;
    }
  } catch (e) {
    // no storage: the system theme applies
  }
})();
