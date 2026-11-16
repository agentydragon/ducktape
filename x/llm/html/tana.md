---
title: Tana, Tana Paste and the /tana command
---

# Tana, Tana Paste format and the `/tana` command

Tana is a personal knowledge management tool I use.

To insert formatted content into it, the right way is to use the bespoke
Tana Paste format. It looks similar to Markdown, but is quite finicky. Make sure
you follow these instructions carefully.

## `/tana` command

When I give you the `/tana` (or `/tanapaste`) command, that's me asking you to present
whatever I invoked it on in Tana Paste format, and put the Tana Paste into a fenced code
block for easy copy-paste. See rest of this document for details.

When invoked standalone without added arguments/context, assume it means "give me the
thing you just showed me but formatted as Tana Paste".

## Tana Paste format

When giving me content to insert into Tana, write it in Tana Paste format.
Read <https://tana.inc/docs/tana-paste> to make sure you get the format right.
Make sure to include the `%%tana%%` at the top.

### Syntax

```
%%tana%%
- #chatgpt Basic syntax demo
  - normal **bold** __italic__ ~~strikethrough~~ ^^highlight^^
- #chatgpt Temporal
  - date: [[date:2025-05-27]]
  - datetime: [[date:2025-05-28T11:32:51[America/Los_Angeles]]]
    - Use this timezone - I live in this timezone.
  - datetime interval: [[date:2025-05-28T13:00:02[America/Los_Angeles]/2025-05-28T14:00:03[America/Los_Angeles]]]
- #chatgpt You can format text nodes as headings
  - !! First heading
  - Foo
  - !! Second heading with __italic__
    - Bar
  - Baz
    - Xyzzy
- #chatgpt Linking to URLs
  - ✅ Supported link syntax:
    - [Link text](https://works.com/) and other text
    - If you want to link to a URL without overriding the text, repeat it in text and targe tURL:
      - Like this: [https://works.com/](https://works.com/)
  - ❌ URLs inserted directly or within angle brackages WILL NOT WORK:
    - http://broken.com/
    - <http://broken.com/>
- #chatgpt Images
  - ✅ Nodes containing only an image work; can set a caption:
    - ![Caption text](https://test.com/image.png)
    - ![](https://test.com/image.png)
  - ❌ Images included as part of other content in a node DO NOT work:
    - ![This](https://test.com/image.png) does not work
    - neither wil ![](https://www.example.com/image.png) this
- #chatgpt Tag all top level nodes in your Tana Paste with `#chatgpt`
```

### Not supported

- Inline code (either with backticks or with `<code>` tags).

### Tables

You may render a node as a table by appending `%%view:table%%` to the
end of the text of the _root node_ of the table.
This "annotation" belongs _only_ at the end of the node's own text - it does not
function like a HTML tag, you do not close it.

Tables will render with each child node as a row, and each attribute defined in any row as a column
(even if the attribute is not defined in all rows). Child nodes of rows that are _not_ attributes
will be rendered initially collapsed. Such child nodes are the best place to put details that
are too verbose or detailed to put into an "overview display" of the table, but which we still
want to include. Tana has easy affordances for expanding and collapsing them.

For example:

```
%%tana%%
- #chatgpt Options for buying a car %%view:table%%
  - Toyota Corolla
    - Price:: $20,000
    - Color:: Red
    - Year:: 2022
    - Good driving, but not very fast
  - Honda Civic
    - Price:: $22,000
    - Color:: Blue
    - Year:: 2021
    - Fast, but not very good driving
    - Actually not that fast either
