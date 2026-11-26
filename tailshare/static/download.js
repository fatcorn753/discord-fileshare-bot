// ダウンロード用ページ。大きいファイルは WebSocket を何本も開いて分割で受け取り、保存先へ直接書く。
// Funnel は 1 本の接続あたりの速さが頭打ちになるので、本数を増やすと速くなる(fetch は 1 本にまとめられてしまう)。
// 保存先へ直接書くには showSaveFilePicker が要るので、使えないブラウザでは今まで通りの 1 本のダウンロードになる。
(() => {
  const link = document.getElementById("dl");
  const cancelBtn = document.getElementById("dl-cancel");
  const error = document.getElementById("dl-error");
  const errorText = document.getElementById("dl-error-text");
  const progress = document.getElementById("dl-progress");
  const bar = document.getElementById("dl-bar");
  const text = document.getElementById("dl-text");

  const size = Number(link.dataset.size);
  const name = link.dataset.name;
  const MIN_SIZE = 16 * 1024 * 1024; // これより小さいと分割しても速さは変わらない
  const PIECE = 8 * 1024 * 1024; // 1 回に頼む大きさ(サーバーの上限は 16MB)
  const WORKERS = 6;
  const RETRIES = 3;

  if (!("showSaveFilePicker" in window) || !(size >= MIN_SIZE)) return;

  const wsUrl = new URL(link.href.replace(/\/download$/, "/ws"));
  wsUrl.protocol = location.protocol === "https:" ? "wss:" : "ws:";

  const formatSize = (n) => {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return i === 0 ? `${n} B` : `${n.toFixed(1)} ${units[i]}`;
  };

  let job = null;

  const setBusy = (busy) => {
    link.hidden = busy;
    cancelBtn.hidden = !busy;
    progress.hidden = !busy;
  };
  const showError = (message) => {
    errorText.textContent = message;
    error.hidden = false;
  };

  // 1 本の WebSocket で、空いている区切りを順に受け取る
  const worker = (state) => new Promise((resolve) => {
    let current = null; // { start, end, got }
    let opened = false;
    const ws = new WebSocket(wsUrl);
    ws.binaryType = "arraybuffer";
    state.sockets.add(ws);

    const next = () => {
      if (state.stopped || state.queue.length === 0) { ws.close(); return; }
      const [start, end] = state.queue.shift();
      current = { start, end, got: 0 };
      ws.send(JSON.stringify({ start, end }));
    };
    ws.addEventListener("open", () => { opened = true; state.anyOpened = true; next(); });
    ws.addEventListener("message", (ev) => {
      if (!current || typeof ev.data === "string") return;
      const data = ev.data;
      const position = current.start + current.got;
      current.got += data.byteLength;
      state.received += data.byteLength;
      state.write(position, data);
      if (current.got >= current.end - current.start) {
        current = null;
        next();
      }
    });
    // 接続先がブロックされたときなど、close が来ずに error だけで終わることがあるので、どちらでも終える
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      state.sockets.delete(ws);
      try { ws.close(); } catch { /* すでに閉じている */ }
      if (current) {
        // 途中で切れた区切りは、最初から受け取り直す(書いた分は上書きされる)
        state.received -= current.got;
        const key = current.start;
        state.retries.set(key, (state.retries.get(key) || 0) + 1);
        if (state.retries.get(key) > RETRIES) state.fail("通信が何度も切れました。時間をおいて、もう一度ダウンロードしてください。");
        else state.queue.push([current.start, current.end]);
      }
      resolve({ opened, retry: Boolean(current) });
    };
    ws.addEventListener("close", finish);
    ws.addEventListener("error", finish);
  });

  const start = async (handle) => {
    const writable = await handle.createWritable();
    const state = {
      queue: [],
      sockets: new Set(),
      retries: new Map(),
      received: 0,
      stopped: false,
      anyOpened: false,
      connectFailures: 0,
      failure: null,
      chain: Promise.resolve(),
      write(position, data) {
        // 書き込みは 1 つずつ順に(同時に呼ぶと失敗するため)
        this.chain = this.chain.then(() => writable.write({ type: "write", position, data }));
        this.chain.catch(() => this.fail("保存先に書き込めませんでした。空き容量を確かめてください。"));
      },
      fail(message) {
        if (this.failure) return;
        this.failure = message;
        this.stop();
      },
      stop() {
        this.stopped = true;
        for (const ws of this.sockets) ws.close();
      },
    };
    for (let s = 0; s < size; s += PIECE) state.queue.push([s, Math.min(s + PIECE, size)]);
    job = state;

    const started = performance.now();
    const tick = () => {
      if (job !== state) return;
      const pct = Math.floor((state.received / size) * 100);
      bar.value = pct;
      const sec = (performance.now() - started) / 1000;
      const speed = sec > 1 ? `・${formatSize(state.received / sec)}/秒` : "";
      text.textContent = `${pct}%(${formatSize(state.received)} / ${formatSize(size)}${speed})`;
      requestAnimationFrame(tick);
    };
    bar.value = 0;
    text.textContent = "0%";
    setBusy(true);
    requestAnimationFrame(tick);

    // 本数ぶんの受け取り役を回す。途中で切れた役は、残りがあれば作り直す
    const runSlot = async () => {
      while (!state.stopped && state.queue.length > 0) {
        const { opened, retry } = await worker(state);
        if (!opened) {
          if (!state.anyOpened) return; // 一度もつながらない(WebSocket が通らない環境)
          state.connectFailures += 1;
          if (state.connectFailures > WORKERS * RETRIES) {
            state.fail("つながらなくなりました。時間をおいて、もう一度ダウンロードしてください。");
            return;
          }
        }
        if (retry || !opened) await new Promise((r) => setTimeout(r, 1000));
      }
    };
    await Promise.all(Array.from({ length: WORKERS }, runSlot));
    try { await state.chain; } catch { /* fail で文言を出している */ }
    job = null;

    if (!state.anyOpened && !state.stopped) {
      await writable.abort();
      setBusy(false);
      location.href = link.href; // 分割で受け取れない環境なので、普通のダウンロードに切り替える
      return;
    }
    if (state.stopped || state.received !== size) {
      await writable.abort().catch(() => {});
      setBusy(false);
      if (state.failure) showError(state.failure);
      else if (!state.stopped) showError("最後まで受け取れませんでした。もう一度ダウンロードしてください。");
      return;
    }
    await writable.close();
    setBusy(false);
    const sec = (performance.now() - started) / 1000;
    bar.value = 100;
    progress.hidden = false;
    text.textContent = `保存しました(${formatSize(size)}・${Math.round(sec)} 秒・${formatSize(size / sec)}/秒)`;
  };

  link.addEventListener("click", async (e) => {
    e.preventDefault();
    if (job) return;
    error.hidden = true;
    let handle;
    try {
      handle = await window.showSaveFilePicker({ suggestedName: name });
    } catch (err) {
      if (err && err.name === "AbortError") return; // 保存先を選ばずに閉じた
      location.href = link.href;
      return;
    }
    try {
      await start(handle);
    } catch {
      job = null;
      setBusy(false);
      showError("保存先を開けませんでした。もう一度ダウンロードしてください。");
    }
  });

  cancelBtn.addEventListener("click", () => {
    if (job) job.stop();
  });

  // 受け取り中にページを閉じかけたら止める
  window.addEventListener("beforeunload", (e) => { if (job) e.preventDefault(); });
})();
