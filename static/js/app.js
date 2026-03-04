function toggleButtonLoading(btn, isLoading) {
    if (!btn) return;
    if (isLoading) {
        if (!btn.dataset.loaderActive) {
            btn.dataset.loaderActive = "1";
            btn.dataset.wasDisabled = btn.disabled ? "true" : "false";
        }
        if ("disabled" in btn) {
            btn.disabled = true;
        }
        btn.classList.add("is-loading");
        if (!btn.querySelector(".btn-loader")) {
            const loader = document.createElement("span");
            loader.className = "btn-loader";
            loader.setAttribute("aria-hidden", "true");
            btn.appendChild(loader);
        }
    } else {
        if (btn.dataset.wasDisabled !== "true" && "disabled" in btn) {
            btn.disabled = false;
        }
        btn.classList.remove("is-loading");
        const loader = btn.querySelector(".btn-loader");
        if (loader) loader.remove();
        delete btn.dataset.loaderActive;
        delete btn.dataset.wasDisabled;
    }
}

window.toggleButtonLoading = toggleButtonLoading;
function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function renderCartItems(cart) {
    const cartContent = document.querySelector('.cart-content');
    if (!cartContent) return;
    if (!cart || !cart.length) {
        cartContent.innerHTML = '<div class="cart-item empty-cart"><p>Your cart is empty.</p></div>';
        return;
    }
    const buildImageSrc = (item) => {
        if (item && item.image_url) return item.image_url;
        if (item && item.image) {
            const cleaned = String(item.image).replace(/^\/+/, "");
            return `/static/${cleaned}`;
        }
        return "https://via.placeholder.com/80?text=Item";
    };
    const html = cart.map((item) => {
        const id = escapeHtml(item.id ?? "");
        const name = escapeHtml(item.name || item.title || "Item");
        const priceVal = typeof item.price === "number" ? item.price : parseFloat(item.price || 0);
        const qty = typeof item.quantity === "number" ? item.quantity : parseInt(item.quantity || 1, 10) || 1;
        const imageSrc = buildImageSrc(item);
        return `
        <div class="cart-item">
          <img src="${imageSrc}" alt="${name}">
          <div>
            <h4>${name}</h4>
            <h5>R${isNaN(priceVal) ? "0.00" : priceVal.toFixed(2)}</h5>
            <form method="POST" action="/remove-from-cart">
              <input type="hidden" name="id" value="${id}">
              <button class="remove-item" type="submit">remove</button>
            </form>
          </div>
          <div>
            <form method="POST" action="/increase_cart">
              <input type="hidden" name="id" value="${id}">
              <button class="fas fa-chevron-up inc-btn" data-id="${id}" type="submit"></button>
            </form>
            <p class="item-amount" id="qty-${id}">${qty}</p>
            <form method="POST" action="/decrease_cart">
              <input type="hidden" name="id" value="${id}">
              <button class="fas fa-chevron-down dec-btn" data-id="${id}" type="submit"></button>
            </form>
          </div>
        </div>
        `;
    }).join("");
    cartContent.innerHTML = html;
}

window.renderCartItems = renderCartItems;

document.addEventListener("DOMContentLoaded", function () {
    const cartOverlay = document.querySelector(".cart-overlay");
    const cart = document.querySelector(".cart");
    const closeCartBtn = document.querySelector(".close-cart");
    const cartBtn = document.querySelector(".cart-btn");

    if (cartOverlay && cart && closeCartBtn && cartBtn) {
        const openCart = () => {
            cart.classList.add("showCart");
            cartOverlay.classList.add("transparentBcg");
        };
        const closeCart = () => {
            cart.classList.remove("showCart");
            cartOverlay.classList.remove("transparentBcg");
        };
        cartBtn.addEventListener("click", openCart);
        closeCartBtn.addEventListener("click", closeCart);
        cartOverlay.addEventListener("click", (e) => {
            if (e.target === cartOverlay) {
                closeCart();
            }
        });
    }

    const userOverlay = document.querySelector(".user-overlay");
    const userPanel = document.querySelector(".user-panel");
    const userBtn = document.querySelector(".user-btn");
    const closeUserBtn = document.querySelector(".close-user");
    if (userOverlay && userPanel && userBtn && closeUserBtn) {
        const openUser = () => {
            userOverlay.classList.add("show");
        };
        const closeUser = () => {
            userOverlay.classList.remove("show");
        };
        userBtn.addEventListener("click", openUser);
        closeUserBtn.addEventListener("click", closeUser);
        userOverlay.addEventListener("click", (e) => {
            if (e.target === userOverlay) {
                closeUser();
            }
        });
    }
});


