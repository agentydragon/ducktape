// `grocy-sf` preview screenshot entry — esbuild bundles this into the `:previews` IIFE. Holds the
// fixtures plus the mount call; the Grocy-only fetch stub is imported before the widget graph
// before the registry/widget graph reaches client.ts. `satisfies RegisteredToolPreviewFixture` ties
// each (serverId, toolName, args, result?) to the registry's real Zod schemas, so a stale id,
// argument, or result shape is a type error.
import "./preview_mock.ts";

import { mountPreviewCards } from "../screenshot/mount.tsx";

import type { RegisteredToolPreviewFixture } from "../index.tsx";

const PREVIEW_FIXTURES = [
  {
    title: "Add Thrive box items to stock",
    serverId: "grocy-sf",
    toolName: "stock_add",
    args: {
      items: [
        { product: "Rolled oats", amount: 2, qu: "pack", location: "Pantry", best_before_date: "2026-12-01" },
        { product: "Almond butter", amount: 1, qu: "jar", location: "Pantry" },
        { product: "Frozen berries", amount: 3, qu: "bag", location: "Freezer" },
        { product: "Oat milk", amount: 6, qu: "carton", location: "Fridge" },
        { product: "Dark chocolate", amount: 4, qu: "bar", location: "Pantry" },
      ],
    },
    // One row per input item; the last one fails so the gallery shows the red failed path.
    result: [
      {
        kind: "ok",
        product_name: "Rolled oats",
        transaction_id: "6f0b2c9e",
        amount_delta: 2,
        new_amount: 5,
        qu_name: "Pack",
        stock_qu_name: null,
        location_name: "Pantry",
        entry_id: 189,
        best_before_date: "2026-12-01",
      },
      {
        kind: "ok",
        product_name: "Almond butter",
        transaction_id: "a13d77b0",
        amount_delta: 1,
        new_amount: 2,
        qu_name: "Jar",
        stock_qu_name: null,
        location_name: "Pantry",
        entry_id: 190,
        best_before_date: "2027-01-08",
      },
      {
        kind: "ok",
        product_name: "Frozen berries",
        transaction_id: "c58e01f4",
        amount_delta: 3,
        new_amount: 3,
        qu_name: "Bag",
        stock_qu_name: null,
        location_name: "Freezer",
        entry_id: 191,
        best_before_date: "2027-07-09",
      },
      {
        kind: "ok",
        product_name: "Oat milk",
        transaction_id: "9b24aa61",
        amount_delta: 6,
        new_amount: 8,
        qu_name: "Carton",
        stock_qu_name: null,
        location_name: "Fridge",
        entry_id: 192,
        best_before_date: "2026-08-02",
      },
      { kind: "error", error: "No product 'Dark chocolate' found — create it with products_create first." },
    ],
  },
  {
    title: "Consume spoiled and used groceries",
    serverId: "grocy-sf",
    toolName: "stock_consume",
    args: {
      items: [
        { product: "Milk", amount: 1, qu: "carton", location: "Fridge", spoiled: true },
        { product: "Spinach", amount: 200, qu: "gram", location: "Fridge" },
      ],
    },
  },
  {
    title: "Create pantry products",
    serverId: "grocy-sf",
    toolName: "products_create",
    args: {
      items: [
        {
          name: "Rolled oats",
          stock_qu: "gram",
          location: "Pantry",
          default_best_before_days: 270,
          min_stock_amount: 500,
          product_group: "Grains",
          description: "Organic thick-cut oats.",
        },
        { name: "Almond butter", stock_qu: "jar", location: "Pantry", default_best_before_days: 180 },
      ],
    },
    result: [
      { kind: "ok", created_object_id: 201 },
      { kind: "ok", created_object_id: 202 },
    ],
  },
  {
    title: "Update pantry product settings",
    serverId: "grocy-sf",
    toolName: "products_edit",
    args: {
      items: [
        {
          product: "Rolled oats",
          location: "Fridge",
          min_stock_amount: 500,
          default_best_before_days: 270,
          product_group: "Grains",
          clear_fields: ["description"],
        },
        { product: "Almond butter", purchase_qu: "jar", consume_qu: "jar" },
      ],
    },
  },
  {
    title: "Check the weekly shopping list",
    serverId: "grocy-sf",
    toolName: "shopping_list_get",
    args: { shopping_list: "Weekly" },
  },
  {
    title: "Add items to the shopping list",
    serverId: "grocy-sf",
    toolName: "shopping_list_items_add",
    args: {
      items: [
        { shopping_list: "Weekly", product: "Rolled oats", amount: 2 },
        { shopping_list: "Weekly", amount: 1, note: "check if we need paper towels" },
        { shopping_list: "Costco run", product: "Almond butter", amount: 1, note: "the crunchy kind" },
      ],
    },
    // One result row per input item; the note-only item has a null product/unit.
    result: [
      { kind: "ok", item_id: 55, product_name: "Rolled oats", amount: 2, qu_name: "Pack" },
      { kind: "ok", item_id: 56, product_name: null, amount: 1, qu_name: null },
      { kind: "ok", item_id: 57, product_name: "Almond butter", amount: 1, qu_name: "Jar" },
    ],
  },
  {
    title: "Bump a shopping-list item to family size",
    serverId: "grocy-sf",
    toolName: "shopping_list_item_edit",
    args: { item_id: 42, amount: 3, note: "family size", done: true },
  },
  {
    title: "Remove purchased shopping-list items",
    serverId: "grocy-sf",
    toolName: "shopping_list_items_remove",
    args: { item_ids: [3, 7, 12, 15, 21, 34, 42, 55] },
    result: [
      { kind: "ok", item_id: 3, product_name: "Milk", amount: 1, qu_name: "Carton" },
      { kind: "ok", item_id: 7, product_name: "Spinach", amount: 200, qu_name: "Gram" },
    ],
  },
] satisfies (RegisteredToolPreviewFixture & { title: string })[];

mountPreviewCards(PREVIEW_FIXTURES);
