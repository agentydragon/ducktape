- When giving me content to insert into Tana, write it in Tana Paste format.
- Read https://tana.inc/docs/tana-paste to make sure you get the format right.
- Make sure to include the `%%tana%%` at the top.
- If you're producing a table for me, make sure to present it as one with `%%view:table%%` at the end of the table's root node - e.g.: `- My table %%view:table%%`
- ## Supertags
  - Here are some supertags in my knowledge base and attributes you should use on them.
  - Make sure that all root nodes created by you have the supertag `#chatgpt`. Most of the time, try to wrap your content in 1 top level root node.
  - **DO NOT use supertags other than `#chatgpt` and those explicitly listed below!**
    - You may suggest creating supertags that might be useful, but ONLY outside of any Tana Paste code blocks, because that make my KB get spammed with new supertags I don't want if I copy-paste that.
    - For example, this is **WRONG** because it uses the `#car` and `#options` supertags which are NOT listed in this document:
      - ```
        %%tana%%
        - #options #chatgpt Options for buying a car %%view:table%%
          - Toyota Corolla #car
            - Price:: $20,000
        ```
    - Instead, you can do:
      - ```
        %%tana%%
        - #chatgpt Options for buying a car %%view:table%%
          - Toyota Corolla
            - Price:: $20,000
        ```
  - ### `#issue`
    - For issues/bugs/TODO items.
    - Fields:
      - `Status::` - one of `[[Open]]`/`[[Done]]`/`[[Waiting]]`/`[[Shelved]]`/`[[Cancelled]]`.
      - `Hotlists::` - are unordered sets of issues this issue belongs to; some important ones include:
        - `[[Do next]]`: issues that are high priority, to be picked up next
      - `Snapshot::` (optional) Brief summary of current state of work on this issue / immediate next action / current blocker. (As opposed to a progress log which includes historic state progression/notes.)
    - Example:
      - ```
        %%tana%%
        - #issue #chatgpt Buy milk
          - Status:: [[Open]]
          - Hotlists::
            - [[Do next]]
            - [[Buy]]
            - [[15 Leroy Place]]
          - I need to buy milk.
          - I can buy milk at various different places.
          - Milk is good for your bones.
        ```
  - ### `#3dmodel`
    - 3D model for 3D printing or a lasercut design.
    - ONLY already existing 3D models found online, with a link. NOT for models that do not yet exist on a website somewhere
    - Fields:
      - `Model tags::` - populate freely with links. Some model tags I use include:
        `[[Laser cutting]]` `[[Electronics]]` `[[Mounting]]` `[[Organization]]` `[[Household]]` `[[Animal]]` `[[Components]]`. Feel free to suggest and use new tags.
      -
    - Example:
      - ```
        %%tana%%
        - #3dmodel #chatgpt Model Name
          - Source link::
            - https://www.printables.com/model/123...
              - URL:: https://www.printables.com/model/132...
          - Model tags::
            - [[Laser cutting]]
            - [[Electronics]]
        ```
