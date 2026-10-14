(() => {
  const form = document.getElementById("login-form");
  const errorBox = document.getElementById("login-error");
  const submit = document.getElementById("login-submit");
  const codeBlock = document.getElementById("code-block");
  const codeInput = document.getElementById("code");
  const back = document.getElementById("code-back");
  let stage = "password";

  // Where to go afterwards: only a path on this site (never another host).
  const params = new URLSearchParams(location.search);
  let next = params.get("next") || "/";
  if (!next.startsWith("/") || next.startsWith("//") || next.includes("\\")) next = "/";

  const fail = (text) => { errorBox.textContent = text; errorBox.hidden = false; };
  if (params.get("demo") === "expired") fail("That demo link has expired or been withdrawn. Ask for a new one.");

  function showCodeStage() {
    stage = "code";
    document.getElementById("password-block").hidden = true;
    codeBlock.hidden = false;
    submit.textContent = "Verify";
    errorBox.hidden = true;
    codeInput.value = "";
    codeInput.focus();
  }

  function showPasswordStage() {
    stage = "password";
    document.getElementById("password-block").hidden = false;
    codeBlock.hidden = true;
    submit.textContent = "Sign in";
    errorBox.hidden = true;
    form.password.value = "";
    form.password.focus();
  }

  back.addEventListener("click", showPasswordStage);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorBox.hidden = true;
    submit.disabled = true;
    try {
      const isCode = stage === "code";
      const res = await fetch(isCode ? "/api/auth/login/totp" : "/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(isCode ? { code: codeInput.value } : { email: form.email.value, password: form.password.value }),
      });
      let data = {};
      try { data = await res.json(); } catch (e) { /* no body */ }
      if (res.ok && data.needs_totp) { showCodeStage(); return; }
      if (res.ok) { location.replace(next); return; }
      fail(data.detail || "Sign-in failed.");
      if (isCode && /timed out/i.test(data.detail || "")) { showPasswordStage(); fail(data.detail); }
      else if (isCode) { codeInput.value = ""; codeInput.focus(); }
      else { form.password.value = ""; form.password.focus(); }
    } catch (e) {
      fail("Could not reach the server. Check your connection and try again.");
    } finally {
      submit.disabled = false;
    }
  });
})();
