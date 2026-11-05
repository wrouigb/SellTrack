// Everything here is an enhancement. Every page works with JavaScript turned off.
//
// The Content Security Policy forbids eval, so Alpine uses its CSP build, where templates can only
// name things registered below. htmx's eval features are turned off in the page's htmx-config.

import Alpine from "@alpinejs/csp";
import "htmx.org";

// -- Money, in whole cents, so that previews can't suffer floating-point errors -----------------

/** "$1,234.50" becomes 123450. Returns null for anything that isn't plainly dollars and cents. */
export function toCents(text) {
  const cleaned = String(text ?? "").trim().replace(/^\$/, "");
  const match = /^(\d{1,3}(?:,\d{3})+|\d*)(?:\.(\d{0,2}))?$/.exec(cleaned);
  if (!match || (match[1] === "" && !match[2])) return null;
  const dollars = Number((match[1] || "0").replaceAll(",", ""));
  const cents = Number((match[2] ?? "").padEnd(2, "0"));
  return dollars * 100 + cents;
}

/** 123450 becomes "$1,234.50", and -300 becomes "−$3.00", the same as the server's format. */
export function formatCents(cents) {
  const whole = Math.trunc(Math.abs(cents) / 100).toLocaleString("en-AU");
  const part = String(Math.abs(cents) % 100).padStart(2, "0");
  return `${cents < 0 ? "−" : ""}$${whole}.${part}`;
}

function plainCents(cents) {
  return `${Math.trunc(cents / 100)}.${String(cents % 100).padStart(2, "0")}`;
}

// -- Components -----------------------------------------------------------------------------------

document.addEventListener("alpine:init", () => {
  // The sale form: fills in the price and shows the profit as you type.
  Alpine.data("saleForm", () => ({
    costEach: null,
    sellPrice: null,
    priceTyped: false,
    preview: "",
    isLoss: false,
    costLabel: "",

    init() {
      const { lockedCost } = this.$el.dataset;
      if (lockedCost) this.costEach = toCents(lockedCost);
      const price = this.$refs.price;
      this.priceTyped = Boolean(price?.value);
      this.readProduct();
      // The product may already be chosen, such as when adding a sale from a product's own page.
      this.autofill();
      this.refresh();
    },

    readProduct() {
      const select = this.$refs.product;
      if (!select || select.tagName !== "SELECT") return;
      const option = select.options[select.selectedIndex];
      this.costEach = option?.dataset.cost ? toCents(option.dataset.cost) : null;
      this.sellPrice = option?.dataset.price ? toCents(option.dataset.price) : null;
    },

    productChanged() {
      this.readProduct();
      this.autofill();
      this.refresh();
    },

    quantityChanged() {
      this.autofill();
      this.refresh();
    },

    priceChanged() {
      // Clearing the box hands it back to the auto-fill.
      this.priceTyped = this.$refs.price.value !== "";
      this.refresh();
    },

    quantity() {
      const value = this.$refs.quantity.value.trim();
      return /^\d+$/.test(value) ? Number(value) : null;
    },

    autofill() {
      if (this.priceTyped || this.sellPrice === null) return;
      const quantity = this.quantity();
      if (quantity !== null) this.$refs.price.value = plainCents(this.sellPrice * quantity);
    },

    refresh() {
      const quantity = this.quantity();
      const price = toCents(this.$refs.price.value);
      this.costLabel = this.costEach === null ? "" : formatCents(this.costEach);
      if (quantity === null || price === null || this.costEach === null) {
        this.preview = "";
        this.isLoss = false;
        return;
      }
      const profit = price - quantity * this.costEach;
      this.preview = formatCents(profit);
      this.isLoss = profit < 0;
    },

    get hasPreview() {
      return this.preview !== "";
    },

    get hasCost() {
      return this.costLabel !== "";
    },
  }));

  // Tick boxes for bulk actions, with a live count.
  Alpine.data("bulkSelect", () => ({
    count: 0,

    init() {
      this.recount();
    },

    boxes() {
      return [...this.$root.querySelectorAll('input[type="checkbox"][data-row]')];
    },

    recount() {
      this.count = this.boxes().filter((box) => box.checked).length;
    },

    toggleAll() {
      const on = this.$root.querySelector("[data-all]").checked;
      for (const box of this.boxes()) {
        box.checked = on;
      }
      this.recount();
    },

    get selected() {
      return this.count > 0;
    },

    get countLabel() {
      const noun = this.$root.dataset.noun ?? "sale";
      return `${this.count} ${noun}${this.count === 1 ? "" : "s"} selected`;
    },
  }));

  // <details> makes the menu work with no JavaScript. This adds closing on Escape or a click away.
  Alpine.data("menu", () => ({
    close() {
      if (this.$el.open) {
        this.$el.open = false;
        this.$el.querySelector("summary").focus();
      }
    },

    closeQuietly() {
      this.$el.open = false;
    },
  }));

  // A dismissible message.
  Alpine.data("toast", () => ({
    shown: true,

    dismiss() {
      this.shown = false;
    },
  }));

  // The running total while shares are being edited.
  Alpine.data("shareTotal", () => ({
    total: "",
    isExact: true,

    init() {
      this.refresh();
    },

    refresh() {
      const cents = [...this.$el.querySelectorAll("input[data-share]")].map((input) =>
        toCents(input.value),
      );
      if (cents.some((value) => value === null)) {
        this.total = "Check the shares";
        this.isExact = false;
        return;
      }
      const sum = cents.reduce((a, b) => a + b, 0);
      this.total = `${Math.trunc(sum / 100)}.${String(sum % 100).padStart(2, "0")}%`;
      this.isExact = sum === 10000;
    },

    get isOff() {
      return !this.isExact;
    },
  }));

  // Hover and arrow keys for a chart. Everything it shows is also in the table beneath the chart.
  Alpine.data("chart", () => ({
    active: -1,

    bands() {
      return [...this.$el.querySelectorAll(".chart-hit")];
    },

    point(event) {
      const band = event.target.closest(".chart-hit");
      if (band) this.show(this.bands().indexOf(band));
    },

    leave() {
      this.show(-1);
    },

    key(event) {
      const count = this.bands().length;
      if (event.key === "ArrowRight") {
        this.show(Math.min(this.active + 1, count - 1));
      } else if (event.key === "ArrowLeft") {
        this.show(this.active < 0 ? count - 1 : Math.max(this.active - 1, 0));
      } else if (event.key === "Escape") {
        this.show(-1);
      } else {
        return;
      }
      event.preventDefault();
    },

    show(index) {
      const bands = this.bands();
      bands.forEach((band, i) => band.classList.toggle("is-active", i === index));
      this.active = index;
      const tip = this.$refs.tip;
      if (index < 0) {
        tip.hidden = true;
        return;
      }
      const band = bands[index];
      this.$refs.tipLabel.textContent = band.dataset.label;
      this.$refs.tipMoney.textContent = band.dataset.money;
      this.$refs.tipProfit.textContent = band.dataset.profit;
      tip.hidden = false;
      const frame = this.$el.getBoundingClientRect();
      const box = band.getBoundingClientRect();
      const left = box.left - frame.left + box.width / 2 - tip.offsetWidth / 2;
      tip.style.left = `${Math.max(0, Math.min(left, frame.width - tip.offsetWidth))}px`;
      tip.style.top = "0";
    },
  }));

  // Shows the date boxes only when "Choose dates" is picked, and otherwise sends the choice straight
  // away. Picking "Choose dates" waits until the dates are filled in.
  Alpine.data("periodPicker", () => ({
    changed() {
      this.$refs.dates.hidden = this.$refs.period.value !== "custom";
    },
  }));

  // Suggests the browser's own time zone during onboarding.
  Alpine.data("timezonePicker", () => ({
    init() {
      if (this.$el.dataset.chosen === "true") return;
      const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      const select = this.$el.querySelector("select");
      if (zone && [...select.options].some((option) => option.value === zone)) {
        select.value = zone;
      }
    },
  }));
});