```

This will initially render approximately like this:

| Name             | Price   | Color | Year |
| ---------------- | ------- | ----- | ---- |
| + Toyota Corolla | $20,000 | Red   | 2022 |
| + Honda Civic    | $22,000 | Blue  | 2021 |

And in Tana one can easily expand details of any row, kind of like a HTML `<details>` element:

```
|   Name           | Price   | Color | Year |
|------------------|---------|-------|------|
| + Toyota Corolla | $20,000 | Red   | 2022 |
| - Honda Civic    | $22,000 | Blue  | 2021 |
|   - Fast, but not very good driving       |
|   - Actually not that fast either         |
```

Attributes may also contain nested content, like this:

```
%%tana%%
- Lizards %%view:table%%
  - Gus-gus
    - Good boy?::
      - Very!
        - Doesn't bark
        - Wags
        - Is cute
    - Aesthetic?::
      - Also very!
        - Black and white
          - Never goes out of style
  - Geico gecko
    - Good boy?::
      - Somewhat
        - Promotes capitalism
        - But is lizard some points
    - Aesthetic?:: Yes
```

Such nested content also has easy collapse/expand affordances. One good use of that is to include optional detail.
(Nested content is also allowed in attributes outside of tables.)

To enable you to create appropriate columns, you _are_ allowed to make up appropriate new attributes
for tables. But this still does NOT involve using any new supertags.

#### Don't create orphan attributes

When presenting a table, only use attributes that will be present and have a value on at least most rows.
DO NOT define one-off attributes that are only present on one row. Each attribute you use induces a _whole new
column_ whether it's used in all rows or jus one. If you create a table with a lot of one-off attributes, the
table will be very wide, almost entirely empty, and hard to read and not useful as it destroys the whole
benefit of presenting data with horizontal and vertical correspondence.

For example, this is BAD:

```
%%tana%%
- #chatgpt Transport options %%view:table%%
  - Toyota Corolla
    - Price:: $20,000
    - Color:: Red
    - Miles/gallon:: 30
  - Tesla Model S
    - Price:: $100,000
    - Color:: Silver
    - Autopilot:: Yes
    - Steering wheel:: No
  - Walking
    - Price:: Free
    - Scenic:: Yes
    - Calories burned:: 200
  - Bicycle
    - Price:: $500
    - Color:: Blue
    - Honk sound:: Cathartic
    - Bicycle day vibes:: Confirmed
    - Calories burned:: 100
  - Teleportation
    - Price:: Priceless
    - Legal status:: Questionable
```

Because it would render roughly like this:

| Name             | Price     | Color  | Miles/gallon | Autopilot | Steering wheel | Scenic | Calories burned | Honk sound | Bicycle day vibes | Legal status |
| ---------------- | --------- | ------ | ------------ | --------- | -------------- | ------ | --------------- | ---------- | ----------------- | ------------ |
| + Toyota Corolla | $20,000   | Red    | 30           |           |                |        |                 |            |                   |              |
| + Tesla Model S  | $100,000  | Silver |              | Yes       | No             |        |                 |            |                   |              |
| + Walking        | Free      |        |              |           |                | Yes    | 200             |            |                   |              |
| + Bicycle        | $500      | Blue   |              |           |                |        | 100             | Cathartic  | Confirmed         |              |
| + Teleportation  | Priceless |        |              |           |                |        |                 |            |                   | Questionable |

_Some_ possible options to fix this include:

- Placing content that is particular to only a couple rows/free-text _and_ should be visible in the table
  without opening disclosure widgets (e.g., "autopilot", "questionable legal status") in a separate attribute
  that may mix multiple semantic elements - let's say `Notes::`.
- Or for context that's fine to put under a disclosure widget, just use non-attribute child nodes
  of the row.

For example, this is BETTER:

```
%%tana%%
- #chatgpt Transport options %%view:table%%
  - Toyota Corolla
    - Price:: $20,000
    - Color:: Red
    - Notes:: 30 miles/gallon
  - Tesla Model S
    - Price:: $100,000
    - Color:: Silver
    - Notes:: Autopilot; no steering wheel
  - Walking
    - Price:: Free
    - Notes::
      - Burns 200 kcal
    - Scenic
  - Bicycle
    - Price:: $500
    - Color:: Blue
    - Cathartic honking
    - Bicycle day vibes
  - Teleportation
    - Price:: Priceless
    - Notes::
      - ⚠️ Legal status questionable
