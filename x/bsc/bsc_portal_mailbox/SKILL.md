# Task: Download all letter PDFs from Blue Shield of California Member Portal

Navigate to the Blue Shield of California member portal message center and download all letter PDFs to
`~/Downloads/bsc-letters/`.

The user is already logged in. Start at: `https://www.blueshieldca.com/memberwebapp/connect/message-center/Inbox/`

---

## Overview

The message center is a narrow Angular SPA. Letters arrive as inbox messages, each with a PDF attachment stored in a
document management backend. There are two API calls needed:

1. **List API** — fetch all messages and their PDF filenames
2. **Download API** — fetch each PDF as a binary blob

---

## Step 1: Hard-navigate to the inbox

Use a full page navigation (not SPA link click) to get a clean page load:

```text
navigate to: https://www.blueshieldca.com/memberwebapp/connect/message-center/Inbox/
wait 4 seconds
```

This is critical. The page uses `XMLHttpRequest.prototype` overrides that **stack across SPA navigations**. A full
reload resets them to native, preventing 2x/3x duplicate downloads later.

**Verify the slate is clean before proceeding:**

```js
XMLHttpRequest.prototype.send.toString().includes("[native code]");
// Must return true. If false, reload again.
```

---

## Step 2: Install a single XHR interceptor

Install this **once and only once** after the clean reload. It does two things: captures the message list API response,
and handles PDF downloads with a dedup guard.

```js
const _origOpen = XMLHttpRequest.prototype.open;
const _origSend = XMLHttpRequest.prototype.send;
const _origSetHdr = XMLHttpRequest.prototype.setRequestHeader;

window._msgData = null;
window._downloadedFiles = new Set();
window._downloadQueue = [];

XMLHttpRequest.prototype.open = function (method, url, ...rest) {
  this.__url = url;
  this.__hdrs = {};
  if (url.includes("documents/download")) this.responseType = "blob";
  return _origOpen.apply(this, arguments);
};

XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
  this.__hdrs[k] = v;
  return _origSetHdr.apply(this, arguments);
};

XMLHttpRequest.prototype.send = function (body) {
  const url = this.__url || "";
  const hdrs = this.__hdrs || {};

  if (url.includes("message/center")) {
    this.addEventListener("load", () => {
      if (this.status === 200)
        try {
          window._msgData = JSON.parse(this.responseText);
        } catch (e) {}
    });
  }

  if (url.includes("documents/download")) {
    const fname = hdrs["fileName"] || "unknown.pdf";
    this.addEventListener("load", () => {
      if (this.status !== 200 || window._downloadedFiles.has(fname)) return;
      window._downloadedFiles.add(fname);
      const blobUrl = URL.createObjectURL(this.response);
      const a = document.createElement("a");
      a.href = blobUrl;
      a.download = "bsc-letters/" + fname;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(blobUrl), 10000);
      window._downloadQueue.push({
        filename: fname,
        status: "ok",
        ts: new Date().toISOString(),
      });
      console.log("✅", window._downloadQueue.length, fname);
    });
  }

  return _origSend.apply(this, arguments);
};
```

---

## Step 3: Trigger the message list API

The list API fires when navigating between folders. Trigger it by clicking Sent, waiting, then clicking Inbox:

```js
// Click Sent
Array.from(document.querySelectorAll("a"))
  .find((a) => a.textContent.trim() === "Sent")
  .click();
// wait 2 seconds
// Click Inbox
Array.from(document.querySelectorAll("a"))
  .find((a) => a.textContent.trim().match(/^0?Inbox$/))
  .click();
// wait 3 seconds
```

Then verify:

```js
window._msgData?.responseBody?.planMember?.messages?.message?.length;
// Should equal the total number of inbox letters
```

---

## Message List API (reference / auto-discovery)

**Endpoint:**

```text
POST /memberwebapp/reverseproxy-secured/v10/bsc/aip/api/bsc/gateway/member/message/center/read/v2
```

**Required headers:**

```text
Accept: application/json, text/plain, */*
Cache-Control: no-store
Content-Type: application/json
Content-Security-Policy: default-src 'self'
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
birthdate: <member DOB as YYYYMMDD — auto-discovered, see notes below>
consumerMemberId: <member ID — auto-discovered from response>
mwa_session_id: <sent automatically via withCredentials / credentials: 'include'>
transactionID: <any random 9-char alphanumeric>
userID: (empty string)
```

**Request body shape:**

```json
{
  "requestHeader": {
    "credentials": {
      "userName": "",
      "password": "",
      "token": "<static JWT — auto-discovered by intercepting a real XHR, see notes>",
      "type": "jwt"
    },
    "consumer": {
      "businessTransactionType": "MESSAGE_ALL_READ",
      "businessUnit": "DIGITAL",
      "clientVersion": "V1",
      "type": "Web Portal",
      "name": "MEMBER",
      "id": "MEMBER",
      "hostName": "localhost",
      "contextId": "",
      "secondContextId": "",
      "thirdContextId": "",
      "requestDateTime": "<current time string, e.g. '3:00:00 PM'>"
    },
    "transactionId": "<same random ID as header>"
  },
  "requestBody": {
    "planMember": {
      "memberIdentifier": "<auto-discovered from response>",
      "groupNumber": "<auto-discovered from response>",
      "offset": "1",
      "limit": "10000",
      "sortBy": "DATE",
      "sortOrder": "DESC",
      "filter": {
        "folderName": ""
      }
    }
  }
}
```

**Key notes:**

