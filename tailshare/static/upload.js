// アップロード用ページ。XHR で送るのは、fetch では送信の進み具合が取れないため
(() => {
  const form = document.getElementById("upload");
  const input = document.getElementById("file");
  const drop = document.getElementById("drop");
  const dropTitle = document.getElementById("drop-title");
  const dropSub = document.getElementById("drop-sub");
  const error = document.getElementById("error");
  const errorText = document.getElementById("error-text");
  const progress = document.getElementById("progress");
  const bar = document.getElementById("bar");
  const progressText = document.getElementById("progress-text");
  const send = document.getElementById("send");
  const cancel = document.getElementById("cancel");
  const done = document.getElementById("done");

  const maxBytes = Number(form.dataset.maxBytes);
  const maxLabel = form.dataset.maxLabel;
  let chosen = null;
  let xhr = null;

  const formatSize = (n) => {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return i === 0 ? `${n} B` : `${n.toFixed(1)} ${units[i]}`;
  };

  const showError = (message) => {
    errorText.textContent = message;
    error.hidden = false;
  };
  const clearError = () => { error.hidden = true; };

  const choose = (file) => {
    clearError();
    chosen = file || null;
    if (!chosen) return;
    drop.dataset.chosen = "";
    dropTitle.textContent = chosen.name;
    dropSub.textContent = `${formatSize(chosen.size)}・クリックして選び直す`;
    if (chosen.size > maxBytes) showError(`ファイルが大きすぎます。${maxLabel} 以下にしてください。`);
  };

  const setBusy = (busy) => {
    send.setAttribute("aria-busy", String(busy));
    send.hidden = busy;
    cancel.hidden = !busy;
    progress.hidden = !busy;
    input.disabled = busy;
    drop.setAttribute("aria-disabled", String(busy));
  };

  input.addEventListener("change", () => choose(input.files[0]));

  for (const type of ["dragenter", "dragover"]) {
    drop.addEventListener(type, (e) => {
      e.preventDefault();
      if (!xhr) drop.dataset.over = "";
    });
  }
  for (const type of ["dragleave", "drop"]) {
    drop.addEventListener(type, () => { delete drop.dataset.over; });
  }
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    if (xhr) return;
    const files = e.dataTransfer.files;
    if (files.length > 1) {
      showError("一度に送れるファイルは 1 つです。複数あるときは zip にまとめてください。");
      return;
    }
    choose(files[0]);
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (xhr) return;
    if (!chosen) { showError("送るファイルを選んでください。"); input.focus(); return; }
    if (chosen.size === 0) { showError("空のファイルは送れません。"); return; }
    if (chosen.size > maxBytes) { showError(`ファイルが大きすぎます。${maxLabel} 以下にしてください。`); return; }
    clearError();

    xhr = new XMLHttpRequest();
    xhr.open("PUT", location.pathname);
    xhr.setRequestHeader("X-Filename", encodeURIComponent(chosen.name));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    const started = Date.now();
    xhr.upload.addEventListener("progress", (ev) => {
      if (!ev.lengthComputable) return;
      const pct = Math.floor((ev.loaded / ev.total) * 100);
      bar.value = pct;
      const sec = (Date.now() - started) / 1000;
      const speed = sec > 1 ? `・${formatSize(ev.loaded / sec)}/秒` : "";
      progressText.textContent = `${pct}%(${formatSize(ev.loaded)} / ${formatSize(ev.total)}${speed})`;
    });
    xhr.addEventListener("load", () => {
      const current = xhr;
      xhr = null;
      setBusy(false);
      let data = {};
      try { data = JSON.parse(current.responseText); } catch { /* 下の汎用の文言を出す */ }
      if (current.status !== 200) {
        showError(data.error || `送信できませんでした(${current.status})。時間をおいてもう一度送ってください。`);
        return;
      }
      form.hidden = true;
      document.getElementById("done-name").textContent = data.filename;
      document.getElementById("done-pass").textContent = data.passphrase || "なし";
      const link = document.getElementById("done-url");
      link.textContent = data.url;
      link.href = data.url;
      document.getElementById("done-expires").textContent = `${data.expires} まで`;
      done.hidden = false;
      document.getElementById("done-title").focus?.();
    });
    xhr.addEventListener("error", () => {
      xhr = null;
      setBusy(false);
      showError("通信が切れました。回線を確かめて、もう一度送ってください。");
    });
    xhr.addEventListener("abort", () => {
      xhr = null;
      setBusy(false);
      bar.value = 0;
      progressText.textContent = "";
    });
    bar.value = 0;
    progressText.textContent = "0%";
    setBusy(true);
    xhr.send(chosen);
  });

  cancel.addEventListener("click", () => { if (xhr) xhr.abort(); });

  const copy = document.getElementById("copy");
  copy.addEventListener("click", async () => {
    const url = document.getElementById("done-url").href;
    try {
      await navigator.clipboard.writeText(url);
      copy.textContent = "コピーしました";
    } catch {
      copy.textContent = "コピーできませんでした";
    }
  });

  // 送信中にページを閉じかけたら止める(送信が無駄になるため)
  window.addEventListener("beforeunload", (e) => { if (xhr) e.preventDefault(); });
})();
