// Shows who is signed in, with a Sign out link, in the page header -- only when login is switched on.
(async () => {
  let me;
  try {
    const res = await fetch("/api/auth/me", { credentials: "same-origin" });
    if (!res.ok) return;
    me = await res.json();
  } catch (e) { return; }
  if (!me.auth_enabled || !me.email) return;

  const header = document.querySelector("header");
  if (!header) return;
  const box = document.createElement("span");
  box.className = "auth-box";
  const who = document.createElement("span");
  who.className = "auth-who";
  who.textContent = me.email;
  const out = document.createElement("button");
  out.type = "button";
  out.className = "auth-out";
  out.textContent = "Sign out";
  out.addEventListener("click", async () => {
    try { await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" }); } catch (e) { /* leave anyway */ }
    location.replace("/login");
  });
  box.append(who, out);
  header.append(box);
})();
