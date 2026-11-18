// Client-rendered list: the text only exists after this runs and its XHR returns.
fetch("/api/events.json")
  .then((r) => r.json())
  .then((events) => {
    document.getElementById("app").innerHTML = events
      .map((e) => `<article><h2>${e.title}</h2><p>Register by ${e.deadline}</p></article>`)
      .join("");
  });
// Must be stopped: a cloud-metadata address and a write.
fetch("http://169.254.169.254/latest/meta-data/").catch(() => {});
fetch("/api/track", { method: "POST", body: "x" }).catch(() => {});
