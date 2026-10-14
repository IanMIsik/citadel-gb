(() => {
  const $ = (id) => document.getElementById(id);
  const fmt = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : "–");

  async function api(path, body, method = "POST") {
    const res = await fetch(path, {
      method, credentials: "same-origin",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (res.status === 401) { location.replace("/login?next=/admin"); throw new Error("signed out"); }
    let data = {};
    try { data = await res.json(); } catch (e) { /* no body */ }
    return { ok: res.ok, data };
  }

  function el(tag, text, cls) {
    const e = document.createElement(tag);
    if (text !== undefined && text !== null) e.textContent = text;
    if (cls) e.className = cls;
    return e;
  }
  function cell(content, cls) {
    const td = el("td", typeof content === "string" ? content : null, cls);
    if (content instanceof Node) td.append(content);
    return td;
  }
  const tag = (text, kind) => el("span", text, "tag " + (kind || ""));
  function note(id, text, kind) { const m = $(id); m.textContent = text; m.className = "msg " + kind; m.hidden = false; }

  function reveal(title, value, hint) {
    $("reveal-title").textContent = title;
    $("reveal-value").textContent = value;
    $("reveal-hint").textContent = hint;
    $("reveal").hidden = false;
    $("reveal").scrollIntoView({ behavior: "smooth", block: "center" });
  }

  let me = null;

  async function loadUsers() {
    const { data } = await api("/api/admin/users", undefined, "GET");
    const role = $("u-role");
    if (!role.options.length) data.roles.forEach((r) => role.add(new Option(r, r)));
    const body = document.querySelector("#users tbody");
    body.replaceChildren();
    for (const u of data.users) {
      const tr = el("tr");
      const locked = u.locked_until && new Date(u.locked_until) > new Date();
      const expired = u.expires_at && new Date(u.expires_at) <= new Date();
      const state = !u.is_active ? tag("disabled", "bad") : expired ? tag("expired", "bad") : locked ? tag("locked", "warn")
        : u.must_change_password ? tag("must change password", "warn") : tag("active", "good");
      tr.append(cell(u.email, "mono"), cell(u.role), cell(state),
        cell(u.totp_enabled ? tag("on", "good") : data.twofa_required_roles.includes(u.role) ? tag("required, not set up", "warn") : tag("off")),
        cell(fmt(u.last_login_at)), cell(u.expires_at ? fmt(u.expires_at) : "–"));
      const actions = el("td", null, "actions");
      const act = (label, action, confirmText) => {
        const b = el("button", label);
        b.type = "button";
        b.addEventListener("click", async () => {
          if (confirmText && !confirm(confirmText)) return;
          const r = await api(`/api/admin/users/${action}`, { email: u.email });
          if (!r.ok) { alert(r.data.detail || "Failed."); return; }
          if (r.data.temporary_password) reveal(`New temporary password for ${u.email}`, r.data.temporary_password,
            "Shown once. Pass it on securely; they must choose their own when they sign in. Their other sessions were ended.");
          await Promise.all([loadUsers(), loadAudit()]);
        });
        actions.append(b);
      };
      if (u.role !== "demo") {
        act(u.is_active ? "Disable" : "Enable", u.is_active ? "disable" : "enable", u.is_active ? `Disable ${u.email}? They are signed out at once.` : null);
        act("Sign out", "logout");
        act("Reset password", "reset-password", `Reset the password for ${u.email}?`);
        if (u.totp_enabled) act("Reset 2FA", "reset-2fa", `Remove two-factor for ${u.email}? They will need to set it up again.`);
      }
      tr.append(actions);
      body.append(tr);
    }
  }

  async function loadLinks() {
    const { data } = await api("/api/admin/demo-links", undefined, "GET");
    const body = document.querySelector("#demo-links tbody");
    body.replaceChildren();
    for (const l of data.links) {
      const tr = el("tr");
      const live = !l.revoked_at && new Date(l.expires_at) > new Date();
      tr.append(cell(l.label), cell(fmt(l.created_at)),
        cell(live ? fmt(l.expires_at) : (l.revoked_at ? tag("revoked", "bad") : tag("expired", "bad"))),
        cell(String(l.use_count)), cell(fmt(l.last_used_at)));
      const actions = el("td", null, "actions");
      if (live) {
        const b = el("button", "Revoke");
        b.type = "button";
        b.addEventListener("click", async () => {
          if (!confirm(`Revoke the link for "${l.label}"? Anyone using it is signed out.`)) return;
          await api("/api/admin/demo-links/revoke", { id: l.id });
          await Promise.all([loadLinks(), loadAudit()]);
        });
        actions.append(b);
      }
      tr.append(actions);
      body.append(tr);
    }
  }

  async function loadAudit() {
    const { data } = await api("/api/admin/audit", undefined, "GET");
    const body = document.querySelector("#audit tbody");
    body.replaceChildren();
    for (const e of data.events) {
      const tr = el("tr");
      tr.append(cell(fmt(e.at)), cell(e.event, "mono"), cell(e.email || "–"), cell(e.ip || "–"), cell(e.detail || ""));
      body.append(tr);
    }
  }

  $("user-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const days = $("u-days").value;
    const r = await api("/api/admin/users", { email: $("u-email").value, role: $("u-role").value, expires_days: days ? Number(days) : null });
    if (!r.ok) { note("user-msg", r.data.detail || "Could not add the user.", "error"); return; }
    $("user-form").reset();
    $("user-msg").hidden = true;
    reveal(`Login created for ${r.data.email}`, r.data.temporary_password,
      "Shown once. Pass it on securely; they must choose their own password the first time they sign in.");
    await Promise.all([loadUsers(), loadAudit()]);
  });

  $("demo-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const r = await api("/api/admin/demo-links", { label: $("d-label").value, days: Number($("d-days").value) });
    if (!r.ok) { note("demo-msg", r.data.detail || "Could not create the link.", "error"); return; }
    $("demo-form").reset();
    $("d-days").value = "3";
    $("demo-msg").hidden = true;
    reveal("Demo link", r.data.url, `Shown once. Valid until ${fmt(r.data.expires_at)}; each visit signs in for a couple of hours, read-only.`);
    await Promise.all([loadLinks(), loadAudit()]);
  });

  (async () => {
    const m = await api("/api/auth/me", undefined, "GET");
    me = m.data;
    await Promise.all([loadUsers(), loadLinks(), loadAudit()]);
  })();
})();