```

This will render like this:

| Name             | Price     | Color  | Notes                        |
| ---------------- | --------- | ------ | ---------------------------- |
| + Toyota Corolla | $20,000   | Red    | 30 miles/gallon              |
| + Tesla Model S  | $100,000  | Silver | Autopilot; no steering wheel |
| + Walking        | Free      |        | Burns 200 kcal               |
| + Bicycle        | $500      | Blue   |                              |
| + Teleportation  | Priceless |        | ⚠️ Legal status questionable |

## Supertags in my Tana

Here are some supertags in my knowledge base and attributes you should use on them.

Make sure that all root nodes created by you are tagged with `#chatgpt`.
Most of the time, try to wrap your content in 1 top level root node.

### `#issue`

Issues/bugs/TODO items have #issue supertag. `#issue`'s have:

- `Status::` field which is `[[Open]]` / `[[Done]]` / `[[Waiting]]` / `[[Shelved]]` / `[[Cancelled]]`.
- `Hotlists::` field, of which some important are:
  - `[[Do next]]` -- for issues that are high priority, to be picked up next
  - `[[Buy]]` -- involves buying something
  - `[[Personal technical infrastructure]]` -- computer/phone setup, automation, etc.
  - `[[Repair]]`, `[[Prevention]]`, `[[Health]]`, `[[Mental health]]`, `[[Home improvement]]`, `[[Socializing]]`
- `Snapshot::` field: brief summary of current state/blockers/... - as opposed to historical evolution/logs

Example:

```
%%tana%%
- #issue #chatgpt Buy milk
  - Status:: [[Open]]
  - Hotlists::
    - [[Do next]]
    - [[Buy]]
  - Snapshot:: Target is out - buy at Costco
```

Example with multiple top-level nodes:

```
%%tana%%
- #chatgpt You should buy a lizard.
  - Lizard options %%view:table%%
    - Bearded dragon
      - Size:: 20 cm
    - Argentine black-and-white tegu
      - Price:: $300
      - Size:: 150 cm
      - Color:: Black and white
      - Certified best dog
  - Lizards are great pets
- #chatgpt If you're buying a lizard you should also buy a terrarium.
- #chatgpt Who needs electricity imagine having 2 lizards
  - But then electricity enables effective sunning
```

### `#hotlist`

Do not create new `#hotlist`'s.

### `#3dmodel`

Use this for 3D models for 3D printing, or lasercut designs. Only for those that actually already exist uploaded somewhere online, e.g. on Printables, 3axis.co, ...

```
%%tana%%
- #3dmodel #chatgpt Model Name
  - Source link::
    - https://www.printables.com/model/...
      - URL:: https://www.printables.com/model/...
  - Model tags::
    - [[Laser cutting]]
    - [[Electronics]]
```

Every value of `Model tags::` has the `#3dmodeltag` supertag.
Some existing ones include: `[[Laser cutting]]` `[[Electronics]]` `[[Mounting]]` `[[Organization]]` `[[Household]]` `[[Animal]]` `[[Components]]`.
Feel free to suggest and use new `#3dmodeltag`s.

## DO NOT use `#supertags` I didn't explicitly tell you about

In Tana, `#foo` does NOT mean just "a kind of loose semantic tag grouping related things". In Tana, the `#foo` syntax is a "supertag", and those define
a sort of _schemaa_ - a _type system_. As such, DO NOT lightly use any supertags I did not explicitly tell you about.

Feel free to _suggest_ supertags that might be useful but OUTSIDE any Tana Paste code blocks, because that make my KB get spammed with new supertags
I don't want if I copy-paste that.

For example, DO NOT do this:

```
%%tana%%
- #options #chatgpt Options for buying a car %%view:table%%
  - Toyota Corolla #car
    - Price:: $20,000
```

This invents the supertags `#options` and `#car`, neither of which exist. Instead, you can do:

```
%%tana%%
- #chatgpt Options for buying a car %%view:table%%
  - Toyota Corolla
    - Price:: $20,000
```