document.addEventListener("DOMContentLoaded", () => {
    const buttons = document.querySelectorAll(".add-to-cart");

    buttons.forEach(btn => {
        btn.addEventListener("click", async () => {
            const product = {
                id: btn.dataset.id,
                title: btn.dataset.title,
                price: btn.dataset.price,
                image: btn.dataset.image,
            };
            toggleButtonLoading(btn, true);
            try {
                const res = await fetch("/add-to-cart", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(product),
                });
                const data = await res.json();
                const navCount = document.querySelector(".cart-items");
                if (navCount && typeof data.items_count !== "undefined") {
                    navCount.textContent = data.items_count;
                }
                const totalEl = document.querySelector(".cart-total");
                if (totalEl && typeof data.cart_total !== "undefined") {
                    totalEl.textContent = Number(data.cart_total).toFixed(2);
                }
                if (Array.isArray(data.cart)) {
                    renderCartItems(data.cart);
                }
                alert((data && data.message) || (product.title + " added to cart!"));
            } catch (err) {
                console.error(err);
                alert("Unable to add item to cart right now.");
            } finally {
                toggleButtonLoading(btn, false);
            }
        });
    });
});

// Handle add-to-cart forms without page refresh
document.addEventListener("DOMContentLoaded", function () {
    const forms = Array.from(document.querySelectorAll("form")).filter(form => {
        const action = (form.getAttribute("action") || "").toLowerCase();
        return action.includes("/add-to-cart");
    });
    if (!forms.length) return;

    forms.forEach(form => {
        form.addEventListener("submit", async function (e) {
            e.preventDefault();
            const submitBtn = form.querySelector('[type="submit"]');
            toggleButtonLoading(submitBtn, true);

            const formData = new FormData(form);
            const productId = formData.get("id");
            if (!productId) {
                toggleButtonLoading(submitBtn, false);
                form.submit();
                return;
            }
            const payload = { id: productId };
            const quantity = formData.get("quantity");
            if (quantity) {
                payload.quantity = quantity;
            }
            const actionUrl = form.getAttribute("action") || "/add-to-cart";

            try {
                const res = await fetch(actionUrl, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "Accept": "application/json"
                    },
                    credentials: "same-origin",
                    body: JSON.stringify(payload)
                });
                let data = null;
                try {
                    data = await res.json();
                } catch (err) {
                    console.error("Could not parse cart response", err);
                }
                if (!data || data.ok === false || !res.ok) {
                    const errorMsg = (data && (data.error || data.message)) || "Unable to add to cart.";
                    alert(errorMsg);
                    return;
                }
                const navCount = document.querySelector(".cart-items");
                if (navCount && typeof data.items_count !== "undefined") {
                    navCount.textContent = data.items_count;
                }
                const totalEl = document.querySelector(".cart-total");
                if (totalEl && typeof data.cart_total !== "undefined") {
                    totalEl.textContent = Number(data.cart_total).toFixed(2);
                }
                const itemName = formData.get("title") || "Item";
                if (Array.isArray(data.cart)) {
                    renderCartItems(data.cart);
                }
                alert(data.message || `${itemName} added to cart!`);
            } catch (err) {
                console.error("Add to cart failed", err);
                alert("Something went wrong while adding to the cart.");
            } finally {
                toggleButtonLoading(submitBtn, false);
            }
        });
    });
});

// Quantity controls: prevent page refresh and update via fetch
document.addEventListener("DOMContentLoaded", function () {
  const cartContent = document.querySelector('.cart-content');
  if (!cartContent) return;

  cartContent.addEventListener('click', async function (e) {
    const incBtn = e.target.closest('button.inc-btn');
    const decBtn = e.target.closest('button.dec-btn');
    if (!incBtn && !decBtn) return;

    e.preventDefault();
    const triggerBtn = incBtn || decBtn;
    const id = triggerBtn.dataset.id;
    const endpoint = incBtn ? '/increase_cart' : '/decrease_cart';

    toggleButtonLoading(triggerBtn, true);
    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id })
      });
      const data = await res.json();
      if (!data || !data.ok) return;

      // Update quantity text
      const qtyEl = document.getElementById('qty-' + id);
      if (qtyEl) qtyEl.textContent = data.new_quantity;

      // Update cart total in footer
      const totalEl = document.querySelector('.cart-total');
      if (totalEl) totalEl.textContent = Number(data.cart_total).toFixed(2);

      // Update navbar cart items count
      const navCount = document.querySelector('.cart-items');
      if (navCount) navCount.textContent = data.items_count;

      // If item removed (quantity 0), remove its nodes from cart
      if (data.removed) {
        const controlsDiv = (incBtn || decBtn).closest('div');
        const detailsDiv = controlsDiv ? controlsDiv.previousElementSibling : null;
        const imgEl = detailsDiv ? detailsDiv.previousElementSibling : null;
        if (controlsDiv) controlsDiv.remove();
        if (detailsDiv) detailsDiv.remove();
        if (imgEl) imgEl.remove();
      }
    } catch (err) {
      console.error('Cart update failed', err);
    } finally {
      toggleButtonLoading(triggerBtn, false);
    }
  });
});

document.addEventListener("DOMContentLoaded", function () {
  enhanceForgotPasswordForm();
  enhanceResetPasswordForm();
  initFlashMessages();
});

