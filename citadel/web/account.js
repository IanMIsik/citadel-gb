(() => {
  const $ = (id) => document.getElementById(id);
  let me = null;

  async function api(path, body, method = "POST") {
    const res = await fetch(path, {
      method, credentials: "same-origin",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (res.status === 401) { location.replace("/login?next=/account"); throw new Error("signed out"); }
    let data = {};
    try { data = await res.json(); } catch (e) { /* no body */ }
    return { ok: res.ok, status: res.status, data };
  }

  function show(el, text, kind) {
    el.textContent = text;
    el.className = "msg " + kind;
    el.hidden = false;
  }

  function render() {
    $("who").textContent = `${me.email} · ${me.role}`;
    const needed = me.setup_needed || [];
    const notice = $("setup-notice");
    if (needed.length) {
      const parts = [];
      if (needed.includes("change_password")) parts.push("choose a new password");
      if (needed.includes("setup_2fa")) parts.push("turn on two-factor authentication");
      notice.textContent = "Before you can use Citadel you need to " + parts.join(" and ") + ".";
      notice.hidden = false;
      $("back").hidden = true;
    } else {
      notice.hidden = true;
      $("back").hidden = false;
    }
    const on = !!me.totp_enabled;
    $("twofa-status").textContent = on
      ? "On. You will be asked for a code from your authenticator app when you sign in."
      : (me.twofa_required ? "Required for your role, and not set up yet." : "Off.");
    $("twofa-off").hidden = on || !$("twofa-setup").hidden;
    $("twofa-on").hidden = !on;
    $("twofa-disable-block").hidden = !on || me.twofa_required;
    $("codes-left").textContent = me.recovery_codes_left == null ? "-" : String(me.recovery_codes_left);
  }

  async function load() {
    const res = await api("/api/auth/me", undefined, "GET");
    me = res.data;
    if (!me.auth_enabled) { document.body.textContent = "Sign-in is not switched on for this server."; return; }
    render();
  }

  $("signout").addEventListener("click", async () => {
    try { await api("/api/auth/logout", {}); } catch (e) { /* leave anyway */ }
    location.replace("/login");
  });

  $("password-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const msg = $("password-msg");
    msg.hidden = true;
    if ($("new-password").value !== $("confirm-password").value) { show(msg, "The two new passwords differ.", "error"); return; }
    const res = await api("/api/auth/change-password", {
      current_password: $("current-password").value, new_password: $("new-password").value });
    if (!res.ok) { show(msg, res.data.detail || "Could not change the password.", "error"); return; }
    $("password-form").reset();
    show(msg, res.data.other_sessions_ended ? `Password changed. ${res.data.other_sessions_ended} other session(s) signed out.` : "Password changed.", "ok");
    await load();
  });

  $("twofa-start").addEventListener("click", async () => {
    const msg = $("twofa-msg");
    msg.hidden = true;
    const res = await api("/api/auth/2fa/setup", {});
    if (!res.ok) { show(msg, res.data.detail || "Could not start setup.", "error"); return; }
    $("twofa-qr").src = res.data.qr;
    $("twofa-secret").textContent = res.data.secret.match(/.{1,4}/g).join(" ");
    $("twofa-setup").hidden = false;
    $("twofa-off").hidden = true;
    $("twofa-code").focus();
  });

  $("twofa-confirm").addEventListener("click", async () => {
    const msg = $("twofa-msg");
    msg.hidden = true;
    const res = await api("/api/auth/2fa/enable", { code: $("twofa-code").value });
    if (!res.ok) { show(msg, res.data.detail || "That code is not right.", "error"); return; }
    $("twofa-setup").hidden = true;
    $("twofa-code").value = "";
    $("twofa-codes-list").textContent = res.data.recovery_codes.join("\n");
    $("twofa-codes").hidden = false;
    $("codes-left").textContent = String(res.data.recovery_codes.length);
  });

  $("twofa-codes-done").addEventListener("click", async () => {
    $("twofa-codes").hidden = true;
    $("twofa-codes-list").textContent = "";
    await load();
  });

  $("regen-codes").addEventListener("click", async () => {
    const msg = $("twofa-msg");
    const res = await api("/api/auth/2fa/recovery-codes", { password: $("regen-password").value });
    if (!res.ok) { show(msg, res.data.detail || "Could not make new codes.", "error"); return; }
    $("regen-password").value = "";
    $("twofa-codes-list").textContent = res.data.recovery_codes.join("\n");
    $("twofa-codes").hidden = false;
    msg.hidden = true;
  });

  $("twofa-disable").addEventListener("click", async () => {
    const msg = $("twofa-msg");
    const res = await api("/api/auth/2fa/disable", { password: $("disable-password").value, code: $("disable-code").value });
    if (!res.ok) { show(msg, res.data.detail || "Could not turn it off.", "error"); return; }
    $("disable-password").value = $("disable-code").value = "";
    show(msg, "Two-factor is off.", "ok");
    await load();
  });

  load();
})();
