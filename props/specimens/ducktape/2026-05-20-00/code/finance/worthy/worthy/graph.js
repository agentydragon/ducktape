const xhr = new XMLHttpRequest();
function handleLoad(e) {
  const data = [];
  for (const entry of xhr.response["Entries"]) {
    data.push({ x: moment(entry["Time"]).toDate(), y: entry["Amount"] });
  }
  console.log(data[0]);
  console.log(data[data.length - 1]);
  const chart = new Chartist.Line(
    ".ct-chart",
    { series: [{ name: "series-1", data: data }] },
    {
      axisX: {
        type: Chartist.FixedScaleAxis,
        divisor: 20,
        labelInterpolationFnc: function (value) {
          return moment(value).format("YYYY-MM");
        },
      },
    }
  );
}
// Download http://localhost:8000/history.json, plot it.
xhr.addEventListener("load", handleLoad);
xhr.responseType = "json";
xhr.open("GET", "/history.json");
xhr.send();

const xhr2 = new XMLHttpRequest();
function curfmt(n, sym) {
  return new Intl.NumberFormat("cs-CZ", { style: "currency", currency: sym }).format(n);
}
function assetHtml(c) {
  return curfmt(c["Amount"], c["Symbol"]);
}
function handleModel(e) {
  const model = xhr2.response;
  console.log(model);
  let html = "<table>";
  html += "<tr>";
  html += "<th>";
  html += assetHtml(model["Total"]);
  for (const yearlyYield of model["YearlyYields"]) {
    html += "<th>" + (yearlyYield * 100).toFixed(0) + "%";
  }
  html += "<tr><th>Perpetuals";
  for (const perpPerYield of model["Perpetuals"]) {
    html += "<td>";
    html += perpPerYield.map(assetHtml).join("<br>");
  }
  for (let i = 0; i < model["MonthlyTargets"].length; ++i) {
    html += "<tr>";
    html += "<th>";
    html += assetHtml(model["MonthlyTargets"][i]);
    for (let j = 0; j < model["YearlyYields"].length; ++j) {
      html += "<td>";
      const result = model["Infos"][i][j];
      const fiInfo = result["ModelFiInfo"];
      if (fiInfo["Reached"]) {
        html += "&check; " + fiInfo["OverreachPercentage"].toFixed(0) + "%";
        // TODO: we should continue the current timestamp...
        // now: = time.Now()
        // if self.Reached{return fmt.Sprintf(
        //     "%.0f%% ✓", self.OverreachPercentage)} durabilityString:
        //     = now.Add(self.Durability).Format("2006-01-02")
        // untilSavedString: = now.Add(self.UntilSavedUp).Format("2006-01-02")
        // // 2912 = upwards arrow to bar
        // // 2913 = downwards arrow to bar
        // return fmt.Sprintf("\u2912 %s\n\u2913 %s", untilSavedString,
        //                    durabilityString)
      } else {
        // 2693 = unicode anchor
        // 1F4B0 = bag with money
        html +=
          "💰 ≥" +
          curfmt(fiInfo["NeedToLastUntilDeadline"], model["Total"]["Symbol"]) +
          "<br>" +
          moment(fiInfo["LastsUntil"]).format("YYYY-MM-DD") +
          "<br>" +
          moment(fiInfo["ProjectedUntilSaved"]).format("YYYY-MM-DD");
        // TODO: result.ModelFiInfo.LastsUntilShortString()
      }
    }
  }
  html += "</table>";
  document.getElementById("thetable").innerHTML = html;
}
// Download http://localhost:8000/model.json, print it.
xhr2.addEventListener("load", handleModel);
xhr2.responseType = "json";
xhr2.open("GET", "/model.json");
xhr2.send();
