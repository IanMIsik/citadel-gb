(() => {
  const form = document.getElementById("login-form");
  const errorBox = document.getElementById("login-error");
  const submit = document.getElementById("login-submit");

  // Where to go afterwards: only a path on this site (never another host).
  const params = new URLSearchParams(location.search);
  let next = params.get("next") || "/";
  if (!next.startsWith("/") || next.startsWith("//") || next.includes("\\")) next = "/";

  const fail = (text) => { errorBox.textContent = text; errorBox.hidden = false; };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorBox.hidden = true;
    submit.disabled = true;
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ email: form.email.value, password: form.password.value }),
      });
      if (res.ok) { location.replace(next); return; }
      let detail = "Sign-in failed.";
      try { detail = (await res.json()).detail || detail; } catch (e) { /* keep the generic text */ }
      fail(detail);
      form.password.value = "";
      form.password.focus();
    } catch (e) {
      fail("Could not reach the server. Check your connection and try again.");
    } finally {
      submit.disabled = false;
    }
  });
})();
