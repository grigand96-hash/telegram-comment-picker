"use strict";
const $ = id => document.getElementById(id);
const pageSessionId = crypto.randomUUID();
let verificationStep = "code";
const remotePage = !["127.0.0.1", "localhost"].includes(location.hostname);
const apiOrigin = remotePage ? "http://127.0.0.1:8894" : "";

async function request(path, body) {
  const response = await fetch(apiOrigin + path, body ? {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
  } : {signal: AbortSignal.timeout(12000)});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Проверьте заполненные поля.");
  return data;
}

async function perform(form, message, action) {
  $("error").hidden = true;
  $("status").textContent = message;
  const controls = [...form.querySelectorAll("input, button")];
  controls.forEach(control => control.disabled = true);
  try {
    await action();
    $("status").textContent = "";
  } catch (error) {
    $("status").textContent = "";
    $("error").textContent = error.message;
    $("error").hidden = false;
    $("error").scrollIntoView({block: "nearest"});
  } finally {
    controls.forEach(control => control.disabled = false);
  }
}

function ready() {
  $("local-service").hidden = true;
  $("connection").hidden = true;
  $("picker").hidden = false;
  $("connection-state").textContent = "Telegram подключён";
  $("connection-state").classList.add("connected");
}

$("login").addEventListener("submit", event => {
  event.preventDefault();
  perform($("login"), "Запрашиваю код в Telegram…", async () => {
    const data = await request("/api/login", {
      api_id: Number($("api-id").value),
      api_hash: $("api-hash").value,
      phone: $("phone").value
    });
    $("api-hash").value = "";
    if (data.step === "ready") {
      ready();
      return;
    }
    $("login").hidden = true;
    $("verify").hidden = false;
    $("verification").focus();
  });
});

$("verify").addEventListener("submit", event => {
  event.preventDefault();
  perform($("verify"), "Подключаю Telegram…", async () => {
    const data = await request("/api/verify", {[verificationStep]: $("verification").value});
    $("verification").value = "";
    if (data.step === "password") {
      verificationStep = "password";
      $("verify-label").textContent = "Облачный пароль Telegram";
      $("verification").autocomplete = "current-password";
    } else {
      ready();
    }
  });
});

function showProgress(state) {
  const labels = {
    waiting: "Ожидаем завершения предыдущего запроса…",
    connecting: "Подключаемся к Telegram…",
    collecting: "Собираем комментарии…",
    selecting: "Выбираем случайный комментарий…",
    done: "Комментарий выбран"
  };
  $("empty-result").hidden = true;
  $("stage-state").textContent = state.phase === "done" ? "Готово" : "В процессе";
  $("draw-progress").hidden = false;
  $("draw-progress").classList.toggle("done", state.phase === "done");
  $("progress-label").textContent = labels[state.phase] || labels.connecting;
  $("progress-count").textContent = new Intl.NumberFormat("ru-RU").format(state.count);
  const noun = new Intl.PluralRules("ru-RU").select(state.count);
  document.querySelector(".progress-caption").textContent = {
    one: "комментарий найден", few: "комментария найдено", many: "комментариев найдено", other: "комментария найдено"
  }[noun];
}

$("draw").addEventListener("submit", event => {
  event.preventDefault();
  $("result").hidden = true;
  const requestId = crypto.randomUUID();
  let polling = true;
  let timer;
  showProgress({phase: "connecting", count: 0});

  async function poll() {
    try {
      const state = await request("/api/progress?request_id=" + encodeURIComponent(requestId));
      if (polling) showProgress(state);
    } catch {
      // The draw response provides the final error if the connection fails.
    }
    if (polling) timer = setTimeout(poll, 500);
  }

  timer = setTimeout(poll, 300);
  perform($("draw"), "", async () => {
    try {
      const result = await request("/api/draw", {
        url: $("post").value,
        request_id: requestId,
        page_session_id: pageSessionId
      });
      polling = false;
      showProgress({phase: "done", count: result.count});
      $("author").textContent = result.author;
      $("text").textContent = result.text;
      $("comment-link").href = result.url;
      $("result").hidden = false;
      $("draw-label").textContent = "Выбрать ещё раз";
    } catch (error) {
      $("draw-progress").hidden = true;
      $("empty-result").hidden = false;
      $("stage-state").textContent = "Ошибка";
      $("empty-heading").textContent = "Выбор не завершён";
      $("empty-hint").textContent = "Попробуйте запустить его ещё раз";
      throw error;
    } finally {
      polling = false;
      clearTimeout(timer);
    }
  });
});

$("post").addEventListener("input", () => {
  $("result").hidden = true;
  $("draw-progress").hidden = true;
  $("draw-label").textContent = "Выбрать победителя";
  $("empty-result").hidden = false;
  $("stage-state").textContent = "Ожидание";
  $("empty-heading").textContent = "Готов к розыгрышу";
  $("empty-hint").textContent = "Выбранный комментарий появится здесь";
});

async function checkConnection() {
  $("error").hidden = true;
  $("status").textContent = "Проверяю подключение…";
  $("retry").disabled = true;
  try {
    const state = await request("/api/status");
    if (state.authorized) ready();
    else {
      $(remotePage ? "local-service" : "connection").hidden = false;
      $("connection-state").textContent = "Требуется подключение";
    }
  } catch (error) {
    $(remotePage ? "local-service" : "connection").hidden = false;
    $("connection-state").textContent = "Нет подключения";
    $("connection-state").classList.remove("connected");
    $("error").textContent = remotePage ? "Проверьте, что локальный сервис запущен и браузер разрешает подключение к нему." : error.message;
    $("error").hidden = false;
    $("error").scrollIntoView({block: "nearest"});
  } finally {
    $("status").textContent = "";
    $("retry").disabled = false;
  }
}

$("retry").addEventListener("click", checkConnection);
checkConnection();