function enhanceForgotPasswordForm() {
  const form = document.querySelector('.forgot-password-form');
  if (!form || !window.fetch) return;
  const feedback = form.querySelector('[data-forgot-feedback]');
  const submitBtn = form.querySelector('button[type="submit"]');

  form.addEventListener('submit', async function (e) {
    e.preventDefault();
    const emailInput = form.querySelector('input[name="email"]');
    const emailVal = emailInput ? emailInput.value.trim() : '';
    if (!emailVal) {
      updateFormFeedback(feedback, 'Please enter your email address.', { isError: true });
      return;
    }
    toggleButtonLoading(submitBtn, true);
    updateFormFeedback(feedback, 'Sending reset link...');
    const action = form.getAttribute('action') || '/forgotPassword';

    try {
      const res = await fetch(action, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json'
        },
        credentials: 'same-origin',
        body: JSON.stringify({ email: emailVal })
      });
      let data = null;
      try {
        data = await res.json();
      } catch (err) {
        data = null;
      }
      if (!res.ok || !data || data.ok === false) {
        const msg = data && data.error ? data.error : 'Unable to send reset link.';
        updateFormFeedback(feedback, msg, { isError: true });
        return;
      }
      const linkText = data.dev_reset_url ? 'Open reset link (dev)' : undefined;
      updateFormFeedback(feedback, data.message || 'Check your inbox for the reset link.', {
        linkUrl: data.dev_reset_url,
        linkText
      });
      if (form.reset) form.reset();
    } catch (err) {
      console.error('Forgot password request failed', err);
      updateFormFeedback(feedback, 'Something went wrong. Please try again.', { isError: true });
    } finally {
      toggleButtonLoading(submitBtn, false);
    }
  });
}

function enhanceResetPasswordForm() {
  const form = document.querySelector('.reset-password-form');
  if (!form || !window.fetch) return;
  const feedback = form.querySelector('[data-reset-feedback]');
  const submitBtn = form.querySelector('button[type="submit"]');

  form.addEventListener('submit', async function (e) {
    e.preventDefault();
    const passwordInput = form.querySelector('input[name="password"]');
    const confirmInput = form.querySelector('input[name="confirm_password"]');
    const password = passwordInput ? passwordInput.value : '';
    const confirm = confirmInput ? confirmInput.value : '';

    if (password.length < 6) {
      updateFormFeedback(feedback, 'New password must be at least 6 characters.', { isError: true });
      return;
    }
    if (password !== confirm) {
      updateFormFeedback(feedback, 'New passwords do not match.', { isError: true });
      return;
    }

    toggleButtonLoading(submitBtn, true);
    updateFormFeedback(feedback, 'Updating password...');
    const action = form.getAttribute('action') || window.location.pathname;

    try {
      const res = await fetch(action, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json'
        },
        credentials: 'same-origin',
        body: JSON.stringify({ password, confirm_password: confirm })
      });
      let data = null;
      try {
        data = await res.json();
      } catch (err) {
        data = null;
      }
      if (!res.ok || !data || data.ok === false) {
        const msg = data && data.error ? data.error : 'Unable to update password.';
        updateFormFeedback(feedback, msg, { isError: true });
        return;
      }
      updateFormFeedback(feedback, data.message || 'Password updated. Redirecting...');
      const redirectUrl = data.redirect_to || '/login';
      setTimeout(() => {
        window.location.href = redirectUrl;
      }, 1400);
    } catch (err) {
      console.error('Reset password failed', err);
      updateFormFeedback(feedback, 'Something went wrong. Please try again.', { isError: true });
    } finally {
      toggleButtonLoading(submitBtn, false);
    }
  });
}

function updateFormFeedback(el, message, opts = {}) {
  if (!el) return;
  const { isError = false, linkUrl = null, linkText = 'Open link' } = opts;
  el.innerHTML = '';
  el.classList.toggle('error', Boolean(isError));
  if (!message && !linkUrl) {
    el.classList.remove('visible');
    return;
  }
  el.classList.add('visible');
  if (message) {
    const textSpan = document.createElement('span');
    textSpan.textContent = message;
    el.appendChild(textSpan);
  }
  if (linkUrl) {
    if (message) {
      el.appendChild(document.createTextNode(' '));
    }
    const link = document.createElement('a');
    link.href = linkUrl;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = linkText || 'Open link';
    el.appendChild(link);
  }
}

function initFlashMessages() {
  const flashes = document.querySelectorAll('[data-flash-message]');
  if (!flashes.length) return;
  flashes.forEach((flash) => {
    const timeoutAttr = flash.getAttribute('data-flash-timeout');
    const timeout = Number(timeoutAttr) || 10000;
    setTimeout(() => {
      flash.classList.add('flash-hide');
      setTimeout(() => {
        if (flash && flash.parentElement) {
          flash.remove();
        }
      }, 400);
    }, timeout);
  });
}
