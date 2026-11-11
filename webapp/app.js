"use strict";

const telegram = window.Telegram && window.Telegram.WebApp;
const initData = telegram ? (telegram.initData || "") : "";
const main = document.getElementById("main");
const notice = document.getElementById("notice");
const tabs = document.getElementById("tabs");
const badge = document.getElementById("group-badge");
const state = { me: null, date: null, page: "attendance" };

if (telegram) {
  telegram.ready();
  telegram.expand();
}

function node(tag, cls, content) {
  const element = document.createElement(tag);
  if (cls) element.className = cls;
  if (content !== undefined && content !== null) element.textContent = String(content);
  return element;
}
function add(parent, ...children) {
  for (const child of children) {
    if (child) parent.appendChild(child);
  }
  return parent;
}
function title(text, subtitle) {
  const wrap = node("div");
  add(wrap, node("h1", "section-title", text), node("p", "sub", subtitle));
  return wrap;
}
function card(...children) { return add(node("div", "card"), ...children); }
function row(...children) { return add(node("div", "row wrap"), ...children); }
function textLine(text, cls) { return node("div", cls || "muted", text); }
function button(label, handler, cls) {
  const b = node("button", "btn " + (cls || ""), label);
  b.type = "button";
  b.addEventListener("click", async function () {
    if (b.disabled) return;
    b.disabled = true;
    try {
      await handler();
      notice.classList.add("hidden");
    } catch (error) {
      showError(error);
    } finally {
      b.disabled = false;
    }
  });
  return b;
}
function showError(error) {
  notice.textContent = error.message || String(error);
  notice.classList.remove("hidden");
  window.scrollTo({ top: 0, behavior: "smooth" });
}
async function api(path, body) {
  const options = {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-Telegram-Init-Data": initData },
    credentials: "omit",
    cache: "no-store"
  };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); } catch (_) {
    throw new Error("Сервер вернул неверный ответ");
  }
  if (!response.ok) throw new Error(data.error || "Ошибка сервера");
  return data;
}
function activate(page) {
  state.page = page;
  for (const b of tabs.querySelectorAll("button")) {
    b.classList.toggle("active", b.dataset.page === page);
    b.setAttribute("aria-current", b.dataset.page === page ? "page" : "false");
  }
}
function dayShift(value, diff) {
  const d = new Date(value + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() + diff);
  return d.toISOString().slice(0, 10);
}
function prettyDate(value) {
  const parts = value.split("-");
  return parts[2] + "." + parts[1] + "." + parts[0];
}
function openBot() {
  const url = "https://t.me/st_schedule_bot";
  if (telegram && telegram.openTelegramLink) telegram.openTelegramLink(url);
  else window.location.href = url;
}
async function openPage(page) {
  activate(page);
  if (page === "schedule" || page === "attendance") await showDay(page);
  else if (page === "stats") await showStats(false);
  else if (page === "profile") showProfile();
}

