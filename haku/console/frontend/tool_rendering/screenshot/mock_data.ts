// Canned payloads the preview widgets fetch (gmail subjects, grocy reference, calendar name,
// tana nodes). Shared across per-server screenshot targets — each target's bundle includes this
// via mock.ts. The data-fetching widgets render resolved names/labels instead of raw ids.
import type { GrocyReferenceResponse } from "../../grocy_client.ts";

// Subject/label lookups the Gmail thread-labels widget fetches; both preview variants render real
// subjects (compact shows the first few, detailed adds labels).
export const SAMPLE_GMAIL_THREADS = {
  t1: {
    subject: "Q3 planning — notes + open questions",
    gmail_url: "https://mail.google.com/mail/u/0/#all/t1",
    current_label_names: ["Inbox", "Work"],
  },
  t2: {
    subject: "Re: dentist appointment confirmation",
    gmail_url: "https://mail.google.com/mail/u/0/#all/t2",
    current_label_names: ["Inbox"],
  },
  t3: {
    subject: "Your Thrive Market order shipped",
    gmail_url: "https://mail.google.com/mail/u/0/#all/t3",
    current_label_names: ["Inbox", "Receipts"],
  },
  t4: {
    subject: "This week in your neighborhood",
    gmail_url: "https://mail.google.com/mail/u/0/#all/t4",
    current_label_names: ["Inbox", "Newsletters"],
  },
};

// The grocy-sf reference the preview widgets resolve id→name against, and (for products) read
// current field values from to render `products_edit` old→new diffs.
export const SAMPLE_GROCY_REFERENCE: GrocyReferenceResponse = {
  products: [
    {
      id: 1,
      name: "Rolled oats",
      location_id: 10,
      qu_id_stock: 20,
      qu_id_purchase: 20,
      qu_id_consume: 20,
      min_stock_amount: 250,
      default_best_before_days: 180,
      due_type: 1,
      parent_product_id: null,
      product_group_id: 30,
      description: "Thin rolled oats.",
      calories: null,
    },
    {
      id: 2,
      name: "Almond butter",
      location_id: 10,
      qu_id_stock: 21,
      qu_id_purchase: 22,
      qu_id_consume: 22,
      min_stock_amount: 0,
      default_best_before_days: 180,
      due_type: 1,
      parent_product_id: null,
      product_group_id: null,
      description: null,
      calories: null,
    },
  ],
  locations: [
    { id: 10, name: "Pantry" },
    { id: 11, name: "Fridge" },
    { id: 12, name: "Freezer" },
  ],
  quantity_units: [
    { id: 20, name: "gram" },
    { id: 21, name: "jar" },
    { id: 22, name: "case" },
    { id: 23, name: "carton" },
  ],
  product_groups: [
    { id: 30, name: "Snacks" },
    { id: 31, name: "Grains" },
    { id: 32, name: "Dairy" },
  ],
  shopping_lists: [
    { id: 40, name: "Weekly" },
    { id: 41, name: "Costco run" },
  ],
  // Shopping-list items, so `shopping_list_item_edit` (item 42) and `shopping_list_items_remove`
  // render resolved names. A couple of the remove ids (15, 34) are deliberately absent to exercise
  // the `Item #id` fallback; item 12 is note-only.
  shopping_list_items: [
    { item_id: 3, product_name: "Milk", note: null, amount: 1, qu_name: "Carton", done: false },
    { item_id: 7, product_name: "Spinach", note: null, amount: 200, qu_name: "Gram", done: false },
    { item_id: 12, product_name: null, note: "paper towels?", amount: 1, qu_name: null, done: false },
    { item_id: 21, product_name: "Dark chocolate", note: null, amount: 2, qu_name: "Bar", done: true },
    { item_id: 42, product_name: "Almond butter", note: null, amount: 1, qu_name: "Jar", done: false },
    { item_id: 55, product_name: "Rolled oats", note: null, amount: 2, qu_name: "Pack", done: false },
  ],
};

// The calendar-name lookup the create-event widget fetches for a non-primary calendar_id; the
// detailed preview renders the name (linked) instead of the raw id.
export const SAMPLE_CALENDAR_SUMMARY = {
  calendar_id: "family@group.calendar.google.com",
  summary: "Family",
  html_link: "https://calendar.google.com/calendar/u/0/r?cid=ZmFtaWx5QGdyb3VwLmNhbGVuZGFyLmdvb2dsZS5jb20",
};

// Node names the tana widgets resolve node ids against (import/move/trash/edit reference these).
export const SAMPLE_TANA_NODES = {
  nodes: [
    { id: "inbox", name: "Inbox" },
    { id: "task", name: "Quarterly planning" },
    { id: "project", name: "Console project" },
    { id: "old-parent", name: "Backlog" },
  ],
};