window.Alpine = Alpine;
Alpine.start();

// -- Confirmations --------------------------------------------------------------------------------
//
// <form data-confirm="Delete this sale?"> asks first, using a native dialog. The message goes in as
// text, never HTML, so a product name can't inject anything (the original app's inline confirm()
// could be exploited that way). Without JavaScript the form just submits, and everything it can do
// is either undoable or asks for a password.

const dialog = document.getElementById("confirm-dialog");

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;

  // A button can ask for its own confirmation, when several share one form.
  const asker = event.submitter?.dataset.confirm ? event.submitter : form;
  if (asker.dataset.confirm && dialog && form.dataset.confirmed !== "yes") {
    event.preventDefault();
    dialog.querySelector("[data-message]").textContent = asker.dataset.confirm;
    dialog.querySelector("[data-accept]").textContent = asker.dataset.confirmLabel || "Yes";
    dialog.onclose = () => {
      if (dialog.returnValue === "accept") {
        form.dataset.confirmed = "yes";
        form.requestSubmit(event.submitter ?? undefined);
      }
    };
    dialog.returnValue = "";
    dialog.showModal();
    return;
  }
  delete form.dataset.confirmed;

  // Stops a double click saving something twice. GET forms can be sent as often as you like.
  if (form.method === "post") {
    if (form.dataset.submitting) {
      event.preventDefault();
      return;
    }
    form.dataset.submitting = "yes";
  }
});

// Coming back with the browser's back button restores the page as it was, mid-submit.
window.addEventListener("pageshow", () => {
  for (const form of document.querySelectorAll("form[data-submitting]")) {
    delete form.dataset.submitting;
  }
});