- `folderName: ""` = all inbox letters. Use `"Sent"`, `"Archive"`, `"Trash"` for other folders.
- `businessTransactionType`: use `"MESSAGE_ALL_READ"` for unfiltered inbox; `"MESSAGE_READ"` for filtered/other folders.
- **Date filtering**: add `fromDate` and `toDate` to the filter object, format `"YYYYMMDD HH.MM.SS"` (e.g.
  `"20260101 00.00.00"` and `"20260331 23.59.00"`). Omit both keys entirely for no date filter.
- The static JWT in `credentials.token` is not a secret auth token — real auth is via the session cookie. It does not
  rotate and can be captured once from any intercepted XHR.
- `memberIdentifier`, `groupNumber`, `birthdate`: auto-discover by capturing the first real XHR the app makes to this
  endpoint (headers and response body contain all values).

**Each message object in the response contains:**

- `documentName` — the PDF filename (e.g. `AP_<documentId>_<timestamp>.pdf`) — this is what you need
- `documentId`, `messageId`, `messageTime` (format: `"YYYYMMDD HH:MM:SS"`), `subjectText`, `folderName`,
  `unreadIndicator`

---

## Step 4: Extract filenames and member ID

```js
const msgs = window._msgData.responseBody.planMember.messages.message;
const memberID = window._msgData.responseBody.planMember.memberIdentifier;
const filenames = msgs.map((m) => m.documentName);
// filenames is now an array of all PDF filenames
```

---

## Step 5: Download all PDFs

```js
window._downloadedFiles = new Set();
window._downloadQueue = [];
window._done = false;

function downloadOne(filename) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/memberwebapp/reverseproxy-secured/v10/bsc/aip/api/bsc/gateway/es/documents/download");
    xhr.responseType = "blob";
    const txn = Math.random().toString(36).substr(2, 9);

    [
      ["Accept", "application/json, text/plain, */*"],
      ["Cache-Control", "no-store"],
      ["Content-Security-Policy", "default-src 'self'"],
      ["Content-Type", "application/json"],
      ["X-Content-Type-Options", "nosniff"],
      ["X-Frame-Options", "DENY"],
      ["birthdate", "<member DOB YYYYMMDD>"],
      ["consumerMemberId", memberID],
      ["fileName", filename],
      ["transactionID", txn],
      ["userID", ""],
    ].forEach(([k, v]) => xhr.setRequestHeader(k, v));

    xhr.onload = function () {
      if (xhr.status === 200) {
        if (window._downloadedFiles.has(filename)) {
          resolve("dup-skip");
          return;
        }
        window._downloadedFiles.add(filename);
        const url = URL.createObjectURL(xhr.response);
        const a = document.createElement("a");
        a.href = url;
        a.download = "bsc-letters/" + filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(url), 10000);
        window._downloadQueue.push({ filename, status: "ok" });
        console.log("✅", window._downloadQueue.length + "/" + filenames.length, filename);
        resolve("ok");
      } else {
        window._downloadQueue.push({ filename, status: "err:" + xhr.status });
        reject("HTTP " + xhr.status);
      }
    };
    xhr.onerror = () => reject("network error");

    xhr.send(
      JSON.stringify({
        requestHeader: {
          credentials: {
            userName: "",
            password: "",
            token: "<static JWT — auto-discovered from intercepted XHR>",
            type: "jwt",
          },
          consumer: {
            name: "MEMBER",
            id: "MEMBER",
            businessUnit: "DIGITAL",
            type: "Web Portal",
            clientVersion: "V1",
            requestDateTime: new Date().toLocaleTimeString(),
            hostName: "localhost",
            businessTransactionType: "ES_DOC_DOWNLOAD",
            contextId: "",
            secondContextId: "",
            thirdContextId: "",
          },
          transactionId: txn,
        },
        requestBody: {
          searchCriteria: {
            criteria: [
              { key: "documentOwner", value: memberID },
              { key: "documentName", value: filename },
            ],
          },
        },
      })
    );
  });
}

(async () => {
  for (const fname of filenames) {
    try {
      await downloadOne(fname);
    } catch (e) {
      console.log("❌", fname, e);
    }
    await new Promise((r) => setTimeout(r, 700));
  }
  window._done = true;
  console.log("🎉 Done!", window._downloadQueue.length + "/" + filenames.length);
})();
```

---

## Document Download API (reference)

**Endpoint:**

```text
POST /memberwebapp/reverseproxy-secured/v10/bsc/aip/api/bsc/gateway/es/documents/download
```

Same headers as the list API, with the addition of `fileName: <documentName>`. Set `responseType = 'blob'`. Response is
raw PDF binary. Chrome automatically creates the `bsc-letters/` subdirectory inside the default downloads folder when
the `download` attribute contains a path.

---

## Step 6: Verify

```js
({
  done: window._done,
  okCount: window._downloadQueue.filter((d) => d.status === "ok").length,
  errCount: window._downloadQueue.filter((d) => d.status !== "ok").length,
  totalQueueEntries: window._downloadQueue.length,
  // okCount and totalQueueEntries should both equal filenames.length
  // errCount should be 0
  // If totalQueueEntries > filenames.length, interceptors were stacked — start over from Step 1
});
```

---

## Auto-discovery of unknown values

If `birthdate`, the static JWT, or `memberID` are not known in advance: install the interceptor (Step 2) immediately
after page load, then trigger the Sent → Inbox folder switch (Step 3). The interceptor will capture all header values
from `this.__hdrs` and all member data from the response body automatically. Specifically:

- `birthdate`: captured in `this.__hdrs['birthdate']` when the app sends its own XHR
- `memberID`: read from `window._msgData.responseBody.planMember.memberIdentifier`
- `groupNumber`: read from `window._msgData.responseBody.planMember.groupNumber`
- static JWT: captured in the parsed request body (`requestHeader.credentials.token`) of any intercepted call — store it
  and reuse it for all subsequent calls