async function showDay(page) {
  activate(page);
  const query = state.date ? "?date=" + encodeURIComponent(state.date) : "";
  const data = await api("/api/day" + query);
  state.date = data.date;
  const content = node("div");
  add(content, title(page === "schedule" ? "Расписание" : "Посещаемость",
    page === "schedule" ? "Пары вашей группы на выбранную дату" :
      "Выберите пару, чтобы отметить себя или открыть журнал"));
  const navigation = node("div", "date-nav");
  add(navigation,
    button("←", async function () { state.date = dayShift(data.date, -1); await showDay(page); }),
    node("div", "date-label", prettyDate(data.date)),
    button("→", async function () { state.date = dayShift(data.date, 1); await showDay(page); })
  );
  add(content, navigation,
    button("Сегодня", async function () { state.date = null; await showDay(page); }, "btn-tiny"));
  if (!data.lessons.length) {
    add(content, node("div", "empty", "На этот день занятий нет"));
  } else {
    for (const lesson of data.lessons) {
      const entry = node("button", "lesson");
      entry.type = "button";
      add(entry,
        add(node("div"),
          node("div", "lesson-index", lesson.number + "-я пара"),
          node("div", "lesson-name", lesson.subject),
          node("div", "lesson-time", lesson.start + " — " + lesson.end)),
        node("span", "muted", "›"));
      entry.addEventListener("click", async function () {
        try { await showLesson(lesson.number); } catch (error) { showError(error); }
      });
      add(content, entry);
    }
  }
  if (page === "attendance" && !state.me.verified) {
    add(content, card(textLine("Для самостоятельной отметки выберите себя в списке группы и получите подтверждение.",
      "muted small"), button("Выбрать своё ФИО", showClaim, "btn-wide btn-primary")));
  }
  if (page === "attendance" && state.me.role === "starosta") {
    add(content, card(
      node("div", "card-title", "Староста"),
      row(button("Заявки студентов", showPending, "btn-wide"))
    ));
  }
  main.replaceChildren(content);
}
function counters(values) {
  const items = [
    ["Присутствуют", values.present || 0],
    ["Опоздали", values.late || 0],
    ["Отсутствуют", values.absent || 0],
    ["Не отмечены", values.unmarked || 0]
  ];
  const wrap = node("div", "counters");
  for (const item of items) {
    add(wrap, add(node("div", "counter"), node("strong", "", item[1]), node("span", "", item[0])));
  }
  return wrap;
}
async function showLesson(number) {
  activate("attendance");
  const query = "?date=" + encodeURIComponent(state.date) + "&number=" + number;
  const data = await api("/api/lesson" + query);
  const content = node("div");
  add(content, title(data.number + "-я пара", data.subject),
    row(textLine(prettyDate(data.date) + " · " + data.start + "–" + data.end, "muted small"),
      node("span", data.closed ? "pill pill-warn" : "pill pill-ok",
        data.closed ? "Журнал закрыт" : "Открыт")),
    counters(data.counts));
  if (data.self_status) {
    add(content, card(node("div", "card-title", "Ваша отметка"),
      textLine(statusLabel(data.self_status), "pill pill-ok")));
  } else if (data.can_self_mark) {
    add(content, button("✓ Я присутствую на паре", async function () {
      await api("/api/mark", { date: state.date, number: number, self: true });
      await showLesson(number);
    }, "btn-primary btn-wide"));
  } else if (!data.verified) {
    add(content, card(textLine("Для самоотметки подтвердите личность.", "muted small"),
      button("Выбрать себя", showClaim, "btn-wide")));
  } else {
    add(content, node("div", "hint",
      "Самостоятельная отметка доступна только во время пары. Исправления делает староста."));
  }
  if (data.monitor) {
    add(content, node("h2", "section-title", "Журнал группы"));
    if (!data.students.length) add(content, node("div", "empty", "Список группы ещё не загружен"));
    for (const student of data.students) {
      const line = node("div", "person");
      add(line,
        row(node("div", "person-name", student.name),
          node("span", student.status ? "pill pill-ok" : "pill", statusLabel(student.status))),
        add(node("div", "actions"),
          button("✓", async function () {
            await markStudent(number, student.person_id, "present");
          }, student.status === "present" ? "btn-positive" : "btn-tiny"),
          button("⌚", async function () {
            await markStudent(number, student.person_id, "late");
          }, student.status === "late" ? "btn-warn" : "btn-tiny"),
          button("✕", async function () {
            await markStudent(number, student.person_id, "absent");
          }, student.status === "absent" ? "btn-danger" : "btn-tiny"),
          button("Ув.", async function () {
            await markStudent(number, student.person_id, "excused");
          }, "btn-tiny")));
      add(content, line);
    }
    if (!data.closed) add(content, card(
      node("div", "muted small", "При закрытии все неотмеченные будут считаться отсутствующими."),
      button("Закрыть журнал пары", async function () {
        if (!window.confirm("Закрыть журнал? Неотмеченные получат статус «Отсутствует».")) return;
        await api("/api/close", { date: state.date, number: number });
        await showLesson(number);
      }, "btn-danger btn-wide")
    ));
  }
  add(content, button("← К списку пар", async function () { await showDay("attendance"); }, "btn-wide"));
  main.replaceChildren(content);
}
function statusLabel(status) {
  return { present: "✓ Присутствует", late: "⌚ Опоздал",
    absent: "✕ Отсутствует", excused: "Уважительная" }[status] || "— Нет отметки";
}
async function markStudent(number, person, status) {
  await api("/api/mark", { date: state.date, number: number, person_id: person, status: status });
  await showLesson(number);
}
async function showClaim() {
  const data = await api("/api/students");
  const content = node("div");
  add(content, title("Кто вы в списке?", "Выберите своё ФИО. Староста подтвердит заявку после проверки личности."));
  for (const person of data.students) {
    add(content, button(person.name, async function () {
      if (!window.confirm("Отправить заявку на имя " + person.name + "?")) return;
      await api("/api/claim", { person_id: person.person_id });
      main.replaceChildren(
        title("Заявка отправлена", "Дождитесь подтверждения старосты или администратора."),
        button("К посещаемости", async function () { await showDay("attendance"); }, "btn-wide"));
    }, "btn-wide"));
  }
  if (!data.students.length) add(content, node("div", "empty",
    "Список студентов ещё не загружен. Обратитесь к администратору бота."));
  add(content, button("Назад", async function () { await showDay("attendance"); }, "btn-wide"));
  main.replaceChildren(content);
}
async function showPending() {
  if (state.me.role !== "starosta") return;
  const data = await api("/api/pending");
  const content = node("div");
  add(content, title("Заявки студентов", "Подтверждайте только тех, чью личность вы проверили."));
  for (const claim of data.claims) {
    add(content, card(
      node("div", "card-title", claim.name),
      textLine("Telegram ID: " + claim.telegram_id, "muted small"),
      button("Подтвердить", async function () {
        if (!window.confirm("Подтверждена личность: " + claim.name + "?")) return;
        await api("/api/approve", { user_id: claim.user_id });
        await showPending();
      }, "btn-primary btn-wide")));
  }
  if (!data.claims.length) add(content, node("div", "empty", "Новых заявок пока нет"));
  add(content, button("Назад", async function () { await showDay("attendance"); }, "btn-wide"));
  main.replaceChildren(content);
}
async function showStats(group) {
  activate("stats");
  if (group && state.me.role !== "starosta") group = false;
  const data = await api("/api/stats?scope=" + (group ? "group" : "my"));
  const content = node("div");
  add(content, title("Статистика", group ? "Посещаемость группы" : "Ваша посещаемость"));
  if (state.me.role === "starosta") {
    add(content, row(
      button("Моя", async function () { await showStats(false); }, !group ? "btn-primary" : ""),
      button("Группы", async function () { await showStats(true); }, group ? "btn-primary" : "")
    ));
  }
  for (const person of data.students) {
    const percent = person.percent === null ? "н/д" : person.percent + "%";
    const info = card(
      row(node("div", "card-title", person.name), node("div", "percent", percent)),
      textLine("Посещено " + person.attended + " из " + person.total +
        " · пропусков " + person.absent + " · уважительных " + person.excused, "muted small")
    );
    if (person.percent !== null) {
      const bar = node("div", "bar");
      const fill = node("div", "bar-fill");
      fill.style.width = person.percent + "%";
      add(info, add(bar, fill));
    }
    add(content, info);
  }
  if (!data.students.length) add(content, node("div", "empty", "Статистика пока не доступна"));
  main.replaceChildren(content);
}
function showProfile() {
  activate("profile");
  const content = node("div");
  add(content, title("Профиль", "Доступ к журналу основан на привязанном аккаунте ЛК"));
  add(content, card(
    node("div", "card-title", state.me.group || "Группа не определена"),
    textLine(state.me.role === "starosta" ? "Роль: староста / администратор" : "Роль: студент"),
    textLine(state.me.verified ? "Личность подтверждена" : "Личность не подтверждена", "small muted")));
  add(content, button("Открыть профиль в Telegram-боте", openBot, "btn-wide btn-primary"));
  main.replaceChildren(content);
}
async function initialize() {
  if (!initData) {
    badge.textContent = "Нет авторизации";
    main.replaceChildren(title("Откройте из Telegram",
      "Для доступа к посещаемости запустите веб-приложение кнопкой в личном чате с ботом."));
    return;
  }
  try {
    state.me = await api("/api/me");
    if (!state.me.linked) {
      badge.textContent = "Нет привязки ЛК";
      main.replaceChildren(title("Нужна привязка к ЛК",
        "Откройте профиль в Telegram-боте и привяжите личный аккаунт ЛК."),
        button("Открыть бота", openBot, "btn-wide btn-primary"));
      return;
    }
    badge.textContent = state.me.group;
    tabs.hidden = false;
    await showDay("attendance");
  } catch (error) {
    showError(error);
    main.replaceChildren(title("Не удалось открыть журнал",
      "Проверьте подключение и попробуйте снова открыть веб-приложение из Telegram."));
  }
}
for (const tab of tabs.querySelectorAll("button")) {
  tab.addEventListener("click", async function () {
    try { notice.classList.add("hidden"); await openPage(tab.dataset.page); }
    catch (error) { showError(error); }
  });
}
initialize();
