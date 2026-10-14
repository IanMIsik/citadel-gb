// Page-wide sign-in plumbing, loaded by every dashboard page:
//  - a request that comes back 401 (the session ended) sends the person to the sign-in page, instead of
//    leaving a tab quietly showing stale data;
//  - when login is on, the header shows who is signed in, links to Account (and Users for admins), and Sign out;
//  - a demo session shows a read-only "Demo" label.
(() => {
  const realFetch = window.fetch.bind(window);
  let leaving = false;
  window.fetch = async (...args) => {
    const res = await realFetch(...args);
    if (res.status === 401 && !leaving) {
      let url = args[0];
      if (url && url.url) url = url.url;
      const sameOrigin = typeof url === "string" && (url.startsWith("/") || url.startsWith(location.origin));
      if (sameOrigin && !String(url).includes("/api/auth/")) {
        leaving = true;
        location.replace("/login?next=" + encodeURIComponent(location.pathname + location.search));
      }
    }
    return res;
  };

  (async () => {
    let me;
    try {
      const res = await realFetch("/api/auth/me", { credentials: "same-origin" });
      if (!res.ok) return;
      me = await res.json();
    } catch (e) { return; }
    if (!me.auth_enabled || !me.email) return;

    const header = document.querySelector("header");
    if (!header) return;
    const box = document.createElement("span");
    box.className = "auth-box";
    if (me.role === "demo") {
      const demo = document.createElement("span");
      demo.className = "auth-demo";
      demo.textContent = "Demo · read-only";
      box.append(demo);
    } else {
      const who = document.createElement("a");
      who.className = "auth-who";
      who.href = "/account";
      who.textContent = me.email;
      who.title = "Your account: password and two-factor";
      box.append(who);
      if (me.role === "admin") {
        const admin = document.createElement("a");
        admin.className = "auth-who";
        admin.href = "/admin";
        admin.textContent = "Users";
        box.append(admin);
      }
    }
    const out = document.createElement("button");
    out.type = "button";
    out.className = "auth-out";
    out.textContent = "Sign out";
    out.addEventListener("click", async () => {
      try { await realFetch("/api/auth/logout", { method: "POST", credentials: "same-origin" }); } catch (e) { /* leave anyway */ }
      location.replace("/login");
    });
    box.append(out);
    header.append(box);
  })();
})();
